"""Additive installer for the Floati Codex Stop waiter."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from .codex_wait_contract import (
    WORKSPACE_MAP_RELATIVE,
    CodexWaitConsentLedger,
    CodexWaitSessionLedger,
    resolve_participant,
    write_workspace_map,
)
from .codex_hook_trust import (
    _CODEX_WAITER_NAMES,
    _codex_waiter_group_candidate,
    codex_hook_current_hash,
    observe_codex_waiter_hooks,
    observe_rebound_waiter,
)
from .errors import ProtocolRefusal
from .waiter_bundle import waiter_runtime_digest, waiter_runtime_files, write_waiter_provenance


_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEATX_NP = getattr(_LIBC, "renameatx_np", None)
if _RENAMEATX_NP is not None:
    _RENAMEATX_NP.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEATX_NP.restype = ctypes.c_int
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEAT2.restype = ctypes.c_int
_DARWIN_AT_FDCWD = -2
_LINUX_AT_FDCWD = -100
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001
_CODEX_WAIT_INTERPRETER = Path("/usr/bin/python3")
WAITER_LAUNCHER_NAME = "floati-codex-wait"
EMPTY_STOP_PREIMAGE = hashlib.sha256(b"").hexdigest()
_STOP_DEFINITION_HASH = re.compile(r"[0-9a-f]{64}\Z")
STOP_HOOK_CHANGE_CONSEQUENCE = (
    "Codex disarms until Settings → Hooks trust, relaunch every seat, then /boardbus"
)
STOP_HOOK_ACK_REMEDY = (
    "re-run the operator native install with --acknowledge-stop-hook-change "
    "<preimage>:<target> after reading that Codex disarms until Settings → "
    "Hooks trust, relaunch every seat, then /boardbus"
)
WAIT_VERB = ("wait", "--for", "fresh-work")
# BL-11: the registered Stop-hook command names this stable launcher, never
# one digest-named generation, so a bundle update cannot change the hook's
# bytes nor spend Codex's trust. The link repoints at the installed bundle.
STABLE_LAUNCHER_DIRECTORY = "current"
HOST_OBSERVER_EVENTS = (
    "PermissionRequest", "PreToolUse", "PostToolUse", "UserPromptSubmit",
    "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
    "PreCompact", "PostCompact", "Interrupt",
)
HOST_OBSERVER_STATUS = "Observing Floati host activity"


def format_waiter_stop_command(launcher: Path, root: Path) -> str:
    return " ".join(
        shlex.quote(part)
        for part in (
            str(_CODEX_WAIT_INTERPRETER),
            str(launcher),
            *WAIT_VERB,
            "--root",
            str(root),
        )
    )


def parse_stop_hook_acknowledgement(value: object) -> tuple[Optional[str], Optional[str]]:
    """Bind operator consent to one `preimage:target` pair. Booleans are not consent."""

    if value is None:
        return None, None
    if not isinstance(value, str):
        raise ProtocolRefusal(
            "stop_hook_definition_change_unacknowledged",
            "Stop-hook acknowledgement must name exact preimage and target hashes",
            remedy=STOP_HOOK_ACK_REMEDY,
        )
    parts = value.split(":")
    if (
        len(parts) != 2
        or _STOP_DEFINITION_HASH.fullmatch(parts[0]) is None
        or _STOP_DEFINITION_HASH.fullmatch(parts[1]) is None
    ):
        raise ProtocolRefusal(
            "stop_hook_definition_change_unacknowledged",
            "Stop-hook acknowledgement must name exact preimage and target hashes",
            remedy=STOP_HOOK_ACK_REMEDY,
        )
    return parts[0], parts[1]


def require_stop_definition_acknowledgement(
    *,
    preimage: str,
    target: str,
    acknowledgement: object,
    hooks_path: Path,
) -> None:
    """Refuse a Stop-definition write unless the named hash pair matches exactly."""

    if preimage == target:
        return
    try:
        acknowledged_preimage, acknowledged_target = parse_stop_hook_acknowledgement(
            acknowledgement
        )
    except ProtocolRefusal:
        acknowledged_preimage = acknowledged_target = None
    pair = f"{preimage}:{target}"
    if acknowledged_preimage == preimage and acknowledged_target == target:
        return
    refusal = ProtocolRefusal(
        "stop_hook_definition_change_unacknowledged",
        (
            f"this operation would change the Stop-hook definition at {hooks_path} "
            f"from {preimage} to {target}; {STOP_HOOK_CHANGE_CONSEQUENCE}"
        ),
        remedy=(
            "re-run the operator native install with --acknowledge-stop-hook-change "
            f"{pair} after reading that {STOP_HOOK_CHANGE_CONSEQUENCE}"
        ),
    )
    # Typed fields for the native CLI artifact (R3): the operator reads the
    # exact pair from evidence, never from a parsed sentence.
    refusal.refusal_detail = {  # type: ignore[attr-defined]
        "preimage": preimage,
        "target": target,
        "hooks_path": str(hooks_path),
        "consequence": STOP_HOOK_CHANGE_CONSEQUENCE,
    }
    raise refusal


def parse_waiter_stop_argv(argv: Sequence[str]) -> Optional[tuple[Path, Path]]:
    """Return (launcher, root) for the old 4-word hook or the wait-verb hook."""

    if not argv or argv[0] != str(_CODEX_WAIT_INTERPRETER):
        return None
    if len(argv) == 4 and argv[2] == "--root":
        return Path(argv[1]), Path(argv[3])
    if len(argv) == 7 and tuple(argv[2:5]) == WAIT_VERB and argv[5] == "--root":
        return Path(argv[1]), Path(argv[6])
    return None



def _require_codex_wait_interpreter() -> None:
    """Refuse the permanent host condition before validating hook shape."""

    if not _CODEX_WAIT_INTERPRETER.exists():
        raise ProtocolRefusal(
            "codex_wait_host_interpreter_absent",
            f"required Codex Stop-hook interpreter is absent: {_CODEX_WAIT_INTERPRETER}",
        )


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _observe_waiter_generation_target(store: Path, digest: str) -> str:
    """Classify one immutable generation leaf with lstat/byte evidence."""

    target = Path(store) / digest
    try:
        identity = target.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise ProtocolRefusal(
            "fleet_update_waiter_invalid",
            "waiter generation target could not be inspected",
        ) from exc
    if not stat.S_ISDIR(identity.st_mode):
        raise ProtocolRefusal(
            "fleet_update_waiter_invalid",
            "waiter generation target is not an ordinary directory",
        )
    try:
        observed = waiter_runtime_digest(target)
    except (OSError, ProtocolRefusal) as exc:
        raise ProtocolRefusal(
            "fleet_update_waiter_invalid",
            "existing waiter generation could not be verified",
        ) from exc
    if observed != digest:
        raise ProtocolRefusal(
            "fleet_update_waiter_invalid",
            "existing waiter generation diverges from the plan",
        )
    return "present"


def _rename_generation_noreplace(source: Path, target: Path) -> None:
    """Atomically publish one directory while preserving every raced leaf."""

    if _RENAMEATX_NP is not None:
        function = _RENAMEATX_NP
        directory = _DARWIN_AT_FDCWD
        flag = _RENAME_EXCL
    elif _RENAMEAT2 is not None:
        function = _RENAMEAT2
        directory = _LINUX_AT_FDCWD
        flag = _RENAME_NOREPLACE
    else:
        raise OSError(
            errno.ENOTSUP,
            "the host has no no-replace directory rename primitive",
            str(target),
        )
    ctypes.set_errno(0)
    result = function(
        directory,
        os.fsencode(source),
        directory,
        os.fsencode(target),
        flag,
    )
    if result != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), str(target))


def _object_value_span(text: str, object_start: int, name: str) -> tuple[int, int]:
    """Locate one direct object member without reserializing sibling bytes."""

    decoder = json.JSONDecoder()
    index = object_start
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "{":
        raise ValueError("object start is invalid")
    index += 1
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] == "}":
            break
        key, key_end = decoder.raw_decode(text, index)
        if not isinstance(key, str):
            raise ValueError("object key is invalid")
        index = key_end
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != ":":
            raise ValueError("object separator is invalid")
        index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        value_start = index
        _value, value_end = decoder.raw_decode(text, value_start)
        if key == name:
            return value_start, value_end
        index = value_end
        while index < len(text) and text[index].isspace():
            index += 1
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "}":
            break
        raise ValueError("object terminator is invalid")
    raise KeyError(name)


def _fault(fault_hook: Optional[Callable[[str], None]], event: str) -> None:
    if fault_hook is not None:
        fault_hook(event)


def _durable_waiter_readback(
    path: Path,
    encoded: bytes,
    *,
    fault_hook: Optional[Callable[[str], None]] = None,
) -> bytes:
    """Replay the directory barrier and exact readback without replacing bytes."""

    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    _fault(fault_hook, "after_waiter_directory_fsync")
    try:
        readback = path.read_bytes()
    except OSError as exc:
        raise ProtocolRefusal(
            "fleet_update_waiter_binding_readback_invalid",
            "waiter hook bytes could not be read back",
        ) from exc
    if path.is_symlink() or not path.is_file() or readback != encoded:
        raise ProtocolRefusal(
            "fleet_update_waiter_binding_readback_invalid",
            "waiter hook bytes did not read back",
        )
    _fault(fault_hook, "after_waiter_readback")
    return readback


def _write_atomic(
    path: Path,
    encoded: bytes,
    *,
    fault_hook: Optional[Callable[[str], None]] = None,
) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    _fault(fault_hook, "before_waiter_temp_create")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.floati-", dir=path.parent
    )
    temporary = Path(temporary_name)
    pending_temporary = True
    try:
        os.fchmod(descriptor, 0o600)
        _fault(fault_hook, "after_waiter_temp_create")
        _fault(fault_hook, "before_waiter_write")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short atomic write")
            offset += written
        _fault(fault_hook, "after_waiter_write")
        _fault(fault_hook, "before_waiter_file_fsync")
        os.fsync(descriptor)
        _fault(fault_hook, "after_waiter_file_fsync")
        os.close(descriptor)
        descriptor = -1
        _fault(fault_hook, "before_waiter_replace")
        os.replace(temporary, path)
        pending_temporary = False
        _fault(fault_hook, "after_waiter_replace")
        return _durable_waiter_readback(path, encoded, fault_hook=fault_hook)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if pending_temporary:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _array_value_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Return the raw spans of one JSON array's direct values."""

    decoder = json.JSONDecoder()
    cursor = start
    while cursor < end and text[cursor].isspace():
        cursor += 1
    if cursor >= end or text[cursor] != "[":
        raise ValueError("array start is invalid")
    cursor += 1
    spans: list[tuple[int, int]] = []
    while True:
        while cursor < end and text[cursor].isspace():
            cursor += 1
        if cursor < end and text[cursor] == "]":
            return spans
        value_start = cursor
        _value, cursor = decoder.raw_decode(text, cursor)
        spans.append((value_start, cursor))
        while cursor < end and text[cursor].isspace():
            cursor += 1
        if cursor < end and text[cursor] == ",":
            cursor += 1
            continue
        if cursor < end and text[cursor] == "]":
            return spans
        raise ValueError("array terminator is invalid")


