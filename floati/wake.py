"""Host-local, non-authoritative one-shot wake registration."""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import secrets
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from .errors import ProtocolRefusal
from .root import FloatiRoot, validate_identifier
from .runtruth import RunLedger
from .scheduler import RunScheduler


_LABEL_PREFIX = "com.landoclusters.floati.oneshot."
_LABEL_PATTERN = re.compile(r"^com\.landoclusters\.floati\.oneshot\.[0-9a-f]{64}$")
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "floati"
_MAX_CALLBACK_PATH_BYTES = 4096
_MAX_CALLBACK_BYTES = 16 * 1024 * 1024
_MAX_LOCAL_PLIST_BYTES = 1024 * 1024
_REGISTRATION_IDENTITY_KEY = "FloatiRegistrationIdentity"
_IDENTITY_FIELDS = (
    "canonical_path", "device", "inode", "mode", "size", "sha256",
)
_IDENTITY_WITH_DIGEST_FIELDS = _IDENTITY_FIELDS + ("semantic_preview_digest",)


def _timestamp(value: object, *, code: str) -> datetime:
    if not isinstance(value, str):
        raise ProtocolRefusal(code, "wake time must be RFC3339 UTC text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolRefusal(code, "wake time must be RFC3339 UTC text") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolRefusal(code, "wake time must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _current_time(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("wake_time_invalid", "callback time must be an aware datetime")
    return current.astimezone(timezone.utc)


def _canonical_timestamp(value: object, *, code: str) -> str:
    parsed = _timestamp(value, code=code)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _terminal_safe(value: str) -> bool:
    return not any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for character in value
    )


@dataclass(frozen=True)
class WakeCallbackIdentity:
    """A descriptor-derived callback snapshot used only for local registration."""

    canonical_path: str
    device: int
    inode: int
    mode: int
    size: int
    sha256: str

    def plist_fields(self) -> Dict[str, object]:
        return {
            "canonical_path": self.canonical_path,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "size": self.size,
            "sha256": self.sha256,
        }


def _callback_refusal(message: str, exc: Optional[BaseException] = None) -> ProtocolRefusal:
    refusal = ProtocolRefusal("wake_callback_invalid", message)
    if exc is not None:
        raise refusal from exc
    return refusal


def _callback_path(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise _callback_refusal("one-shot callback path must be an absolute Path")
    text = str(value)
    if not _terminal_safe(text):
        raise _callback_refusal("one-shot callback path is terminal-unsafe or overlong")
    try:
        encoded = os.fsencode(text)
    except UnicodeEncodeError as exc:
        raise _callback_refusal("one-shot callback path is not filesystem-encodable", exc)
    if len(encoded) > _MAX_CALLBACK_PATH_BYTES:
        raise _callback_refusal("one-shot callback path is terminal-unsafe or overlong")
    parts = value.parts
    if len(parts) < 2 or any(part in ("", ".", "..") or not _terminal_safe(part) for part in parts[1:]):
        raise _callback_refusal("one-shot callback path has unsafe components")
    return value


def _callback_identity(value: Path) -> WakeCallbackIdentity:
    """Walk every absolute component without following a directory or leaf symlink."""
    path = _callback_path(value)
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory = -1
    descriptor = -1
    try:
        directory = os.open("/", directory_flags)
        for component in path.parts[1:-1]:
            next_directory = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = next_directory
            if not stat.S_ISDIR(os.fstat(directory).st_mode):
                raise _callback_refusal("one-shot callback parent is not a directory")
        descriptor = os.open(path.parts[-1], file_flags, dir_fd=directory)
        opened = os.fstat(descriptor)
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not (mode & 0o444)
            or not (mode & 0o111)
            or opened.st_size <= 0
            or opened.st_size > _MAX_CALLBACK_BYTES
        ):
            raise _callback_refusal("one-shot callback must be a bounded readable executable regular file")
        digest = hashlib.sha256()
        total = 0
        for chunk in iter(lambda: os.read(descriptor, 65_536), b""):
            total += len(chunk)
            if total > _MAX_CALLBACK_BYTES:
                raise _callback_refusal("one-shot callback exceeds the bounded size")
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (final.st_dev, final.st_ino, stat.S_IMODE(final.st_mode), final.st_size) != (
            opened.st_dev, opened.st_ino, mode, opened.st_size,
        ) or total != opened.st_size:
            raise _callback_refusal("one-shot callback changed while its descriptor was held")
        return WakeCallbackIdentity(
            canonical_path=str(path), device=opened.st_dev, inode=opened.st_ino,
            mode=mode, size=opened.st_size, sha256=digest.hexdigest(),
        )
    except ProtocolRefusal:
        raise
    except OSError as exc:
        raise _callback_refusal("one-shot callback could not be opened without following links", exc)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory >= 0:
            os.close(directory)


@dataclass(frozen=True)
class OneShotWakeRequest:
    root: FloatiRoot
    run_id: str
    item_id: str
    attempt_id: str
    wake_at: str
    scheduler_epoch: int
    fence_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.root, FloatiRoot):
            raise ProtocolRefusal("wake_root_invalid", "wake request requires a validated FloatiRoot")
        validate_identifier(self.run_id, "wake_run_id")
        validate_identifier(self.item_id, "wake_item_id")
        validate_identifier(self.attempt_id, "wake_attempt_id")
        object.__setattr__(self, "wake_at", _canonical_timestamp(self.wake_at, code="wake_time_invalid"))
        if (not isinstance(self.scheduler_epoch, int) or isinstance(self.scheduler_epoch, bool)
                or self.scheduler_epoch < 1):
            raise ProtocolRefusal("wake_epoch_invalid", "wake scheduler epoch must be a positive integer")
        if not isinstance(self.fence_token, str) or not re.fullmatch(r"[0-9a-f]{64}", self.fence_token):
            raise ProtocolRefusal("wake_fence_invalid", "wake fence token must be lowercase SHA-256 text")


