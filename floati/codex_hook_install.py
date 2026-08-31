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
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from .codex_wait_contract import (
    WORKSPACE_MAP_RELATIVE,
    CodexWaitConsentLedger,
    CodexWaitSessionLedger,
    resolve_participant,
)
from .codex_hook_trust import (
    codex_hook_current_hash,
    observe_codex_waiter_hooks,
    observe_rebound_waiter,
)
from .errors import ProtocolRefusal
from .waiter_bundle import waiter_runtime_digest, waiter_runtime_files


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


def plan_waiter_rebind(hooks_path: Path, store: Path, target_digest: str) -> Dict[str, object]:
    """Plan one surgical Floati Stop-hook launcher replacement without writing."""

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
        if "floati-codex-wait" in json.dumps(block, sort_keys=True)
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
    if len(argv) < 4 or argv[0] != "/usr/bin/python3" or argv[2] != "--root":
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook command is invalid")
    current_launcher = Path(argv[1])
    try:
        relative = current_launcher.relative_to(store)
    except ValueError as exc:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter launcher is outside its declared store") from exc
    if (
        len(relative.parts) != 3
        or re.fullmatch(r"[0-9a-f]{64}", relative.parts[0]) is None
        or relative.parts[1:] != ("scripts", "floati-codex-wait")
    ):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter launcher is not content addressed")
    target_launcher = store / target_digest / "scripts" / "floati-codex-wait"
    target_block = json.loads(json.dumps(block))
    target_argv = [argv[0], str(target_launcher), *argv[2:]]
    target_block["hooks"][0]["command"] = shlex.join(target_argv)
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
    }


def commit_waiter_rebind(
    staged: Dict[str, object],
    *,
    _fault_hook: Optional[Callable[[str], None]] = None,
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
        readback = _write_atomic(path, after, fault_hook=_fault_hook)
    else:
        # Exact post bytes do not prove that a predecessor completed the
        # parent-directory barrier.  Replay that barrier and readback without
        # another replacement before evidence may be appended.
        readback = _durable_waiter_readback(path, after, fault_hook=_fault_hook)
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
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        installed_digest = waiter_runtime_digest(target)
        if installed_digest != bundle_digest:
            raise ProtocolRefusal("codex_wait_install_digest_mismatch", "existing installed bytes differ")
        return bundle_digest, target, installed_digest

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
        if len(argv) != 4 or argv[0] != "/usr/bin/python3" or argv[2] != "--root":
            return False
        if argv[3] != str(self.bus_home):
            return False
        launcher = Path(argv[1])
        try:
            relative = launcher.relative_to(self.destination)
        except ValueError:
            return False
        return (
            len(relative.parts) == 3
            and re.fullmatch(r"[0-9a-f]{64}", relative.parts[0]) is not None
            and relative.parts[1:] == ("scripts", "floati-codex-wait")
        )

    def _plan_hook_rewrite(
        self, before: bytes, block: Dict[str, object]
    ) -> tuple[str, bytes]:
        try:
            text = before.decode("utf-8")
            document = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolRefusal("codex_hooks_invalid", "hooks file is not ordinary JSON") from exc
        if not isinstance(document, dict) or not isinstance(document.get("hooks"), dict):
            raise ProtocolRefusal("codex_hooks_invalid", "hooks document shape is invalid")
        stop = document["hooks"].get("Stop")
        if not isinstance(stop, list):
            raise ProtocolRefusal("codex_hooks_invalid", "Stop hooks must be a list")
        floati_indexes = [
            index
            for index, entry in enumerate(stop)
            if "floati-codex-wait" in json.dumps(entry, sort_keys=True)
        ]
        if len(floati_indexes) > 1 or any(
            not self._valid_waiter_block(stop[index]) for index in floati_indexes
        ):
            raise ProtocolRefusal(
                "codex_wait_hook_conflict",
                "Floati waiter blocks are malformed or ambiguous",
            )
        if not floati_indexes:
            state = "installed"
            stop.append(block)
        elif stop[floati_indexes[0]] == block:
            return "already_installed", before
        else:
            state = "replaced"
            stop[floati_indexes[0]] = block
        try:
            root_start = next(index for index, character in enumerate(text) if not character.isspace())
            hooks_start, _hooks_end = _object_value_span(text, root_start, "hooks")
            stop_start, stop_end = _object_value_span(text, hooks_start, "Stop")
        except (KeyError, StopIteration, ValueError, json.JSONDecodeError) as exc:
            raise ProtocolRefusal("codex_hooks_invalid", "hooks document spans are invalid") from exc
        replacement = json.dumps(stop, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        after = (text[:stop_start] + replacement + text[stop_end:]).encode("utf-8")
        return state, after

    def _write_workspace_map(self, workspace: Path, node_id: str) -> str:
        map_path = self.bus_home / WORKSPACE_MAP_RELATIVE
        if map_path.exists():
            try:
                raw = json.loads(map_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProtocolRefusal("codex_wait_workspace_map_invalid", "workspace map is unreadable") from exc
            if not isinstance(raw, dict) or set(raw) != {"schema_version", "tenant_id", "mappings"}:
                raise ProtocolRefusal("codex_wait_workspace_map_invalid", "workspace map shape is invalid")
            mappings = raw.get("mappings")
            if raw.get("schema_version") != 0 or raw.get("tenant_id") != self.bus_home.name or not isinstance(mappings, list):
                raise ProtocolRefusal("codex_wait_workspace_map_invalid", "workspace map identity is invalid")
        else:
            mappings = []
        canonical = workspace.resolve(strict=True).as_posix()
        retained = []
        found = False
        for entry in mappings:
            if not isinstance(entry, dict) or set(entry) != {"workspace", "node_id"}:
                raise ProtocolRefusal("codex_wait_workspace_map_invalid", "workspace mapping is invalid")
            if entry["workspace"] == canonical:
                if entry["node_id"] != node_id:
                    raise ProtocolRefusal("codex_wait_workspace_conflict", "workspace names another node")
                found = True
            retained.append(entry)
        if not found:
            retained.append({"workspace": canonical, "node_id": node_id})
        retained.sort(key=lambda row: (row["workspace"], row["node_id"]))
        encoded = json.dumps(
            {"schema_version": 0, "tenant_id": self.bus_home.name, "mappings": retained},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if not map_path.exists() or map_path.read_bytes() != encoded:
            _write_atomic(map_path, encoded)
        return _digest_bytes(encoded)

    def install(
        self,
        workspace: Path,
        node_id: str,
        *,
        hook_timeout_seconds: int,
        wait_deadline_seconds: int,
        session_id: Optional[str] = None,
    ) -> Dict[str, object]:
        workspace = Path(workspace).expanduser()
        if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
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
        launcher = planned_target / "scripts/floati-codex-wait"
        command = " ".join(
            (shlex.quote("/usr/bin/python3"), shlex.quote(str(launcher)), "--root", shlex.quote(str(self.bus_home)))
        )
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
        state, after = self._plan_hook_rewrite(before, block)

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
            **trust,
        }
        if session is not None:
            result["acting_session_id"] = session["acting_session_id"]
            result["session_receipt_id"] = session["id"]
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
    args = parser.parse_args(argv)
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
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
