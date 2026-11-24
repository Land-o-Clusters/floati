"""Explicit operator-owned clone seats, with private reversible ownership records."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .codex_hook_install import _rename_generation_noreplace
from .bus_epoch import epoch_guard
from .codex_wait_contract import WORKSPACE_MAP_RELATIVE, resolve_participant, CodexWaitConsentLedger
from .events import EventLog
from .cursor import SparseCursor
from .errors import ProtocolRefusal
from .git_process import fixed_git_command, fixed_git_environment
from .jsonl import _locked_path, transact
from .registry import Registry, REGISTRY_KINDS, utc_now
from .ids import uuid7_hex
from .root import validate_identifier
from .seat_declaration import FleetGovernance, SeatDeclaration


def _refuse(detail):
    raise ProtocolRefusal("seat_onboard_invalid", detail,
        remedy="inspect the named seat ownership record and coordinate; preserve foreign data, then retry with corrected explicit paths")


def _path(value):
    if not isinstance(value, (str, Path)) or any(ord(char) < 32 or ord(char) == 127 for char in str(value)):
        _refuse("seat paths must be printable coordinates without control characters")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path != path.resolve():
        _refuse("seat paths must be normalized absolute coordinates without symlinks: " + str(path))
    for parent in (path, *path.parents):
        if parent.is_symlink():
            _refuse("seat coordinate has a symlink: " + str(path))
    return path


def _codex_path(value):
    """Keep only the declared Codex launcher stable across target upgrades."""
    from .wake_daemon_adapters import CODEX_EXECUTABLE

    if not isinstance(value, (str, Path)) or any(ord(char) < 32 or ord(char) == 127 for char in str(value)):
        _refuse("seat paths must be printable coordinates without control characters")
    path = Path(value)
    if path != CODEX_EXECUTABLE:
        return _path(value)
    if not path.is_absolute() or ".." in path.parts:
        _refuse("declared Codex executable must be a normalized absolute coordinate")
    # This is a declaration, including in a historical ownership record. The
    # current target is checked before launch; it is never saved as identity.
    return path


def _checked_codex_path(value):
    path = _codex_path(value)
    try:
        target = _path(path.resolve(strict=True))
    except (OSError, RuntimeError):
        _refuse("Codex executable must resolve to an ordinary executable")
    if not target.is_file() or not os.access(target, os.X_OK):
        _refuse("Codex executable must be an ordinary executable")
    return path


def _bytes(path):
    path = _path(path)
    identity = path.lstat()
    if not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.getuid() or identity.st_size > 32 * 1024 * 1024:
        _refuse("ownership input must be a bounded ordinary account-owned file: " + str(path))
    return path.read_bytes()


def _atomic(path, content):
    if len(content) > 32 * 1024 * 1024:
        _refuse("seat ownership record exceeds the 32 MiB bound")
    path = _path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=".seat-write-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if _bytes(path) != content:
            _refuse("seat write readback differs: " + str(path))
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def _save(path, record):
    _atomic(path, (json.dumps(record, sort_keys=True, indent=2) + "\n").encode())


def _git(repository, *arguments):
    result = subprocess.run(fixed_git_command("/usr/bin/git", repository, arguments),
        env=fixed_git_environment("/usr/bin/git"), capture_output=True, text=True, timeout=120)
    if result.returncode:
        _refuse("seat Git operation failed: " + result.stderr.strip())
    return result.stdout.strip()


def _inventory(home):
    inventory = {}
    for path in sorted(home.rglob("*")):
        relative = str(path.relative_to(home))
        metadata = path.lstat()
        if path.is_symlink():
            inventory[relative] = {"type": "symlink", "target": os.readlink(path)}
        elif path.is_dir():
            inventory[relative] = {"type": "directory", "mode": stat.S_IMODE(metadata.st_mode)}
        elif path.is_file():
            inventory[relative] = {"type": "file", "mode": stat.S_IMODE(metadata.st_mode),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            _refuse("owned home contains a special file: " + relative)
    return inventory


def _directory_identity(path):
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        _refuse("owned staging is not an ordinary account-owned directory")
    return [metadata.st_dev, metadata.st_ino]


def _mark(path, record, step):
    if step not in record["steps"]:
        record["steps"].append(step)
        _save(path, record)


_CHILD_OUTPUT_PATHS = {
    "clone": ["repo"],
    "stage": ["SEAT.md", "scratch", "wt"],
    "declaration": ["repo/SEAT.json"],
    "warm": ["repo/.build", "scratch/warm-build.log"],
    "cli": ["scratch/cli-start.jsonl", "scratch/cli-start.stderr"],
}


def _child_owns_path(kind, name):
    return name in _CHILD_OUTPUT_PATHS[kind] or (
        kind == "warm" and name.startswith("repo/.build/")) or (
        kind == "clone" and name.startswith("repo/"))


def _merge_child_inventory(before, outputs, kind):
    return {**{name: entry for name, entry in before.items() if not _child_owns_path(kind, name)},
        **outputs}


def _capture_child_outputs(path, record, home, intent):
    """Finalize the exact output cut before a child failure leaves this command."""
    actual = _inventory(home)
    outputs = {name: entry for name, entry in actual.items() if _child_owns_path(intent["kind"], name)}
    for name, entry in outputs.items():
        if name.startswith("scratch/") and entry["type"] != "file":
            _refuse("child log output is not an ordinary file: " + name)
        if name == "repo/.build" and entry["type"] != "directory":
            _refuse("warm build output root is not an ordinary directory")
        if intent["kind"] == "clone" and name == "repo" and entry["type"] != "directory":
            _refuse("cloned repository output root is not an ordinary directory")
        if intent["kind"] == "stage" and entry["type"] != ("file" if name == "SEAT.md" else "directory"):
            _refuse("fixed staging output has an unexpected type: " + name)
        if intent["kind"] == "declaration" and entry["type"] != "file":
            _refuse("seat declaration output is not an ordinary file")
    expected = _merge_child_inventory(intent["before_inventory"], outputs, intent["kind"])
    intent.update(state="finalized", output_inventory=outputs)
    record["inventory"] = expected
    _save(path, record)
    if actual != expected:
        _refuse("child changed files outside its declared output contract; preserve the changed workspace")


@contextmanager
def _child_output_boundary(path, record, home, kind):
    """Reserve a bounded output capability; never adopt a later recovery scan."""
    before = record["inventory"]
    if _directory_identity(home) != record["stage_identity"] or _inventory(home) != before:
        _refuse("owned home changed before child launch; preserve it")
    for relative in _CHILD_OUTPUT_PATHS[kind]:
        output = _path(home / relative)
        if (relative.startswith("scratch/") or kind in {"clone", "stage"}) and output.exists():
            _refuse("child log output already exists; preserve it: " + relative)
        if relative == "repo/.build" and output.exists() and not output.is_dir():
            _refuse("warm build output root must be an ordinary directory")
    intent = {"kind": kind, "state": "pending", "before_inventory": before,
        "output_inventory": None}
    record.setdefault("child_outputs", []).append(intent)
    _save(path, record)
    try:
        yield
    finally:
        # A native fixed writer has returned, or subprocess.run has collected
        # or terminated its direct child. Repository scripts own their children.
        # This finally runs outside the log contexts, after their handles close.
        # Provider output is not quiescent until every owned process is collected.
        # A lost cleanup keeps the durable intent pending; removal cannot adopt it.
        if intent["kind"] != "cli" or all(provider["exit_code"] is not None and provider["group_gone"]
                for provider in record.get("cli_start", {}).get("providers", [])):
            _capture_child_outputs(path, record, home, intent)


def _lifecycle_id(record):
    if record.get("lifecycle_id") is not None:
        return record["lifecycle_id"]
    # Older journals already recorded a unique staging coordinate. Derive a
    # stable replay key without rewriting their testimony or reusing a home key.
    return hashlib.sha256((str(record.get("stage")) + "\0" + record["registry_row"]["id"]
        + "\0" + record["timestamp"]).encode()).hexdigest()[:32]


def _register_owned(root, record):
    row = record["registry_row"]
    def decide(prior):
        previous = next((item for item in reversed(prior) if item.get("kind") == "registry_entry"
            and item.get("node_id") == row["node_id"]), None)
        if previous is not None and not (record.get("rehome") and previous == record.get("previous_registry_entry") and previous["state"] == "retired"):
            _refuse("suggested node was registered concurrently; preserve that foreign registration")
        return row, row
    return transact(root, Path("registry/entries.jsonl"), decide, allowed_kinds=REGISTRY_KINDS)


def _verify_ready(root, record):
    from . import seat_harness_config
    plan = record["config_plan"]
    profiles = seat_harness_config._gateway(_bytes(Path(plan["gateway"]["path"])))
    if profiles.get("profiles", {}).get(record["profile"]) != plan["profile"]:
        _refuse("prepared gateway profile is absent or changed")
    baseline = seat_harness_config._gateway(base64.b64decode(plan["gateway"]["baseline_base64"]))
    transport = plan["profile"]["transport"]
    if profiles["transports"][transport] != baseline["transports"][transport]:
        _refuse("prepared gateway transport changed after onboarding")
    from .seat_transport_preflight import preflight_transport
    preflight_transport(profiles["transports"][transport])
    codex = seat_harness_config._toml(_bytes(Path(plan["codex"]["path"])))
    workspace = Path(record["home"]) / "repo"
    if codex.get("projects", {}).get(str(workspace)) != {"trust_level": "trusted"}:
        _refuse("prepared project trust entry is absent or changed")
    participant = resolve_participant(root.path, workspace)
    if participant is None or participant.binding.node_id != record["node_id"]:
        _refuse("prepared workspace mapping or active registration is absent or changed")
    CodexWaitConsentLedger(root).require_armed(participant.binding)
    if Registry(root)._latest(record["node_id"]) != record["registry_row"]:
        _refuse("prepared registration changed after onboarding")
    declaration = SeatDeclaration.load(workspace)
    if declaration is None or declaration.node_id != record["node_id"] or declaration.root != str(root.path):
        _refuse("prepared seat declaration is absent or changed")
    if record.get("cli_start") is not None:
        marker = _owned_cli_pause(root, record)
        return "paused" if marker is not None else "not_paused"


def _record_path(root, home):
    token = hashlib.sha256(str(home).encode()).hexdigest()
    return root.resolve_relative(Path("state/seat-onboarding") / (token + ".json"))


@contextmanager
def _operation(root):
    relative = Path("state/seat-onboarding/operation.lock")
    path = root.resolve_relative(relative)
    with epoch_guard(root, exclusive=False):
        # This serializes onboarding operations only. It is not a ledger lock;
        # native operations still acquire epoch -> wake -> ledger in that order.
        with _locked_path(path, exclusive=True, relative=relative, order_tracked=False):
            yield


def _load(path, root, home):
    from . import seat_harness_config, seat_waiter_lifecycle
    from .records import validate_record, _timestamp_value
    import re

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _refuse("seat ownership record repeats a field: " + key)
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-JSON numeric constant in ownership record")

    def coordinate(value):
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            _refuse("saved seat coordinate must be a terminal-safe path string")
        result = _path(value)
        if str(result) != value:
            _refuse("saved seat coordinate is not normalized")
        return result

    def inventory(value):
        if not isinstance(value, dict):
            _refuse("saved seat inventory must be an object")
        for name, entry in value.items():
            relative = Path(name)
            if not name or relative.is_absolute() or str(relative) != name or "." in relative.parts or ".." in relative.parts:
                _refuse("saved seat inventory contains an escaping or nonnormalized path")
            if not isinstance(entry, dict):
                _refuse("saved seat inventory entry must be an object")
            kind = entry.get("type")
            fields = {"type", "target"} if kind == "symlink" else {"type", "mode"}
            if kind == "file":
                fields.add("sha256")
            if kind not in {"directory", "file", "symlink"} or set(entry) != fields:
                _refuse("saved seat inventory entry has an unsupported shape")
            if kind == "symlink":
                if not isinstance(entry["target"], str) or "\x00" in entry["target"]:
                    _refuse("saved seat symlink target is invalid")
            elif type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o7777:
                _refuse("saved seat inventory mode is invalid")
            if kind == "file" and (not isinstance(entry["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None):
                _refuse("saved seat inventory digest is invalid")

    try:
        record = json.loads(_bytes(path), object_pairs_hook=unique, parse_constant=reject_constant)
        required = {"schema_version", "root", "home", "node_id", "profile", "request", "state",
            "timestamp", "config_plan", "map_before", "steps", "rehome", "previous_registry_entry", "registry_row"}
        optional = {"stage", "stage_identity", "inventory", "rehome_map_before", "prior_life_messages",
            "historical_receipts", "result", "removal_steps", "waiter_removal", "removal_stage", "remove_result", "child_outputs", "lifecycle_id", "cli_start", "cli_removal"}
        if not isinstance(record, dict) or not required <= set(record) or set(record) - required - optional:
            _refuse("seat ownership record has missing or unknown fields")
        if type(record["schema_version"]) is not int or record["schema_version"] != 1 or record["root"] != str(root.path) or record["home"] != str(home):
            _refuse("seat ownership record does not match this root and home")
        if Path(path) != _record_path(root, home):
            _refuse("seat ownership journal path differs from its home coordinate")
        node = validate_identifier(record["node_id"], "node")
        profile = validate_identifier(record["profile"], "profile")
        _timestamp_value(record["timestamp"], "timestamp", lambda code, detail: _refuse(detail))
        if record["state"] not in {"preparing", "failed", "ready", "removing", "removed"} or type(record["rehome"]) is not bool:
            _refuse("saved seat phase or rehome flag is invalid")
        request = record["request"]
        request_fields = {"source", "source_sha", "gateway_config", "codex_config", "codex_hooks", "transport", "coordinator", "repo",
            "mode", "requested_node", "requested_profile", "codex_executable", "warm_build_script", "rehome"}
        if not isinstance(request, dict) or set(request) != request_fields or request["rehome"] is not record["rehome"]:
            _refuse("saved seat request is malformed or disagrees with rehome")
        source = coordinate(request["source"])
        coordinate(request["codex_hooks"])
        if home == source or home in source.parents or source in home.parents or home == root.path or home in root.path.parents or root.path in home.parents:
            _refuse("saved seat home overlaps its source or bus root")
        if not isinstance(request["source_sha"], str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", request["source_sha"]) is None:
            _refuse("saved seat source commit is invalid")
        for field in ("transport", "coordinator", "repo"):
            validate_identifier(request[field], field)
        for field, expected in (("requested_node", node), ("requested_profile", profile)):
            if request[field] is not None and request[field] != expected:
                _refuse("saved seat requested identity disagrees with its ownership identity")
        if request["mode"] not in {"desktop", "cli"}:
            _refuse("saved seat mode is invalid")
        if request["codex_executable"] is not None:
            executable = _codex_path(request["codex_executable"])
            if str(executable) != request["codex_executable"]:
                _refuse("saved Codex executable coordinate is not normalized")
        if (request["mode"] == "cli") != (request["codex_executable"] is not None):
            _refuse("saved CLI mode and executable disagree")
        if request["warm_build_script"] is not None:
            script = request["warm_build_script"]
            if not isinstance(script, str) or not script or Path(script).is_absolute() or str(Path(script)) != script or ".." in Path(script).parts:
                _refuse("saved warm build script must be repository-relative")
        if record["rehome"] and (request["requested_node"] is None or request["requested_profile"] is None):
            _refuse("saved rehome must name its exact node and profile")
        plan = record["config_plan"]
        seat_harness_config._plan(plan)
        workspace = home / "repo"
        expected_profile = {"transport": request["transport"], "git_common_dir": str(workspace / ".git"),
            "workspace_root": str(workspace), "bus_root": str(root.path), "identity": node,
            "allowed_operations": ["ack", "attach", "board", "inbox", "send"],
            "send_repository": request["repo"], "send_recipients": [request["coordinator"]]}
        if plan["profile_name"] != profile or plan["profile"] != expected_profile or plan.get("replace_profile", False) is not record["rehome"]:
            _refuse("saved configuration plan names different seat ownership coordinates")
        for name, field in (("gateway", "gateway_config"), ("codex", "codex_config")):
            if coordinate(plan[name]["path"]) != coordinate(request[field]):
                _refuse("saved configuration plan names a different request path")
        for field in ("map_before", "rehome_map_before"):
            if field in record and record[field] is not None:
                if not isinstance(record[field], str) or len(record[field]) > 48 * 1024 * 1024:
                    _refuse("saved workspace map backup is not bounded base64")
                seat_waiter_lifecycle._map(root, base64.b64decode(record[field], validate=True))
        row = validate_record(record["registry_row"], root.tenant_id, frozenset({"registry_entry"}), integrity=False)
        if row["node_id"] != node or row["state"] != "active" or row["role"].casefold() != "codex":
            _refuse("saved registry ownership belongs to another node or harness")
        previous = record["previous_registry_entry"]
        if previous is not None:
            previous = validate_record(previous, root.tenant_id, frozenset({"registry_entry"}), integrity=False)
            if not record["rehome"] or previous["node_id"] != node or previous["role"].casefold() != "codex":
                _refuse("saved previous registration does not match this rehome")
            if previous["state"] == "active" and row != previous:
                _refuse("saved active rehome registration differs from its prior incarnation")
        elif record["rehome"]:
            _refuse("saved rehome has no prior registration")
        allowed_steps = ["home", "registered"] + (["rehome"] if record["rehome"] else []) + ["waiter", "config"]
        steps = record["steps"]
        if not isinstance(steps, list) or steps != allowed_steps[:len(steps)]:
            _refuse("saved seat steps are not a valid ordered transaction prefix")
        removal = record.get("removal_steps", [])
        if not isinstance(removal, list) or any(not isinstance(step, str) for step in removal) or len(removal) != len(set(removal)) or not set(removal) <= {"cli", "config", "waiter", "registry"}:
            _refuse("saved removal steps are invalid")
        cleanup_order = (["cli"] if "cli_start" in record else [])
        cleanup_order += [step for step, intent in (("config", "config"), ("waiter", "waiter"), ("registry", "registered")) if intent in steps]
        if removal != cleanup_order[:len(removal)]:
            _refuse("saved removal steps are not a reachable ordered cleanup prefix")
        if any(({"registry": "registered", "cli": "config"}.get(step, step)) not in steps for step in removal):
            _refuse("saved removal completion has no corresponding add intent")
        if "cli" in removal and "cli_start" not in record:
            _refuse("saved CLI cleanup has no corresponding startup intent")
        if "stage" in record:
            stage = coordinate(record["stage"])
            if stage.parent != home.parent or not stage.name.startswith(".seat-add-"):
                _refuse("saved staging path does not belong to this home parent")
        if "lifecycle_id" in record:
            if not isinstance(record["lifecycle_id"], str) or re.fullmatch(r"[0-9a-f]{32}", record["lifecycle_id"]) is None or ("stage" in record and record["stage"] != str(home.parent / (".seat-add-" + record["lifecycle_id"]))):
                _refuse("saved lifecycle identifier disagrees with its staging coordinate")
        if "stage" not in record and (steps or record["state"] not in {"preparing", "removed"}):
            _refuse("saved seat with effects has no staging coordinate")
        identity = record.get("stage_identity")
        if identity is not None and (not isinstance(identity, list) or len(identity) != 2 or any(type(value) is not int or value < 0 for value in identity)):
            _refuse("saved staging identity is invalid")
        if identity is not None and "stage" not in record:
            _refuse("saved staging identity has no corresponding coordinate")
        if steps and identity is None:
            _refuse("saved seat effects have no staging ownership identity")
        if record.get("inventory") is not None:
            inventory(record["inventory"])
        children = record.get("child_outputs", [])
        child_kinds = ["clone", "stage", "declaration"] + (["warm"] if request["warm_build_script"] is not None else []) + (["cli"] if request["mode"] == "cli" else [])
        if not isinstance(children, list) or len(children) > len(child_kinds):
            _refuse("saved child output boundaries are invalid")
        previous_cut = None
        for index, child in enumerate(children):
            if not isinstance(child, dict) or set(child) != {"kind", "state", "before_inventory", "output_inventory"} or child["kind"] != child_kinds[index]:
                _refuse("saved child output boundary differs from the declared request")
            if child["state"] not in {"pending", "finalized"}:
                _refuse("saved child output phase is invalid")
            inventory(child["before_inventory"])
            if previous_cut is not None and child["before_inventory"] != previous_cut:
                _refuse("saved child boundaries do not preserve the prior inventory cut")
            if child["state"] == "pending":
                if index != len(children) - 1 or child["output_inventory"] is not None or record["state"] in {"ready", "removed"}:
                    _refuse("saved pending child cannot claim finalized output ownership")
                previous_cut = child["before_inventory"]
            else:
                inventory(child["output_inventory"])
                for name, entry in child["output_inventory"].items():
                    if not _child_owns_path(child["kind"], name) or (name.startswith("scratch/") and entry["type"] != "file") or (name == "repo/.build" and entry["type"] != "directory"):
                        _refuse("saved child inventory includes undeclared output ownership")
                    if (child["kind"] == "clone" and name == "repo" and entry["type"] != "directory") or (child["kind"] == "stage" and entry["type"] != ("file" if name == "SEAT.md" else "directory")) or (child["kind"] == "declaration" and entry["type"] != "file"):
                        _refuse("saved fixed writer output has an unexpected type")
                previous_cut = _merge_child_inventory(child["before_inventory"], child["output_inventory"], child["kind"])
            required_step = {"declaration": "waiter", "warm": "config", "cli": "config"}.get(child["kind"])
            if required_step is not None and required_step not in steps:
                _refuse("saved writer output has no corresponding transaction intent")
        if children and record.get("inventory") != previous_cut:
            _refuse("saved child output cut disagrees with its transaction inventory")
        if record["state"] == "ready" and len(children) != len(child_kinds):
            _refuse("ready seat is missing its declared child output boundaries")
        if record["state"] == "ready" and (steps != allowed_steps or record.get("inventory") is None or not isinstance(record.get("result"), dict)):
            _refuse("ready seat is missing completed ownership evidence")
        if "cli_start" in record:
            from .wake_control import validate_session_id
            start = record["cli_start"]
            if request["mode"] != "cli" or "config" not in steps or not children or children[-1]["kind"] != "cli" or not isinstance(start, dict) or set(start) != {"thread_id", "pause_key", "pause_intent", "pause_receipt_id", "providers", "resumable", "provider_home"}:
                _refuse("saved CLI startup has an invalid shape or no child intent")
            if Path(request["codex_config"]).name != "config.toml" or coordinate(start["provider_home"]) != Path(request["codex_config"]).parent:
                _refuse("saved CLI provider home differs from its explicit config.toml coordinate")
            if start["pause_key"] != "seat-start-pause-" + _lifecycle_id(record) or type(start["resumable"]) is not bool:
                _refuse("saved CLI pause intent differs from its lifecycle")
            if start["thread_id"] is not None:
                validate_session_id(start["thread_id"])
            if start["pause_intent"] is not None:
                pause = validate_record(start["pause_intent"], root.tenant_id, frozenset({"wake_control_receipt"}), integrity=False)
                if start["thread_id"] is None or pause["node_id"] != node or pause["session_digest"] != hashlib.sha256(start["thread_id"].encode()).hexdigest() or pause["operation"] != "pause" or pause["state"] != "paused" or pause["predecessor_receipt_id"] is not None or pause["idempotency_key"] != start["pause_key"]:
                    _refuse("saved CLI pause reservation belongs to another coordinate or operation")
            if start["pause_receipt_id"] is not None and (start["thread_id"] is None or not isinstance(start["pause_receipt_id"], str) or re.fullmatch(r"wake-control-[0-9a-f]{32}", start["pause_receipt_id"]) is None):
                _refuse("saved CLI pause receipt is invalid")
            if start["pause_receipt_id"] is not None and (start["pause_intent"] is None or start["pause_receipt_id"] != start["pause_intent"]["id"]):
                _refuse("saved CLI pause receipt differs from its reserved native identity")
            providers = start["providers"]
            if not isinstance(providers, list) or len(providers) > 2:
                _refuse("saved CLI provider observations are invalid")
            for index, provider in enumerate(providers):
                if not isinstance(provider, dict) or set(provider) != {"purpose", "pid", "exit_code", "group_gone"} or provider["purpose"] != ("create", "resume")[index] or type(provider["pid"]) is not int or provider["pid"] <= 0 or type(provider["group_gone"]) is not bool or (provider["exit_code"] is not None and type(provider["exit_code"]) is not int):
                    _refuse("saved CLI provider identity or exit is invalid")
                if (provider["exit_code"] is None or not provider["group_gone"]) and (index != len(providers) - 1 or children[-1]["state"] != "pending"):
                    _refuse("saved CLI output was finalized before its provider was collected")
            if len(providers) == 2 and (providers[0]["exit_code"] != 0 or not providers[0]["group_gone"] or start["thread_id"] is None or start["pause_receipt_id"] is None):
                _refuse("saved CLI resume preceded creation, collection, or pause")
            if start["resumable"] or record["state"] == "ready":
                if not start["resumable"] or len(providers) != 2 or any(provider["exit_code"] != 0 or not provider["group_gone"] for provider in providers) or start["pause_receipt_id"] is None:
                    _refuse("saved CLI resumability lacks two collected providers and native pause")
        if "cli_removal" in record:
            removal_capability = record["cli_removal"]
            if "cli_start" not in record or not isinstance(removal_capability, dict) or set(removal_capability) != {"before", "after"}:
                _refuse("saved CLI pause removal has an invalid shape")
            for field in ("before", "after"):
                capability = removal_capability[field]
                if capability is None:
                    continue
                session = record["cli_start"]["thread_id"]
                pause = record["cli_start"]["pause_intent"]
                marker = str(root.path / "state/wake-control" / node / (hashlib.sha256(session.encode()).hexdigest() + ".json")) if session is not None else None
                if not isinstance(capability, dict) or set(capability) != {"state", "session_id", "marker", "pause_receipt_id"} or capability["session_id"] != session or capability["marker"] != marker or capability["pause_receipt_id"] != (None if pause is None else pause["id"]) or capability["state"] not in {"paused", "removing", "absent"} or (session is None and capability["state"] != "absent") or (field == "after" and capability["state"] != "absent"):
                    _refuse("saved CLI capability inventory differs from its exact pause intent")
            if "cli" in removal and removal_capability["after"] is None:
                _refuse("saved CLI cleanup has no absent capability inventory")
        elif "cli" in removal:
            _refuse("saved CLI cleanup has no capability inventory")
        if "removal_stage" in record and coordinate(record["removal_stage"]) != home.with_name(".seat-remove-" + path.stem[:16]):
            _refuse("saved removal staging differs from its derived coordinate")
        if record["state"] == "removed" and not isinstance(record.get("remove_result"), dict):
            _refuse("removed seat has no removal result")
        for field in ("result", "remove_result"):
            if field in record:
                result = record[field]
                if not isinstance(result, dict) or result.get("node_id") != node or result.get("home") != str(home):
                    _refuse("saved seat result belongs to a different home or node")
                if field == "result" and (result.get("state") != "prepared" or result.get("workspace") != str(workspace)
                        or result.get("profile") != profile or result.get("ownership_record") != str(path)):
                    _refuse("saved preparation result disagrees with its journal coordinates")
                if field == "result" and record.get("cli_start") is not None:
                    thread = result.get("thread")
                    start = record["cli_start"]
                    saved_prompt = result.get("boot_prompt")
                    if saved_prompt not in (_legacy_boot(profile, workspace), _boot(profile, workspace)):
                        _refuse("saved CLI boot prompt is not a canonical coordinate-derived version")
                    expected_resume = [request["codex_executable"], "--cd", str(workspace), "resume", start["thread_id"]]
                    expected_thread = {"thread_id": start["thread_id"], "state": "paused", "resumable": True,
                        "startup_receipt": "current", "provider_state": "saved_without_turn",
                        "wake_control_state": "paused", "cached_session_state": "unknown", "turn_started": False,
                        "exit_code": 0, "receipt": str(home / "scratch/cli-start.jsonl"),
                        "pause_receipt_id": start["pause_receipt_id"], "providers": start["providers"],
                        "resume_argv": expected_resume, "resume_prompt": saved_prompt,
                        "provider_home": start["provider_home"], "resume_env": {"CODEX_HOME": start["provider_home"]},
                        "first_turn": "seat board claims and resumes through the normal wake path",
                        "output_paths": list(_CHILD_OUTPUT_PATHS["cli"])}
                    if not isinstance(thread, dict) or result.get("boot_prompt") != expected_thread["resume_prompt"] or json.dumps(thread, sort_keys=True, separators=(",", ":")) != json.dumps(expected_thread, sort_keys=True, separators=(",", ":")):
                        _refuse("saved CLI result differs from its paused startup evidence")
                    expected_action = ("CLI thread created and paused. Run "
                        + shlex.join(["env", "CODEX_HOME=" + start["provider_home"], *expected_resume])
                        + " and paste this: " + saved_prompt)
                    if result.get("operator_action") != expected_action:
                        _refuse("saved CLI operator action differs from its validated resume coordinates and prompt")
                if field == "remove_result":
                    if result.get("state") != "removed" or result.get("after_inventory") != {}:
                        _refuse("saved removal result has an invalid state or residual inventory")
                    inventory(result.get("before_inventory"))
                    if result.get("cli_pause") != record.get("cli_removal"):
                        _refuse("saved removal result differs from its native CLI capability inventory")
        for field in ("prior_life_messages", "historical_receipts"):
            if field in record and not isinstance(record[field], list):
                _refuse("saved historical evidence must be a list")
    except ProtocolRefusal:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        _refuse("seat ownership record is unreadable or malformed: " + str(exc))
    return record


def _name(root, home):
    used = {row.get("node_id") for row in Registry(root).known_records()}
    base = "seat-" + hashlib.sha256(str(home).encode()).hexdigest()[:8]
    name, number = base, 2
    while name in used:
        name, number = base + "-" + str(number), number + 1
    return name


def _legacy_boot(profile, workspace):
    """Frozen pre-F2 bytes remain testimony for saved CLI ownership journals."""
    return ("Read SEAT.md in the parent of this directory, then run "
        "codex-fleet-bus " + shlex.quote(profile) + " board as the sole terminal command "
        "from " + shlex.quote(str(workspace)) + ". Leave workspace files unchanged. Stop and report any refusal.")


def _boot(profile, workspace):
    return ("Read SEAT.md in the parent of this directory, then run "
        "codex-fleet-bus " + shlex.quote(profile) + " board --take-over as the sole terminal command "
        "from " + shlex.quote(str(workspace)) + ". Present the successful artifact's takeover_report to the operator. "
        "Leave workspace files unchanged. Stop and report any refusal.")


def _check_hook(root, hooks, config):
    from .codex_hook_install import CodexHookInstaller, parse_waiter_stop_argv
    from .codex_hook_trust import observe_codex_waiter_hooks
    if not hooks.exists():
        observations = []
    else:
        try:
            observations = observe_codex_waiter_hooks(hooks, config)
        except (ProtocolRefusal, OSError, ValueError, KeyError, TypeError) as exc:
            raise ProtocolRefusal("seat_onboard_hook_unavailable",
                "cannot validate the explicitly named Codex Stop hook: " + str(hooks),
                remedy="review the malformed hook or trust configuration at " + str(hooks) + " and " + str(config)) from exc
    if not observations:
        # A global definition must be preparable before the new seat exists.
        # Match native node boot's default; the remedy names this store explicitly.
        runtime = Path(__file__).resolve().parents[1]
        destination = Path.home() / ".local/share/floati/waiter"
        installer = CodexHookInstaller(source_root=runtime, bus_home=root.path,
            hooks_path=hooks, destination=destination)
        raise ProtocolRefusal("seat_onboard_hook_unavailable",
            "the fleet's global Stop hook is absent; the command prepares only its runtime and definition; "
            "after reviewing the exact Stop-definition change, host hook trust and relaunch remain operator actions",
            remedy=installer.preparation_command(hook_timeout_seconds=1800))
    try:
        document = json.loads(_bytes(hooks))
        matching = []
        for observation in observations:
            group = document["hooks"]["Stop"][observation["hook_group_index"]]
            command = group["hooks"][0]["command"]
            parsed = parse_waiter_stop_argv(shlex.split(command))
            if parsed is not None and parsed[1] == root.path:
                launcher = parsed[0]
                if not launcher.is_absolute() or launcher.name not in {"floati-codex-wait", "floati-codex-wait.py"} or not launcher.is_file():
                    _refuse("the exact fleet Stop launcher is absent: " + str(launcher))
                matching.append(observation)
    except (ProtocolRefusal, OSError, ValueError, KeyError, TypeError) as exc:
        raise ProtocolRefusal("seat_onboard_hook_unavailable", "cannot validate the explicitly named Codex Stop hook: " + str(hooks),
            remedy="inspect the exact fleet Stop launcher named by " + str(hooks) + " before retrying seat add") from exc
    if len(matching) != 1:
        raise ProtocolRefusal("seat_onboard_hook_unavailable",
            "seat preparation requires exactly one Floati Stop hook for " + str(root.path),
            remedy="preserve foreign hooks; resolve the missing or ambiguous fleet hook at " + str(hooks))
    if matching[0]["hook_armed"] is not True:
        raise ProtocolRefusal("seat_onboard_hook_unavailable",
            "the fleet Stop hook is installed; host hook trust is the remaining operator action",
            remedy=matching[0]["hook_trust_remediation"])


def _ack_history(root, node, messages, session):
    """Receipt only the archived cut, never a fresh scan of the recipient inbox."""
    from .wake_hold import wake_coordination_guard
    from .events import MAX_PRESENTATION_ITEMS
    receipts = []
    sessions = {row.get("worker_session_id") for row in messages}
    for worker_session in sorted(sessions, key=lambda value: value or ""):
        selected = [row for row in messages if row.get("worker_session_id") == worker_session]
        with wake_coordination_guard(root, node, worker_session_id=worker_session):
            Registry(root).require_protocol_lease(node, act="fresh delivery to recipient")
            for offset in range(0, len(selected), MAX_PRESENTATION_ITEMS):
                batch = selected[offset:offset + MAX_PRESENTATION_ITEMS]
                delivery = EventLog(root)._write_delivery_receipt(node, batch,
                    worker_session_id=worker_session)
                acknowledgment = SparseCursor(root)._ack_already_guarded(node,
                    [row["id"] for row in batch], acting_session_id=session,
                    worker_session_id=worker_session)
                receipts.append({"delivery": delivery, "acknowledgment": acknowledgment})
    return receipts


def add(root, *, home, source, gateway_config, codex_config, transport,
        coordinator, repo, mode="desktop", node=None, profile=None,
        codex_executable=None, warm_build_script=None, rehome=False, codex_hooks=None):
    from . import seat_harness_config, seat_waiter_lifecycle

    home, source = _path(home), _path(source)
    gateway_config, codex_config = _path(gateway_config), _path(codex_config)
    codex_hooks = _path(codex_hooks if codex_hooks is not None else codex_config.with_name("hooks.json"))
    if not home.parent.is_dir() or not source.is_dir():
        _refuse("source and home parent must be existing ordinary directories")
    if home == root.path or root.path in home.parents or home in root.path.parents or home == source or source in home.parents or home in source.parents:
        _refuse("seat home must be disjoint from the source repository and bus root")
    if mode not in {"desktop", "cli"}:
        _refuse("seat mode must be desktop or cli")
    if mode == "cli" and codex_config.name != "config.toml":
        _refuse("CLI onboarding requires the provider's explicit config.toml path")
    governance = FleetGovernance.load(root)
    if governance is None or governance.coordinator != coordinator:
        _refuse("seat add requires existing fleet governance naming --coordinator")
    validate_identifier(repo, "repo")
    validate_identifier(transport, "transport")
    _check_hook(root, codex_hooks, codex_config)
    if rehome and (node is None or profile is None):
        _refuse("explicit re-home requires --node and --profile")
    if mode == "cli":
        if codex_executable is None:
            _refuse("CLI onboarding requires the exact --codex-executable")
        executable = _checked_codex_path(codex_executable)
    else:
        executable = None
    top = _git(source, "rev-parse", "--show-toplevel")
    if top != str(source):
        _refuse("source must be the exact Git worktree root")
    source_sha = _git(source, "rev-parse", "HEAD")
    if _git(source, "status", "--porcelain", "--untracked-files=no"):
        _refuse("source has tracked modifications; commit the intended source first")
    if warm_build_script is not None:
        script = _path(source / warm_build_script)
        if source not in script.parents or not script.is_file() or not os.access(script, os.X_OK):
            _refuse("warm build must name one executable script in the source repository")
        if not _git(source, "ls-files", "--", warm_build_script):
            _refuse("warm build script must be committed in the source repository")
    elif (source / "Package.swift").exists():
        _refuse("compiled package requires its declared --warm-build-script")
    request = {"source": str(source), "source_sha": source_sha,
        "gateway_config": str(gateway_config), "codex_config": str(codex_config),
        "codex_hooks": str(codex_hooks),
        "transport": transport, "coordinator": coordinator, "repo": repo, "mode": mode,
        "requested_node": node, "requested_profile": profile,
        "codex_executable": None if executable is None else str(executable),
        "warm_build_script": warm_build_script, "rehome": rehome}
    record_path = _record_path(root, home)
    seat_harness_config.validate_transport(gateway_config, transport)
    with _operation(root):
        if record_path.exists():
            prior = _load(record_path, root, home)
            if prior.get("state") == "ready" and prior.get("request") == request:
                if _inventory(home) != prior["inventory"]:
                    _refuse("seat add replay found changed owned home; preserve it for inspection")
                pause_state = _verify_ready(root, prior)
                result = dict(prior["result"])
                if prior.get("cli_start") is not None:
                    result["thread"] = {**result["thread"], "startup_receipt": "historical",
                        "current_wake_control_state": pause_state}
                return result
            if prior.get("state") == "removed" and not home.exists():
                archive = root.resolve_relative(Path("state/seat-onboarding-history") / (record_path.stem + "-" + _lifecycle_id(prior) + ".json"))
                if archive.exists():
                    _refuse("previous seat lifecycle archive already exists; preserve it for inspection")
                _atomic(archive, _bytes(record_path))
                record_path.unlink()
            else:
                _refuse("seat ownership record already exists with a different request or unfinished state; run seat remove for this home")
        if home.exists():
            _refuse("seat home already exists; choose an absent home")
        node = validate_identifier(node or _name(root, home), "node")
        profile = validate_identifier(profile or root.tenant_id + "-" + node, "profile")
        previous_entry = Registry(root)._latest(node)
        if previous_entry is not None and not rehome:
            _refuse("node has prior registry history; use a suggested unused name until re-home is explicitly requested")
        if rehome:
            if previous_entry is None or str(previous_entry["role"]).casefold() != "codex":
                _refuse("re-home requires an existing Codex node")
            seat_waiter_lifecycle.preflight_rehome(root, node)
        workspace = home / "repo"
        settings = {"transport": transport, "git_common_dir": str(workspace / ".git"),
            "workspace_root": str(workspace), "bus_root": str(root.path), "identity": node,
            "allowed_operations": ["ack", "attach", "board", "inbox", "send"],
            "send_repository": repo, "send_recipients": [coordinator]}
        config_plan = seat_harness_config.prepare(gateway_config, codex_config, profile, settings,
            replace_profile=rehome)
        map_path = root.path / WORKSPACE_MAP_RELATIVE
        map_before = _bytes(map_path) if map_path.exists() else None
        record = {"schema_version": 1, "root": str(root.path), "home": str(home),
            "node_id": node, "profile": profile, "request": request, "state": "preparing",
            "timestamp": utc_now(), "config_plan": config_plan,
            "map_before": None if map_before is None else base64.b64encode(map_before).decode(),
            "steps": []}
        record["rehome"] = rehome
        record["previous_registry_entry"] = previous_entry
        record["registry_row"] = {"schema_version": 0, "id": "registry-" + uuid7_hex(),
            "tenant_id": root.tenant_id, "timestamp": utc_now(), "kind": "registry_entry",
            "node_id": node, "role": "Codex", "state": "active"}
        if rehome and previous_entry["state"] == "active":
            record["registry_row"] = previous_entry
        record["lifecycle_id"] = uuid7_hex()
        stage = home.parent / (".seat-add-" + record["lifecycle_id"])
        record["stage"] = str(stage)
        record["stage_identity"] = None
        _save(record_path, record)
        stage.mkdir(mode=0o700)
        record["stage_identity"] = _directory_identity(stage)
        _save(record_path, record)
        try:
            record["inventory"] = {}
            with _child_output_boundary(record_path, record, stage, "clone"):
                _git(home.parent, "clone", "--no-hardlinks", "--", str(source), str(stage / "repo"))
                _git(stage / "repo", "checkout", "--detach", source_sha)
                origin = _git(source, "remote", "get-url", "origin") if "origin" in _git(source, "remote").splitlines() else str(source)
                _git(stage / "repo", "remote", "set-url", "origin", origin)
            boot = _boot(profile, workspace)
            with _child_output_boundary(record_path, record, stage, "stage"):
                for directory in ("wt", "scratch"):
                    (stage / directory).mkdir(mode=0o700)
                _atomic(stage / "SEAT.md", ("# Seat identity\n\nNode: `" + node + "`\nRoot: `" + str(root.path)
                    + "`\nWorkspace: `" + str(workspace) + "`\nProfile: `" + profile
                    + "`\nCoordinator: `" + coordinator + "`\n\n" + boot + "\n").encode())
            _mark(record_path, record, "home")
            _rename_generation_noreplace(stage, home)
            _mark(record_path, record, "registered")
            if rehome and previous_entry["state"] == "active":
                if Registry(root)._latest(node) != previous_entry:
                    _refuse("re-home registration changed after preflight")
            else:
                _register_owned(root, record)
            rehome_result = None
            prior_messages = []
            if rehome:
                _mark(record_path, record, "rehome")
                rehome_result = seat_waiter_lifecycle.rehome_waiter(root, node,
                    "seat-rehome-" + _lifecycle_id(record))
                record["rehome_map_before"] = record["map_before"]
                record["map_before"] = base64.b64encode(_bytes(map_path)).decode() if map_path.exists() else None
                pending = seat_waiter_lifecycle.prior_life_mail(root, node)
                frames, _ = EventLog(root).compatible_event_records(snapshot=True)
                prior_messages = [row for row in frames if row["id"] in pending]
                record["prior_life_messages"] = prior_messages
                _save(record_path, record)
                record["historical_receipts"] = _ack_history(root, node, prior_messages,
                    "seat-rehome-" + _lifecycle_id(record))
                _save(record_path, record)
            _mark(record_path, record, "waiter")
            seat_waiter_lifecycle.prepare_waiter(root, node, workspace)
            with _child_output_boundary(record_path, record, home, "declaration"):
                SeatDeclaration.publish_declared_workspace(workspace, node, root, governance)
            _mark(record_path, record, "config")
            seat_harness_config.apply(config_plan)
            warm = {"state": "not_applicable", "reason": "no compiled package"}
            if warm_build_script is not None:
                script = _path(workspace / warm_build_script)
                if workspace not in script.parents or not script.is_file() or not os.access(script, os.X_OK):
                    _refuse("warm build must name one executable script in the cloned repository")
                with _child_output_boundary(record_path, record, home, "warm"):
                    with (home / "scratch/warm-build.log").open("xb") as log:
                        child = subprocess.run([str(script)], cwd=workspace, stdout=log, stderr=log, timeout=1200)
                if child.returncode:
                    _refuse("warm build failed; inspect scratch/warm-build.log and the ownership record")
                warm = {"state": "completed", "script": warm_build_script, "exit_code": child.returncode,
                    "output_paths": list(_CHILD_OUTPUT_PATHS["warm"])}
            elif (workspace / "Package.swift").exists():
                _refuse("compiled package requires its declared --warm-build-script")
            action = "Open Codex on " + str(workspace) + " and paste this: " + boot
            result = {"state": "prepared", "node_id": node, "suggested_name": request["requested_node"] is None,
                "profile": profile, "home": str(home), "workspace": str(workspace), "boot_prompt": boot,
                "operator_action": action, "warm_build": warm, "ownership_record": str(record_path),
                "hearing": "unproven", "live_waiter": "unproven", "rehome": rehome_result,
                "prior_life_messages": prior_messages,
                "prior_life_disposition": "historical_acknowledged" if rehome else "not_applicable"}
            if mode == "cli":
                with _child_output_boundary(record_path, record, home, "cli"):
                    result["thread"] = _start_cli_thread(executable, workspace, boot, home,
                        root=root, record_path=record_path, record=record)
                result["thread"]["output_paths"] = list(_CHILD_OUTPUT_PATHS["cli"])
                result["operator_action"] = ("CLI thread created and paused. Run "
                    + shlex.join(["env", "CODEX_HOME=" + result["thread"]["provider_home"],
                        *result["thread"]["resume_argv"]]) + " and paste this: " + boot)
            if _inventory(home) != record["inventory"]:
                _refuse("owned home changed after its final output cut; preserve it")
            _verify_ready(root, record)
            record.update(state="ready", result=result)
            _save(record_path, record)
            return result
        except BaseException:
            record["state"] = "failed"
            _save(record_path, record)
            raise


def _start_cli_thread(executable, workspace, prompt, home, *, root, record_path, record):
    """Create a persistent thread without a model turn or an unattended child."""
    from .adapters.codex_live import AppServerSession
    from .workers import WorkerAdapterFailure
    from .wake_control import WakeController, validate_session_id

    output = home / "scratch/cli-start.jsonl"
    provider_home = _path(record["request"]["codex_config"]).parent
    if Path(record["request"]["codex_config"]).name != "config.toml":
        _refuse("CLI creation requires the explicit provider config.toml coordinate")
    provider_environment = dict(os.environ)
    provider_environment.pop("CODEX_THREAD_ID", None)
    provider_environment.pop("CODEX_TURN_ID", None)
    provider_environment["CODEX_HOME"] = str(provider_home)
    start = {"thread_id": None, "pause_key": "seat-start-pause-" + _lifecycle_id(record), "pause_intent": None,
        "pause_receipt_id": None, "providers": [], "resumable": False, "provider_home": str(provider_home)}
    record["cli_start"] = start
    _save(record_path, record)

    class CreationSession(AppServerSession):
        frames = 0
        observed_thread_id = None
        resume_phase = False

        def _read(self, deadline):
            import select
            process = self.process
            if process is None or process.stdout is None:
                raise WorkerAdapterFailure("process_died")
            while b"\n" not in self._stdout_buffer:
                if len(self._stdout_buffer) > 65536:
                    raise WorkerAdapterFailure("protocol_error")
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    raise WorkerAdapterFailure("process_timeout")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise WorkerAdapterFailure("protocol_error" if self._stdout_buffer else "process_died")
                self._stdout_buffer.extend(chunk)
            line, _, remainder = self._stdout_buffer.partition(b"\n")
            self._stdout_buffer = bytearray(remainder)
            self.frames += 1
            if len(line) > 65536 or self.frames > 128:
                raise WorkerAdapterFailure("protocol_error")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate protocol field")
                    result[key] = value
                return result
            def reject_constant(value):
                raise ValueError("non-JSON numeric constant")
            try:
                raw = line.decode("utf-8")
                self.transcript.write(raw + "\n")
                self.transcript.flush()
                message = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
            except (UnicodeError, ValueError, RecursionError) as exc:
                raise WorkerAdapterFailure("protocol_error") from exc
            if not isinstance(message, dict):
                raise WorkerAdapterFailure("protocol_error")
            if "jsonrpc" in message and message["jsonrpc"] != "2.0":
                raise WorkerAdapterFailure("protocol_error")
            return message

        def _notification(self, message):
            import re
            if set(message) - {"method", "params", "jsonrpc", "emittedAtMs"} or not isinstance(message.get("params"), dict):
                raise WorkerAdapterFailure("protocol_error")
            if "emittedAtMs" in message and (type(message["emittedAtMs"]) is not int or not 0 <= message["emittedAtMs"] < 2 ** 53):
                raise WorkerAdapterFailure("protocol_error")
            params, method = message["params"], message.get("method")
            if method == "remoteControl/status/changed":
                # Codex 0.153.0 emits this disabled observation during startup.
                # It grants no remote capability and changes no thread state.
                if set(params) != {"status", "serverName", "installationId", "environmentId"} or params["status"] != "disabled" or params["environmentId"] is not None or not isinstance(params["serverName"], str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", params["serverName"]) is None or not isinstance(params["installationId"], str) or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", params["installationId"]) is None:
                    raise WorkerAdapterFailure("protocol_error")
            elif method == "thread/started":
                thread = params.get("thread")
                if not isinstance(thread, dict):
                    raise WorkerAdapterFailure("protocol_error")
                thread_id = validate_session_id(thread.get("id"))
                if self.observed_thread_id not in {None, thread_id}:
                    raise WorkerAdapterFailure("protocol_error")
                if ("ephemeral" in thread and thread["ephemeral"] is not False) or ("turns" in thread and thread["turns"] != []) or ("status" in thread and (not isinstance(thread["status"], dict) or set(thread["status"]) != {"type"} or thread["status"].get("type") not in {"idle", "notLoaded"})):
                    raise WorkerAdapterFailure("protocol_error")
                self.observed_thread_id = thread_id
            elif method == "thread/status/changed":
                if self.observed_thread_id is None or params.get("threadId") != self.observed_thread_id or not isinstance(params.get("status"), dict) or set(params["status"]) != {"type"} or params["status"].get("type") not in {"idle", "notLoaded"}:
                    raise WorkerAdapterFailure("protocol_error")
            elif method == "thread/goal/cleared":
                if not self.resume_phase or self.observed_thread_id is None or set(params) != {"threadId"} or params["threadId"] != self.observed_thread_id:
                    raise WorkerAdapterFailure("protocol_error")
            else:
                raise WorkerAdapterFailure("protocol_error")

        def _wait_for_response(self, request_id, deadline, *, allow_named_read_absence=False):
            while True:
                message = self._read(deadline)
                if "method" in message:
                    if "id" in message:
                        self._refuse_server_request(message)
                    self._notification(message)
                    continue
                if allow_named_read_absence and not set(message) - {"id", "error", "jsonrpc"} and type(message.get("id")) is int and message["id"] == request_id and isinstance(message.get("error"), dict) and set(message["error"]) == {"code", "message"} and type(message["error"]["code"]) is int and message["error"]["code"] == -32601 and message["error"]["message"] == "list_turns is not supported yet":
                    # Named read absence is recorded in the transcript. It is
                    # not persistence proof; the fresh provider must resume.
                    return None
                if set(message) - {"id", "result", "jsonrpc"} or type(message.get("id")) is not int or message["id"] != request_id or not isinstance(message.get("result"), dict):
                    raise WorkerAdapterFailure("protocol_error")
                return message["result"]

        def finish(self):
            self.process.stdin.close()
            # Drain every final notification before EOF, so an unsolicited model
            # turn after the creation response cannot be hidden by process exit.
            while True:
                try:
                    message = self._read(self.deadline)
                except WorkerAdapterFailure as exc:
                    if exc.code == "process_died":
                        return
                    raise
                if "id" in message:
                    self._refuse_server_request(message)
                self._notification(message)

        def close(self):
            # Both bounded connections share these exact owned log handles.
            self.transcript = self.stderr = None
            super().close()

        def checked_thread(self, response, expected_id=None):
            thread = response.get("thread")
            if not isinstance(thread, dict) or thread.get("ephemeral") is not False:
                _refuse("CLI thread/start, creator thread/read or fresh thread/resume did not prove a persistent thread; inspect the owned CLI logs")
            thread_id = validate_session_id(thread.get("id"))
            if thread.get("cwd") != str(workspace) or thread.get("turns") != [] or not isinstance(thread.get("status"), dict) or set(thread["status"]) != {"type"} or thread["status"].get("type") not in {"idle", "notLoaded"} or thread.get("sessionId", thread_id) != thread_id or self.observed_thread_id not in {None, thread_id} or (expected_id is not None and thread_id != expected_id):
                _refuse("CLI creation-only thread has different identity/workspace or an active/prior turn")
            self.observed_thread_id = thread_id
            return thread_id

    with output.open("x", encoding="utf-8") as transcript, (home / "scratch/cli-start.stderr").open("xb") as stderr:
        for purpose in ("create", "resume"):
            executable = _checked_codex_path(executable)
            session = CreationSession([str(executable), "app-server", "--stdio"], workspace)
            session.transcript, session.stderr = transcript, stderr
            session.deadline = time.monotonic() + 120
            child = None
            observation = None
            try:
                child = session.process = subprocess.Popen(session.command, cwd=workspace,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                    bufsize=0, start_new_session=True, env=provider_environment)
                observation = {"purpose": purpose, "pid": child.pid, "exit_code": None, "group_gone": False}
                start["providers"].append(observation)
                _save(record_path, record)
                handshake = session._request("initialize", {"clientInfo": {
                    "name": "floati-seat", "version": "0"}})
                session._wait_for_response(handshake, session.deadline)
                session._notify("initialized")
                method = "thread/start" if purpose == "create" else "thread/resume"
                params = {"cwd": str(workspace), "approvalPolicy": "on-request",
                    "sandbox": "workspace-write"}
                if purpose == "create":
                    params["ephemeral"] = False
                else:
                    params["threadId"] = start["thread_id"]
                    # Resume proves stored metadata availability. No-turn
                    # testimony comes from this new thread's closed transcript,
                    # not hydration of excluded paginated history.
                    params["excludeTurns"] = True
                    session.observed_thread_id = start["thread_id"]
                    session.resume_phase = True
                request_id = session._request(method, params)
                response = session._wait_for_response(request_id, session.deadline)
                thread_id = session.checked_thread(response, None if purpose == "create" else start["thread_id"])
                if purpose == "create":
                    start["thread_id"] = thread_id
                    _save(record_path, record)  # exact pause intent precedes its native effect
                    def reserve_pause(receipt):
                        start["pause_intent"] = receipt
                        _save(record_path, record)
                    pause = WakeController(root).pause(record["node_id"], thread_id,
                        idempotency_key=start["pause_key"], reserve_receipt=reserve_pause)
                    start["pause_receipt_id"] = pause["receipt"]["id"]
                    _save(record_path, record)
                    # Codex 0.153.0 materializes a loaded paginated thread on
                    # this provider-owned read, without starting a model turn.
                    read_id = session._request("thread/read", {"threadId": thread_id, "includeTurns": True})
                    read_response = session._wait_for_response(read_id, session.deadline, allow_named_read_absence=True)
                    if read_response is not None:
                        session.checked_thread(read_response, thread_id)
                session.finish()
            except (WorkerAdapterFailure, OSError, ValueError) as exc:
                _refuse("CLI creation-only " + purpose + " failed without a resumable receipt: " + str(exc)
                    + "; inspect the owned CLI logs and remove the failed seat before retrying")
            finally:
                try:
                    session.close()
                finally:
                    if child is not None and child.poll() is not None:
                        from .thread_source import _group_exists
                        observation["exit_code"] = child.returncode
                        deadline = time.monotonic() + 0.5
                        while _group_exists(child.pid) and time.monotonic() < deadline:
                            time.sleep(0.005)
                        observation["group_gone"] = not _group_exists(child.pid)
                        _save(record_path, record)
            if observation["exit_code"] != 0 or not observation["group_gone"]:
                _refuse("CLI creation-only provider did not exit cleanly; it cannot prove paused resumability")
        start["resumable"] = True
        _save(record_path, record)
        transcript.flush()
        os.fsync(transcript.fileno())
        os.fsync(stderr.fileno())
    _owned_cli_pause(root, record, require_paused=True)
    return {"thread_id": start["thread_id"], "state": "paused", "resumable": True,
        "startup_receipt": "current", "provider_state": "saved_without_turn",
        "wake_control_state": "paused", "cached_session_state": "unknown",
        "turn_started": False, "exit_code": 0, "receipt": str(output),
        "pause_receipt_id": start["pause_receipt_id"], "providers": start["providers"],
        "provider_home": str(provider_home), "resume_env": {"CODEX_HOME": str(provider_home)},
        "resume_argv": [str(executable), "--cd", str(workspace), "resume", start["thread_id"]],
        "resume_prompt": prompt, "first_turn": "seat board claims and resumes through the normal wake path"}


def _owned_cli_pause(root, record, *, require_paused=False):
    from .jsonl import read_records_snapshot
    from .wake_control import WakeController

    start = record["cli_start"]
    if start["thread_id"] is None:
        return None
    controller = WakeController(root)
    node, digest = record["node_id"], hashlib.sha256(start["thread_id"].encode()).hexdigest()
    marker_path = controller._marker(node, digest)
    pause = start["pause_intent"]
    quarantine = None if pause is None else controller._resume_quarantine(marker_path, pause["id"])
    if quarantine is not None and (quarantine.exists() or quarantine.is_symlink()):
        if marker_path.exists() or marker_path.is_symlink() or require_paused:
            _refuse("CLI pause and interrupted removal staging conflict; preserve both")
        marker_path = quarantine
    marker = controller._read_marker(marker_path, node, digest)
    if marker is None:
        if require_paused:
            _refuse("created CLI thread's pause was resumed or is absent; its saved paused receipt is no longer current")
        return None
    if pause is None:
        _refuse("CLI pause marker has no reserved native identity; preserve it")
    expected_marker = {"schema_version": 0, "node_id": node, "session_digest": digest,
        "state": "paused", "paused_at": pause["timestamp"], "receipt_id": pause["id"]}
    rows = read_records_snapshot(root, controller._ledger(node), allowed_kinds={"wake_control_receipt"})
    matches = [row for row in rows if row["id"] == pause["id"] or row["idempotency_key"] == start["pause_key"]]
    metadata = marker_path.lstat()
    if marker != expected_marker or _bytes(marker_path) != controller._encode_marker(expected_marker) or stat.S_IMODE(metadata.st_mode) != 0o600 or (matches and (len(matches) != 1 or matches[0] != pause)) or (require_paused and not matches):
        _refuse("CLI pause marker was replaced by foreign evidence; preserve it before seat removal")
    return marker


def _cli_pause_capability(root, record):
    from .wake_control import WakeController
    start = record["cli_start"]
    if start["thread_id"] is None:
        return {"state": "absent", "session_id": None, "marker": None, "pause_receipt_id": None}
    node, session = record["node_id"], start["thread_id"]
    controller = WakeController(root)
    path = controller._marker(node, hashlib.sha256(session.encode()).hexdigest())
    marker = _owned_cli_pause(root, record)
    return {"state": ("paused" if path.exists() else "removing") if marker is not None else "absent",
        "session_id": session, "marker": str(path),
        "pause_receipt_id": None if start["pause_intent"] is None else start["pause_intent"]["id"]}


def remove(root, *, home):
    from . import seat_harness_config, seat_waiter_lifecycle

    home = _path(home)
    path = _record_path(root, home)
    with _operation(root):
        record = _load(path, root, home)
        if record["state"] == "removed":
            if home.exists():
                _refuse("removed seat home was recreated; it is now foreign")
            return record["remove_result"]
        if any(child["state"] == "pending" for child in record.get("child_outputs", [])):
            _refuse("interrupted child has no finalized output inventory; preserve its logs and workspace for explicit recovery")
        if record["state"] not in {"ready", "failed", "preparing", "removing"}:
            _refuse("seat transaction has an unknown durable phase")
        steps = record["steps"]
        node, workspace = record["node_id"], home / "repo"
        if not record.get("stage"):
            if steps or home.exists() or record["state"] != "preparing":
                _refuse("unfinished seat record has no staged directory; preserve the record for inspection")
            result = {"state": "removed", "node_id": node, "home": str(home),
                "before_inventory": {}, "after_inventory": {}, "waiter": None,
                "retained": ["registry and consent history", str(path)], "foreign_stage_preserved": None}
            record.update(state="removed", remove_result=result)
            _save(path, record)
            return result
        stage = _path(record["stage"])
        if stage.parent != home.parent or not stage.name.startswith(".seat-add-"):
            _refuse("seat staging coordinate differs from its declared home parent")
        if not record.get("stage_identity") and (stage.exists() or home.exists()):
            _refuse("interrupted directory creation has no durable identity; preserve it for inspection")
        home_owned = home.exists() and _directory_identity(home) == record["stage_identity"]
        if home.exists() and not home_owned:
            _refuse("seat home was replaced; preserve foreign data")
        if home_owned and record.get("inventory") is not None and _inventory(home) != record["inventory"]:
            _refuse("owned home changed; removal preserves it and all configuration")
        if "registered" in steps:
            latest = Registry(root)._latest(node)
            if latest is not None and latest["state"] == "active":
                if latest != record["registry_row"]:
                    _refuse("a newer foreign registration replaced the owned seat; preserve it")
                _map_path, _map_bytes, mapping = seat_waiter_lifecycle._current_map(root)
                if mapping is not None and any(entry["node_id"] == node and entry["workspace"] != str(workspace)
                    for entry in mapping["mappings"]):
                    _refuse("this node is now mapped to another home; remove that incarnation before this historical home")
        completed = record.setdefault("removal_steps", [])
        cli_before = None
        if record.get("cli_start") is not None:
            cli_before = _cli_pause_capability(root, record)
            if "cli" in completed and cli_before is not None and cli_before["state"] != "absent":
                _refuse("completed CLI pause cleanup acquired a new capability; preserve it before continuing removal")
        if "config" in steps and "config" not in completed:
            seat_harness_config.preflight_restore(record["config_plan"])
        if "waiter" in steps and "waiter" not in completed and not workspace.is_dir():
            _refuse("owned waiter workspace is missing; preserve its capabilities for explicit recovery")
        if "waiter" in steps and "waiter" not in completed and workspace.is_dir():
            seat_waiter_lifecycle.preflight_retire_waiter(root, node, workspace)
        record["state"] = "removing"
        _save(path, record)
        if record.get("cli_start") is not None and "cli" not in completed:
            from .wake_control import WakeController
            record.setdefault("cli_removal", {"before": cli_before, "after": None})
            _save(path, record)
            start = record["cli_start"]
            if cli_before is not None and cli_before["state"] != "absent":
                controller = WakeController(root)
                controller.reconcile_pause(node, start["thread_id"], expected_receipt=start["pause_intent"])
                controller.resume(node, start["thread_id"],
                    idempotency_key="seat-remove-pause-" + _lifecycle_id(record),
                    expected_pause_receipt_id=start["pause_intent"]["id"])
            after = _cli_pause_capability(root, record)
            if after is not None and after["state"] != "absent":
                _refuse("CLI pause remains after conditional removal; preserve its native state")
            record["cli_removal"]["after"] = after
            completed.append("cli")
            _save(path, record)
        if "config" in steps and "config" not in completed:
            seat_harness_config.restore(record["config_plan"])
            completed.append("config")
            _save(path, record)
        if "waiter" in steps and "waiter" not in completed and workspace.is_dir():
            before = None if record["map_before"] is None else base64.b64decode(record["map_before"])
            waiter = seat_waiter_lifecycle.retire_waiter(root, node, workspace,
                "seat-remove-" + _lifecycle_id(record), previous_map=before)
            completed.append("waiter")
            record["waiter_removal"] = waiter
            _save(path, record)
        else:
            waiter = record.get("waiter_removal")
        if "registered" in steps and "registry" not in completed:
            owns_registration = any(row == record["registry_row"] for row in Registry(root).known_records())
            latest = Registry(root)._latest(node)
            if owns_registration and latest["state"] == "active":
                if latest != record["registry_row"]:
                    _refuse("a newer foreign registration replaced the owned seat; preserve it")
                Registry(root).retire(node)
            completed.append("registry")
            _save(path, record)
        inventory = _inventory(home) if home_owned else record.get("inventory") or {}
        if home_owned:
            trash = home.with_name(".seat-remove-" + path.stem[:16])
            if trash.exists():
                _refuse("owned removal staging already exists; inspect it before retry")
            record["removal_stage"] = str(trash)
            _save(path, record)
            _rename_generation_noreplace(home, trash)
        trash = _path(record["removal_stage"]) if record.get("removal_stage") else None
        if trash is not None and trash.exists():
            if trash != home.with_name(".seat-remove-" + path.stem[:16]) or _directory_identity(trash) != record["stage_identity"]:
                _refuse("removal staging identity differs; preserve foreign data")
            remaining = _inventory(trash)
            if record.get("inventory") is not None and any(record["inventory"].get(name) != entry for name, entry in remaining.items()):
                _refuse("removal staging acquired foreign data; preserve it")
            shutil.rmtree(trash)
        retained_stage = None
        if stage.exists() and _directory_identity(stage) == record["stage_identity"]:
            remaining = _inventory(stage)
            if (record.get("inventory") is None and remaining) or (
                record.get("inventory") is not None and any(record["inventory"].get(name) != entry for name, entry in remaining.items())
            ):
                _refuse("unfinished staging contents have no matching durable inventory; preserve them")
            shutil.rmtree(stage)
        elif stage.exists():
            retained_stage = str(stage)
        if record.get("cli_start") is not None:
            current_cli = _cli_pause_capability(root, record)
            if current_cli is not None and current_cli["state"] != "absent":
                _refuse("CLI pause capability changed during removal; preserve its exact native state")
            record["cli_removal"]["after"] = current_cli
        result = {"state": "removed", "node_id": node, "home": str(home),
            "before_inventory": inventory, "after_inventory": {}, "waiter": waiter,
            "retained": ["registry and consent history", str(path)]
                + (["native Codex thread and wake-control history"] if record.get("cli_start") is not None else []),
            "cli_pause": record.get("cli_removal"),
            "foreign_stage_preserved": retained_stage}
        record.update(state="removed", remove_result=result)
        _save(path, record)
        return result
