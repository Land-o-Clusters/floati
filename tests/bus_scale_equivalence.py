"""Audited artifact equivalence for private bus-scale diagnostic children.

This helper measures nothing and never calls a CLI, Doctor, subprocess, clock
patch, or writer. Native read projections prove the recorded own-clock facts;
only proved clock derivatives are then projected at the caller's retained CPU
observation. Raw artifacts and every non-normalized field remain testimony.
Unsupported artifacts fail closed instead of inheriting a generic scrub rule.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat

from floati.artifact_subject import artifact_subject
from floati.bus_epoch import ledger_roll_policy
from floati.command_scope import CommandScope
from floati.codex_wait_contract import mapped_workspaces
from floati.delivery_health import DeliveryHealthAnalyzer
from floati.doctor_disk_watch import FLEET_DISK_FLOOR_BYTES, _fleet_trees, _disk_watch_finding
from floati.doctor import (
    _codex_home_path, _finding, _role_bindings, project_codex_wait_window_findings,
)
from floati.codex_hook_trust import observe_codex_waiter_hooks
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_health import WakeHealthProjection


_ROOT = Path(__file__).resolve().parents[1]
_UTC = timezone.utc
_FIXTURE = "<bus-scale-fixture>"


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("artifact must contain finite JSON data") from exc


def _same(left, right):
    return _json(left) == _json(right)


def _time(value, label):
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError(label + " must be an aware datetime")
    return value.astimezone(_UTC)


def _parse(value, label):
    if type(value) is not str:
        raise ValueError(label + " must be a stamped UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(label + " has an invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(label + " must be UTC")
    return parsed.astimezone(_UTC)


def _bound(moment, begin, end, *, precision=0):
    # Native stamps truncate to seconds/milliseconds. The exact recent-session
    # window retains microseconds and receives no rounding allowance.
    if not begin - timedelta(seconds=precision) <= moment <= end:
        raise ValueError("artifact observation lies outside retained invocation bounds")


def _check(actual, expected, label):
    """Validate every native field, retaining unrelated extension fields."""
    if type(expected) is dict:
        if type(actual) is not dict or not set(expected) <= set(actual):
            raise ValueError(label + " is missing native projection fields")
        for key, value in expected.items():
            _check(actual[key], value, label + "/" + str(key))
    elif type(expected) is list:
        if type(actual) is not list or len(actual) != len(expected):
            raise ValueError(label + " has different native membership")
        for index, value in enumerate(expected):
            _check(actual[index], value, label + "/" + str(index))
    elif not _same(actual, expected):
        raise ValueError(label + " differs from its native own-clock projection")


def _pointer(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _changed(actual, expected, pointer, allowed, notes, reason, path=()):
    """Apply only explicit changed leaves; extensions are never regenerated."""
    if type(expected) is dict:
        for key, value in expected.items():
            current = actual[key]
            if type(value) in (dict, list):
                _changed(current, value, _pointer(pointer, key), allowed, notes, reason, path + (key,))
            elif not _same(current, value):
                if path + (key,) not in allowed:
                    raise ValueError("canonical time changes a semantic fact: " + _pointer(pointer, key))
                actual[key] = copy.deepcopy(value)
                notes.append({"pointer": _pointer(pointer, key), "reason": reason})
    elif type(expected) is list:
        if len(actual) != len(expected):
            raise ValueError("canonical time changes native membership: " + pointer)
        for index, value in enumerate(expected):
            if type(value) in (dict, list):
                _changed(actual[index], value, _pointer(pointer, index), allowed, notes, reason, path + (index,))
            elif not _same(actual[index], value):
                raise ValueError("canonical time changes native list values: " + pointer)


_WAKE_CLOCKS = {
    ("observed_at",), ("waiter_receipt_age_minutes",), ("remedy",),
    ("oldest_unread", "observed_at"), ("oldest_unread", "age_minutes"),
    ("recent_sessions", "window_start"), ("recent_sessions", "window_end"),
}
_ATTENTION_CLOCKS = {("observed_at",), ("oldest_waiting_age_seconds",), ("detail",)}


def _transition(actual, own, canonical, pointer, allowed, notes):
    _check(actual, own, pointer)
    _changed(actual, canonical, pointer, allowed, notes,
             "native own-clock fact validated, then projected at retained CPU clock")


def _one(findings, code, subject=None):
    rows = [(index, row) for index, row in enumerate(findings)
            if type(row) is dict and row.get("code") == code
            and (subject is None or row.get("subject") == subject)]
    if len(rows) != 1:
        raise ValueError("expected one native finding: " + code + ":" + str(subject))
    return rows[0]


def _root_coordinates(result, home, notes, *, case):
    evidence = result["evidence"]
    if evidence.get("root") != str(home):
        raise ValueError("artifact root does not name the exact arm fixture")

    def replace(container, key, pointer, *, prose=False):
        value = container.get(key)
        if type(value) is not str:
            return
        original = str(home)
        if prose:
            # A complete coordinate or a descendant, not a similarly prefixed
            # sibling. Only explicitly selected native prose fields use this.
            pattern = re.escape(original) + r"(?=$|[/\s'\"`;)])"
            updated = re.sub(pattern, _FIXTURE, value)
        elif value == original or value.startswith(original + "/"):
            updated = _FIXTURE + value[len(original):]
        elif value == artifact_subject(home) or value.startswith(artifact_subject(home) + "/"):
            subject = artifact_subject(home)
            updated = _FIXTURE + value[len(subject):]
        else:
            return
        if updated != value:
            container[key] = updated
            notes.append({"pointer": pointer, "reason": "exact arm clone coordinate"})

    replace(evidence, "root", "/evidence/root")
    if case == "doctor":
        for index, finding in enumerate(evidence["findings"]):
            prefix = "/evidence/findings/" + str(index)
            replace(finding, "subject", prefix + "/subject")
            # Clock-sensitive remedies have already been proved natively.
            # Other remedy strings retain all tokens except the exact clone.
            replace(finding, "remediation", prefix + "/remediation", prose=True)
            if finding.get("code") == "host_codex_wait_window":
                replace(finding["wait_window"], "wait_deadline_source",
                        prefix + "/wait_window/wait_deadline_source")
            if "wake_health" in finding:
                replace(finding["wake_health"], "remedy", prefix + "/wake_health/remedy", prose=True)
            if finding.get("code") == "host_disk_watch":
                for tree_index, tree in enumerate(finding["disk_watch"]["trees"]):
                    replace(tree, "path", prefix + "/disk_watch/trees/" + str(tree_index) + "/path")
    else:
        if "scope" in evidence:
            if evidence["scope"].get("root") != str(home):
                raise ValueError("status scope names a different root")
            replace(evidence["scope"], "root", "/evidence/scope/root")
        for index, node in enumerate(evidence["nodes"]):
            replace(node["wake_health"], "remedy", "/evidence/nodes/" + str(index) + "/wake_health/remedy", prose=True)


def _doctor_disk(root, workspace, findings, notes):
    index, finding = _one(findings, "host_disk_watch")
    disk = finding.get("disk_watch")
    if type(disk) is not dict:
        raise ValueError("doctor disk observation is missing")
    free, floor = disk.get("free_bytes"), disk.get("floor_bytes")
    if (type(free) is not int or type(floor) is not int
            or floor != FLEET_DISK_FLOOR_BYTES or free < floor
            or disk.get("partial") is not False or type(disk.get("exit")) is not int
            or disk["exit"] != 0 or disk.get("reason") is not None
            or type(disk.get("build_bytes")) is not int or disk["build_bytes"] != 0
            or disk.get("build_names") != []):
        raise ValueError("only measured healthy, complete, build-free disk observations may normalize")
    trees = disk.get("trees")
    if type(trees) is not list or not trees:
        raise ValueError("doctor disk tree observation is unavailable")
    if workspace not in [path for path, _node in mapped_workspaces(root.path)]:
        raise ValueError("declared fixture workspace is not in its native workspace map")
    native_paths = [str(path) for path, _files_only in _fleet_trees(root)]
    if [tree.get("path") for tree in trees if type(tree) is dict] != native_paths:
        raise ValueError("doctor disk trees differ from the native fleet input set")
    for tree in trees:
        if (type(tree) is not dict or tree.get("partial") is not False
                or tree.get("reason") is not None or type(tree.get("build_bytes")) is not int
                or tree["build_bytes"] != 0 or type(tree.get("path")) is not str):
            raise ValueError("doctor disk tree is not a complete build-free measurement")
    expected = _disk_watch_finding(disk, finding["subject"])
    _check(finding, expected, "host_disk_watch")
    prefix = "/evidence/findings/" + str(index)
    finding["disk_watch"]["free_bytes"] = "<measured-healthy-free-bytes>"
    finding["detail"] = (
        "host free <measured-healthy-free-bytes> bytes against fleet floor "
        + str(floor) + " bytes; build output 0 bytes"
    )
    for suffix in ("/disk_watch/free_bytes", "/detail"):
        notes.append({"pointer": prefix + suffix,
                      "reason": "validated host free-space observation; healthy classification retained"})


def _doctor_roll(root, findings, begin, end, canonical, notes):
    index, finding = _one(findings, "ledger_roll_policy")
    path = root.tenant_home / "events.jsonl"
    if not path.exists():
        # No age was measured; no absence or policy text is removed.
        return
    policy = ledger_roll_policy(root)
    with path.open("rb") as stream:
        first = stream.readline()
    try:
        oldest = _parse(json.loads(first)["timestamp"], "first event timestamp")
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("ledger age cannot be independently validated") from exc
    source = "shipped default" if policy["source"] == "shipped_default" else "operator-declared"
    prefix = f"live ledger is {path.stat().st_size} bytes (policy {policy['max_bytes']}, {source}); oldest entry "
    suffix = f" days old (policy {policy['max_age_days']} days)"
    detail = finding.get("detail")
    if type(detail) is not str or not detail.startswith(prefix) or not detail.endswith(suffix):
        raise ValueError("ledger policy prose differs from native inputs")
    token = detail[len(prefix):-len(suffix)]
    if re.fullmatch(r"\d+\.\d", token) is None:
        raise ValueError("ledger age is not the native one-decimal observation")
    low = max(0.0, (begin - oldest).total_seconds() / 86400)
    high = max(0.0, (end - oldest).total_seconds() / 86400)
    # Admit only roundings possible during this exact invocation, including a
    # decimal rounding boundary. No epsilon expands the underlying time range.
    if not float(f"{low:.1f}") <= float(token) <= float(f"{high:.1f}"):
        raise ValueError("ledger age lies outside retained invocation bounds")
    over_size = path.stat().st_size > int(policy["max_bytes"])
    over_low = low > float(policy["max_age_days"])
    over_high = high > float(policy["max_age_days"])
    age = max(0.0, (canonical - oldest).total_seconds() / 86400)
    over_canonical = age > float(policy["max_age_days"])
    if not over_size and (over_low != over_high or over_low != over_canonical):
        raise ValueError("ledger age comparison crosses a native policy threshold")
    expected = _finding(
        "ledger_roll_policy", "warning" if over_size or over_low else "ok",
        str(root.path), detail,
        "floati epoch roll --root ROOT --as NODE --idempotency-key KEY" if over_size or over_low else None,
    )
    _check(finding, expected, "ledger_roll_policy")
    updated = prefix + f"{age:.1f}" + suffix
    if updated != detail:
        finding["detail"] = updated
        notes.append({"pointer": "/evidence/findings/" + str(index) + "/detail",
                      "reason": "native one-decimal ledger age validated within invocation bounds"})


def _doctor(result, root, workspace, begin, end, canonical, notes):
    evidence = result["evidence"]
    if evidence.get("source") != str(_ROOT) or evidence.get("ref") != "HEAD":
        raise ValueError("doctor equivalence requires the fixed instrument source and HEAD")
    findings = evidence.get("findings")
    if type(findings) is not list:
        raise ValueError("doctor findings are missing")
    # Reconstruct the default host-scoped observation from the same native
    # consent, workspace and hook readers before changing its source path.
    hooks = _codex_home_path("hooks.json")
    trust_rows = []
    if hooks.exists():
        try:
            trust_rows = observe_codex_waiter_hooks(hooks)
        except ProtocolRefusal:
            pass  # Doctor also retains readable consent without hook trust.
    windows = project_codex_wait_window_findings(root, hooks, trust_rows, host_scoped=True)
    window_codes = {row["code"] for row in windows}
    supplied_windows = [row for row in findings if type(row) is dict
                        and (row.get("code") in window_codes or "wait_window" in row)]
    _check(supplied_windows, windows, "native Codex wait-window findings")
    registry = Registry(root)
    nodes = list(registry.active_node_ids())
    if not nodes:
        raise ValueError("doctor fixture has no active native nodes")
    # Doctor takes one full-precision observation for all health readers. The
    # recent-session window is its retained native full-precision testimony.
    times = []
    for node in nodes:
        _, finding = _one(findings, "wake_health", node)
        try:
            own = _parse(finding["wake_health"]["recent_sessions"]["window_end"], "doctor observation")
        except (KeyError, TypeError) as exc:
            raise ValueError("doctor observation is unavailable") from exc
        _bound(own, begin, end)
        times.append(own)
    if any(moment != times[0] for moment in times):
        raise ValueError("doctor health readers did not share one native observation")
    own = times[0]
    bindings = _role_bindings(_ROOT, nodes, registry.known_records(), root=root, legacy_findings=[])
    cadences = {node: template.cadence for node, template in bindings.items()}
    slas = {node: int(template.ack_sla_minutes) for node, template in bindings.items()
            if template.ack_sla_minutes is not None}
    events, _unknown, _skew = EventLog(root)._compatible_event_records_with_skew(snapshot=True)
    health = DeliveryHealthAnalyzer.analyze(events, root, nodes, own, cadences, slas)
    common_health = DeliveryHealthAnalyzer.analyze(events, root, nodes, canonical, cadences, slas)
    wake = WakeHealthProjection(root)
    for node in nodes:
        for code, actual_map, canonical_map, clocks in (
            ("delivery_health", health.findings_by_node, common_health.findings_by_node,
             {("detail",), ("oldest_unread", "observed_at"), ("oldest_unread", "age_minutes")}),
            ("acknowledgment_health", health.acknowledgment_findings_by_node,
             common_health.acknowledgment_findings_by_node,
             {("detail",), ("acknowledgment", "oldest_attention_minutes")}),
        ):
            index, finding = _one(findings, code, node)
            _transition(finding, actual_map[node], canonical_map[node],
                        "/evidence/findings/" + str(index), clocks, notes)
        index, finding = _one(findings, "wake_health", node)
        original = wake.fact(node, own)
        projected = wake.fact(node, canonical)
        severity = "error" if original["state"] in {"stale_claim_with_unread_mail", "breaker_open"} else "warning" if original["state"] == "paused" else "ok"
        expected = _finding("wake_health", severity, node,
                            "wake path projects " + str(original["state"]),
                            str(original["remedy"]) if severity != "ok" else None)
        _check(finding, expected, "wake_health finding")
        _transition(finding["wake_health"], original, projected,
                    "/evidence/findings/" + str(index) + "/wake_health", _WAKE_CLOCKS, notes)
        if severity != "ok" and expected["remediation"] != projected["remedy"]:
            finding["remediation"] = projected["remedy"]
            notes.append({"pointer": "/evidence/findings/" + str(index) + "/remediation",
                          "reason": "validated native clock-derived wake remedy"})
        attention = wake.seat_attention(node, own)
        common_attention = wake.seat_attention(node, canonical)
        if attention["state"] == "unknown":
            index, finding = _one(findings, "seat_attention_unknown", node)
            expected = _finding("seat_attention_unknown", "warning", node,
                                str(attention["detail"]), str(attention["remedy"]))
            _check(finding, expected, "seat_attention finding")
            _transition(finding["seat_attention"], attention, common_attention,
                        "/evidence/findings/" + str(index) + "/seat_attention", _ATTENTION_CLOCKS, notes)
            if attention["detail"] != common_attention["detail"]:
                finding["detail"] = common_attention["detail"]
                notes.append({"pointer": "/evidence/findings/" + str(index) + "/detail",
                              "reason": "validated native clock-derived seat attention prose"})
        elif common_attention["state"] != attention["state"] or common_attention["reason"] != attention["reason"]:
            raise ValueError("canonical time changes seat attention classification")
    _doctor_disk(root, workspace, findings, notes)
    _doctor_roll(root, findings, begin, end, canonical, notes)
    _root_coordinates(result, root.tenant_home, notes, case="doctor")


def _status(result, root, workspace, begin, end, canonical, notes):
    evidence = result["evidence"]
    own = _parse(evidence.get("observed_at"), "status observation")
    _bound(own, begin, end, precision=0.001)
    # Native status serializes milliseconds but recent sessions retain the
    # precise input clock. Prefer that clock when proving the full snapshot.
    nodes = evidence.get("nodes")
    if type(nodes) is not list or not nodes:
        raise ValueError("status nodes are unavailable")
    moments = []
    for node in nodes:
        try:
            moment = _parse(node["wake_health"]["recent_sessions"]["window_end"], "status full clock")
        except (KeyError, TypeError) as exc:
            raise ValueError("status full clock is unavailable") from exc
        _bound(moment, begin, end)
        moments.append(moment)
    if any(moment != moments[0] for moment in moments):
        raise ValueError("status nodes disagree on their native clock")
    own = moments[0]
    projection = FleetProjection(root)
    scope = CommandScope(root=str(root.path), tenant=root.tenant_id, root_source="explicit")
    # _snapshot is the native cold projector behind status_artifact. Calling
    # it rather than status_artifact avoids cache refresh and all writes.
    own_snapshot, own_mode = projection._snapshot(own)
    canonical_snapshot, canonical_mode = projection._snapshot(canonical)
    expected = {"status_schema_version": 1, "kind": "fleet_status",
                "root": str(root.path), "tenant_id": root.tenant_id,
                "mode": own_mode, **own_snapshot, "scope": scope.evidence()}
    projected = {"status_schema_version": 1, "kind": "fleet_status",
                 "root": str(root.path), "tenant_id": root.tenant_id,
                 "mode": canonical_mode, **canonical_snapshot, "scope": scope.evidence()}
    _check(evidence, expected, "full status snapshot")
    clocks = {("observed_at",)}
    for index, _node in enumerate(own_snapshot["nodes"]):
        prefix = ("nodes", index)
        clocks |= {prefix + ("oldest_unread", "observed_at"), prefix + ("oldest_unread", "age_minutes")}
        clocks |= {prefix + ("wake_health",) + path for path in _WAKE_CLOCKS}
        clocks |= {prefix + ("seat_attention",) + path for path in _ATTENTION_CLOCKS}
    _changed(evidence, projected, "/evidence", clocks, notes,
             "complete native own-clock status validated, then projected at retained CPU clock")
    # installer_shadow and unknown fields were not regenerated or removed.
    _root_coordinates(result, root.tenant_home, notes, case="status")


def normalize_artifact(case, artifact, home, workspace, wall_begin, wall_end, canonical_time):
    """Return a deep-copied normal form and explicit JSON-pointer audit trail.

    Times are aware datetimes retained by the owner, not caller-generated
    substitute clocks. Doctor/status are supported. Other cases deliberately
    refuse until their parent-validated authored-row bindings have an explicit
    API; no historical or authored ID is erased by a generic normalization.
    """
    begin = _time(wall_begin, "wall_begin")
    end = _time(wall_end, "wall_end")
    canonical = _time(canonical_time, "canonical_time")
    if end < begin:
        raise ValueError("invocation wall-clock boundaries are reversed")
    home, workspace = Path(home), Path(workspace)
    for path, label in ((home, "home"), (workspace, "workspace")):
        if (not path.is_absolute() or path.resolve(strict=True) != path
                or not stat.S_ISDIR(path.lstat().st_mode) or path.lstat().st_uid != os.getuid()):
            raise ValueError(label + " must be an owned canonical ordinary directory")
    if type(artifact) is not dict or artifact.get("artifact_version") != 0 or type(artifact.get("artifact_version")) is not int:
        raise ValueError("native artifact envelope is invalid")
    if case not in {"doctor", "status"} or artifact.get("command") != case:
        raise ValueError("unsupported artifact equivalence case")
    if artifact.get("status") != ("degraded" if case == "doctor" else "ok") or type(artifact.get("evidence")) is not dict:
        raise ValueError("native artifact outcome differs from the declared fixture")
    _json(artifact)
    result = copy.deepcopy(artifact)
    notes = []
    root = FloatiRoot.open_direct_home(home, create=False)
    try:
        if case == "doctor":
            _doctor(result, root, workspace, begin, end, canonical, notes)
        else:
            _status(result, root, workspace, begin, end, canonical, notes)
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("native artifact has an unsupported projection shape") from exc
    # Native clock reprojection and exact clone substitution can both change
    # one prose leaf. The complete transform needs one coordinate carrying
    # both reasons, so restoring it recovers the original in one operation.
    composed = {}
    for note in notes:
        pointer = note["pointer"]
        if pointer in composed:
            composed[pointer]["reason"] += "; " + note["reason"]
        else:
            composed[pointer] = dict(note)
    from tests.bus_scale_integration import _roundtrip
    notes = list(composed.values())
    _roundtrip(artifact, result, notes)
    return {"artifact": result, "normalizations": notes}
