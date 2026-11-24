"""Install one zcode project Stop hook through explicit governed coordinates.

The command names root, node, and runtime. Nothing is read from the account
home. A second call replaces only an owned zcode waiter command and leaves
every foreign Stop command in place.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Mapping, NoReturn, TextIO

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .root import FloatiRoot, validate_identifier
from .waiter_consent import arm_waiter_consent


# Measured Stop-hook hold. The 24 h rung is pending and raises both if it completes.
# docs/evidence/f2-hook-ceiling-1-2026-10-24.md
ZCODE_HOLD_SECONDS = (3700, 3600)


def format_zcode_stop_command(
    *, runtime: Path, root: Path, node: str, hook_timeout_seconds: int,
    deadline_seconds: int,
) -> str:
    launcher = Path(runtime) / "scripts" / "floati"
    return " ".join(
        shlex.quote(part)
        for part in (
            str(launcher),
            "wake",
            "wait",
            "--harness",
            "zcode",
            "--root",
            str(root),
            "--as",
            node,
            "--runtime",
            str(runtime),
            "--deadline-seconds",
            str(int(deadline_seconds)),
            "--hook-timeout-seconds",
            str(int(hook_timeout_seconds)),
        )
    )


def _owned_zcode_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return argv[1:5] == ["wake", "wait", "--harness", "zcode"]


def _state_dir(root: Path, node: str) -> Path:
    return root / "state" / "zcode-wait" / node


def _capture_preinstall(root: Path, node: str, hooks_path: Path) -> None:
    """Record the hook file once, before the first write for this node."""

    if not _readable_fleet_root(root):
        return
    directory = _state_dir(root, node)
    meta_path = directory / "pre-install.json"
    if meta_path.is_file() and not meta_path.is_symlink():
        return
    directory.mkdir(parents=True, exist_ok=True)
    existed = hooks_path.is_file() and not hooks_path.is_symlink()
    if existed:
        (directory / "pre-install-hooks.bin").write_bytes(hooks_path.read_bytes())
    meta_path.write_text(
        json.dumps(
            {"existed": existed, "hooks_path": str(hooks_path)},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _readable_fleet_root(root: Path) -> bool:
    try:
        FloatiRoot.open_direct_home(root)
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError):
        return False
    return True


def _refuse_hooks(detail: str) -> NoReturn:
    raise ProtocolRefusal(
        "hook_install_hooks_invalid",
        detail,
        remedy="fix or remove the project .zcode/hooks.json and re-run install",
    )


def install_zcode_hook(
    *,
    workspace: Path,
    root: Path,
    node: str,
    runtime: Path,
    plan=None,
) -> dict:
    """Write ``workspace/.zcode/hooks.json`` and one root install receipt."""

    workspace = Path(workspace)
    root = Path(root)
    runtime = Path(runtime)
    if not workspace.is_absolute() or workspace.is_symlink() or (plan is None and not workspace.is_dir()):
        raise ProtocolRefusal(
            "hook_install_workspace_invalid",
            "workspace must be an absolute nonsymlink directory",
            remedy="pass --workspace as an absolute project directory",
        )
    if not root.is_absolute():
        raise ProtocolRefusal(
            "hook_install_root_invalid",
            "root must be an absolute path",
            remedy="pass --root as an absolute fleet directory",
        )
    if not runtime.is_absolute():
        raise ProtocolRefusal(
            "hook_install_runtime_invalid",
            "runtime must be an absolute path",
            remedy="pass --runtime as the absolute floati checkout or install",
        )
    validate_identifier(node, field="node")
    launcher = runtime / "scripts" / "floati"
    if launcher.is_symlink() or not launcher.is_file():
        raise ProtocolRefusal(
            "hook_install_runtime_invalid",
            "runtime has no scripts/floati launcher",
            remedy="pass --runtime pointing at a floati checkout or install",
        )
    hook_timeout_seconds, deadline_seconds = ZCODE_HOLD_SECONDS
    command = format_zcode_stop_command(
        runtime=runtime, root=root, node=node,
        hook_timeout_seconds=hook_timeout_seconds,
        deadline_seconds=deadline_seconds,
    )
    hook = {"type": "command", "command": command, "timeout": int(hook_timeout_seconds)}
    hooks_dir = workspace / ".zcode"
    hooks_path = hooks_dir / "hooks.json"
    if hooks_dir.is_symlink() or hooks_path.is_symlink():
        _refuse_hooks("project .zcode hook path is a symlink")
    payload: dict = {"hooks": {"Stop": []}}
    if hooks_path.is_file():
        try:
            existing = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            _refuse_hooks("existing .zcode/hooks.json is not JSON")
        if not isinstance(existing, dict):
            _refuse_hooks("existing .zcode/hooks.json is not an object")
        payload = existing
    hooks = payload.get("hooks")
    if hooks is None:
        payload["hooks"] = {}
    elif not isinstance(hooks, dict):
        _refuse_hooks("existing hooks object is invalid")
    stops = payload["hooks"].get("Stop", [])
    if not isinstance(stops, list):
        _refuse_hooks("existing Stop hooks are not a list")
    kept = []
    for entry in stops:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            kept.append(entry)
            continue
        retained = [
            row for row in entry["hooks"]
            if not (isinstance(row, dict) and _owned_zcode_command(row.get("command")))
        ]
        if retained:
            kept.append({**entry, "hooks": retained})
    kept.append({"matcher": "*", "hooks": [hook]})
    payload["hooks"]["Stop"] = kept
    encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    directory = _state_dir(root, node)
    receipt = {"hooks_path": str(hooks_path), "node": node, "root": str(root),
               "workspace": str(workspace)}
    if plan is not None:
        plan.consent(workspace, node, "zcode", hook_timeout_seconds, deadline_seconds)
        plan.json(directory / "pre-install.json", {"existed": False, "hooks_path": str(hooks_path)})
        plan.json(directory / "installed-hook.json", receipt)
        plan.file(hooks_path, encoded)
        return plan.commands(workspace, "zcode")
    arm_waiter_consent(root, workspace, node, "zcode",
        hook_timeout_seconds, deadline_seconds)
    _capture_preinstall(root, node, hooks_path)
    hooks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = hooks_path.with_name(f".{hooks_path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(descriptor, encoded) != len(encoded):
            raise OSError("short hooks.json write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, hooks_path)
    if _readable_fleet_root(root):
        receipt_path = directory / "installed-hook.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(
                receipt,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    from .lifecycle_prompts import install_seat_commands

    install_seat_commands(workspace, "zcode")
    return {
        "schema_version": 0,
        "harness": "zcode",
        "workspace": str(workspace),
        "hooks_path": str(hooks_path),
        "command": command,
        "root": str(root),
        "node": node,
    }


def run_zcode_stop_wait(
    *, root: Path, node: str, payload: Mapping[str, object],
    stdout: TextIO, stderr: TextIO, poll_seconds: float,
) -> int:
    """Validate this hook's actual seat, then reuse the native Stop waiter."""
    from .codex_wait import run_stop_waiter
    from .codex_wait_contract import resolve_participant
    from .registry import Registry

    participant = resolve_participant(root, Path.cwd())
    if participant is None:
        raise ProtocolRefusal("zcode_wait_workspace_unbound",
            "the actual working directory has no active waiter mapping",
            remedy="run the zcode Stop hook from its declared seat workspace")
    if participant.binding.node_id != node:
        raise ProtocolRefusal("zcode_wait_workspace_mismatch",
            "the actual workspace mapping names a different node",
            remedy="restore the declared zcode workspace and node mapping")
    if Registry(participant.root).require_active(node)["role"].casefold() != "zcode":
        raise ProtocolRefusal("zcode_wait_harness_mismatch",
            "the declared Stop-hook node is not registered as zcode",
            remedy="run this hook only for its registered zcode seat")
    # The harness session ID is native input; cwd authority is the process's
    # actual mapping. A payload path cannot borrow a different seat's consent.
    native_payload = dict(payload, cwd=str(Path.cwd()))
    return run_stop_waiter(bus_home=root, hook_payload=native_payload,
        stdout=stdout, stderr=stderr, poll_interval_seconds=poll_seconds)