# The retired repository name, built from hex rather than spelled. This salt
# sits INSIDE the sha256 preimage below, so its bytes decide the label of every
# one-shot wake already scheduled with the host: a changed byte orphans them.
# Built for the reason floati/identity_fence.py builds its governed tokens -- a
# fence that must forbid this word may not find it in shipped source, and the
# runtime value may not move to satisfy the fence. Pinned in
# tests/test_retired_name_pins.py.
_RETIRED_NAME = bytes.fromhex("736c6970776179").decode("ascii")
_ONE_SHOT_WAKE_DOMAIN = _RETIRED_NAME + "-one-shot-wake-v1"


def _label(request: OneShotWakeRequest) -> str:
    coordinates = "\0".join((
        _ONE_SHOT_WAKE_DOMAIN, str(request.root.path), request.root.tenant_id,
        request.run_id, request.item_id, request.attempt_id, request.wake_at,
        str(request.scheduler_epoch), request.fence_token,
    ))
    return _LABEL_PREFIX + hashlib.sha256(coordinates.encode("utf-8")).hexdigest()


def _program_arguments(request: OneShotWakeRequest, callback_path: str) -> list[str]:
    return [
        callback_path, "wake-callback", "--root", str(request.root.path),
        "--tenant", request.root.tenant_id, "--run-id", request.run_id, "--item-id", request.item_id,
        "--attempt-id", request.attempt_id, "--wake-at", request.wake_at,
        "--scheduler-epoch", str(request.scheduler_epoch), "--fence-token", request.fence_token,
    ]


def _calendar_interval(request: OneShotWakeRequest, calendar_timezone: Optional[tzinfo]) -> Dict[str, int]:
    due = _timestamp(request.wake_at, code="wake_time_invalid")
    calendar_due = due.astimezone() if calendar_timezone is None else due.astimezone(calendar_timezone)
    return {
        "Year": calendar_due.year, "Month": calendar_due.month, "Day": calendar_due.day,
        "Hour": calendar_due.hour, "Minute": calendar_due.minute, "Second": calendar_due.second,
    }