def _decode_hooks_document(before: bytes) -> tuple[str, dict]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-JSON numeric constant")

    try:
        text = before.decode("utf-8")
        document = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolRefusal("codex_hooks_invalid", "hooks file is not unambiguous JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("hooks"), dict):
        raise ProtocolRefusal("codex_hooks_invalid", "hooks document shape is invalid")
    return text, document


def _rewrite_hook_group(text: str, event: str, block: dict, index=None) -> bytes:
    """Replace/append only the owned value; retain every foreign raw span."""
    encoded = json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        root_start = next(i for i, character in enumerate(text) if not character.isspace())
        hooks_start, hooks_end = _object_value_span(text, root_start, "hooks")
        try:
            start, end = _object_value_span(text, hooks_start, event)
        except KeyError:
            # Only the new member is encoded; existing members and formatting remain intact.
            existing, _ = json.JSONDecoder().raw_decode(text, hooks_start)
            insertion = ("," if existing else "") + json.dumps(event) + ":[" + encoded + "]"
            return (text[:hooks_end - 1] + insertion + text[hooks_end - 1:]).encode("utf-8")
        spans = _array_value_spans(text, start, end)
        if index is not None:
            first, last = spans[index]
            return (text[:first] + encoded + text[last:]).encode("utf-8")
        position = spans[-1][1] if spans else start + 1
        insertion = ("," if spans else "") + encoded
        return (text[:position] + insertion + text[position:]).encode("utf-8")
    except (KeyError, StopIteration, IndexError, ValueError) as exc:
        raise ProtocolRefusal("codex_hooks_invalid", "hooks document spans are invalid") from exc


def plan_waiter_rebind(hooks_path: Path, store: Path, target_digest: str) -> Dict[str, object]:
    """Plan one surgical Floati Stop-hook launcher replacement without writing."""

    _require_codex_wait_interpreter()
    hooks_path, store = Path(hooks_path), Path(store)
    if (
        not hooks_path.is_absolute()
        or hooks_path.is_symlink()
        or not hooks_path.is_file()
        or not store.is_absolute()
        or store.is_symlink()
        or not store.is_dir()
        or re.fullmatch(r"[0-9a-f]{64}", target_digest) is None
    ):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter rebind coordinate is invalid")
    try:
        before = hooks_path.read_bytes()
        text = before.decode("utf-8")
        document = json.loads(text)
        stop = document["hooks"]["Stop"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook document is unreadable") from exc
    if not isinstance(stop, list):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter Stop hooks are invalid")
    indexes = [
        index for index, block in enumerate(stop)
        if _codex_waiter_group_candidate(block)
    ]
    if len(indexes) != 1:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook block is ambiguous")
    index = indexes[0]
    block = stop[index]
    if not isinstance(block, dict) or set(block) != {"hooks"}:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook block is invalid")
    hooks = block.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook handler is invalid")
    command = hooks[0].get("command")
    try:
        argv = shlex.split(command) if isinstance(command, str) else []
    except ValueError as exc:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook command is invalid") from exc
    parsed = parse_waiter_stop_argv(argv)
    if parsed is None:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook command is invalid")
    current_launcher, _root = parsed
    try:
        relative = current_launcher.relative_to(store)
    except ValueError as exc:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter launcher is outside its declared store") from exc
    stable_shape = (
        len(relative.parts) == 3
        and relative.parts[0] == STABLE_LAUNCHER_DIRECTORY
        and relative.parts[1:] == ("scripts", "floati-codex-wait")
    )
    legacy_shape = (
        len(relative.parts) == 3
        and re.fullmatch(r"[0-9a-f]{64}", relative.parts[0]) is not None
        and relative.parts[1:] == ("scripts", "floati-codex-wait")
    )
    if not stable_shape and not legacy_shape:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter launcher is not content addressed")
    if stable_shape:
        # BL-11: the stable launcher never moves. The update repoints the
        # stable directory at the new generation; the registered bytes and
        # Codex's trust stand still.
        target_launcher = current_launcher
        target_block = json.loads(json.dumps(block))
        repoint = {"link": str(store / STABLE_LAUNCHER_DIRECTORY), "digest": target_digest}
    else:
        target_launcher = store / target_digest / "scripts" / "floati-codex-wait"
        target_block = json.loads(json.dumps(block))
        target_argv = [argv[0], str(target_launcher), *argv[2:]]
        target_block["hooks"][0]["command"] = shlex.join(target_argv)
        repoint = None
    try:
        root_start = next(position for position, character in enumerate(text) if not character.isspace())
        hooks_start, _hooks_end = _object_value_span(text, root_start, "hooks")
        stop_start, stop_end = _object_value_span(text, hooks_start, "Stop")
        block_start, block_end = _array_value_spans(text, stop_start, stop_end)[index]
    except (StopIteration, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook block span is invalid") from exc
    after = (
        text[:block_start]
        + json.dumps(target_block, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + text[block_end:]
    ).encode("utf-8")
    return {
        "configuration": str(hooks_path),
        "store": str(store),
        "current_launcher": str(current_launcher),
        "target_launcher": str(target_launcher),
        "before": before,
        "after": after,
        "current_hook_hash": codex_hook_current_hash(block),
        "target_hook_hash": codex_hook_current_hash(target_block),
        "repoint": repoint,
    }


def commit_waiter_rebind(
    staged: Dict[str, object],
    *,
    _fault_hook: Optional[Callable[[str], None]] = None,
    acknowledge_stop_hook_change: Optional[str] = None,
) -> Dict[str, object]:
    """Commit one already-planned Floati hook block and verify the exact bytes."""

    required = {"configuration", "before", "after", "current_hook_hash", "target_hook_hash"}
    if not isinstance(staged, dict) or not required <= set(staged):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "staged waiter rebind is invalid")
    path = Path(str(staged["configuration"]))
    before, after = staged["before"], staged["after"]
    if not isinstance(before, bytes) or not isinstance(after, bytes):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "staged waiter bytes are invalid")
    try:
        observed = path.read_bytes()
    except OSError as exc:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook document is unreadable") from exc
    if observed not in {before, after}:
        raise ProtocolRefusal("fleet_update_waiter_binding_drift", "waiter hook document changed after preflight")
    expected_pre_trust = staged.get("pre_trust_observation")
    if observed == before and expected_pre_trust is not None:
        rows = observe_codex_waiter_hooks(path)
        if len(rows) != 1 or {
            "hook_trust_key": rows[0].get("hook_trust_key"),
            "current_hook_hash": rows[0].get("hook_trust_current_hash"),
            "observed_trusted_hash": rows[0].get("hook_trust_observed_hash"),
            "observed_enabled": rows[0].get("hook_enabled"),
        } != expected_pre_trust:
            raise ProtocolRefusal("fleet_update_waiter_binding_drift", "waiter trust changed after preflight")
    if observed == before and after != before:
        require_stop_definition_acknowledgement(
            preimage=str(staged["current_hook_hash"]),
            target=str(staged["target_hook_hash"]),
            acknowledgement=acknowledge_stop_hook_change,
            hooks_path=path,
        )
        readback = _write_atomic(path, after, fault_hook=_fault_hook)
    else:
        # Exact post bytes do not prove that a predecessor completed the
        # parent-directory barrier.  Replay that barrier and readback without
        # another replacement before evidence may be appended.
        readback = _durable_waiter_readback(path, after, fault_hook=_fault_hook)
    repoint = staged.get("repoint")
    if isinstance(repoint, dict):
        _repoint_store_link(
            Path(str(repoint["link"])), str(repoint["digest"]), _fault_hook
        )
    trust = observe_rebound_waiter(
        path,
        expected_key=str(staged.get("hook_trust_key")),
        expected_hash=str(staged["target_hook_hash"]),
    )
    _fault(_fault_hook, "after_waiter_trust_observation")
    return {
        "configuration": str(path),
        "pre_digest": _digest_bytes(before),
        "post_digest": _digest_bytes(readback),
        "current_hook_hash": staged["current_hook_hash"],
        "target_hook_hash": staged["target_hook_hash"],
        "trust_observation": trust,
    }


def _repoint_store_link(link: Path, digest: str, fault_hook=None) -> None:
    """Point the stable launcher directory at one digest, atomically (BL-11)."""

    _fault(fault_hook, "before_waiter_link_repoint")
    if not link.is_absolute() or link.parent.is_symlink():
        raise ProtocolRefusal(
            "fleet_update_waiter_binding_invalid", "stable launcher link is invalid"
        )
    temporary = link.parent / f".{link.name}.{os.getpid()}"
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    temporary.symlink_to(digest)
    os.replace(temporary, link)
    fd = os.open(link.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fault(fault_hook, "after_waiter_link_repoint")
    if link.resolve(strict=True) != (link.parent / digest).resolve(strict=True):
        raise ProtocolRefusal(
            "fleet_update_waiter_binding_drift",
            "stable launcher does not resolve to the target generation",
        )


def _fsync_waiter_tree(tree: Path) -> None:
    """Fsync every immutable generation file and directory bottom-up."""

    files = sorted(path for path in tree.rglob("*") if path.is_file())
    for path in files:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    directories = sorted(
        (path for path in tree.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    directories.append(tree)
    for path in directories:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _durable_waiter_generation(
    target: Path,
    store: Path,
    expected_digest: str,
    *,
    fault_hook: Optional[Callable[[str], None]] = None,
) -> None:
    _fsync_waiter_tree(target)
    descriptor = os.open(store, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fault(fault_hook, "after_waiter_generation_store_fsync")
    if waiter_runtime_digest(target) != expected_digest:
        raise ProtocolRefusal("fleet_update_waiter_invalid", "staged waiter runtime readback diverged")
    _fault(fault_hook, "after_waiter_generation_readback")


def stage_waiter_runtime(
    source_root: Path,
    store: Path,
    expected_digest: str,
    *,
    _fault_hook: Optional[Callable[[str], None]] = None,
) -> Dict[str, object]:
    """Copy one verified runtime into its immutable digest directory, never live-bind it."""

    source_root, store = Path(source_root), Path(store)
    if (
        not source_root.is_absolute() or source_root.is_symlink() or not source_root.is_dir()
        or not store.is_absolute() or store.is_symlink() or not store.is_dir()
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter runtime stage coordinate is invalid")
    paths = waiter_runtime_files(source_root)
    if waiter_runtime_digest(source_root) != expected_digest:
        raise ProtocolRefusal("fleet_update_waiter_invalid", "waiter runtime source digest diverges from the plan")
    target = store / expected_digest
    if _observe_waiter_generation_target(store, expected_digest) == "present":
        _durable_waiter_generation(
            target, store, expected_digest, fault_hook=_fault_hook
        )
        return {"target": str(target), "digest": expected_digest, "staged": False}
    staging = Path(tempfile.mkdtemp(prefix=".floati-fleet-stage-", dir=store))
    try:
        for source in paths:
            relative = source.relative_to(source_root)
            copied = staging / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copied)
            copied.chmod(source.stat().st_mode & 0o777)
        if waiter_runtime_digest(staging) != expected_digest:
            raise ProtocolRefusal("fleet_update_waiter_invalid", "staged waiter runtime bytes diverged")
        write_waiter_provenance(source_root, staging)
        _fsync_waiter_tree(staging)
        _fault(_fault_hook, "before_waiter_generation_replace")
        try:
            _rename_generation_noreplace(staging, target)
        except OSError as exc:
            unsupported = {
                errno.ENOTSUP,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
                errno.ENOSYS,
                errno.EINVAL,
            }
            if exc.errno == errno.EEXIST:
                detail = "waiter generation target appeared during exclusive publication"
            elif exc.errno in unsupported:
                detail = "host filesystem cannot publish a waiter generation without replacement"
            else:
                detail = "waiter generation exclusive publication failed"
            raise ProtocolRefusal(
                "fleet_update_waiter_invalid", detail
            ) from exc
        _fault(_fault_hook, "after_waiter_generation_replace")
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    _durable_waiter_generation(
        target, store, expected_digest, fault_hook=_fault_hook
    )
    return {"target": str(target), "digest": expected_digest, "staged": True}


class CodexHookInstaller:
    def __init__(
        self,
        *,
        source_root: Path,
        bus_home: Path,
        hooks_path: Path,
        destination: Path,
    ) -> None:
        self.source_root = Path(source_root).expanduser()
        self.bus_home = Path(bus_home).expanduser()
        self.hooks_path = Path(hooks_path).expanduser()
        self.destination = Path(destination).expanduser()
        for name, path in (
            ("source", self.source_root),
            ("bus", self.bus_home),
            ("hooks", self.hooks_path),
            ("destination", self.destination),
        ):
            if not path.is_absolute() or path.is_symlink():
                raise ProtocolRefusal(
                    "codex_wait_install_path_invalid",
                    f"{name} path must be absolute and non-symlinked",
                )

    def _install_bundle(self) -> tuple[str, Path, str]:
        paths = waiter_runtime_files(self.source_root)
        required = {
            "LICENSE",
            "scripts/floati-codex-wait",
            "floati/codex_wait.py",
            "floati/codex_wait_contract.py",
        }
        present = {path.relative_to(self.source_root).as_posix() for path in paths}
        if not required <= present:
            raise ProtocolRefusal("codex_wait_source_incomplete", "waiter runtime source is incomplete")
        bundle_digest = waiter_runtime_digest(self.source_root)
        target = self.destination / bundle_digest
        self.destination.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            staging = Path(tempfile.mkdtemp(prefix=".floati-wake-", dir=self.destination))
            try:
                for source in paths:
                    relative = source.relative_to(self.source_root)
                    installed = staging / relative
                    installed.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, installed)
                    installed.chmod(source.stat().st_mode & 0o777)
                if waiter_runtime_digest(staging) != bundle_digest:
                    raise ProtocolRefusal("codex_wait_install_digest_mismatch", "installed bytes differ")
                write_waiter_provenance(self.source_root, staging)
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        installed_digest = waiter_runtime_digest(target)
        if installed_digest != bundle_digest:
            raise ProtocolRefusal("codex_wait_install_digest_mismatch", "existing installed bytes differ")
        self._repoint_current(target)
        return bundle_digest, target, installed_digest

    def _repoint_current(self, target: Path) -> None:
        """Point the stable launcher directory at this bundle, atomically.

        BL-11: the registered hook command names ``<store>/current`` so the
        registered bytes never depend on the installed bundle's digest.
        """

        link = self.destination / STABLE_LAUNCHER_DIRECTORY
        temporary = self.destination / f".{STABLE_LAUNCHER_DIRECTORY}.{os.getpid()}"
        if temporary.is_symlink() or temporary.exists():
            if temporary.is_dir() and not temporary.is_symlink():
                shutil.rmtree(temporary)
            else:
                temporary.unlink()
        temporary.symlink_to(target.name)
        os.replace(temporary, link)
        fd = os.open(self.destination, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if link.resolve(strict=True) != target.resolve(strict=True):
            raise ProtocolRefusal(
                "codex_wait_install_digest_mismatch",
                "the stable launcher does not resolve to the installed bundle",
            )

    def _valid_waiter_block(self, entry: object) -> bool:
        if not isinstance(entry, dict) or set(entry) != {"hooks"}:
            return False
        hooks = entry.get("hooks")
        if not isinstance(hooks, list) or len(hooks) != 1:
            return False
        hook = hooks[0]
        if not isinstance(hook, dict) or set(hook) != {
            "type", "command", "timeout", "statusMessage"
        }:
            return False
        if (
            hook.get("type") != "command"
            or hook.get("statusMessage") != "Watching Floati bus"
            or not isinstance(hook.get("timeout"), int)
            or isinstance(hook.get("timeout"), bool)
        ):
            return False
        try:
            argv = shlex.split(hook["command"])
        except (TypeError, ValueError):
            return False
        parsed = parse_waiter_stop_argv(argv)
        if parsed is None:
            return False
        launcher, bus_home = parsed
        if str(bus_home) != str(self.bus_home):
            return False
        try:
            relative = launcher.relative_to(self.destination)
        except ValueError:
            return False
        # BL-11: the stable ``current`` launcher is the ruled shape; the
        # digest-named shape stays valid so one update migrates older installs.
        if not (
            len(relative.parts) == 3
            and (
                relative.parts[0] == STABLE_LAUNCHER_DIRECTORY
                or re.fullmatch(r"[0-9a-f]{64}", relative.parts[0]) is not None
            )
            and relative.parts[1] == "scripts"
            and relative.parts[2] in _CODEX_WAITER_NAMES
        ):
            return False
        if relative.parts[0] == STABLE_LAUNCHER_DIRECTORY:
            # The stable launcher must resolve; a dangling link is the
            # malformed case, while legacy digest directories keep the old
            # shape-only validation.
            return launcher.exists()
        return True

    def _plan_hook_rewrite(
        self, before: bytes, block: Dict[str, object]
    ) -> tuple[str, bytes, str]:
        text, document = _decode_hooks_document(before)
        stop = document["hooks"].get("Stop", [])
        if not isinstance(stop, list):
            raise ProtocolRefusal("codex_hooks_invalid", "Stop hooks must be a list")
        floati_indexes = [
            index
            for index, entry in enumerate(stop)
            if _codex_waiter_group_candidate(entry)
        ]
        if len(floati_indexes) > 1 or any(
            not self._valid_waiter_block(stop[index]) for index in floati_indexes
        ):
            raise ProtocolRefusal(
                "codex_wait_hook_conflict",
                "Floati waiter blocks are malformed or ambiguous",
                remedy="review the named Codex Stop hooks and resolve malformed or duplicate Floati "
                    "launcher groups before re-running the explicit native installer",
            )
        if not floati_indexes:
            state = "installed"
            preimage = EMPTY_STOP_PREIMAGE
            index = None
        elif stop[floati_indexes[0]] == block:
            return "already_installed", before, codex_hook_current_hash(block)
        else:
            state = "replaced"
            preimage = codex_hook_current_hash(stop[floati_indexes[0]])
            index = floati_indexes[0]
        after = _rewrite_hook_group(text, "Stop", block, index)
        return state, after, preimage

    def _plan_host_observers(self, before: bytes, command: str) -> tuple[bytes, dict]:
        after = before
        definitions = {}
        for event in HOST_OBSERVER_EVENTS:
            text, document = _decode_hooks_document(after)
            groups = document["hooks"].get(event, [])
            if not isinstance(groups, list):
                raise ProtocolRefusal("codex_host_hook_conflict", "host event hooks must be a list",
                    remedy="fix the named Codex host event hooks to an array before re-running native install")
            block = {"hooks": [{"type": "command", "command": command,
                "timeout": 3 if event == "Interrupt" else 10,
                "statusMessage": HOST_OBSERVER_STATUS}]}
            owned = [i for i, group in enumerate(groups)
                if _codex_waiter_group_candidate(group)]
            # This closed observer definition has no matcher, async decision,
            # sibling handler, caller-selected workspace or session selector.
            if len(owned) > 1 or any(groups[i] != block or
                    type(groups[i]["hooks"][0]["timeout"]) is not int for i in owned):
                raise ProtocolRefusal("codex_host_hook_conflict",
                    "Floati host observer groups are malformed or ambiguous",
                    remedy="review the named Codex host event and resolve malformed or duplicate "
                        "Floati observer groups before re-running the explicit native installer")
            state = "already_installed" if owned else "installed"
            if not owned:
                after = _rewrite_hook_group(text, event, block)
            # The existing native trust normalizer hardcodes event_name=stop.
            # This is an explicitly local definition digest, never a fabricated
            # observer trust hash or a claim that Stop trust covers observers.
            digest = _digest_bytes(json.dumps({"event": event, "group": block},
                ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            definitions[event] = {"definition_hash": digest,
                "hash_provenance": "SHA-256 of canonical event/group JSON; not a native trust hash",
                "state": state, "trust": "unmeasured"}
        return after, definitions

    def _write_workspace_map(self, workspace: Path, node_id: str) -> str:
        return write_workspace_map(self.bus_home, workspace, node_id)

    def plan_stop_definition(self, *, hook_timeout_seconds: int,
                             before: Optional[bytes] = None) -> Dict[str, object]:
        """Name the installed preimage and planned target without writing."""

        launcher = (
            self.destination / STABLE_LAUNCHER_DIRECTORY / "scripts" / WAITER_LAUNCHER_NAME
        )
        command = format_waiter_stop_command(launcher, self.bus_home)
        block: Dict[str, object] = {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": hook_timeout_seconds,
                    "statusMessage": "Watching Floati bus",
                }
            ]
        }
        hooks_existed = True
        if before is None:
            try:
                before = self.hooks_path.read_bytes()
            except FileNotFoundError:
                hooks_existed = False
                before = b'{"hooks":{"Stop":[]}}\n'
        state, after, preimage = self._plan_hook_rewrite(before, block)
        return {
            "preimage": preimage,
            "target": codex_hook_current_hash(block),
            "state": state,
            "before": before,
            "after": after,
            "block": block,
            "command": command,
            "hooks_existed": hooks_existed,
        }

    def prepare(self, *, hook_timeout_seconds: int,
                acknowledge_stop_hook_change: Optional[str] = None) -> Dict[str, object]:
        """Prepare the global Stop hook without a seat, mapping, consent or trust write."""
        _require_codex_wait_interpreter()
        from .root import FloatiRoot
        FloatiRoot.open_direct_home(self.bus_home)
        if type(hook_timeout_seconds) is not int or not 0 < hook_timeout_seconds <= 86400:
            raise ProtocolRefusal("arguments_invalid", "hook timeout must be an integer in 1..86400",
                remedy="pass --hook-timeout-seconds as an integer in 1..86400")
        plan = self.plan_stop_definition(hook_timeout_seconds=hook_timeout_seconds)
        require_stop_definition_acknowledgement(preimage=plan["preimage"], target=plan["target"],
            acknowledgement=acknowledge_stop_hook_change, hooks_path=self.hooks_path)
        # Validate the exact planned trust coordinate before any persistent write.
        observe_codex_waiter_hooks(self.hooks_path, definition=plan["after"])
        bundle_digest, target, installed_digest = self._install_bundle()
        if plan["after"] != plan["before"] or not self.hooks_path.exists():
            _write_atomic(self.hooks_path, plan["after"])
        readback = self.hooks_path.read_bytes()
        if readback != plan["after"]:
            raise ProtocolRefusal("codex_hooks_readback_mismatch", "hooks write did not read back")
        trust_rows = observe_codex_waiter_hooks(self.hooks_path)
        if len(trust_rows) != 1:
            raise ProtocolRefusal("codex_wait_hook_trust_unavailable",
                "prepared waiter trust coordinate is ambiguous")
        return {"artifact_version": 0, "state": plan["state"], "scope": "global_stop_hook",
            "bundle_digest": bundle_digest, "installed_bundle_digest": installed_digest,
            "installed_path": str(target),
            "hooks_before_state": "present" if plan["hooks_existed"] else "absent",
            "hooks_before_digest": _digest_bytes(plan["before"]) if plan["hooks_existed"] else None,
            "hooks_after_digest": _digest_bytes(readback), "command": plan["command"],
            **trust_rows[0]}

    def preparation_command(self, *, hook_timeout_seconds: int) -> str:
        """Name the exact definition change for caller review without writing it."""
        plan = self.plan_stop_definition(hook_timeout_seconds=hook_timeout_seconds)
        return shlex.join(["floati", "hook", "install", "--harness", "codex", "--prepare-only",
            "--root", str(self.bus_home), "--runtime", str(self.source_root),
            "--hooks", str(self.hooks_path), "--destination", str(self.destination),
            "--hook-timeout-seconds", str(hook_timeout_seconds),
            "--acknowledge-stop-hook-change", plan["preimage"] + ":" + plan["target"]])

    def install(
        self,
        workspace: Path,
        node_id: str,
        *,
        hook_timeout_seconds: int,
        wait_deadline_seconds: int,
        session_id: Optional[str] = None,
        acknowledge_stop_hook_change: Optional[str] = None,
        include_host_observers: bool = False,
        plan=None,
    ) -> Dict[str, object]:
        _require_codex_wait_interpreter()
        if type(include_host_observers) is not bool:
            raise ProtocolRefusal("codex_host_hooks_invalid", "include_host_observers must be boolean",
                remedy="pass a boolean include_host_observers value to the governed installer")
        workspace = Path(workspace).expanduser()
        if not workspace.is_absolute() or workspace.is_symlink() or (plan is None and not workspace.is_dir()):
            raise ProtocolRefusal("codex_wait_workspace_invalid", "workspace must be an existing absolute directory")
        paths = waiter_runtime_files(self.source_root)
        required = {
            "LICENSE", "scripts/floati-codex-wait", "floati/codex_wait.py",
            "floati/codex_wait_contract.py",
        }
        present = {path.relative_to(self.source_root).as_posix() for path in paths}
        if not required <= present:
            raise ProtocolRefusal("codex_wait_source_incomplete", "waiter runtime source is incomplete")
        planned_bundle_digest = waiter_runtime_digest(self.source_root)
        planned_target = self.destination / planned_bundle_digest
        if plan is not None:
            from .installed_reader import PROVENANCE_NAME
            from .waiter_bundle import waiter_source_provenance

            before = plan.read(self.hooks_path)
            hook = self.plan_stop_definition(hook_timeout_seconds=hook_timeout_seconds,
                before=before if before is not None else b'{"hooks":{}}\n')
            if hook['state'] != 'installed':
                raise ProtocolRefusal('setup_codex_hook_present',
                    'The Codex home already has a Floati Stop hook.',
                    remedy='Preserve that hook and preview this seat without --hook.')
            for source in paths:
                plan.file(planned_target / source.relative_to(self.source_root), source.read_bytes(),
                          mode=source.stat().st_mode & 0o777)
            plan.json(planned_target / PROVENANCE_NAME, waiter_source_provenance(self.source_root))
            plan.link(self.destination / STABLE_LAUNCHER_DIRECTORY, planned_target.name)
            plan.consent(workspace, node_id, 'codex', hook_timeout_seconds, wait_deadline_seconds)
            plan.commands(workspace, 'codex', codex_home=self.hooks_path.parent, runtime=self.source_root)
            plan.file(self.hooks_path, hook['after'], replace=True)
            return {'hooks_path': str(self.hooks_path), 'preimage': hook['preimage'],
                    'target': hook['target'], 'consequence': STOP_HOOK_CHANGE_CONSEQUENCE,
                    'native_trust': 'requires_review'}
        # BL-11: the registered command names the stable launcher, so bundle
        # updates repoint the link instead of rewriting the hook's bytes.
        launcher = (
            self.destination / STABLE_LAUNCHER_DIRECTORY / "scripts" / WAITER_LAUNCHER_NAME
        )
        command = format_waiter_stop_command(launcher, self.bus_home)
        block: Dict[str, object] = {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": hook_timeout_seconds,
                    "statusMessage": "Watching Floati bus",
                }
            ]
        }
        before = self.hooks_path.read_bytes()
        state, after, preimage = self._plan_hook_rewrite(before, block)
        observer_definitions = {}
        if include_host_observers:
            after, observer_definitions = self._plan_host_observers(after, command)
        require_stop_definition_acknowledgement(
            preimage=preimage,
            target=codex_hook_current_hash(block),
            acknowledgement=acknowledge_stop_hook_change,
            hooks_path=self.hooks_path,
        )
        prior_trust_rows = observe_codex_waiter_hooks(self.hooks_path)

        bundle_digest, target, installed_digest = self._install_bundle()
        if bundle_digest != planned_bundle_digest or target != planned_target:
            raise ProtocolRefusal("codex_wait_install_digest_mismatch", "planned waiter bundle identity changed")
        map_digest = self._write_workspace_map(workspace, node_id)
        participant = resolve_participant(self.bus_home, workspace)
        if participant is None or participant.binding.node_id != node_id:
            raise ProtocolRefusal("codex_wait_participant_unresolved", "installed workspace did not resolve")
        consent = CodexWaitConsentLedger(participant.root).arm(
            participant.binding,
            hook_timeout_seconds=hook_timeout_seconds,
            wait_deadline_seconds=wait_deadline_seconds,
            idempotency_key="codex-wait-install-" + hashlib.sha256(
                json.dumps(
                    {
                        "node_id": participant.binding.node_id,
                        "workspace": participant.binding.workspace.as_posix(),
                        "hook_timeout_seconds": hook_timeout_seconds,
                        "wait_deadline_seconds": wait_deadline_seconds,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:32],
        )
        session = None
        if session_id is not None:
            session_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
            session = CodexWaitSessionLedger(participant.root).arm(
                participant.binding,
                consent,
                session_id,
                idempotency_key=f"codex-wait-install-session-{session_key}",
                take_over=True,
            )
        if after != before:
            _write_atomic(self.hooks_path, after)
        readback = self.hooks_path.read_bytes()
        if readback != after:
            raise ProtocolRefusal("codex_hooks_readback_mismatch", "hooks write did not read back")
        trust_rows = observe_codex_waiter_hooks(self.hooks_path)
        if len(trust_rows) != 1:
            raise ProtocolRefusal(
                "codex_wait_hook_trust_unavailable",
                "installed waiter trust coordinate is ambiguous",
            )
        trust = trust_rows[0]
        result: Dict[str, object] = {
            "artifact_version": 0,
            "state": state,
            "bundle_digest": bundle_digest,
            "installed_bundle_digest": installed_digest,
            "hooks_before_digest": _digest_bytes(before),
            "hooks_after_digest": _digest_bytes(readback),
            "command_digest": _digest_bytes(command.encode("utf-8")),
            "workspace_map_digest": map_digest,
            "consent_receipt_id": consent["id"],
            "installed_path": str(target),
            "command": command,
            "hook_trust_invalidated_by_install": (
                state == "replaced"
                and any(row.get("hook_armed") is True for row in prior_trust_rows)
                and trust["hook_armed"] is False
            ),
            **trust,
        }
        if session is not None:
            result["acting_session_id"] = session["acting_session_id"]
            result["session_receipt_id"] = session["id"]
        if include_host_observers:
            result["host_observer_definitions"] = observer_definitions
            result["host_observer_adoption"] = (
                "Observer trust is unmeasured per definition; owner must review Settings → Hooks "
                "trust and relaunch every seat, then /boardbus. Stop trust does not qualify observers."
            )
        from .lifecycle_prompts import install_seat_commands

        result["seat_commands"] = install_seat_commands(workspace, "codex",
            codex_home=self.hooks_path.parent, runtime=self.source_root)
        return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--hooks", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--hook-timeout-seconds", type=int, required=True)
    parser.add_argument("--wait-deadline-seconds", type=int, required=True)
    parser.add_argument("--session")
    parser.add_argument("--acknowledge-stop-hook-change")
    args = parser.parse_args(argv)
    try:
        receipt = CodexHookInstaller(
            source_root=Path(args.source),
            bus_home=Path(args.root),
            hooks_path=Path(args.hooks),
            destination=Path(args.destination),
        ).install(
            Path(args.workspace),
            args.node,
            hook_timeout_seconds=args.hook_timeout_seconds,
            wait_deadline_seconds=args.wait_deadline_seconds,
            session_id=args.session,
            acknowledge_stop_hook_change=args.acknowledge_stop_hook_change,
        )
    except ProtocolRefusal as exc:
        evidence: Dict[str, object] = {
            "code": exc.code,
            "detail": exc.detail,
            "remedy": exc.remedy,
        }
        detail = getattr(exc, "refusal_detail", None)
        if (
            exc.code == "stop_hook_definition_change_unacknowledged"
            and isinstance(detail, dict)
        ):
            evidence.update(detail)
            # Only the human running this module consents. The remedy is this
            # exact invocation plus the pair, so a stale pair cannot be reused.
            prior = list(sys.argv[1:] if argv is None else argv)
            kept: list[str] = []
            skip = False
            for part in prior:
                if skip:
                    skip = False
                    continue
                if part == "--acknowledge-stop-hook-change":
                    skip = True
                    continue
                if part.startswith("--acknowledge-stop-hook-change="):
                    continue
                kept.append(part)
            rerun = " ".join(
                [shlex.quote(sys.executable), "-m", "floati.codex_hook_install"]
                + [shlex.quote(part) for part in kept]
                + [
                    "--acknowledge-stop-hook-change",
                    f"{detail['preimage']}:{detail['target']}",
                ]
            )
            evidence["remedy"] = (
                f"the operator reads this, then runs: {rerun} ; "
                f"{STOP_HOOK_CHANGE_CONSEQUENCE}"
            )
        print(
            json.dumps(
                {
                    "artifact_version": 0,
                    "command": "floati.codex_hook_install",
                    "status": "refused",
                    "evidence": evidence,
                },
                sort_keys=True,
            )
        )
        return 20
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
