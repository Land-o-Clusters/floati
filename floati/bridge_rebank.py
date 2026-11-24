"""Receipt-bound refresh of an already adopted fixed ZCode runtime bank.

This module neither imports private tooling nor installs hooks. Prior bank
testimony authorizes refresh only; current testimony remains transport-bound.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import shlex
import stat
import tempfile
import time

from .bridge_bank import (BridgeBankUnavailable, STABLE_LAUNCHER_SHA256, BRIDGE_PROVIDER_GENERATIONS,
    _DIGEST, _SHA, _checked, _digest, _installed_manifest, _json, _ordinary,
    _read, _reader_version, _verify_store, _provider_generation, verify_bridge_store)
from .errors import ProtocolRefusal

KIND = "bridge_rebank"


def bridge_rebank_remedy(source_root=None, *, plan_digest=None):
    """Name only a locally present R3 recovery tool, otherwise its prerequisite.

    The public package never executes or imports the private companion here.
    The update target may be an older or public tree, so it is not a remedy root.
    """
    source_root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]
    available = False
    try:
        _ordinary(source_root, directory=True)
        shell = _read(source_root / "scripts/install-zcode-bridge.sh", 64 * 1024, private_file=False)
        companion = _read(source_root / "tools/zcode/install_stop_hook_bridge.py", 256 * 1024, private_file=False)
        syntax = ast.parse(companion)
        operations = {node.value for node in ast.walk(syntax)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        available = bool(shell) and {"prepare_bridge_rebank", "apply_bridge_rebank", "replay_bridge_rebank"}.issubset(operations)
    except (OSError, ValueError, SyntaxError):
        pass
    if plan_digest is not None and (not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None):
        raise BridgeRebankUnavailable("recovery remedy requires one exact plan digest")
    installer = "sh " + (shlex.quote(str(source_root / "scripts/install-zcode-bridge.sh")) if available else
                          "R3_SOURCE_ROOT/scripts/install-zcode-bridge.sh")
    prefix = "run the governed bridge re-bank recovery: " if available else (
        "recovery requires a reviewed private R3 source checkout; from that checkout run ")
    if plan_digest is not None:
        return prefix + installer + " --apply " + plan_digest
    return prefix + installer + " --dry-run; then " + installer + " --apply PLAN_DIGEST from that dry-run"


class BridgeRebankUnavailable(ProtocolRefusal):
    def __init__(self, detail, code="bridge_rebank_unavailable", receipt=None):
        super().__init__(code, detail, "Run the governed bridge re-bank dry-run and apply its exact plan digest.")
        self.receipt = receipt


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _store(home):
    return home / ".local/share/floati-zcode-bridge"


def _selector(bank, *, launcher_sha256=STABLE_LAUNCHER_SHA256):
    _provider_generation(launcher_sha256)
    return _encoded({"schema_version": 0, "bank_sha256": bank.name,
                     "launcher_sha256": launcher_sha256})


def _index_payload(plan_digest):
    return _encoded({"schema_version": 0, "plan_digest": plan_digest})


def _index(home, name="active-receipt.json"):
    if name not in ("active-receipt.json", "pending-rebank.json"):
        raise BridgeRebankUnavailable("unknown receipt pointer coordinate")
    path = _store(home) / name
    raw = _optional_read(path, 4096)
    if raw is None:
        return None, None
    record = _json(raw, path)
    if (not isinstance(record, dict) or set(record) != {"schema_version", "plan_digest"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 0
            or not isinstance(record["plan_digest"], str) or _DIGEST.fullmatch(record["plan_digest"]) is None
            or raw != _index_payload(record["plan_digest"])):
        raise BridgeRebankUnavailable(f"current receipt index changed: {path}")
    return record["plan_digest"], raw


def _config_paths(home):
    mandatory, optional = [], []
    for ordinal in range(1, 5):
        workspace = home / "Projects" / ("floati-zcode" if ordinal == 1 else f"floati-zcode-{ordinal}")
        mandatory.extend(workspace / ".zcode" / name for name in ("settings.json", "hooks.json"))
        (mandatory if ordinal == 1 else optional).append(workspace / "hooks/hooks.json")
    return mandatory, optional


def _bridge_command_identity(home, *, bridge_payload=None):
    """Read the fixed identity from digest-verified private provider bytes.

    The public reader carries no private seat vocabulary and executes no
    provider code. This is command rendering, never a source of authority.
    """
    selector_path = _store(home) / "active.json"
    selector = _json(_read(selector_path, 4096), selector_path)
    generation = _provider_generation(_digest(_read(_store(home) / "launcher.py", 1024 * 1024)))
    if (not isinstance(selector, dict)
            or set(selector) != {"schema_version", "bank_sha256", "launcher_sha256"}
            or type(selector["schema_version"]) is not int or selector["schema_version"] != 0
            or selector["launcher_sha256"] != generation[0]
            or not isinstance(selector["bank_sha256"], str)
            or _DIGEST.fullmatch(selector["bank_sha256"]) is None):
        raise BridgeRebankUnavailable("command provider selector changed")
    bank = _store(home) / "banks" / selector["bank_sha256"]
    _checked(bank / "launcher.py", generation[1], 1024 * 1024)
    captured = _checked(bank / "hooks/stop-hook-bridge.py", generation[2], 1024 * 1024)
    if bridge_payload is None:
        bridge_payload = captured
    if not isinstance(bridge_payload, bytes) or bridge_payload != captured:
        raise BridgeRebankUnavailable("private command provider digest changed")
    tree = ast.parse(bridge_payload, filename="verified-private-bridge")
    defaults = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id not in {"ROOT", "NODE", "SESSION"}:
            continue
        value = statement.value
        if (not isinstance(value, ast.Call) or ast.unparse(value.func) != "os.environ.get"
                or len(value.args) != 2 or value.keywords
                or not isinstance(value.args[0], ast.Constant)
                or value.args[0].value != "HOOK_" + target.id):
            raise BridgeRebankUnavailable("private command provider declaration changed")
        default = value.args[1]
        if target.id == "ROOT":
            if (not isinstance(default, ast.Call) or ast.unparse(default.func) != "os.path.expanduser"
                    or len(default.args) != 1 or default.keywords):
                raise BridgeRebankUnavailable("private command root declaration changed")
            default = default.args[0]
        if (target.id in defaults or not isinstance(default, ast.Constant)
                or not isinstance(default.value, str) or not default.value):
            raise BridgeRebankUnavailable("private command identity is absent or ambiguous")
        defaults[target.id] = default.value
    if set(defaults) != {"ROOT", "NODE", "SESSION"}:
        raise BridgeRebankUnavailable("private command identity is incomplete")
    relative = PurePosixPath(defaults["ROOT"][2:])
    if (not defaults["ROOT"].startswith("~/") or relative.is_absolute()
            or ".." in relative.parts or str(relative) != defaults["ROOT"][2:]):
        raise BridgeRebankUnavailable("private command root is not home-relative")
    return relative, defaults["NODE"], defaults["SESSION"]


def _portable_command(home, *, bridge_payload=None):
    root, node, session = _bridge_command_identity(home, bridge_payload=bridge_payload)
    body = (f'HOOK_ROOT="$HOME/{root}" '
            f'HOOK_NODE={shlex.quote(node)} HOOK_SESSION={shlex.quote(session)} '
            'HOOK_REPO="$HOME/Projects/floati-zcode" '
            'exec /usr/bin/python3 -I -B "$HOME/.local/share/floati-zcode-bridge/launcher.py"')
    return "/bin/sh -c " + shlex.quote(body)


def _legacy_command(home):
    root, node, session = _bridge_command_identity(home)
    body = (f"HOOK_ROOT={shlex.quote(str(home / root))} "
            f"HOOK_NODE={shlex.quote(node)} HOOK_SESSION={shlex.quote(session)} "
            f"exec /usr/bin/python3 {shlex.quote(str(home / 'Projects/floati-zcode/hooks/stop-hook-bridge.py'))}")
    return "/bin/sh -c " + shlex.quote(body)


def _config(command):
    return {"hooks": {"Stop": [{"matcher": "*", "hooks": [
        {"type": "command", "command": command, "timeout": 30}]}]}}


def _optional_read(path, maximum=4 * 1024 * 1024, *, private_file=True):
    if not path.exists() and not path.is_symlink():
        for parent in path.parents:
            if parent.exists() or parent.is_symlink():
                if not stat.S_ISDIR(parent.lstat().st_mode):
                    raise BridgeRebankUnavailable(f"linked or non-directory absent coordinate parent: {parent}")
        return None
    return _read(path, maximum, private_file=private_file)


def _configuration_read(home, path):
    mandatory, optional = _config_paths(home)
    if path not in mandatory + optional:
        raise BridgeRebankUnavailable("configuration coordinate is outside the fixed twelve")
    return _optional_read(path, private_file=False)


def _record_digest(plan):
    return _digest(_encoded({key: value for key, value in plan.items() if key != "plan_digest"}))


def _validate_plan(home, plan):
    """Validate every readable/writable coordinate before trusting receipt paths."""
    common = {"schema_version", "kind", "home", "noop", "restore_configurations", "plan_digest"}
    if (not isinstance(plan, dict) or plan.get("home") != str(home) or plan.get("kind") != KIND
            or type(plan.get("schema_version")) is not int or plan["schema_version"] != 1
            or type(plan.get("noop")) is not bool or type(plan.get("restore_configurations")) is not bool
            or not isinstance(plan.get("plan_digest"), str) or _DIGEST.fullmatch(plan["plan_digest"]) is None
            or _record_digest(plan) != plan["plan_digest"]):
        raise BridgeRebankUnavailable("re-bank plan schema or digest changed")
    if plan.get("reason") == "store_absent":
        if set(plan) != common | {"reason"} or not plan["noop"]:
            raise BridgeRebankUnavailable("absent-store plan shape changed")
        return
    fields = common | {"source_sha", "manifest_sha256", "registry_sha256", "bank_root",
        "prior_receipt", "previous", "previous_selector", "selector", "receipt_index",
        "previous_receipt_index", "inventory", "files"}
    if set(plan) != fields:
        raise BridgeRebankUnavailable("re-bank plan fields changed")
    for name, pattern in (("source_sha", _SHA), ("manifest_sha256", _DIGEST), ("registry_sha256", _DIGEST)):
        if not isinstance(plan[name], str) or pattern.fullmatch(plan[name]) is None:
            raise BridgeRebankUnavailable(f"re-bank target identity changed: {name}")
    store = _store(home)
    bank = Path(plan["bank_root"]) if isinstance(plan["bank_root"], str) else Path("invalid")
    if _DIGEST.fullmatch(bank.name) is None or bank != store / "banks" / bank.name:
        raise BridgeRebankUnavailable("re-bank target is outside the fixed bank store")
    previous = plan["previous"]
    if not isinstance(previous, dict) or set(previous) != {"bank_root", "source_sha", "manifest_sha256"}:
        raise BridgeRebankUnavailable("re-bank prior identity shape changed")
    prior_bank = Path(previous["bank_root"]) if isinstance(previous["bank_root"], str) else Path("invalid")
    if (_DIGEST.fullmatch(prior_bank.name) is None or prior_bank != store / "banks" / prior_bank.name
            or not isinstance(previous["source_sha"], str) or _SHA.fullmatch(previous["source_sha"]) is None
            or not isinstance(previous["manifest_sha256"], str) or _DIGEST.fullmatch(previous["manifest_sha256"]) is None):
        raise BridgeRebankUnavailable("re-bank prior identity or bank coordinate changed")
    selected = plan["previous_selector"]
    if not isinstance(selected, dict):
        raise BridgeRebankUnavailable("re-bank previous selector changed")
    generation = _provider_generation(selected.get("launcher_sha256"))
    if selected != {"schema_version": 0, "bank_sha256": prior_bank.name, "launcher_sha256": generation[0]} or type(selected.get("schema_version")) is not int:
        raise BridgeRebankUnavailable("re-bank previous selector changed")
    if plan["selector"] != {"path": str(store / "active.json"), "before_sha256": _digest(_encoded(selected)),
                           "after_sha256": _digest(_selector(bank, launcher_sha256=generation[0]))}:
        raise BridgeRebankUnavailable("re-bank selector coordinate or digest changed")
    receipt = plan["prior_receipt"]
    if not isinstance(receipt, dict) or set(receipt) != {"path", "sha256"}:
        raise BridgeRebankUnavailable("re-bank prior receipt shape changed")
    prior_name = Path(receipt["path"]).stem if isinstance(receipt["path"], str) else "invalid"
    if (_DIGEST.fullmatch(prior_name) is None or receipt["path"] != str(store / "receipts" / (prior_name + ".json"))
            or not isinstance(receipt["sha256"], str) or _DIGEST.fullmatch(receipt["sha256"]) is None):
        raise BridgeRebankUnavailable("re-bank prior receipt coordinate or digest changed")
    previous_index = plan["previous_receipt_index"]
    if previous_index is not None and (previous_index != {"schema_version": 0, "plan_digest": prior_name}
            or type(previous_index.get("schema_version")) is not int):
        raise BridgeRebankUnavailable("re-bank prior receipt index changed")
    if plan["receipt_index"] != {"path": str(store / "active-receipt.json"),
        "before_sha256": None if previous_index is None else _digest(_encoded(previous_index))}:
        raise BridgeRebankUnavailable("re-bank receipt index coordinate changed")
    mandatory, optional = _config_paths(home)
    inventory = plan["inventory"]
    if (not isinstance(inventory, list) or len(inventory) != 12
            or any(not isinstance(row, dict) or set(row) != {"path", "before_sha256", "after_sha256", "reason"}
                   or not isinstance(row["path"], str) for row in inventory)
            or {row["path"] for row in inventory} != {str(path) for path in mandatory + optional}):
        raise BridgeRebankUnavailable("re-bank inventory is not twelve unique fixed configurations")
    for row in inventory:
        for name in ("before_sha256", "after_sha256"):
            value = row[name]
            if value is not None and (not isinstance(value, str) or _DIGEST.fullmatch(value) is None):
                raise BridgeRebankUnavailable("re-bank configuration digest changed")
        if row["after_sha256"] is None:
            if row["before_sha256"] is not None or Path(row["path"]) not in optional or row["reason"] != "declared optional config is absent":
                raise BridgeRebankUnavailable("re-bank configuration absence changed")
        elif row["before_sha256"] is None or row["reason"] is not None:
            raise BridgeRebankUnavailable("re-bank configuration cannot be created")
        if row["before_sha256"] != row["after_sha256"] and not plan["restore_configurations"]:
            raise BridgeRebankUnavailable("automatic re-bank cannot rewrite configurations")
    rows = plan["files"]
    names = []
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4099:
        raise BridgeRebankUnavailable("re-bank file inventory bound changed")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"path", "sha256", "mode"}
                or not isinstance(row["path"], str) or not row["path"]
                or not isinstance(row["sha256"], str) or _DIGEST.fullmatch(row["sha256"]) is None
                or type(row["mode"]) is not int or row["mode"] != 0o600):
            raise BridgeRebankUnavailable("re-bank file entry changed")
        path = PurePosixPath(row["path"])
        if path.is_absolute() or ".." in path.parts or str(path) != row["path"] or row["path"] == "bank-manifest.json":
            raise BridgeRebankUnavailable("re-bank file path changed")
        names.append(row["path"])
    if names != sorted(set(names)):
        raise BridgeRebankUnavailable("re-bank file inventory is duplicated or unordered")
    provider_rows = {row["path"]: row["sha256"] for row in rows}
    if (provider_rows.get("launcher.py") != generation[1]
            or provider_rows.get("hooks/stop-hook-bridge.py") != generation[2]):
        raise BridgeRebankUnavailable("re-bank cannot change its prior provider generation")
    manifest = _encoded({"schema_version": 0, "source_sha": plan["source_sha"],
        "registry_sha256": plan["registry_sha256"], "transport_manifest_sha256": plan["manifest_sha256"], "files": rows})
    if _digest(manifest) != bank.name or plan["noop"] != (bank == prior_bank and all(row["before_sha256"] == row["after_sha256"] for row in inventory)):
        raise BridgeRebankUnavailable("re-bank manifest address or no-op meaning changed")


def _validate_rebank_record(home, record):
    fields = {"schema_version", "kind", "status", "plan", "completed", "remaining"}
    if (not isinstance(record, dict) or set(record) not in (fields, fields | {"detail", "observed_selector_sha256"})
            or record.get("kind") != KIND or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1 or record.get("status") not in ("intent", "partial", "complete")):
        raise BridgeRebankUnavailable("re-bank receipt schema or status changed")
    if set(record) != fields:
        observed = record["observed_selector_sha256"]
        if (record["status"] != "partial" or not isinstance(record["detail"], str)
                or observed is not None and (not isinstance(observed, str) or _DIGEST.fullmatch(observed) is None)):
            raise BridgeRebankUnavailable("re-bank partial receipt observation changed")
    _validate_plan(home, record["plan"])
    actions = _actions(record["plan"])
    completed, remaining = record["completed"], record["remaining"]
    if (not isinstance(completed, list) or not isinstance(remaining, list)
            or completed != actions[:len(completed)] or remaining != actions[len(completed):]
            or record["status"] == "complete" and remaining):
        raise BridgeRebankUnavailable("re-bank receipt actions are not an exact ordered frontier")


def _validate_adoption_plan(home, plan, bank, selector):
    fields = {"schema_version", "source_sha", "manifest_sha256", "registry_sha256", "bank_root", "launcher",
              "configs", "removals", "inventory", "entrypoint", "selector", "active_bridge", "reader_scope", "plan_digest"}
    if (set(plan) != fields or type(plan["schema_version"]) is not int or plan["schema_version"] != 0
            or plan["bank_root"] != str(bank) or plan["launcher"] != str(bank / "launcher.py")
            or plan["reader_scope"] != "only the twelve declared configuration paths"):
        raise BridgeRebankUnavailable("prior adoption plan schema or bank coordinate changed")
    for name, pattern in (("source_sha", _SHA), ("manifest_sha256", _DIGEST), ("registry_sha256", _DIGEST)):
        if not isinstance(plan[name], str) or pattern.fullmatch(plan[name]) is None:
            raise BridgeRebankUnavailable("prior adoption transport identity changed")
    selected = _json(selector, _store(home) / "active.json")
    generation = _provider_generation(selected.get("launcher_sha256"))
    for name, path, output in (("entrypoint", _store(home) / "launcher.py", generation[0]),
                              ("selector", _store(home) / "active.json", _digest(selector))):
        row = plan[name]
        if (not isinstance(row, dict) or set(row) != {"path", "before_sha256", "after_sha256"}
                or row["path"] != str(path) or row["after_sha256"] != output
                or row["before_sha256"] is not None and (not isinstance(row["before_sha256"], str) or _DIGEST.fullmatch(row["before_sha256"]) is None)):
            raise BridgeRebankUnavailable("prior adoption store coordinate or digest changed")
    active = plan["active_bridge"]
    if (not isinstance(active, dict) or set(active) != {"path", "sha256"}
            or active["path"] != str(home / "Projects/floati-zcode/hooks/stop-hook-bridge.py")
            or active["sha256"] not in {"82965865ada5de7b8b873b83acc5b0be95edeaeec6bd1221dbbf4501165be1da"}
                | {row[2] for row in BRIDGE_PROVIDER_GENERATIONS}):
        raise BridgeRebankUnavailable("prior adoption authority coordinate changed")
    mandatory, optional = _config_paths(home)
    inventory = plan["inventory"]
    if (not isinstance(inventory, list) or len(inventory) != 12
            or any(not isinstance(row, dict) or set(row) != {"path", "before_sha256", "after_sha256", "reason"} for row in inventory)
            or {row["path"] for row in inventory} != {str(path) for path in mandatory + optional}):
        raise BridgeRebankUnavailable("prior adoption configuration inventory changed")
    portable_digest = _digest(_encoded(_config(_portable_command(home))))
    present = set()
    for row in inventory:
        if row["before_sha256"] is not None and (not isinstance(row["before_sha256"], str) or _DIGEST.fullmatch(row["before_sha256"]) is None):
            raise BridgeRebankUnavailable("prior adoption configuration preimage changed")
        if row["after_sha256"] is None:
            if Path(row["path"]) not in optional or row["before_sha256"] is not None or row["reason"] != "declared optional config is absent":
                raise BridgeRebankUnavailable("prior adoption optional absence changed")
        elif row["after_sha256"] != portable_digest or row["reason"] is not None:
            raise BridgeRebankUnavailable("prior adoption configuration output changed")
        else:
            present.add(row["path"])
    if (not isinstance(plan["configs"], list) or len(plan["configs"]) != len(present)
            or {row.get("path") for row in plan["configs"] if isinstance(row, dict)} != present):
        raise BridgeRebankUnavailable("prior adoption configuration outputs changed")
    for row in plan["configs"]:
        if (set(row) != {"path", "before_sha256", "after_sha256", "after_command"}
                or row["after_command"] != _portable_command(home) or row["after_sha256"] != portable_digest
                or not isinstance(row["before_sha256"], str) or _DIGEST.fullmatch(row["before_sha256"]) is None
                or row["before_sha256"] != next(item["before_sha256"] for item in inventory if item["path"] == row["path"])):
            raise BridgeRebankUnavailable("prior adoption command or digest changed")
    stale = home / ".local/share/floati/hooks/stop-hook-bridge.py"
    stale_digest = "ff044a1e1ef7a53da79898a3f03812b6dd584ef880dd913c6b5e77dfc8925655"
    removals = [{"path": str(stale), "before_sha256": stale_digest, "after_sha256": None},
                {"path": str(stale.with_suffix(".sha256")), "before_sha256": _digest((stale_digest + "\n").encode()), "after_sha256": None}]
    if plan["removals"] not in ([], removals):
        raise BridgeRebankUnavailable("prior adoption cleanup coordinates or preimages changed")


def _receipts(home, receipt_digest=None):
    directory = _store(home) / "receipts"
    _ordinary(directory, directory=True)
    paths = [directory / (receipt_digest + ".json")] if receipt_digest is not None else sorted(directory.iterdir())
    if len(paths) > 256:
        raise BridgeRebankUnavailable(f"bridge receipt inventory exceeds bound: {directory}")
    result = []
    for path in paths:
        if path.suffix != ".json" or _DIGEST.fullmatch(path.stem) is None:
            raise BridgeRebankUnavailable(f"unknown bridge receipt coordinate: {path}")
        raw = _read(path, 4 * 1024 * 1024)
        record = _json(raw, path)
        plan = record.get("plan") if isinstance(record, dict) else None
        if (not isinstance(plan, dict) or plan.get("plan_digest") != path.stem
                or _record_digest(plan) != path.stem):
            raise BridgeRebankUnavailable(f"bridge receipt plan digest changed: {path}")
        result.append((path, raw, record))
    return result


def _prior(home, *, selector_raw=None, receipt_digest=None):
    store = _store(home)
    selected = _read(store / "active.json", 4096) if selector_raw is None else selector_raw
    selector = _json(selected, store / "active.json")
    if (not isinstance(selector, dict) or set(selector) != {"schema_version", "bank_sha256", "launcher_sha256"}
            or type(selector["schema_version"]) is not int or selector["schema_version"] != 0
            or not isinstance(selector["launcher_sha256"], str)
            or not isinstance(selector["bank_sha256"], str) or _DIGEST.fullmatch(selector["bank_sha256"]) is None):
        raise BridgeRebankUnavailable("selected prior bridge selector is malformed")
    _provider_generation(selector["launcher_sha256"])
    bank = store / "banks" / selector["bank_sha256"]
    if receipt_digest is None:
        receipt_digest, _index_raw = _index(home)
    candidates = []
    for path, raw, record in _receipts(home, receipt_digest):
        plan = record["plan"]
        if receipt_digest is not None and path.stem != receipt_digest:
            continue
        if plan.get("bank_root") != str(bank) or receipt_digest is None and plan.get("noop") is True:
            continue
        if record.get("status") != "complete" or record.get("remaining") != []:
            continue
        if record.get("kind") == KIND:
            _validate_rebank_record(home, record)
        else:
            _validate_adoption_plan(home, plan, bank, selected)
            expected_keys = {"schema_version", "status", "plan", "completed", "remaining"}
            actions = [{"path": str(bank), "kind": "bank", "after_sha256": bank.name}]
            for name in ("entrypoint", "selector"):
                row = plan.get(name, {})
                actions.append({"path": row.get("path"), "kind": name, "after_sha256": row.get("after_sha256")})
            actions += [{"path": row.get("path"), "kind": "config", "after_sha256": row.get("after_sha256")}
                        for row in plan.get("configs", [])]
            actions += [{"path": row.get("path"), "kind": "remove", "after_sha256": None}
                        for row in plan.get("removals", [])]
            if (set(record) != expected_keys or type(record["schema_version"]) is not int
                    or record["schema_version"] != 0 or record["completed"] != actions):
                raise BridgeRebankUnavailable(f"prior adoption receipt shape changed: {path}")
        output = plan.get("selector", {})
        if (output.get("path") != str(store / "active.json")
                or output.get("after_sha256") != _digest(selected)):
            raise BridgeRebankUnavailable(f"prior selector output differs from receipt: {path}")
        source_sha, manifest_sha = plan.get("source_sha"), plan.get("manifest_sha256")
        if (not isinstance(source_sha, str) or _SHA.fullmatch(source_sha) is None
                or not isinstance(manifest_sha, str) or _DIGEST.fullmatch(manifest_sha) is None):
            raise BridgeRebankUnavailable(f"prior source identity is malformed: {path}")
        prior = _verify_store(home, prior_identity={"source_sha": source_sha,
            "manifest_sha256": manifest_sha, "bank_sha256": bank.name}, selector_payload=selected)
        if plan["registry_sha256"] != prior["manifest"]["registry_sha256"]:
            raise BridgeRebankUnavailable("prior bank registry testimony differs from its receipt")
        mandatory, optional = _config_paths(home)
        inventory = plan.get("inventory")
        if (not isinstance(inventory, list) or len(inventory) != 12
                or {row.get("path") for row in inventory if isinstance(row, dict)}
                   != {str(path) for path in mandatory + optional}):
            raise BridgeRebankUnavailable(f"prior configuration inventory is not the fixed twelve: {path}")
        candidates.append((path, raw, record, prior))
    if len(candidates) != 1:
        raise BridgeRebankUnavailable(f"selected bank needs exactly one complete prior receipt; observed={len(candidates)} bank={bank}")
    return candidates[0]


def _capture_transport(home):
    path = home / ".codex/fleet-bus-profiles.json"
    raw = _read(path, 1024 * 1024)
    registry = _json(raw, path)
    try:
        source_sha = registry["transports"]["floati-installed-v0"]["source_sha"]
    except (KeyError, TypeError) as exc:
        raise BridgeRebankUnavailable(f"fixed installed transport is absent: {path}") from exc
    metadata, transport = _installed_manifest(home, source_sha)
    document = _json(metadata, home / ".local/share/floati/.floati-install/manifest.v0.json")
    files = {"runtime/.floati-install/manifest.v0.json": metadata}
    checks = {path: raw, home / ".local/share/floati/.floati-install/manifest.v0.json": metadata}
    total = 0
    for row in document["files"]:
        if (not isinstance(row, dict) or set(row) != {"path", "sha256"}
                or not isinstance(row["path"], str) or not row["path"]
                or not isinstance(row["sha256"], str) or _DIGEST.fullmatch(row["sha256"]) is None):
            raise BridgeRebankUnavailable("fixed runtime inventory is malformed")
        relative = PurePosixPath(row["path"])
        name = "runtime/" + row["path"]
        if (relative.is_absolute() or ".." in relative.parts or str(relative) != row["path"] or name in files):
            raise BridgeRebankUnavailable("fixed runtime inventory has unsafe or duplicate paths")
        source = home / ".local/share/floati" / relative
        # Installed public files may be executable; bank copies are always 0600.
        payload = _read(source, transport["max_file_bytes"], private_file=False)
        if _digest(payload) != row["sha256"]:
            raise BridgeRebankUnavailable(f"digest changed: {source} expected={row['sha256']} observed={_digest(payload)}")
        total += len(payload)
        if total > transport["max_total_bytes"]:
            raise BridgeRebankUnavailable("installed runtime exceeds total transport bound")
        files[name], checks[source] = payload, payload
    if not {"runtime/floati/records.py", "runtime/floati/__init__.py"}.issubset(files):
        raise BridgeRebankUnavailable("fixed runtime lacks its reader package")
    _reader_version(files["runtime/floati/records.py"], Path("floati/records.py"))
    if _read(path, 1024 * 1024) != raw:
        raise BridgeRebankUnavailable("fixed registry changed during capture")
    return source_sha, files, checks, raw, metadata


@dataclass
class PreparedBridgeRebank:
    home: Path
    plan: dict
    files: dict
    manifest: bytes
    checks: dict
    runtime_checks: dict
    configuration_outputs: dict


@dataclass
class CapturedBridgeUpdate:
    home: Path
    plan: dict
    checks: dict


def capture_bridge_update(home):
    """Read-only preflight before native writes, including repin recovery.