def _wake_parent(request: OneShotWakeRequest) -> Path:
    """Resolve only the trusted contained parent; the deterministic leaf stays unresolved."""
    return request.root.resolve_relative(Path("wake"))


def _wake_leaf(label: str) -> str:
    return f"{label}.plist"


def _open_wake_parent(path: Path, *, absent_ok: bool = False) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError:
        if absent_ok:
            return -1
        raise ProtocolRefusal("wake_local_testimony_invalid", "local wake parent is absent") from None
    except OSError as exc:
        raise ProtocolRefusal("wake_local_testimony_invalid", "local wake parent is unavailable") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ProtocolRefusal("wake_local_testimony_invalid", "local wake parent is not a directory")
    return descriptor


def _open_or_create_wake_parent(request: OneShotWakeRequest) -> int:
    """Create and hold the fixed wake directory without following a substituted leaf."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    tenant_descriptor = -1
    try:
        tenant_descriptor = os.open(str(request.root.tenant_home), flags)
        try:
            os.mkdir("wake", 0o700, dir_fd=tenant_descriptor)
        except FileExistsError:
            pass
        else:
            os.fsync(tenant_descriptor)
        return os.open("wake", flags, dir_fd=tenant_descriptor)
    except OSError as exc:
        raise ProtocolRefusal(
            "wake_local_testimony_invalid", "local wake parent cannot be created or opened without following links",
        ) from exc
    finally:
        if tenant_descriptor >= 0:
            os.close(tenant_descriptor)


def _quarantine_failed_wake_leaf(parent_descriptor: int, leaf: str, created: os.stat_result) -> None:
    """Retain only the observed created file; never delete a replacement pathname."""
    try:
        current = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ProtocolRefusal("wake_local_testimony_invalid", "wake leaf cannot be inspected after durability failure") from exc
    if not _same_file_object(created, current):
        raise ProtocolRefusal("wake_local_testimony_invalid", "wake leaf was replaced after durability failure")
    quarantine = f".{leaf}.failure-{secrets.token_hex(16)}"
    try:
        os.rename(leaf, quarantine, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
        retained = os.stat(quarantine, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise ProtocolRefusal("wake_local_testimony_invalid", "wake leaf could not enter failure quarantine") from exc
    if not _same_file_object(created, retained):
        try:
            os.link(
                quarantine, leaf,
                src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProtocolRefusal(
                "wake_local_testimony_invalid",
                "wake failure quarantine retained a replacement but could not restore its deterministic leaf",
            ) from exc
        try:
            restored = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            retained_after = os.stat(quarantine, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ProtocolRefusal(
                "wake_local_testimony_invalid",
                "wake replacement restoration identity is unavailable",
            ) from exc
        if (
            not _same_file_identity(retained, restored)
            or not _same_file_identity(retained, retained_after)
        ):
            raise ProtocolRefusal(
                "wake_local_testimony_invalid",
                "wake replacement restoration identity is ambiguous",
            )
        raise ProtocolRefusal(
            "wake_local_testimony_invalid",
            "wake failure quarantine retained and restored a replacement",
        )


def _read_wake_leaf(parent_descriptor: int, leaf: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size < 1 or opened.st_size > _MAX_LOCAL_PLIST_BYTES:
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake leaf is not a bounded regular file")
        chunks = []
        total = 0
        for chunk in iter(lambda: os.read(descriptor, 65_536), b""):
            total += len(chunk)
            if total > _MAX_LOCAL_PLIST_BYTES:
                raise ProtocolRefusal("wake_local_testimony_invalid", "local wake plist exceeds its read bound")
            chunks.append(chunk)
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino, final.st_mode, final.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size)
            or total != opened.st_size
        ):
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake plist changed during validation")
        return b"".join(chunks), opened
    except ProtocolRefusal:
        raise
    except OSError as exc:
        raise ProtocolRefusal("wake_local_testimony_invalid", "local wake leaf could not be opened without following links") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mode, left.st_size) == (
        right.st_dev, right.st_ino, right.st_mode, right.st_size,
    )


def _same_file_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _preview(
    request: OneShotWakeRequest, identity: WakeCallbackIdentity, *, calendar_timezone: Optional[tzinfo] = None
) -> Dict[str, object]:
    label = _label(request)
    plist = {
        "Label": label,
        "ProgramArguments": _program_arguments(request, identity.canonical_path),
        "StartCalendarInterval": _calendar_interval(request, calendar_timezone),
    }
    identity_payload = identity.plist_fields()
    plist[_REGISTRATION_IDENTITY_KEY] = identity_payload
    semantic_encoded = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
    semantic_digest = hashlib.sha256(semantic_encoded).hexdigest()
    plist[_REGISTRATION_IDENTITY_KEY] = dict(
        identity_payload, semantic_preview_digest=semantic_digest,
    )
    encoded = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
    path = request.root.resolve_relative(Path("wake") / f"{label}.plist")
    return {
        "state": "preview", "label": label, "path": str(path), "plist": plist,
        "callback_identity": identity.plist_fields(), "digest": semantic_digest,
        "plist_digest": hashlib.sha256(encoded).hexdigest(), "encoded": encoded,
    }


class OneShotWakeRegistrar:
    """Writes only preview-approved plist testimony; it never activates launchd."""

    def __init__(
        self,
        *,
        now: Optional[Callable[[], datetime]] = None,
        root: Optional[FloatiRoot] = None,
        calendar_timezone: Optional[tzinfo] = None,
        callback_path: Optional[Path] = None,
    ) -> None:
        if root is not None and not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("wake_root_invalid", "registrar root must be a validated FloatiRoot")
        if calendar_timezone is not None and not isinstance(calendar_timezone, tzinfo):
            raise ProtocolRefusal("wake_timezone_invalid", "calendar timezone must be a datetime tzinfo")
        self._now = (lambda: datetime.now(timezone.utc)) if now is None else now
        self._root = root
        self._calendar_timezone = calendar_timezone
        self._paths: Dict[str, Path] = {}
        self._callback_path = _SCRIPT_PATH if callback_path is None else _callback_path(callback_path)
        self._previews: Dict[str, Dict[str, object]] = {}
        self._requests: Dict[str, OneShotWakeRequest] = {}

    def _check_root(self, request: OneShotWakeRequest) -> None:
        if self._root is not None and self._root != request.root:
            raise ProtocolRefusal("wake_root_mismatch", "registrar root must match the wake request root")

    def _check_future(self, request: OneShotWakeRequest) -> None:
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ProtocolRefusal("wake_time_invalid", "registrar clock must return an aware datetime")
        if _timestamp(request.wake_at, code="wake_time_invalid") <= now.astimezone(timezone.utc):
            raise ProtocolRefusal("wake_not_future", "one-shot wake must be strictly in the future")

    def preview(self, request: OneShotWakeRequest) -> Dict[str, object]:
        self._check_root(request)
        self._check_future(request)
        preview = _preview(
            request, _callback_identity(self._callback_path), calendar_timezone=self._calendar_timezone,
        )
        self._paths[str(preview["label"])] = Path(str(preview["path"]))
        self._previews[str(preview["label"])] = preview
        self._requests[str(preview["label"])] = request
        return {key: value for key, value in preview.items() if key != "encoded"}

    def register(self, request: OneShotWakeRequest, approved_preview_digest: object) -> Dict[str, object]:
        self._check_root(request)
        label = _label(request)
        preview = self._previews.get(label)
        if preview is None:
            self.preview(request)
            preview = self._previews[label]
        digest = preview["digest"]
        if not isinstance(approved_preview_digest, str) or approved_preview_digest != digest:
            raise ProtocolRefusal("wake_preview_unapproved", "registration requires the exact preview digest")
        self._check_future(request)
        registered_identity = preview.get("callback_identity")
        current_identity = _callback_identity(self._callback_path).plist_fields()
        if current_identity != registered_identity:
            raise ProtocolRefusal("wake_callback_changed", "one-shot callback changed after preview approval")
        path = Path(str(preview["path"]))
        leaf = _wake_leaf(label)
        parent = -1
        descriptor = -1
        created: Optional[os.stat_result] = None
        try:
            parent = _open_or_create_wake_parent(request)
            descriptor = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
        except FileExistsError as exc:
            raise ProtocolRefusal("wake_already_registered", "one-shot wake label is already written locally") from exc
        except OSError as exc:
            raise ProtocolRefusal("wake_local_testimony_invalid", "one-shot wake leaf could not be created without following links") from exc
        try:
            created = os.fstat(descriptor)
            os.fchmod(descriptor, 0o600)
            if not stat.S_ISREG(created.st_mode):
                raise ProtocolRefusal("wake_local_testimony_invalid", "one-shot wake leaf is not a regular file")
            payload = bytes(preview["encoded"])
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("short one-shot wake write")
            written_metadata = os.fstat(descriptor)
            if not _same_file_object(created, written_metadata) or written_metadata.st_size != len(payload):
                raise ProtocolRefusal("wake_local_testimony_invalid", "one-shot wake leaf changed while held")
            os.fsync(descriptor)
            final = os.fstat(descriptor)
            if not _same_file_identity(written_metadata, final) or final.st_size != len(payload):
                raise ProtocolRefusal("wake_local_testimony_invalid", "one-shot wake leaf changed while held")
            os.fsync(parent)
        except BaseException as exc:
            if created is None:
                try:
                    created = os.fstat(descriptor)
                except OSError as identity_exc:
                    raise ProtocolRefusal(
                        "wake_local_testimony_invalid",
                        "one-shot wake created leaf identity is unavailable after registration failure",
                    ) from identity_exc
            _quarantine_failed_wake_leaf(parent, leaf, created)
            if isinstance(exc, ProtocolRefusal):
                raise
            raise ProtocolRefusal("wake_local_testimony_invalid", "one-shot wake local durability is unavailable") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent >= 0:
                os.close(parent)
        self._paths[str(preview["label"])] = path
        return {"state": "written_unloaded", "label": preview["label"], "digest": digest}

    def _stored_identity(
        self, plist: Mapping[str, object], encoded: bytes, request: OneShotWakeRequest,
    ) -> Dict[str, object]:
        if encoded.count(b"<key>FloatiRegistrationIdentity</key>") != 1:
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake plist has duplicate registration identity")
        identity = plist.get(_REGISTRATION_IDENTITY_KEY)
        if not isinstance(identity, dict) or set(identity) != set(_IDENTITY_WITH_DIGEST_FIELDS):
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake plist lacks an exact registration identity")
        for key in ("canonical_path", "sha256", "semantic_preview_digest"):
            value = identity.get(key)
            if not isinstance(value, str):
                raise ProtocolRefusal("wake_local_testimony_invalid", "local wake registration identity has invalid text")
        try:
            stored_callback_path = _callback_path(Path(str(identity["canonical_path"])))
        except ProtocolRefusal as exc:
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake registration path is malformed") from exc
        if (
            str(stored_callback_path) != str(self._callback_path)
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity["sha256"]))
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity["semantic_preview_digest"]))
        ):
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake registration identity is malformed")
        for key in ("device", "inode", "mode", "size"):
            value = identity.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ProtocolRefusal("wake_local_testimony_invalid", "local wake registration identity has invalid numeric testimony")
        if (
            identity["mode"] > 0o7777
            or not (identity["mode"] & 0o444)
            or not (identity["mode"] & 0o111)
            or identity["size"] <= 0
            or identity["size"] > _MAX_CALLBACK_BYTES
        ):
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake registration identity is outside bounds")
        callback_identity = WakeCallbackIdentity(
            canonical_path=str(identity["canonical_path"]), device=int(identity["device"]),
            inode=int(identity["inode"]), mode=int(identity["mode"]), size=int(identity["size"]),
            sha256=str(identity["sha256"]),
        )
        expected = _preview(request, callback_identity, calendar_timezone=self._calendar_timezone)
        if plist != expected["plist"] or encoded != expected["encoded"]:
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake registration does not match its closed request payload")
        return dict(identity)

    @staticmethod
    def _stored_request(plist: Mapping[str, object], root: FloatiRoot) -> OneShotWakeRequest:
        arguments = plist.get("ProgramArguments")
        if not isinstance(arguments, list) or len(arguments) != 18 or any(
            not isinstance(value, str) for value in arguments
        ):
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake callback arguments are malformed")
        flags = (
            "wake-callback", "--root", "--tenant", "--run-id", "--item-id",
            "--attempt-id", "--wake-at", "--scheduler-epoch", "--fence-token",
        )
        if tuple(arguments[index] for index in (1, 2, 4, 6, 8, 10, 12, 14, 16)) != flags:
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake callback arguments are not canonical")
        if arguments[3] != str(root.path) or arguments[5] != root.tenant_id:
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake callback arguments name another root")
        try:
            scheduler_epoch = int(arguments[15])
        except ValueError as exc:
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake callback epoch is malformed") from exc
        try:
            return OneShotWakeRequest(
                root=root, run_id=arguments[7], item_id=arguments[9], attempt_id=arguments[11],
                wake_at=arguments[13], scheduler_epoch=scheduler_epoch, fence_token=arguments[17],
            )
        except ProtocolRefusal as exc:
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake callback request is malformed") from exc

    def status(self, request: OneShotWakeRequest) -> Dict[str, object]:
        """Read only local plist testimony; launchd state and future execution remain unobserved."""
        self._check_root(request)
        label = _label(request)
        parent_path = _wake_parent(request)
        leaf = _wake_leaf(label)
        path = parent_path / leaf
        absent = {
            "schema_version": 1, "artifact_version": 0, "kind": "wake_path_status",
            "path_state": "absent", "label": label, "plist_path": str(path), "plist_digest": None,
            "registered_callback_path": None, "registered_callback_device": None,
            "registered_callback_inode": None, "registered_callback_mode": None,
            "registered_callback_size": None, "registered_callback_sha256": None,
            "is_live": "unknown", "deliverability": "unknown", "self_wake": "unknown",
            "reason": "local_plist_absent_activation_unobserved",
            "observation_limits": [
                "local_plist_absent", "launchd_state_not_observed", "callback_activation_not_observed",
            ],
        }
        parent_descriptor = _open_wake_parent(parent_path, absent_ok=True)
        if parent_descriptor < 0:
            return absent
        try:
            try:
                os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return absent
            encoded, _metadata = _read_wake_leaf(parent_descriptor, leaf)
            try:
                plist = plistlib.loads(encoded)
            except (ValueError, TypeError) as exc:
                raise ProtocolRefusal("wake_local_testimony_invalid", "local wake plist is malformed") from exc
        finally:
            os.close(parent_descriptor)
        if not isinstance(plist, dict) or set(plist) != {
            "Label", "ProgramArguments", "StartCalendarInterval", _REGISTRATION_IDENTITY_KEY,
        } or plist.get("Label") != label:
            raise ProtocolRefusal("wake_local_testimony_invalid", "local wake plist is not the deterministic registration")
        identity = self._stored_identity(plist, encoded, request)
        return {
            "schema_version": 1, "artifact_version": 0, "kind": "wake_path_status",
            "path_state": "written_unloaded", "label": label, "plist_path": str(path),
            "plist_digest": hashlib.sha256(encoded).hexdigest(),
            "registered_callback_path": identity["canonical_path"],
            "registered_callback_device": identity["device"],
            "registered_callback_inode": identity["inode"], "registered_callback_mode": identity["mode"],
            "registered_callback_size": identity["size"], "registered_callback_sha256": identity["sha256"],
            "is_live": "unknown", "deliverability": "unknown", "self_wake": "will_not_self_wake",
            "reason": "activation_unverified",
            "observation_limits": [
                "plist_written_locally", "registration_identity_from_local_plist",
                "launchd_load_not_attempted", "callback_activation_not_observed",
            ],
        }

    def cleanup(self, label: str) -> None:
        """Move the exact deterministic leaf aside without a pathname deletion.

        Success reflects an identity match at the rename observation and
        deterministic-leaf absence at decision. It returns no artifact and makes
        no durable quarantine-retention, activation, or disarm claim.
        """
        if not isinstance(label, str) or not _LABEL_PATTERN.fullmatch(label):
            raise ProtocolRefusal("wake_label_invalid", "cleanup requires a deterministic one-shot wake label")
        request = self._requests.get(label)
        root = request.root if request is not None else self._root
        if root is None:
            raise ProtocolRefusal("wake_label_unknown", "cleanup knows no local one-shot wake label")
        parent_path = root.resolve_relative(Path("wake"))
        leaf = _wake_leaf(label)
        parent_descriptor = _open_wake_parent(parent_path, absent_ok=True)
        if parent_descriptor < 0:
            return
        try:
            try:
                os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            encoded, opened = _read_wake_leaf(parent_descriptor, leaf)
            try:
                plist = plistlib.loads(encoded)
            except (ValueError, TypeError) as exc:
                raise ProtocolRefusal("wake_local_testimony_invalid", "cleanup refuses malformed local wake testimony") from exc
            if not isinstance(plist, dict) or set(plist) != {
                "Label", "ProgramArguments", "StartCalendarInterval", _REGISTRATION_IDENTITY_KEY,
            } or plist.get("Label") != label:
                raise ProtocolRefusal("wake_local_testimony_invalid", "cleanup refuses a nonmatching local wake plist")
            if request is None:
                request = self._stored_request(plist, root)
            if _label(request) != label:
                raise ProtocolRefusal("wake_local_testimony_invalid", "cleanup refuses a plist whose request does not match its label")
            self._stored_identity(plist, encoded, request)
            quarantine = f".{label}.cleanup-{secrets.token_hex(16)}"
            try:
                os.rename(
                    leaf, quarantine, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
                )
            except FileNotFoundError as exc:
                raise ProtocolRefusal("wake_local_testimony_invalid", "local wake plist disappeared after validation") from exc
            except OSError as exc:
                raise ProtocolRefusal("wake_local_testimony_invalid", "local wake plist could not enter cleanup quarantine") from exc
            try:
                os.fsync(parent_descriptor)
            except OSError as exc:
                raise ProtocolRefusal(
                    "wake_local_testimony_invalid",
                    "cleanup quarantine rename durability is unavailable",
                ) from exc
            try:
                quarantined = os.stat(quarantine, dir_fd=parent_descriptor, follow_symlinks=False)
            except OSError as exc:
                raise ProtocolRefusal("wake_local_testimony_invalid", "cleanup quarantine identity is unavailable") from exc
            if not _same_file_identity(opened, quarantined):
                try:
                    os.link(
                        quarantine, leaf, src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor, follow_symlinks=False,
                    )
                    os.fsync(parent_descriptor)
                except OSError as exc:
                    raise ProtocolRefusal(
                        "wake_local_testimony_invalid",
                        "cleanup could not complete raced replacement restoration",
                    ) from exc
                raise ProtocolRefusal(
                    "wake_local_testimony_invalid",
                    "cleanup restored a raced replacement without deleting it",
                )
            try:
                os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ProtocolRefusal(
                    "wake_local_testimony_invalid",
                    "cleanup did not delete a quarantine path but could not verify the deterministic path",
                ) from exc
            else:
                raise ProtocolRefusal(
                    "wake_local_testimony_invalid",
                    "cleanup did not delete a quarantine path but a new deterministic wake leaf exists",
                )
            self._paths.pop(label, None)
            self._requests.pop(label, None)
        finally:
            os.close(parent_descriptor)


class FakeOneShotWakeRegistrar:
    """Deterministic in-memory host-local registrar for focused contracts."""

    def __init__(
        self,
        *,
        now: Optional[Callable[[], datetime]] = None,
        calendar_timezone: Optional[tzinfo] = None,
    ) -> None:
        if calendar_timezone is not None and not isinstance(calendar_timezone, tzinfo):
            raise ProtocolRefusal("wake_timezone_invalid", "calendar timezone must be a datetime tzinfo")
        self._now = (lambda: datetime(2099, 1, 1, tzinfo=timezone.utc)) if now is None else now
        self._calendar_timezone = calendar_timezone
        self._previews: Dict[str, Dict[str, object]] = {}
        self.registered_labels: list[str] = []

    def preview(self, request: OneShotWakeRequest) -> Dict[str, object]:
        registrar = OneShotWakeRegistrar(
            now=self._now, calendar_timezone=self._calendar_timezone
        )
        preview = registrar.preview(request)
        self._previews[str(preview["label"])] = preview
        return preview

    def register(self, request: OneShotWakeRequest, approved_preview_digest: object) -> Dict[str, object]:
        preview = self.preview(request)
        if not isinstance(approved_preview_digest, str) or approved_preview_digest != preview["digest"]:
            raise ProtocolRefusal("wake_preview_unapproved", "registration requires the exact preview digest")
        label = str(preview["label"])
        if label not in self.registered_labels:
            self.registered_labels.append(label)
        return {"state": "written_unloaded", "label": label, "digest": preview["digest"]}

    def cleanup(self, label: str) -> None:
        if label not in self._previews:
            raise ProtocolRefusal("wake_label_unknown", "cleanup knows no local one-shot wake label")
        self._previews.pop(label)
        self.registered_labels[:] = [current for current in self.registered_labels if current != label]


def replay_one_shot_wake(
    request: OneShotWakeRequest, scheduler: RunScheduler, *, now: Optional[datetime] = None
) -> Dict[str, object]:
    """Re-read canonical run truth and hand a due current attempt to the normal scheduler."""
    if not isinstance(scheduler, RunScheduler):
        raise ProtocolRefusal("wake_scheduler_invalid", "wake callback requires the ordinary RunScheduler")
    if scheduler.ledger.root != request.root:
        raise ProtocolRefusal("wake_root_mismatch", "wake callback scheduler must use the request root")
    if _current_time(now) < _timestamp(request.wake_at, code="wake_time_invalid"):
        raise ProtocolRefusal("wake_not_due", "one-shot wake callback ran before its due time")
    run = scheduler.ledger.project().run(request.run_id)
    attempt_ids = run["item_attempt_ids"].get(request.item_id, [])
    if not attempt_ids:
        raise ProtocolRefusal("wake_stale_fence", "wake callback item has no current attempt")
    current = run["attempts"][attempt_ids[-1]]["opened"]
    if (
        current["attempt_id"] != request.attempt_id
        or current["scheduler_epoch"] != request.scheduler_epoch
        or current["fence_token"] != request.fence_token
    ):
        raise ProtocolRefusal("wake_stale_fence", "wake callback coordinates no longer match current run truth")
    records = scheduler.reconcile(request.run_id, request.item_id, now=now)
    return {"state": "replayed", "records": records}


def run_one_shot_wake_callback(request: OneShotWakeRequest, *, now: Optional[datetime] = None) -> Dict[str, object]:
    """Execute a due callback through canonical replay, then remove its exact local plist."""
    registrar = OneShotWakeRegistrar(root=request.root)
    try:
        result = replay_one_shot_wake(request, RunScheduler(RunLedger(request.root)), now=now)
    except Exception:
        try:
            registrar.cleanup(_label(request))
        except Exception:
            pass
        raise
    registrar.cleanup(_label(request))
    return result
