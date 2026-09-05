"""Consent-gated, identity-scrubbed maintainer support bundles."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .errors import ProtocolRefusal
from .host_facts import collect_host_facts
from .identity_fence import (
    GOVERNED_TEMP_FENCES,
    GOVERNED_TEMP_PREFIXES,
    redact_governed_temp_prefixes,
    HOME_PATTERN,
    HOME_PREFIX,
    OWNER_USERNAME,
    PRIVATE_TMP_PREFIX,
    PRIVATE_VAR_TMP_PREFIX,
    TMP_PREFIX,
    VAR_FOLDERS_PREFIX,
)
from .manifest import verify_manifest
from .root import FloatiRoot
from .storage_identity import SNAPSHOT_DIRECTORY


DEFAULT_LINES = 240
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
_COLLECTOR_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
Collector = Tuple[str, Callable[[], Mapping[str, object]]]

_BASE64_IMAGE_PREFIX_CHARS = 64
_BASE64_WHITESPACE = re.compile(r"[\t\n\r\f\v ]+")
_BASE64_ALPHABET = re.compile(r"\A[A-Za-z0-9+/]*={0,2}\Z")
_DATA_BASE64_PREFIX = re.compile(
    r"\Adata:(?:[^,;\s]+/[^,;\s]+)?;base64,",
    re.IGNORECASE,
)
_RASTER_PREFIXES = (
    ("png", b"\x89PNG\r\n\x1a\n"),
    ("gif", b"GIF87a"),
    ("gif", b"GIF89a"),
    ("jpeg", b"\xff\xd8\xff"),
    ("bmp", b"BM"),
    ("tiff", b"II*\x00"),
    ("tiff", b"MM\x00*"),
)
_OPAQUE_BINARY_PREFIXES = (
    ("pdf", b"%PDF-"),
    ("zip", b"PK\x03\x04"),
    ("zip", b"PK\x05\x06"),
    ("zip", b"PK\x07\x08"),
    ("gzip", b"\x1f\x8b\x08"),
    ("mach-o", b"\xfe\xed\xfa\xce"),
    ("mach-o", b"\xce\xfa\xed\xfe"),
    ("mach-o", b"\xfe\xed\xfa\xcf"),
    ("mach-o", b"\xcf\xfa\xed\xfe"),
    ("mach-o", b"\xca\xfe\xba\xbe"),
    ("mach-o", b"\xbe\xba\xfe\xca"),
    ("mach-o", b"\xca\xfe\xba\xbf"),
    ("mach-o", b"\xbf\xba\xfe\xca"),
    ("elf", b"\x7fELF"),
)


class _OpaqueMember(Exception):
    def __init__(self, opaque_format: str, key_path: str) -> None:
        super().__init__(f"{opaque_format} at {key_path}")
        self.opaque_format = opaque_format
        self.key_path = key_path


def _scrub_string(value: str) -> str:
    scrubbed = HOME_PATTERN.sub("~", value).replace(HOME_PREFIX, "~/")
    scrubbed = redact_governed_temp_prefixes(scrubbed)
    return scrubbed.replace(OWNER_USERNAME, "<operator>")


def _encoded_opaque_format(value: str) -> Optional[str]:
    prefix = _DATA_BASE64_PREFIX.match(value)
    payload = value[prefix.end() :] if prefix is not None else value
    compact = _BASE64_WHITESPACE.sub("", payload)
    if _BASE64_ALPHABET.fullmatch(compact) is None:
        return None
    padding = len(compact) - len(compact.rstrip("="))
    if padding:
        if len(compact) % 4:
            return None
        decoded_length: Optional[int] = (len(compact) // 4) * 3 - padding
    else:
        quartets, remainder = divmod(len(compact), 4)
        decoded_length = (
            None if remainder == 1 else quartets * 3 + max(0, remainder - 1)
        )
    sample_length = min(len(compact), _BASE64_IMAGE_PREFIX_CHARS)
    sample_length -= sample_length % 4
    if sample_length < 4:
        return None
    try:
        encoded = compact[:sample_length].encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return None
    if base64.b64encode(decoded) != encoded:
        return None
    for image_format, signature in _RASTER_PREFIXES:
        if decoded.startswith(signature):
            if image_format == "bmp":
                if len(decoded) < 14:
                    return None
                declared_size = int.from_bytes(decoded[2:6], "little")
                if decoded_length is None or declared_size != decoded_length:
                    return None
                if decoded[6:10] != b"\x00" * 4:
                    return None
                pixel_offset = int.from_bytes(decoded[10:14], "little")
                if not 14 <= pixel_offset < declared_size:
                    return None
            return image_format
    if (
        len(decoded) >= 12
        and decoded.startswith(b"RIFF")
        and decoded[8:12] == b"WEBP"
    ):
        return "webp"
    for opaque_format, signature in _OPAQUE_BINARY_PREFIXES:
        if decoded.startswith(signature):
            return opaque_format
    return None


def _key_path(components: Tuple[object, ...]) -> str:
    rendered = "$"
    for component in components:
        if isinstance(component, int):
            rendered += f"[{component}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(component)):
            rendered += f".{component}"
        else:
            rendered += f"[{json.dumps(str(component), ensure_ascii=True)}]"
    return rendered


def _sanitize(value: object, path: Tuple[object, ...] = ()) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise _OpaqueMember("bytes", _key_path(path))
    if isinstance(value, str):
        opaque_format = _encoded_opaque_format(value)
        if opaque_format is not None:
            raise _OpaqueMember(opaque_format, _key_path(path))
        return _scrub_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item, (*path, str(key)))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, (*path, index)) for index, item in enumerate(value)]
    raise TypeError("collector result is not JSON-safe")


def identity_gate(data: bytes) -> None:
    """Refuse UTF-8 support data that still contains a governed identity."""

    for reason_code, token in (
        ("operator_home_path", HOME_PREFIX),
        *GOVERNED_TEMP_FENCES,
        ("owner_username", OWNER_USERNAME),
    ):
        if token.encode("ascii") in data:
            raise ProtocolRefusal(
                "snapshot_identity_fence_failed",
                "support bundle retains governed identity bytes after scrubbing",
                remedy=f"collector output still contains {reason_code}",
            )


def _encoded(value: Mapping[str, object]) -> bytes:
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    identity_gate(data)
    return data


def _tail_jsonl(path: Path, lines: int) -> Mapping[str, object]:
    if not path.is_file() or path.is_symlink():
        raise OSError("ledger is not an ordinary file")
    retained = path.read_text(encoding="utf-8").splitlines()[-lines:]
    rows = [json.loads(line) for line in retained if line]
    return {"status": "ok", "line_count": len(rows), "records": rows}


def derive_collectors(
    root: FloatiRoot, source: Path, lines: int
) -> Tuple[Collector, ...]:
    """Derive named collectors, including every JSONL plane in the explicit root."""

    from .doctor import Doctor
    from .registry import Registry
    from .wake_health import WakeHealthProjection

    selected_source = Path(source).resolve()

    def doctor() -> Mapping[str, object]:
        artifact, _status = Doctor(selected_source, root.path, ref="origin/main").artifact()
        return artifact

    def selftest_result() -> Mapping[str, object]:
        return {
            "status": "unavailable",
            "reason_code": "selftest_result_not_recorded",
        }

    def wake_health() -> Mapping[str, object]:
        now = datetime.now(timezone.utc)
        projection = WakeHealthProjection(root)
        return {
            "status": "ok",
            "nodes": {
                node: projection.fact(node, now)
                for node in Registry(root).active_node_ids()
            },
        }

    def registry_state() -> Mapping[str, object]:
        path = root.resolve_relative(Path("registry/entries.jsonl"))
        return _tail_jsonl(path, lines)

    def manifest_verification() -> Mapping[str, object]:
        errors = verify_manifest(selected_source)
        return {
            "status": "bundle_verified" if not errors else "bundle_mismatch",
            "errors": errors,
        }

    collectors: list[Collector] = [
        ("doctor", doctor),
        ("selftest", selftest_result),
        ("wake-health", wake_health),
        ("registry-state", registry_state),
        ("installed-manifest-verification", manifest_verification),
        (
            "host-facts",
            lambda: collect_host_facts(install_path=selected_source),
        ),
    ]
    for path in sorted(root.path.rglob("*.jsonl")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root.path).as_posix()
        name = "ledger-" + re.sub(r"[^a-z0-9._-]+", "-", relative.lower())
        collectors.append((name, lambda path=path: _tail_jsonl(path, lines)))
    return tuple(collectors)


def _collect(collectors: Sequence[Collector]) -> Dict[str, bytes]:
    sections: Dict[str, bytes] = {}
    names: set[str] = set()
    for name, collector in collectors:
        if not isinstance(name, str) or _COLLECTOR_NAME.fullmatch(name) is None:
            raise ProtocolRefusal(
                "snapshot_collector_name_invalid",
                "collector names must be unique bounded lowercase identifiers",
            )
        if name in names:
            raise ProtocolRefusal(
                "snapshot_collector_duplicate",
                "collector names must be unique",
            )
        names.add(name)
        try:
            raw = collector()
            if not isinstance(raw, Mapping):
                raise TypeError("collector result must be an object")
            sanitized = _sanitize(raw)
        except _OpaqueMember as exc:
            sanitized = {
                "status": "unavailable",
                "reason_code": "snapshot_opaque_member",
                "opaque_format": exc.opaque_format,
                "key_path": exc.key_path,
            }
        except Exception:
            sanitized = {
                "status": "unavailable",
                "reason_code": "collector_failed",
            }
        assert isinstance(sanitized, Mapping)
        sections[f"collectors/{name}.json"] = _encoded(sanitized)
    return sections


def _manifest(sections: Mapping[str, bytes]) -> bytes:
    files = [
        {
            "path": path,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for path, data in sorted(sections.items())
    ]
    inventory = {"schema_version": 0, "files": files}
    inventory_bytes = json.dumps(
        inventory, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload = {
        **inventory,
        "manifest_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
    }
    return _encoded(payload)


def _archive(sections: Mapping[str, bytes]) -> Tuple[bytes, str]:
    manifest = _manifest(sections)
    manifest_sha = json.loads(manifest)["manifest_sha256"]
    payloads = {**sections, "manifest.json": manifest}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path, data in sorted(payloads.items()):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o600
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue(), str(manifest_sha)


def _kilobytes(byte_count: int) -> str:
    return f"{max(1, (byte_count + 1023) // 1024)} KB"


def _consent(sections: Mapping[str, bytes]) -> str:
    labels = {
        "doctor": "doctor",
        "host-facts": "host facts",
        "installed-manifest-verification": "manifest verification",
        "registry-state": "registry state",
        "selftest": "selftest",
        "wake-health": "wake health",
    }
    rows = []
    ledger_lines = 0
    ledger_bytes = 0
    ledger_planes = 0
    for path, data in sorted(sections.items()):
        name = Path(path).stem
        if name.startswith("ledger-"):
            ledger_planes += 1
            ledger_bytes += len(data)
            try:
                line_count = json.loads(data).get("line_count", 0)
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                line_count = 0
            if isinstance(line_count, int) and not isinstance(line_count, bool):
                ledger_lines += line_count
            continue
        label = labels.get(name, name.replace("-", " "))
        rows.append(f"  {label:<22} {'1 artifact':>14}   {_kilobytes(len(data)):>8}")
    if ledger_planes:
        label = f"ledgers ({ledger_planes} planes)"
        measure = f"{ledger_lines:,} lines"
        rows.append(f"  {label:<22} {measure:>14}   {_kilobytes(ledger_bytes):>8}")
    total = sum(len(data) for data in sections.values())
    return (
        "This bundle is for a maintainer. Read what it holds before you send it.\n\n"
        + "\n".join(rows)
        + "\n                                    ---------\n"
        + f"  total                              {_kilobytes(total):>8}\n\n"
        + "Removed: your username, your home path, temporary paths.\n"
        + "Kept: your bus history, your node names, your host facts.\n\n"
    )


def _reserved_output(root: FloatiRoot, out: Path) -> bool:
    reserved = (root.path / SNAPSHOT_DIRECTORY).resolve()
    candidate = out.resolve()
    return candidate == reserved or reserved in candidate.parents


def create_support_bundle(
    *,
    root: FloatiRoot,
    source: Path,
    out: Path,
    lines: int = DEFAULT_LINES,
    yes: bool = False,
    stream: object = sys.stdout,
    input_stream: object = sys.stdin,
    collectors: Optional[Sequence[Collector]] = None,
    max_bytes: int = MAX_BUNDLE_BYTES,
    fault_hook: Optional[Callable[[str], None]] = None,
) -> Dict[str, object]:
    """Collect, disclose, consent, and atomically write one support tarball."""

    selected_out = Path(out).expanduser().resolve()
    if _reserved_output(root, selected_out):
        raise ProtocolRefusal(
            "snapshot_output_reserved",
            "support bundle output may not be inside .floati-snapshots",
        )
    if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= 10000:
        raise ProtocolRefusal(
            "snapshot_lines_invalid", "snapshot lines must be from 1 through 10000"
        )
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ProtocolRefusal("snapshot_cap_invalid", "snapshot cap must be positive")
    if not yes and not getattr(input_stream, "isatty", lambda: False)():
        raise ProtocolRefusal(
            "snapshot_consent_unavailable",
            "snapshot requires an interactive terminal or explicit --yes",
        )
    selected_collectors = (
        derive_collectors(root, Path(source), lines)
        if collectors is None
        else tuple(collectors)
    )
    sections = _collect(selected_collectors)
    archive_bytes, manifest_sha = _archive(sections)
    if len(archive_bytes) > max_bytes:
        raise ProtocolRefusal(
            "snapshot_bundle_too_large",
            "support bundle exceeds the ruled 32 MiB hard cap",
        )
    disclosure = _consent(sections)
    print(disclosure, end="", file=stream)
    if not yes:
        print("Write this file? [y/N] ", end="", file=stream)
        answer = input_stream.readline()
        if not isinstance(answer, str) or answer.strip().lower() != "y":
            return {
                "written": False,
                "consent": "declined",
                "out": str(selected_out),
            }
    parent = selected_out.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ProtocolRefusal(
            "snapshot_output_parent_invalid",
            "support bundle output parent must be an existing ordinary directory",
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{selected_out.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(archive_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        if fault_hook is not None:
            fault_hook("after_fsync_before_replace")
        os.replace(temporary, selected_out)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return {
        "written": True,
        "out": str(selected_out),
        "bytes": len(archive_bytes),
        "collector_count": len(sections),
        "manifest_sha256": manifest_sha,
    }


def verify_support_bundle(path: Path) -> list[str]:
    """Verify member inventory and the top-level manifest digest."""

    errors: list[str] = []
    try:
        with tarfile.open(Path(path), "r:gz") as archive:
            members = {
                member.name: archive.extractfile(member).read()
                for member in archive.getmembers()
                if member.isfile()
            }
    except (OSError, EOFError, tarfile.TarError):
        return ["snapshot_bundle_unreadable"]
    try:
        manifest = json.loads(members.pop("manifest.json"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return ["snapshot_manifest_invalid"]
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "files",
        "manifest_sha256",
    }:
        return ["snapshot_manifest_invalid"]
    inventory = {"schema_version": manifest["schema_version"], "files": manifest["files"]}
    calculated_manifest = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if calculated_manifest != manifest["manifest_sha256"]:
        errors.append("snapshot_manifest_digest_mismatch")
    listed = manifest.get("files")
    if not isinstance(listed, list):
        return errors + ["snapshot_manifest_files_invalid"]
    expected_paths: set[str] = set()
    for row in listed:
        if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
            errors.append("snapshot_manifest_entry_invalid")
            continue
        member = members.get(row["path"])
        expected_paths.add(str(row["path"]))
        if member is None:
            errors.append(f"snapshot_member_missing:{row['path']}")
            continue
        if len(member) != row["bytes"]:
            errors.append(f"snapshot_member_size_mismatch:{row['path']}")
        if hashlib.sha256(member).hexdigest() != row["sha256"]:
            errors.append(f"snapshot_member_digest_mismatch:{row['path']}")
    if set(members) != expected_paths:
        errors.append("snapshot_member_set_mismatch")
    return errors