An incomplete earlier refresh must be replayed explicitly. A completed prior
bank remains a valid preflight witness even after a prior native repin failed.
"""
    try:
        home = Path(home)
        _ordinary(home, directory=True)
        store = _store(home)
        if not store.exists() and not store.is_symlink():
            _optional_read(store / "launcher.py")
            return CapturedBridgeUpdate(home, {"reason": "store_absent"}, {})
        pending_digest, pending_raw = _index(home, "pending-rebank.json")
        if pending_digest is not None:
            _path, _raw, pending = _receipts(home, pending_digest)[0]
            _validate_rebank_record(home, pending)
            if pending["status"] != "complete":
                raise BridgeRebankUnavailable(f"replay pending governed re-bank plan {pending_digest} before another update", "bridge_rebank_recovery_required", pending)
            if _index(home)[0] != pending_digest:
                raise BridgeRebankUnavailable("completed pending receipt differs from the active receipt index")
        path, raw, record, prior = _prior(home)
        checks = {path: raw, store / "active.json": prior["selector_bytes"],
                  store / "launcher.py": prior["launcher_bytes"],
                  prior["bank"] / "bank-manifest.json": prior["manifest_bytes"],
                  store / "active-receipt.json": _index(home)[1],
                  store / "pending-rebank.json": pending_raw}
        checks.update({prior["bank"] / name: payload for name, payload in prior["files"].items()})
        mandatory, optional = _config_paths(home)
        inventory = {row["path"]: row for row in record["plan"]["inventory"]}
        for coordinate in mandatory + optional:
            payload = _configuration_read(home, coordinate)
            if payload is None:
                if coordinate not in optional or inventory[str(coordinate)]["after_sha256"] is not None:
                    raise BridgeRebankUnavailable(f"adopted configuration is absent: {coordinate}")
            elif _json(payload, coordinate) != _config(_portable_command(home)):
                raise BridgeRebankUnavailable(f"configuration must select the adopted portable bank before automatic update: {coordinate}")
            elif _digest(payload) != inventory[str(coordinate)]["after_sha256"]:
                raise BridgeRebankUnavailable(f"configuration differs from the receipted adopted output: {coordinate}")
            checks[coordinate] = payload
        return CapturedBridgeUpdate(home, {"reason": "adopted"}, checks)
    except BridgeRebankUnavailable:
        raise
    except (BridgeBankUnavailable, OSError, ValueError, TypeError, KeyError) as exc:
        raise BridgeRebankUnavailable(str(exc)) from exc


def refuse_adopted_bridge_fleet_update(destination, transport):
    """Fleet G2/G3 have no joined re-bank step; refuse this target before writes."""
    home = Path.home()
    store = _store(home)
    if (Path(destination).resolve() == (home / ".local/share/floati").resolve()
            and (store.exists() or store.is_symlink())):
        raise BridgeRebankUnavailable(f"fleet update transport {transport} targets the adopted fixed bridge install; use ordinary profile-bound update with --profile-registry and --fleet-profile", "bridge_rebank_fleet_update_unsupported")


def _prepare(home, restore_configurations):
    home = Path(home)
    _ordinary(home, directory=True)
    if type(restore_configurations) is not bool:
        raise BridgeRebankUnavailable("restore_configurations must be a boolean")
    store = _store(home)
    if not store.exists() and not store.is_symlink():
        plan = {"schema_version": 1, "kind": KIND, "home": str(home), "noop": True,
                "reason": "store_absent", "restore_configurations": restore_configurations}
        plan["plan_digest"] = _record_digest(plan)
        return PreparedBridgeRebank(home, plan, {}, b"", {}, {}, {})
    selector_now = _read(store / "active.json", 4096)
    pending_digest, pending_raw = _index(home, "pending-rebank.json")
    if pending_digest is not None:
        _pending_path, _raw, pending = _receipts(home, pending_digest)[0]
        _validate_rebank_record(home, pending)
        if pending["status"] != "complete":
            if _digest(selector_now) not in (pending["plan"]["selector"]["before_sha256"], pending["plan"]["selector"]["after_sha256"]):
                raise BridgeRebankUnavailable("pending re-bank selector has drifted from both planned states")
            if not restore_configurations and pending["plan"]["restore_configurations"]:
                raise BridgeRebankUnavailable(f"pending manual re-bank {pending_digest} requires explicit manual replay; automatic update cannot restore configurations", "bridge_rebank_manual_recovery_required")
            return _restore(home, pending["plan"])
        if _index(home)[0] != pending_digest:
            raise BridgeRebankUnavailable("completed pending receipt differs from the active receipt index")
    prior_path, prior_raw, prior_record, prior = _prior(home)
    _index_digest, index_raw = _index(home)
    source_sha, files, runtime_checks, registry_raw, metadata = _capture_transport(home)
    for relative in ("launcher.py", "hooks/stop-hook-bridge.py"):
        files[relative] = prior["files"][relative]
    manifest = _encoded({"schema_version": 0, "source_sha": source_sha,
        "registry_sha256": _digest(registry_raw), "transport_manifest_sha256": _digest(metadata),
        "files": [{"path": name, "sha256": _digest(payload), "mode": 0o600}
                  for name, payload in sorted(files.items())]})
    bank = store / "banks" / _digest(manifest)
    checks = {store / "active.json": prior["selector_bytes"], store / "launcher.py": prior["launcher_bytes"],
              prior_path: prior_raw, prior["bank"] / "bank-manifest.json": prior["manifest_bytes"]}
    checks.update({prior["bank"] / name: payload for name, payload in prior["files"].items()})
    checks[store / "active-receipt.json"] = index_raw
    checks[store / "pending-rebank.json"] = pending_raw
    mandatory, optional = _config_paths(home)
    inventory, outputs = [], {}
    portable, legacy = _config(_portable_command(home)), _config(_legacy_command(home))
    original_inventory = {row["path"]: row for row in prior_record["plan"]["inventory"]}
    for path in mandatory + optional:
        before = _configuration_read(home, path)
        if before is None:
            if path not in optional or original_inventory[str(path)]["after_sha256"] is not None:
                raise BridgeRebankUnavailable(f"receipted configuration is missing: {path}")
            after = None
        else:
            document = _json(before, path)
            if document == portable:
                after = before
            elif restore_configurations and document == legacy:
                after = _encoded(portable)
                outputs[path] = after
            else:
                raise BridgeRebankUnavailable(f"configuration does not select the adopted portable bank: {path}")
        checks[path] = before
        inventory.append({"path": str(path), "before_sha256": None if before is None else _digest(before),
                          "after_sha256": None if after is None else _digest(after),
                          "reason": "declared optional config is absent" if before is None else None})
    plan = {"schema_version": 1, "kind": KIND, "home": str(home), "source_sha": source_sha,
        "manifest_sha256": _digest(metadata), "registry_sha256": _digest(registry_raw), "bank_root": str(bank),
        "prior_receipt": {"path": str(prior_path), "sha256": _digest(prior_raw)},
        "previous": {"bank_root": str(prior["bank"]), "source_sha": prior["source_sha"],
                     "manifest_sha256": prior["manifest"]["transport_manifest_sha256"]},
        "previous_selector": _json(prior["selector_bytes"], store / "active.json"),
        "selector": {"path": str(store / "active.json"), "before_sha256": _digest(prior["selector_bytes"]),
                     "after_sha256": _digest(_selector(bank, launcher_sha256=_digest(prior["launcher_bytes"])))},
        "receipt_index": {"path": str(store / "active-receipt.json"),
                          "before_sha256": None if index_raw is None else _digest(index_raw)},
        "previous_receipt_index": None if index_raw is None else json.loads(index_raw),
        "inventory": inventory, "restore_configurations": restore_configurations,
        "files": json.loads(manifest)["files"], "noop": bank == prior["bank"] and not outputs}
    plan["plan_digest"] = _record_digest(plan)
    return PreparedBridgeRebank(home, plan, files, manifest, checks, runtime_checks, outputs)


def prepare_bridge_rebank(home, *, restore_configurations=False):
    try:
        return _prepare(home, restore_configurations)
    except BridgeRebankUnavailable:
        raise
    except (BridgeBankUnavailable, OSError, ValueError, TypeError, KeyError) as exc:
        raise BridgeRebankUnavailable(str(exc)) from exc


def revalidate_bridge_rebank(prepared):
    """Recheck unchanged bridge inputs after native install, before refresh."""
    if prepared.plan.get("reason") == "store_absent":
        if _store(prepared.home).exists() or _store(prepared.home).is_symlink():
            raise BridgeRebankUnavailable("bridge store appeared during update")
        return
    configurations = sum(_config_paths(prepared.home), [])
    for path, expected in prepared.checks.items():
        if _optional_read(path, 64 * 1024 * 1024, private_file=path not in configurations) != expected:
            raise BridgeRebankUnavailable(f"captured bridge preimage changed: {path}")


def _actions(plan):
    if plan.get("reason") == "store_absent":
        return []
    result = [] if plan.get("noop") is True else [{"path": plan["bank_root"], "kind": "bank", "after_sha256": Path(plan["bank_root"]).name},
              {"path": plan["selector"]["path"], "kind": "selector", "after_sha256": plan["selector"]["after_sha256"]}]
    return result + [{"path": row["path"], "kind": "config", "after_sha256": row["after_sha256"]}
                     for row in plan["inventory"] if row["before_sha256"] != row["after_sha256"]] + [
        {"path": plan["receipt_index"]["path"], "kind": "receipt_index", "after_sha256": _digest(_index_payload(plan["plan_digest"]))}]


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic(path, payload, *, private_file=True):
    _ordinary(path.parent, directory=True)
    if path.exists() or path.is_symlink():
        _ordinary(path, private_file=private_file)
    descriptor, temporary = tempfile.mkstemp(prefix=".rebank-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
        if _read(path, private_file=private_file) != payload:
            raise BridgeRebankUnavailable(f"re-bank output readback changed: {path}")
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _apply_lock(home):
    parent = home / ".local/share"
    _ordinary(parent, directory=True)
    path = parent / ".floati-zcode-bridge-apply.lock"
    if path.exists() or path.is_symlink():
        _ordinary(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        item = os.fstat(descriptor)
        if not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid() or stat.S_IMODE(item.st_mode) != 0o600:
            raise BridgeRebankUnavailable("shared bridge apply lock is not ordinary")
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BridgeRebankUnavailable("another bridge writer owns the bounded shared lock", "bridge_rebank_busy")
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def _restore(home, plan):
    _validate_plan(home, plan)
    previous_selector = _encoded(plan["previous_selector"])
    prior_name = Path(plan["prior_receipt"]["path"]).stem
    if (not isinstance(prior_name, str) or _DIGEST.fullmatch(prior_name) is None
            or plan["prior_receipt"]["path"] != str(_store(home) / "receipts" / (prior_name + ".json"))):
        raise BridgeRebankUnavailable("re-bank prior receipt coordinate changed")
    prior_path, prior_raw, _record, prior = _prior(home, selector_raw=previous_selector, receipt_digest=prior_name)
    if plan["prior_receipt"] != {"path": str(prior_path), "sha256": _digest(prior_raw)}:
        raise BridgeRebankUnavailable("re-bank prior receipt changed")
    if plan["previous"] != {"bank_root": str(prior["bank"]), "source_sha": prior["source_sha"],
                           "manifest_sha256": prior["manifest"]["transport_manifest_sha256"]}:
        raise BridgeRebankUnavailable("re-bank prior bank identity differs from its receipt")
    source_sha, files, runtime_checks, registry_raw, metadata = _capture_transport(home)
    if (source_sha != plan["source_sha"] or _digest(metadata) != plan["manifest_sha256"]
            or _digest(registry_raw) != plan["registry_sha256"]):
        raise BridgeRebankUnavailable("re-bank recovery transport differs from the receipted target")
    for relative in ("launcher.py", "hooks/stop-hook-bridge.py"):
        files[relative] = prior["files"][relative]
    manifest = _encoded({"schema_version": 0, "source_sha": source_sha,
        "registry_sha256": plan["registry_sha256"], "transport_manifest_sha256": _digest(metadata),
        "files": [{"path": name, "sha256": _digest(payload), "mode": 0o600}
                  for name, payload in sorted(files.items())]})
    if (plan["bank_root"] != str(_store(home) / "banks" / _digest(manifest))
            or plan["files"] != json.loads(manifest)["files"]
            or plan["selector"] != {"path": str(_store(home) / "active.json"),
                "before_sha256": _digest(previous_selector),
                "after_sha256": _digest(_selector(Path(plan["bank_root"]), launcher_sha256=plan["previous_selector"]["launcher_sha256"]))}):
        raise BridgeRebankUnavailable("re-bank recovery target bank or selector changed")
    current_selector = _read(_store(home) / "active.json", 4096)
    if _digest(current_selector) not in (plan["selector"]["before_sha256"], plan["selector"]["after_sha256"]):
        raise BridgeRebankUnavailable("re-bank recovery selector differs from both planned states")
    checks = {prior_path: prior_raw, _store(home) / "active.json": current_selector,
              _store(home) / "launcher.py": prior["launcher_bytes"],
              prior["bank"] / "bank-manifest.json": prior["manifest_bytes"]}
    checks.update({prior["bank"] / name: payload for name, payload in prior["files"].items()})
    index_path = _store(home) / "active-receipt.json"
    previous_index = plan["previous_receipt_index"]
    before_index = None if previous_index is None else _encoded(previous_index)
    if (plan["receipt_index"] != {"path": str(index_path), "before_sha256": None if before_index is None else _digest(before_index)}
            or previous_index is not None and before_index != _index_payload(prior_name)):
        raise BridgeRebankUnavailable("re-bank prior receipt index changed")
    _current_digest, index_raw = _index(home)
    if index_raw not in (before_index, _index_payload(plan["plan_digest"])):
        raise BridgeRebankUnavailable("re-bank receipt index drifted from both planned states")
    checks[index_path] = index_raw
    _pending_digest, pending_raw = _index(home, "pending-rebank.json")
    if pending_raw not in (None, _index_payload(prior_name), _index_payload(plan["plan_digest"])):
        raise BridgeRebankUnavailable("re-bank pending receipt pointer changed")
    checks[_store(home) / "pending-rebank.json"] = pending_raw
    mandatory, optional = _config_paths(home)
    rows = plan["inventory"]
    if len(rows) != 12 or {row["path"] for row in rows} != {str(path) for path in mandatory + optional}:
        raise BridgeRebankUnavailable("re-bank recovery configuration inventory changed")
    outputs = {}
    for row in rows:
        path = Path(row["path"])
        current = _configuration_read(home, path)
        observed = None if current is None else _digest(current)
        if observed not in (row["before_sha256"], row["after_sha256"]):
            raise BridgeRebankUnavailable(f"re-bank recovery configuration drift: {path}")
        if current is None:
            if path not in optional or row["before_sha256"] is not None or row["after_sha256"] is not None:
                raise BridgeRebankUnavailable(f"re-bank recovery has false configuration absence: {path}")
        else:
            document = _json(current, path)
            allowed = [_config(_portable_command(home))]
            if plan["restore_configurations"] is True:
                allowed.append(_config(_legacy_command(home)))
            if document not in allowed:
                raise BridgeRebankUnavailable(f"re-bank recovery configuration shape changed: {path}")
        if row["before_sha256"] != row["after_sha256"]:
            output = _encoded(_config(_portable_command(home)))
            if not plan["restore_configurations"] or _digest(output) != row["after_sha256"]:
                raise BridgeRebankUnavailable(f"re-bank recovery configuration output changed: {path}")
            outputs[path] = output
        checks[path] = current
    return PreparedBridgeRebank(home, plan, files, manifest, checks, runtime_checks, outputs)


def _fault(_phase):
    """Native tests inject failures at actual durable publication boundaries."""


def _validate_capture(prepared, plan_digest):
    if (not isinstance(prepared, PreparedBridgeRebank) or prepared.plan.get("plan_digest") != plan_digest
            or _record_digest(prepared.plan) != plan_digest or prepared.plan.get("home") != str(prepared.home)):
        raise BridgeRebankUnavailable("apply requires the unchanged exact re-bank plan digest")
    _validate_plan(prepared.home, prepared.plan)
    if prepared.plan.get("reason") == "store_absent":
        return
    restored = _restore(prepared.home, prepared.plan)
    if (prepared.files != restored.files or prepared.manifest != restored.manifest
            or prepared.runtime_checks != restored.runtime_checks
            or set(prepared.checks) != set(restored.checks)):
        raise BridgeRebankUnavailable("captured re-bank closure differs from its receipted plan")
    for path, expected in restored.checks.items():
        if path not in prepared.checks or prepared.checks[path] != expected:
            raise BridgeRebankUnavailable(f"captured re-bank preimage differs: {path}")
    if prepared.configuration_outputs != restored.configuration_outputs:
        raise BridgeRebankUnavailable("captured configuration outputs differ from the plan")


def apply_bridge_rebank(prepared, *, plan_digest):
    try:
        if isinstance(prepared, PreparedBridgeRebank) and prepared.plan.get("plan_digest") == plan_digest:
            _validate_plan(prepared.home, prepared.plan)
            existing_path = _store(prepared.home) / "receipts" / (plan_digest + ".json")
            existing_raw = _optional_read(existing_path)
            if existing_raw is not None:
                existing_record = _json(existing_raw, existing_path)
                _validate_rebank_record(prepared.home, existing_record)
                if existing_record["plan"] != prepared.plan:
                    raise BridgeRebankUnavailable("existing receipt differs from the exact prepared plan")
                if existing_record["status"] == "complete":
                    return replay_bridge_rebank(prepared.home, plan_digest=plan_digest)
        _validate_capture(prepared, plan_digest)
        plan, home = prepared.plan, prepared.home
        if plan.get("reason") == "store_absent":
            revalidate_bridge_rebank(prepared)
            return {"schema_version": 1, "kind": KIND, "status": "complete", "plan": plan,
                    "completed": [], "remaining": []}
        path = _store(home) / "receipts" / (plan_digest + ".json")
        actions, completed = _actions(plan), []
        with _apply_lock(home):
            _validate_capture(prepared, plan_digest)
            for coordinate, expected in prepared.runtime_checks.items():
                private = coordinate in {home / ".codex/fleet-bus-profiles.json",
                    home / ".local/share/floati/.floati-install/manifest.v0.json"}
                if _read(coordinate, private_file=private) != expected:
                    raise BridgeRebankUnavailable(f"captured new transport changed: {coordinate}")
            selected = _read(_store(home) / "active.json", 4096)
            if _digest(selected) not in (plan["selector"]["before_sha256"], plan["selector"]["after_sha256"]):
                raise BridgeRebankUnavailable("selected bridge changed before compare-and-replace")
            existing = _optional_read(path)
            if existing is not None:
                record = _json(existing, path)
                _validate_rebank_record(home, record)
                if record.get("plan") != plan:
                    raise BridgeRebankUnavailable("existing re-bank receipt differs from the exact plan")
                if record.get("status") == "complete":
                    _verify_post(home, plan)
                    return record
            try:
                intent = {"schema_version": 1, "kind": KIND, "status": "intent", "plan": plan,
                          "completed": [], "remaining": actions}
                _atomic(path, _encoded(intent))
                pending_path = _store(home) / "pending-rebank.json"
                if _optional_read(pending_path, 4096) != prepared.checks[pending_path]:
                    raise BridgeRebankUnavailable("pending receipt pointer changed before replacement")
                _atomic(pending_path, _index_payload(plan_digest))
                _fault("after_intent")
                bank = Path(plan["bank_root"])
                if bank.exists() or bank.is_symlink():
                    _verify_store(home, selector_payload=_selector(bank, launcher_sha256=plan["previous_selector"]["launcher_sha256"]))
                    _sync(bank.parent)
                else:
                    _ordinary(bank.parent, directory=True)
                    staging = Path(tempfile.mkdtemp(prefix=".rebank-", dir=bank.parent))
                    try:
                        for relative, payload in {**prepared.files, "bank-manifest.json": prepared.manifest}.items():
                            target = staging / relative
                            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            _atomic(target, payload)
                        for relative, payload in {**prepared.files, "bank-manifest.json": prepared.manifest}.items():
                            if _read(staging / relative) != payload:
                                raise BridgeRebankUnavailable(f"staged bank readback differs: {relative}")
                        for directory in sorted((path for path in staging.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
                            _sync(directory)
                        _sync(staging)
                        os.rename(staging, bank)
                        _sync(bank.parent)
                    finally:
                        if staging.exists():
                            shutil.rmtree(staging)
                    _verify_store(home, selector_payload=_selector(bank, launcher_sha256=plan["previous_selector"]["launcher_sha256"]))
                if not plan["noop"]:
                    completed.append(actions[0])
                _fault("after_bank")
                if _read(_store(home) / "active.json", 4096) != selected:
                    raise BridgeRebankUnavailable("selector changed before replacement")
                if not plan["noop"]:
                    _atomic(_store(home) / "active.json", _selector(bank, launcher_sha256=plan["previous_selector"]["launcher_sha256"]))
                    completed.append(actions[1])
                _fault("after_selector")
                for coordinate, payload in prepared.configuration_outputs.items():
                    if _configuration_read(home, coordinate) != prepared.checks[coordinate]:
                        raise BridgeRebankUnavailable(f"configuration changed before replacement: {coordinate}")
                    _atomic(coordinate, payload, private_file=False)
                    completed.append(next(action for action in actions if action["path"] == str(coordinate)))
                _verify_post(home, plan, check_index=False)
                pending = {"schema_version": 1, "kind": KIND, "status": "partial", "plan": plan,
                           "completed": completed, "remaining": [actions[-1]]}
                _atomic(path, _encoded(pending))
                _fault("before_receipt_index")
                index_path = _store(home) / "active-receipt.json"
                if _optional_read(index_path, 4096) != prepared.checks[index_path]:
                    raise BridgeRebankUnavailable("receipt index changed before replacement")
                _atomic(index_path, _index_payload(plan_digest))
                completed.append(actions[-1])
                _fault("after_receipt_index")
                _verify_post(home, plan)
                receipt = {"schema_version": 1, "kind": KIND, "status": "complete", "plan": plan,
                           "completed": actions, "remaining": []}
                _atomic(path, _encoded(receipt))
                _fault("after_complete_receipt")
                return receipt
            except Exception as exc:
                try:
                    observed = _digest(_read(_store(home) / "active.json", 4096))
                except (OSError, ValueError):
                    observed = None
                partial = {"schema_version": 1, "kind": KIND, "status": "partial", "plan": plan,
                           "completed": completed, "remaining": actions[len(completed):], "detail": str(exc),
                           "observed_selector_sha256": observed}
                try:
                    _atomic(path, _encoded(partial))
                except (OSError, ValueError, BridgeRebankUnavailable) as persistence:
                    # Report only the receipt that can actually be read back.
                    # A durable intent still authorizes exact bounded recovery.
                    try:
                        durable = _json(_read(path, 4 * 1024 * 1024), path)
                        _validate_rebank_record(home, durable)
                        partial = durable if durable["plan"] == plan else None
                    except (OSError, ValueError, BridgeRebankUnavailable):
                        partial = None
                    raise BridgeRebankUnavailable(str(exc) + "; partial receipt persistence failed: " + str(persistence),
                                                 "bridge_rebank_partial", partial) from exc
                raise BridgeRebankUnavailable(str(exc), "bridge_rebank_partial", partial) from exc
    except BridgeRebankUnavailable:
        raise
    except (BridgeBankUnavailable, OSError, ValueError, TypeError, KeyError) as exc:
        raise BridgeRebankUnavailable(str(exc)) from exc


def _verify_post(home, plan, *, check_index=True):
    verified = verify_bridge_store(home)
    if (str(verified["bank"]) != plan["bank_root"] or verified["source_sha"] != plan["source_sha"]
            or _digest(_read(_store(home) / "active.json", 4096)) != plan["selector"]["after_sha256"]):
        raise BridgeRebankUnavailable("re-bank post-state does not match the receipted target")
    if check_index and _index(home)[0] != plan["plan_digest"]:
        raise BridgeRebankUnavailable("re-bank current receipt index does not select this plan")
    for row in plan["inventory"]:
        payload = _configuration_read(home, Path(row["path"]))
        if (None if payload is None else _digest(payload)) != row["after_sha256"]:
            raise BridgeRebankUnavailable(f"configuration post-state differs: {row['path']}")
        if payload is not None and _json(payload, row["path"]) != _config(_portable_command(home)):
            raise BridgeRebankUnavailable(f"configuration post-state command changed: {row['path']}")


def replay_bridge_rebank(home, *, plan_digest):
    try:
        home = Path(home)
        if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
            raise BridgeRebankUnavailable("re-bank replay requires one exact plan digest")
        path = _store(home) / "receipts" / (plan_digest + ".json")
        payload = _optional_read(path)
        if payload is None:
            return None
        record = _json(payload, path)
        if not isinstance(record, dict) or record.get("kind") != KIND:
            return None
        _validate_rebank_record(home, record)
        plan = record["plan"]
        if plan.get("plan_digest") != plan_digest or _record_digest(plan) != plan_digest:
            raise BridgeRebankUnavailable("re-bank replay receipt digest changed")
        if record.get("status") == "complete":
            with _apply_lock(home):
                _verify_post(home, plan)
                if record["completed"] != _actions(plan) or record["remaining"] != []:
                    raise BridgeRebankUnavailable("complete re-bank receipt action closure changed")
            return record
        prepared = _restore(home, plan)
        return apply_bridge_rebank(prepared, plan_digest=plan_digest)
    except BridgeRebankUnavailable:
        raise
    except (BridgeBankUnavailable, OSError, ValueError, TypeError, KeyError) as exc:
        raise BridgeRebankUnavailable(str(exc)) from exc
