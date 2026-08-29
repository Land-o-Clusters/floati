"""Additive installer for the Floati Codex Stop waiter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional, Sequence

from .codex_wait_contract import (
    WORKSPACE_MAP_RELATIVE,
    CodexWaitConsentLedger,
    CodexWaitSessionLedger,
    resolve_participant,
)
from .codex_hook_trust import observe_codex_waiter_hooks
from .errors import ProtocolRefusal


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


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


def _write_atomic(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError("short atomic write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _runtime_files(source_root: Path) -> list[Path]:
    paths = [source_root / "LICENSE", source_root / "scripts/floati-codex-wait"]
    paths.extend(sorted((source_root / "floati").glob("**/*.py")))
    paths.extend(sorted((source_root / "schemas").glob("v[0-9]*/*.json")))
    return [path for path in paths if path.is_file() and "__pycache__" not in path.parts]


def _tree_digest(source_root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


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
        paths = _runtime_files(self.source_root)
        required = {
            "LICENSE",
            "scripts/floati-codex-wait",
            "floati/codex_wait.py",
            "floati/codex_wait_contract.py",
        }
        present = {path.relative_to(self.source_root).as_posix() for path in paths}
        if not required <= present:
            raise ProtocolRefusal("codex_wait_source_incomplete", "waiter runtime source is incomplete")
        bundle_digest = _tree_digest(self.source_root, paths)
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
                if _tree_digest(staging, _runtime_files(staging)) != bundle_digest:
                    raise ProtocolRefusal("codex_wait_install_digest_mismatch", "installed bytes differ")
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        installed_digest = _tree_digest(target, _runtime_files(target))
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
        paths = _runtime_files(self.source_root)
        required = {
            "LICENSE", "scripts/floati-codex-wait", "floati/codex_wait.py",
            "floati/codex_wait_contract.py",
        }
        present = {path.relative_to(self.source_root).as_posix() for path in paths}
        if not required <= present:
            raise ProtocolRefusal("codex_wait_source_incomplete", "waiter runtime source is incomplete")
        planned_bundle_digest = _tree_digest(self.source_root, paths)
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