def _require_install_paths(*, workspace: Path, root: Path, node: str, runtime: Path) -> Path:
    workspace = Path(workspace)
    root = Path(root)
    runtime = Path(runtime)
    if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
        raise ProtocolRefusal(
            "hook_install_workspace_invalid",
            "workspace must be an absolute nonsymlink directory",
            remedy="pass --workspace as an absolute project directory",
        )
    if not root.is_absolute():
        raise ProtocolRefusal(
            "hook_install_root_invalid",
            "root must be an absolute path",
            remedy="pass --root as an absolute fleet directory",
        )
    if not runtime.is_absolute():
        raise ProtocolRefusal(
            "hook_install_runtime_invalid",
            "runtime must be an absolute path",
            remedy="pass --runtime as the absolute floati checkout or install",
        )
    validate_identifier(node, field="node")
    launcher = runtime / "scripts" / "floati"
    if launcher.is_symlink() or not launcher.is_file():
        raise ProtocolRefusal(
            "hook_install_runtime_invalid",
            "runtime has no scripts/floati launcher",
            remedy="pass --runtime pointing at a floati checkout or install",
        )
    return workspace / ".zcode" / "hooks.json"


def uninstall_zcode_hook(
    *,
    workspace: Path,
    root: Path,
    node: str,
    runtime: Path,
) -> dict:
    """Put the project hook file back to the bytes from before install."""

    hooks_path = _require_install_paths(
        workspace=workspace, root=root, node=node, runtime=runtime,
    )
    if hooks_path.is_symlink() or hooks_path.parent.is_symlink():
        _refuse_hooks("project .zcode hook path is a symlink")
    directory = _state_dir(Path(root), node)
    meta_path = directory / "pre-install.json"
    if not meta_path.is_file() or meta_path.is_symlink():
        raise ProtocolRefusal(
            "zcode_hook_uninstall_absent",
            "no zcode hook install snapshot is recorded for this node",
            remedy="pass the same root, node, and workspace used to install",
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _refuse_hooks("zcode hook install snapshot is unreadable")
    if not isinstance(meta, dict) or meta.get("hooks_path") != str(hooks_path):
        raise ProtocolRefusal(
            "zcode_hook_uninstall_mismatch",
            "the recorded snapshot names a different project hook file",
            remedy="uninstall with the workspace that was installed",
        )
    existed = meta.get("existed")
    if existed is True:
        blob_path = directory / "pre-install-hooks.bin"
        if not blob_path.is_file() or blob_path.is_symlink():
            _refuse_hooks("zcode hook install snapshot bytes are missing")
        hooks_path.write_bytes(blob_path.read_bytes())
        blob_path.unlink()
    elif existed is False:
        if hooks_path.is_file():
            hooks_path.unlink()
    else:
        _refuse_hooks("zcode hook install snapshot does not say whether the file existed")
    receipt_path = directory / "installed-hook.json"
    if receipt_path.is_file() and not receipt_path.is_symlink():
        receipt_path.unlink()
    meta_path.unlink()
    return {
        "schema_version": 0,
        "harness": "zcode",
        "workspace": str(workspace),
        "hooks_path": str(hooks_path),
        "root": str(root),
        "node": node,
        "restored": True,
    }
