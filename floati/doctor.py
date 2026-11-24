"""Physically read-only typed diagnostics for a Floati source and root."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .artifact_subject import artifact_subject
from .consumption import ConsumptionLedger
from .copy import (
    DOCTOR_LIVE_DIRS_EXPECTED_ABSENT_DETAIL,
    DOCTOR_PROFILE_INVALID_DETAIL,
)
from .cursor_workers import project_remote_control_worker_findings
from .delivery_health import DeliveryHealthAnalyzer
from .doctor_disk_watch import project_disk_watch
from .doctor_wake_receipt_legacy_read import project_wake_receipt_legacy_read
from .errors import DOCTOR_CURRENCY_REMEDY, IntegrityFailure, ProtocolRefusal
from .gateway import GatewayConfig
from .holds import project_hold_findings
from .inbound_hold import project_inbound_hold_findings
from .jsonl import read_records_compatible_snapshot, read_records_snapshot
from .installer_shadow import observe_installer_shadow, observation_exit_code
from .manifest import verify_manifest
from .proxy_awareness import project_model_proxy_findings
from .registry import read_registry_compatible
from .root import FloatiRoot
from .git_process import GIT_MAINTENANCE_OPTIONS, fixed_git_command, fixed_git_environment
from .git_process import is_shallow_repository
from .sandbox_probe import probe_write_set
from .sandbox_remedy import remedy_for
from .storage_identity import INSTALL_METADATA_DIRECTORY
from . import doctor_wd_4
from .update_status import project_update_findings
from .version_skew import older_readers, vocabulary_skew_fact


def _codex_home_path(relative: str) -> Path:
    try:
        home = Path.home()
        if not home.is_absolute():
            raise ValueError("HOME must be absolute")
        return home.resolve() / ".codex" / relative
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtocolRefusal(
            "doctor_home_invalid", "HOME does not name a resolvable absolute account home directory.",
            "Set HOME to an absolute account home directory or supply explicit Codex hooks and gateway paths.",
        ) from exc


RULED_PROFILES = ("bus-only", "orchestration")
CODEX_GATEWAY_SOURCE = Path("tools/codex/codex-fleet-bus.py")
CODEX_GATEWAY_DIGEST = Path("tools/codex/codex-fleet-bus.sha256.json")
BUS_WATCH_SOURCE = Path("scripts/bus-watch/floati-bus-watch.ts")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INTERPRETER_TRUST_OK_DETAIL = (
    "Reconciliation can run here. Floati trusts this Python and every directory above it."
)
_INTERPRETER_TRUST_WARNING_DETAIL = (
    "Reconciliation cannot run, so every effect stays unknown. "
    "Floati will not trust {failing_component}."
)
_INTERPRETER_TRUST_REMEDIATION = (
    "Every directory above the Python must be a real directory owned by root and writable "
    "by nobody else. Run Floati with a root-owned Python; on macOS that is /usr/bin/python3."
)

# LAUNCH-1. scripts/floati never resolves its interpreter through PATH. It reads
# an operator declaration from LAUNCHER_INTERPRETER_DECLARATION, else walks this
# fixed candidate list in order, else refuses. The chosen path arrives here in
# LAUNCHER_INTERPRETER_SELECTION and doctor reports which of the two ruled
# sources it came from. The list is repeated in scripts/floati because a shell
# script cannot import it; test_doctor pins the two copies equal.
LAUNCHER_INTERPRETER_CANDIDATES = ("/usr/bin/python3", "/bin/python3")
LAUNCHER_INTERPRETER_DECLARATION = "FLOATI_PYTHON"
LAUNCHER_INTERPRETER_SELECTION = "FLOATI_LAUNCHER_INTERPRETER"
_LAUNCHER_INTERPRETER_UNRULED_REMEDIATION = (
    "Start Floati through <destination>/scripts/floati, or declare one absolute canonical "
    "interpreter path in {declaration}. PATH is never consulted."
)


#: LEDGER-1 (c): the root layout is the product's declared families;
#: anything else at the top level is reported with its size and NEVER
#: touched — floati deleting an unmanaged entry is the LANES-1 failure
#: mode. Measured on the live root: hand-made ``tmp/`` blobs sit inside
#: a bus root invisible to every health surface.
_MANAGED_ROOT_DIRECTORIES = frozenset(
    {
        "registry",
        "nodes",
        "state",
        "receipts",
        "cursors",
        "authority-grants",
        "work",
        "codex-wait",
        "scratch",
        "effects",
        "liveness-presence",
        "runs",
        "sequencer",
    }
)
_MANAGED_ROOT_FILE_PREFIXES = ("archive-", "quarantine-")
_MANAGED_ROOT_FILES = frozenset({"events.jsonl", "events.jsonl.lock"})


def _managed_prefix_directories() -> frozenset[str]:
    from .inbox_prefix import CACHE_NAME as inbox
    from .registry_prefix import CACHE_NAME as registry
    from .send_prefix import CACHE_NAME as send

    return frozenset({inbox, registry, send})


def _managed_root_families() -> frozenset[str]:
    """LEDGER-1 (c) Am.1: DERIVE the families from the constants that own
    them, so the list cannot drift from the writers. Only entries no
    constant names are hand-listed, each naming its writer."""

    from .intake import _INTAKE_PAYLOAD_DIRECTORY
    from .solo import SOLO_CONFIG
    from .storage_identity import SNAPSHOT_DIRECTORY
    from .adhoc_records import ADHOC_DIRECTORY, PROVIDER_STATE_DIRECTORY
    from .bus_epoch import EPOCH_TURNSTILE_NAME

    derived = {
        # floati/bus_epoch.py: the empty epoch turnstile, made by init or by a
        # tenant's first contended exclusive acquisition, which may be a
        # contended pristine policy observation; managed, never unmanaged bytes.
        EPOCH_TURNSTILE_NAME,
        ADHOC_DIRECTORY.parts[0],
        PROVIDER_STATE_DIRECTORY.parts[0],
        SNAPSHOT_DIRECTORY,  # floati/snapshot.py writes <root>/.floati-snapshots/v0
        _INTAKE_PAYLOAD_DIRECTORY.parts[0],  # floati/intake.py writes <root>/intake/v0
        SOLO_CONFIG.parts[0],  # floati/solo.py writes <root>/solo.json on the first run
    }
    hand_listed = {
        "registry",  # floati/registry.py
        "nodes",  # floati/lane_scaling.py / workspace_layout.py
        "state",  # floati/wake_daemon*.py, state_receipts.py
        "receipts",  # floati/events.py, bus_epoch.py
        "cursors",  # floati/cli.py init (REL-1)
        "authority-grants",  # floati/events.py:919
        "work",  # floati/doctor.py:1414 / orchestrate.py
        "codex-wait",  # floati/codex_wait_contract.py
        "scratch",  # floati/verification.py scratch/verifications
        "effects",  # floati/workers.py / worker_isolation.py
        "liveness-presence",  # floati/planes.py
        "runs",  # floati/orchestrate.py run records
        "sequencer",  # floati/sequencer.py
    }
    return frozenset(derived | hand_listed | _managed_prefix_directories())


def project_ledger_roll_policy_finding(root: FloatiRoot) -> Dict[str, object]:
    """LEDGER-1 (b): the live ledger's size and age against the roll policy.

    Doctor reports thresholds without initiating a new roll. Ordinary native
    activity maintains the epoch; the explicit ``epoch roll`` remains grant-
    gated and idempotent. Numbers floati did not measure are never filled in — an
    unreadable first entry reports the age as unknown.
    """

    from .bus_epoch import ledger_roll_policy

    policy = ledger_roll_policy(root)
    policy_source = (
        "shipped default" if policy["source"] == "shipped_default" else "operator-declared"
    )
    ledger = root.resolve_relative("events.jsonl")
    if not ledger.is_file():
        return _finding(
            "ledger_roll_policy",
            "ok",
            str(root.path),
            "no live ledger yet (typed absence); roll policy "
            f"max_bytes={policy['max_bytes']} max_age_days={policy['max_age_days']}"
            f" ({policy['source']})",
        )
    size = ledger.stat().st_size
    age_days: Optional[float] = None
    with ledger.open("rb") as handle:
        first = handle.readline()
    try:
        record = json.loads(first.decode("utf-8"))
        raw_stamp = str(record["timestamp"])
        # Slicing, not str.replace: the epoch-barrier census reads every
        # `.replace` call in this module as a filesystem rename on tainted
        # ledger paths, and this function is a read-only reporter.
        normalized = (
            raw_stamp[:-1] + "+00:00" if raw_stamp.endswith("Z") else raw_stamp
        )
        oldest = datetime.fromisoformat(normalized)
        age_days = max(0.0, (datetime.now(timezone.utc) - oldest).total_seconds() / 86400)
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, ValueError, TypeError):
        age_days = None
    age_text = (
        f"{age_days:.1f} days old" if age_days is not None else "age unknown (first entry unreadable)"
    )
    over_size = size >= int(policy["max_bytes"])
    over_age = age_days is not None and age_days >= float(policy["max_age_days"])
    detail = (
        f"live ledger is {size} bytes (policy {policy['max_bytes']}, "
        f"{policy_source}); oldest entry {age_text} "
        f"(policy {policy['max_age_days']} days)"
    )
    # LEDGER-1 (b) Am.1: age unknown is its own severity word — an
    # unmeasured age is not a confirmably healthy one.
    if age_days is None:
        return _finding("ledger_roll_policy", "warning", str(root.path), detail)
    if not (over_size or over_age):
        return _finding("ledger_roll_policy", "ok", str(root.path), detail)
    return _finding(
        "ledger_roll_policy",
        "warning",
        str(root.path),
        detail,
        "ordinary activity by an active registered node maintains due epochs; "
        "carried nonreclaimable work stays live; explicit manual remedy: "
        "floati epoch roll --root ROOT --as NODE --idempotency-key KEY",
    )


def _entry_bytes(entry: Path) -> int:
    """Recursive size of one root entry; a symlink's target is NOT walked."""

    if entry.is_symlink():
        return entry.lstat().st_size
    if entry.is_dir():
        return sum(
            candidate.stat().st_size
            for candidate in entry.rglob("*")
            if candidate.is_file() and not candidate.is_symlink()
        )
    return entry.stat().st_size


def project_seat_declaration_findings(root: FloatiRoot) -> list[Dict[str, object]]:
    """BL-10: a registered, mapped workspace with no SEAT.json names its migration.

    The remediation IS the migration command for that exact root/node/workspace —
    an instrument that prints its own remedy.
    """
    from .codex_wait_contract import mapped_workspaces
    from .registry import Registry
    from .seat_declaration import SEAT_MARKER
    from .seat_migration import remedy_for

    try:
        active = set(Registry(root).active_node_ids())
        mapped = mapped_workspaces(root.tenant_home)
    except ProtocolRefusal as exc:
        return [_exception_finding(exc, "warning", str(root.path))]
    except Exception:
        return []
    findings: list[Dict[str, object]] = []
    for workspace, node_id in mapped:
        if node_id not in active:
            continue
        try:
            if (workspace / SEAT_MARKER).exists():
                continue
        except OSError:
            continue
        findings.append(
            _finding(
                "seat_declaration_absent",
                "warning",
                str(workspace),
                "registered workspace has no seat declaration; boarding refuses "
                "until the seat is migrated once",
                remedy_for(root, node_id, workspace),
            )
        )
    return findings


def project_codex_wait_window_findings(root, hooks_path, trust_rows, *, host_scoped=False):
    """Observe exact mapped consent and installed hook windows without arming.

    Defaults are never observations. A hook for another bus cannot supply
    this root's timeout, and multiple matching hooks have no unique source.
    Doctor does not run the harness or promote a fixture bound to a ceiling.
    """
    from .codex_hook_install import parse_waiter_stop_argv
    from .codex_hook_trust import codex_hook_current_hash
    from .codex_wait_contract import (
        CodexWaitConsentLedger, consent_ledger_relative, mapped_workspaces,
        resolve_participant,
    )
    from .registry import Registry

    try:
        active = set(Registry(root).active_node_ids())
    except (ProtocolRefusal, IntegrityFailure) as exc:
        return [_exception_finding(exc, "error", str(root.path))]
    except OSError:
        return [_finding("codex_wait_window_unavailable", "warning", str(root.path),
                         "The registry could not be read; no window values are inferred.",
                         "Inspect this root's registry before rerunning doctor.")]
    try:
        document = json.loads(Path(hooks_path).read_bytes())
        groups = document["hooks"]["Stop"]
    except (OSError, ValueError, KeyError, TypeError):
        groups = []  # Preserve readable consent when hook evidence is absent.
    candidates = []
    for trust in trust_rows:
        try:
            block = groups[trust["hook_group_index"]]
            # Do not combine a newly edited hook with the old trust snapshot.
            if codex_hook_current_hash(block) != trust["hook_trust_current_hash"]:
                continue
            handler = block["hooks"][0]
            parsed = parse_waiter_stop_argv(shlex.split(handler["command"]))
            if parsed is None or parsed[0].name != "floati-codex-wait" or parsed[1] != root.tenant_home:
                continue
            candidates.append((max(1, handler.get("timeout", 600)), trust))
        except (ProtocolRefusal, ValueError, KeyError, IndexError, TypeError):
            continue
    findings = []
    for workspace, node in mapped_workspaces(root.tenant_home):
        if node not in active:
            continue
        consent = None
        consent_error = None
        try:
            participant = resolve_participant(root.tenant_home, workspace)
            if participant is None or participant.binding.node_id != node:
                state = "workspace_unresolved"
            else:
                consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
                state = "matched"
        except (ProtocolRefusal, IntegrityFailure, OSError) as exc:
            state = "consent_unavailable"
            consent_error = getattr(exc, "code", "consent_read_unavailable")
            if isinstance(exc, IntegrityFailure):
                findings.append(_exception_finding(exc, "error", str(workspace)))
        hook = candidates[0] if len(candidates) == 1 else None
        if consent is not None:
            if not candidates:
                state = "hook_unavailable"
            elif hook is None:
                state = "hook_ambiguous"
            elif (consent["hook_timeout_seconds"] != hook[0]
                  or not 0 < consent["wait_deadline_seconds"] < hook[0]):
                state = "mismatch"
        observation = {
            "node_id": node,
            "window_state": state,
            "hook_timeout_seconds": None if hook is None else hook[0],
            "consent_hook_timeout_seconds": None if consent is None else consent["hook_timeout_seconds"],
            "wait_deadline_seconds": None if consent is None else consent["wait_deadline_seconds"],
            "consent_receipt_id": None if consent is None else consent["id"],
            "consent_error_code": consent_error,
            "hook_timeout_source": None if hook is None else hook[1]["hook_trust_key"],
            # Name the lexical source even when its reader refused a symlink.
            # Resolving again here could repeat that refusal outside the catch.
            "wait_deadline_source": str(root.tenant_home / consent_ledger_relative(node)),
            "hook_armed": None if hook is None else hook[1]["hook_armed"],
            "harness_ceiling_seconds": None,
        }
        hook_text = "unknown" if hook is None else f"{hook[0]} s"
        deadline_text = "unknown" if consent is None else f"{consent['wait_deadline_seconds']} s"
        detail = (f"Codex idle window: {state}; installed hook timeout "
                  f"{hook_text}; consent deadline {deadline_text}. "
                  "Harness ceiling is not observed by doctor; matching values do not prove a live waiter.")
        remediation = (None if state == "matched" else
                       "Inspect this workspace's exact hook and consent sources; use the governed installer and operator re-trust for a definition change.")
        # Ambient host configuration must not change this root's health.
        # Explicit hook selection retains root-scoped warning semantics;
        # genuine native consent-integrity findings above stay errors.
        finding = (_host_finding("codex_wait_window", str(workspace), detail, remediation)
                   if host_scoped else
                   _finding("codex_wait_window", "ok" if state == "matched" else "warning",
                            str(workspace), detail, remediation))
        finding["wait_window"] = observation
        findings.append(finding)
    return findings


def project_seat_execution_findings(root: FloatiRoot) -> list[Dict[str, object]]:
    """Native car execution is independent of mail, ACK and elapsed silence."""
    from .seat_execution import SeatExecutionController
    from .registry import Registry
    try:
        controller = SeatExecutionController(root)
        nodes = Registry(root).active_node_ids()
    except (IntegrityFailure, ProtocolRefusal, OSError) as exc:
        return [_finding('seat_execution_unknown', 'warning', str(root.path),
            json.dumps({'state': 'UNKNOWN', 'reason': getattr(exc, 'code', 'execution_read_unavailable')},
                       sort_keys=True, ensure_ascii=True))]
    findings = []
    for node in nodes:
        projection = controller.project(node)
        state = projection["state"]
        findings.append(_finding("seat_execution_" + state.lower(),
            "warning" if state in {"STALLED", "UNKNOWN"} else "ok", node,
            _seat_execution_detail(projection)))
    return findings


def _seat_execution_detail(projection: Dict[str, object]) -> str:
    """Keep Doctor's v0 detail bound while identifying the full durable row."""
    rendered = json.dumps(projection, sort_keys=True, ensure_ascii=True)
    if len(rendered) <= 8192:
        return rendered
    latest = projection.get('latest_execution')
    summary = {key: value for key, value in projection.items()
               if key != 'latest_execution'}
    summary['projection_preview'] = True
    if isinstance(latest, dict):
        retained = {key: latest[key] for key in
            ('id', 'timestamp', 'source_kind', 'event', 'code', 'command', 'detail')}
        for field in ('command', 'detail'):
            original = retained[field]
            while len(json.dumps(retained[field], ensure_ascii=True)) > 2000:
                retained[field] = retained[field][:len(retained[field]) // 2]
            retained[field + '_truncated'] = retained[field] != original
        summary['latest_execution'] = retained
    rendered = json.dumps(summary, sort_keys=True, ensure_ascii=True)
    while len(rendered) > 8192 and isinstance(latest, dict):
        field = max(('command', 'detail'),
                    key=lambda key: len(json.dumps(retained[key], ensure_ascii=True)))
        retained[field] = retained[field][:len(retained[field]) // 2]
        retained[field + '_truncated'] = True
        rendered = json.dumps(summary, sort_keys=True, ensure_ascii=True)
    return rendered


def project_root_layout_findings(root: FloatiRoot) -> list[Dict[str, object]]:
    """Report unmanaged bytes inside the fleet root: path + size, never touched."""

    from .cursor_prefix import CACHE_NAME_PREFIX

    findings: list[Dict[str, object]] = []
    try:
        entries = sorted(root.path.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        return [
            _finding(
                "root_layout_unreadable",
                "warning",
                str(root.path),
                f"root layout could not be listed: {exc}",
            )
        ]
    unmanaged: list[tuple[str, int]] = []
    managed_families = _managed_root_families()
    prefix_directories = _managed_prefix_directories()
    for entry in entries:
        from .cut_prefix import CACHE_NAME_PREFIX as CUT_PREFIX
        cursor_bank = (
            entry.name.startswith(CACHE_NAME_PREFIX)
            and _SHA256.fullmatch(entry.name[len(CACHE_NAME_PREFIX):]) is not None
        )
        cut_bank = (entry.name.startswith(CUT_PREFIX)
            and _SHA256.fullmatch(entry.name[len(CUT_PREFIX):]) is not None)
        if entry.name in prefix_directories or cursor_bank or cut_bank:
            if not entry.is_symlink() and entry.is_dir():
                continue
        elif (
            entry.name in _MANAGED_ROOT_FILES
            or entry.name in managed_families
            or entry.name.startswith(_MANAGED_ROOT_FILE_PREFIXES)
        ):
            continue
        unmanaged.append((entry.name, _entry_bytes(entry)))
    if not unmanaged:
        return [
            _finding(
                "root_layout_managed",
                "ok",
                str(root.path),
                "every top-level root entry belongs to a managed family",
            )
        ]
    for name, size in unmanaged:
        findings.append(
            _finding(
                "root_unmanaged_bytes",
                "warning",
                name,
                f"unmanaged bytes inside the fleet root: {size} bytes; "
                "reported, never touched (LEDGER-1 c)",
                "inspect and remove by hand if unwanted; floati never "
                "deletes an unmanaged entry",
            )
        )
    return findings


def _finding(
    code: str,
    severity: str,
    subject: str,
    detail: str,
    remediation: Optional[str] = None,
) -> Dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "subject": artifact_subject(subject),
        "detail": detail,
        "remediation": remediation,
    }


def _captured_refusal(exc: BaseException) -> Optional[ProtocolRefusal]:
    """Recover a refusal through deliberate diagnostic wrappers, without cycles."""
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ProtocolRefusal):
            return exc
        if exc.__cause__ is None:
            break
        exc = exc.__cause__
    return None


def _exception_finding(
    exc: IntegrityFailure | ProtocolRefusal,
    severity: str,
    subject: str,
    remediation: Optional[str] = None,
    *,
    code: Optional[str] = None,
    detail: Optional[str] = None,
) -> Dict[str, object]:
    finding = _finding(code or exc.code, severity, subject,
                       exc.detail if detail is None else detail, remediation)
    refusal = _captured_refusal(exc)
    if refusal is not None:
        finding["remedy"] = deepcopy(refusal.remedy)
        if finding["code"] != refusal.code:
            finding["refusal_code"] = refusal.code
    return finding


def project_harness_binary_inventory(
    root: FloatiRoot, *, currency_current: bool
) -> list[Dict[str, object]]:
    """HARNESS-VER-1 / FQ-4 (public issue #18): per declared harness,
    name which absolute path floati runs and by what rule, its measured
    version, and every earlier-on-PATH same-name copy with its version.
    A differing or unmeasurable copy is the typed warning
    harness_binary_shadowed. Identical copies are a note. Unreadable
    PATH entries are summarised by count in the detail (SHADOW-DETAIL-1)
    and kept in full on the finding - never dropped, never a crash
    (INS-1). PATH is an inventory only - it never chooses what floati
    runs. Doctor lines are the Cold-Read voice in
    docs/copy/2026-09-08-fq-4-doctor-lines-cold-read.md."""

    from .harness_versions import harness_version_artifact

    artifact = harness_version_artifact(root)
    declared_rows = artifact["declared"]
    if not declared_rows:
        return [_finding(
            "harness_binary_inventory_absent",
            "ok",
            str(root.path),
            "no harness executable is declared on this root (typed absence, "
            "not a silent pass)",
        )]
    findings: list[Dict[str, object]] = []
    for row in declared_rows:
        subject = f"{row['node']}/{row['harness']}"
        version_text = (
            row["version"] if row["version"] is not None else "<unmeasurable>"
        )
        inventory = row["shadow_inventory"]
        shadows = inventory["shadows"]
        unreadable = inventory["unreadable_entries"]
        bound = row["executable"]

        def _shadow_item(shadow: Dict[str, object]) -> str:
            measured = shadow["version"]
            text = measured if measured is not None else "<unmeasurable>"
            return f"{shadow['path']} ({text})"

        def _shadow_clause() -> str:
            items = ", ".join(_shadow_item(shadow) for shadow in shadows)
            if row["status"] == "shadowed":
                if len(shadows) == 1:
                    return (
                        "an earlier copy on your PATH is a different "
                        f"version: {items}"
                    )
                return (
                    "earlier copies on your PATH are different versions: "
                    + items
                )
            if len(shadows) == 1:
                return f"an earlier copy on your PATH: {items}"
            return f"earlier copies on your PATH: {items}"

        # Cold-Read voice (verbatim): runs/path/rule, shadow answer,
        # "and it measures", then "floati could not read N".
        parts = [
            f"floati runs {bound} — the wake-daemon adapter binds it by "
            "absolute path",
        ]
        if row["status"] in ("shadowed", "unmeasurable"):
            if shadows:
                parts.append(_shadow_clause())
            parts.append(f"and it measures {version_text}")
            if row["status"] == "unmeasurable":
                parts.append(
                    "a copy whose version cannot be measured cannot be "
                    "proven identical"
                )
            if unreadable:
                parts.append(
                    f"floati could not read {len(unreadable)} PATH entries"
                )
            finding = _finding(
                "harness_binary_shadowed",
                "warning",
                subject,
                "; ".join(parts),
                # Cold-Read remedy: lead with "nothing broken for floati".
                "floati is already using the version you declared, so "
                "nothing here is broken for floati. If you want your own "
                f"shell to match, put {bound} earlier on your PATH. PATH is "
                "an inventory to floati; it never chooses.",
            )
        else:
            parts.append(f"and it measures {version_text}")
            if row["status"] == "identical":
                parts.append(
                    "an earlier PATH copy exists with the identical version"
                )
            else:
                parts.append("no earlier PATH copy of the same binary name")
            if unreadable:
                parts.append(
                    f"floati could not read {len(unreadable)} PATH entries"
                )
            finding = _finding(
                "harness_binary_inventory",
                "ok",
                subject,
                "; ".join(parts),
            )
        finding["harness_inventory"] = row
        if unreadable:
            finding["path_entries_unreadable"] = list(unreadable)
        findings.append(finding)
    return findings


def _host_finding(
    code: str,
    subject: str,
    detail: str,
    remediation: Optional[str] = None,
) -> Dict[str, object]:
    """Keep ambient installation observations separate from root health."""

    finding = _finding("host_" + code, "info", subject, detail, remediation)
    # Normalize the path before adding a scope prefix: the prefix itself is
    # not a filesystem path and must not bypass artifact_subject redaction.
    finding["subject"] = "host:" + str(finding["subject"])
    return finding


def _workspace_layout_finding(row: Dict[str, str]) -> Dict[str, object]:
    """Project one read-only layout defect onto the ordinary doctor shape."""

    code = str(row["code"])
    node_id = str(row["node_id"])
    severity = "ok" if row["severity"] == "notice" else str(row["severity"])
    return _finding(
        code,
        severity,
        str(row["path"]),
        f"{node_id}: {code.replace('_', ' ')}",
    )


# The six shipped v1 pins preceding e2e1863's version/SLA migration. Both
# endpoints are fixed: an arbitrary modified or custom template is not proof
# of a historical assignment, even if stripping its SLA yields a matching pin.
_LEGACY_ROLE_PINS = {
    "architect": (
        "7ac392ebe9f68676e117282d81e65cccfb7d19cf249e1159f012bfa89b689d24",
        "28131d93807d83a8f42f7ce5fb89507f1ddd3579c617cad28941a9f0ba36bc10",
    ),
    "builder": (
        "ae6ccfa31c8ef96b2f378830333c45ea5390ba3d70924f6d0b56319a7c97573b",
        "12a1177a569f629e44795e1b1538f735cc9ad0ca93ce62901ca8093a5d699846",
    ),
    "github-manager": (
        "43b55bbdafc21a5b93d803673e3f656ba29888aa066e779c0ddd34d862af2bdf",
        "eea7a59ff550efd817a5ca12add836533c7e4fbee6ee535c8b99a18953bf1929",
    ),
    "researcher": (
        "cf3e039f7a6b14ee3eea1edf3cca859ea131638c50c05a974db9822e413a8e82",
        "2f2b5905e6e1ac7d8df23e426c8e4410956033d3aa7f0f5ba4f53a2870197a24",
    ),
    "reviewer": (
        "477135125e53df3a5820d347577b011453c0bf0ed87e04ba09075179f58cd9a1",
        "a5599b1881daecf2fbd8987d4ed27da7857ebce9104ac62beb905e0ba99f8c46",
    ),
    "sre": (
        "3fed4d1e92b21a4a12bff60f7e574e32afc19e0c6d702fca34406c42ed7fc516",
        "802f929e75099ee1b15a5f3e7a02ca675dda7572c0d16045f976accbfaa63448",
    ),
}


def _legacy_role_binding(template: object, record: Dict[str, object]) -> Optional[object]:
    from .role_templates import parse_role_template

    pins = _LEGACY_ROLE_PINS.get(str(record.get("template_role")))
    if (
        pins is None
        or type(record.get("template_version")) is not int
        or record["template_version"] != 1
        or record.get("template_sha256") != pins[0]
        or template.template_version != 2
        or template.digest != pins[1]
    ):
        return None
    historical = template.record
    historical["template_version"] = 1
    historical.pop("ack_sla_minutes", None)
    try:
        parsed = parse_role_template(historical)
    except ProtocolRefusal:
        return None
    return parsed if parsed.digest == pins[0] else None


def _role_bindings(
    source: Path,
    nodes: list[str],
    registry_rows: list[Dict[str, object]],
    *,
    root: Optional[FloatiRoot] = None,
    legacy_findings: Optional[list[Dict[str, object]]] = None,
) -> Dict[str, object]:
    """Resolve declared templates or exactly proved shipped historical pins."""

    from .role_templates import load_shipped_role_templates

    latest: Dict[str, Dict[str, object]] = {}
    for row in registry_rows:
        if row.get("kind") == "registry_role_record":
            latest[str(row.get("node_id"))] = row
    assigned = {
        node: record
        for node in nodes
        if (record := latest.get(node)) is not None
        and record.get("state") == "active"
    }
    if not assigned:
        return {}
    try:
        if root is None:
            templates = load_shipped_role_templates(source / "roles" / "shipped")
        else:
            from .role_library import RoleTemplateLibrary
            templates = RoleTemplateLibrary(
                root, shipped_directory=source / "roles" / "shipped"
            ).templates()
    except ProtocolRefusal as exc:
        raise IntegrityFailure("role_cadence_unavailable", exc.detail) from exc
    bindings: Dict[str, object] = {}
    for node, record in assigned.items():
        template = templates.get(str(record.get("template_role")))
        if (
            template is None
            or record.get("template_sha256") != template.digest
            or record.get("template_version") != template.template_version
        ):
            historical = (
                _legacy_role_binding(template, record) if template is not None else None
            )
            if historical is None:
                raise IntegrityFailure(
                    "role_cadence_invalid",
                    f"active role evidence for {node} does not match a declared template",
                )
            template = historical
            if legacy_findings is not None:
                migration_command = [
                    "floati", "node", "role", "--root",
                    str(root.path) if root is not None else "ROOT",
                    "--node", node, "--template", template.role,
                    "--expected-predecessor", str(record["id"]),
                ]
                for key in sorted(record["answers"]):
                    migration_command.extend(
                        ["--answer", key + "=" + str(record["answers"][key])]
                    )
                legacy_findings.append(_finding(
                    "role_evidence_legacy", "info", node,
                    "active role evidence matches a proved shipped v1 template; "
                    "historical cadence is retained and acknowledgment SLA is undeclared",
                    f"`{shlex.join(migration_command)}`. "
                    "This command re-supplies every recorded v1 answer and "
                    f"migrates role {template.role} version 1 to the same "
                    "role version 2, preserving every recorded answer; the "
                    "writer compare-and-swaps the current row",
                ))
        bindings[node] = template
    return bindings


def _role_cadences(
    source: Path,
    nodes: list[str],
    registry_rows: list[Dict[str, object]],
    *,
    root: Optional[FloatiRoot] = None,
) -> Dict[str, str]:
    """Resolve only digest-bound declared cadence evidence for active nodes."""

    return {
        node: template.cadence
        for node, template in _role_bindings(source, nodes, registry_rows, root=root).items()
    }


def _role_ack_slas(
    source: Path,
    nodes: list[str],
    registry_rows: list[Dict[str, object]],
    *,
    root: Optional[FloatiRoot] = None,
) -> Dict[str, int]:
    """Resolve only digest-bound declared acknowledgment SLAs for active nodes."""

    return {
        node: int(template.ack_sla_minutes)
        for node, template in _role_bindings(source, nodes, registry_rows, root=root).items()
        if template.ack_sla_minutes is not None
    }


def _git(source: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *GIT_MAINTENANCE_OPTIONS, *args],
            cwd=source,
            env={**os.environ, "GIT_ATTR_NOSYSTEM": "1"},
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtocolRefusal(
            "deployment_currency_unavailable",
            str(exc),
            remedy=DOCTOR_CURRENCY_REMEDY,
        ) from exc
    if result.returncode != 0:
        raise ProtocolRefusal(
            "deployment_currency_unavailable",
            result.stderr.strip() or "git inspection failed",
            remedy=DOCTOR_CURRENCY_REMEDY,
        )
    return result.stdout.strip()


def _is_symlink_entry(path: Path) -> bool:
    """Test the invoked identity itself, without resolving system ancestors."""

    return path.is_symlink()


def _installer_shadow_finding(
    artifact: Dict[str, object], destination: Path | str | None
) -> Dict[str, object]:
    outcome = str(artifact["outcome"])
    severity = {
        "found": "warning",
        "affirmative_none": "ok",
        "unknown": "warning",
        "launcher_not_on_path": "warning",
        "path_entry_unreadable": "warning",
        "cannot_speak": "error",
    }[outcome]
    finding = _finding(
        "installer_shadow",
        severity,
        "FLOATI_INSTALL_DESTINATION" if destination is None else str(destination),
        str(artifact["reason"]),
    )
    finding["installer_shadow"] = artifact
    if "remedy" in artifact:
        finding["remediation"] = artifact["remedy"]
    return finding


def _fold_shadow_exit(current: int, shadow: int) -> int:
    """Preserve doctor’s existing aggregate diagnostic precedence."""

    # The standalone shadow observer uses exit 20 for affirmative absence.
    # Inside Doctor that same observation is an ``ok`` finding, not a
    # ProtocolRefusal.  Preserve Doctor's current result instead of leaking
    # the observer command's independent exit vocabulary into the aggregate.
    if shadow == 20:
        return current
    if current in {20, 33, 35}:
        return current
    return 35


def project_launcher_interpreter() -> Dict[str, object]:
    """Report which interpreter scripts/floati chose for this process, and why.

    LAUNCH-1. The launcher exports the path it selected; this reads that claim
    back and classifies it against the two ruled sources it could have come
    from - the operator's declaration, or the fixed candidate list. A claim
    outside both is reported as unruled rather than accepted, and the running
    interpreter is named beside it because the two are not always the same file
    (/usr/bin/python3 on macOS is a shim onto a framework build).
    """

    running = os.path.realpath(sys.executable)
    selected = os.environ.get(LAUNCHER_INTERPRETER_SELECTION)
    declared = os.environ.get(LAUNCHER_INTERPRETER_DECLARATION)
    candidates = ", ".join(LAUNCHER_INTERPRETER_CANDIDATES)
    if not selected:
        return _finding(
            "launcher_interpreter",
            "ok",
            running,
            f"absent: this process was not started by scripts/floati, so no launcher "
            f"interpreter choice belongs to it. It is running {running}.",
        )
    if declared and selected == declared:
        return _finding(
            "launcher_interpreter",
            "ok",
            selected,
            f"declared: scripts/floati ran {selected} because the operator named it in "
            f"{LAUNCHER_INTERPRETER_DECLARATION}. It is running {running}.",
        )
    if selected in LAUNCHER_INTERPRETER_CANDIDATES:
        return _finding(
            "launcher_interpreter",
            "ok",
            selected,
            f"candidate: scripts/floati ran {selected}, the first present entry of the fixed "
            f"list {candidates}. PATH was never consulted. It is running {running}.",
        )
    return _finding(
        "launcher_interpreter",
        "warning",
        selected,
        f"unruled: {selected} is neither the path declared in "
        f"{LAUNCHER_INTERPRETER_DECLARATION} nor one of the fixed candidates {candidates}. "
        f"It is running {running}.",
        _LAUNCHER_INTERPRETER_UNRULED_REMEDIATION.format(
            declaration=LAUNCHER_INTERPRETER_DECLARATION,
        ),
    )


def project_effect_reconciliation_interpreter_trust() -> Dict[str, object]:
    """Project the launcher's unchanged interpreter fence before work needs it."""

    from .effect_reconciliation_exec import (
        _InterpreterTrustFailure,
        _freeze_trusted_interpreter,
    )

    interpreter_path = os.path.realpath(sys.executable)
    try:
        _freeze_trusted_interpreter(interpreter_path)
    except _InterpreterTrustFailure as exc:
        observation = dict(exc.observation)
        finding = _finding(
            "effect_reconciliation_interpreter_trust",
            "warning",
            interpreter_path,
            _INTERPRETER_TRUST_WARNING_DETAIL.format(
                failing_component=observation["failing_component"],
            ),
            _INTERPRETER_TRUST_REMEDIATION,
        )
        finding["interpreter_trust"] = observation
        return finding
    return _finding(
        "effect_reconciliation_interpreter_trust",
        "ok",
        interpreter_path,
        _INTERPRETER_TRUST_OK_DETAIL,
    )


def installed_bridge_paths() -> Optional[tuple[Path, Path]]:
    """The portable bridge store; never inspect the historical foreign copy."""
    root = Path.home() / ".local/share/floati-zcode-bridge"
    if not root.exists() and not root.is_symlink():
        return None
    return root / "launcher.py", root / "active.json"


def project_installed_bridge_currency(
    source_root: Path, *, currency_current: bool
) -> Dict[str, object]:
    """Observe the host bridge without making host access a root failure."""

    try:
        return _installed_bridge_currency(source_root, currency_current=currency_current)
    except (OSError, UnicodeError, ValueError) as exc:
        return _host_finding(
            "wake_bridge_unavailable",
            "wake_bridge",
            f"host wake-bridge currency is unobserved: {exc}",
        )


def _installed_bridge_currency(
    source_root: Path, *, currency_current: bool
) -> Dict[str, object]:
    """Read the selected portable bank without importing or executing it."""
    paths = installed_bridge_paths()
    if paths is None:
        return _host_finding(
            "wake_bridge_uninstalled",
            str(Path.home() / ".local/share/floati-zcode-bridge"),
            "typed absence: the portable bridge store is absent; bound registrations are audited separately",
        )
    from .bridge_bank import verify_bridge_store
    observation = verify_bridge_store(Path.home())
    return _host_finding(
        "wake_bridge_current", str(paths[0]),
        f"portable bridge bank {observation['bank'].name} is verified; runtime source {observation['source_sha']}",
    )


_BRIDGE_SCRIPT_NAME = "stop-hook-bridge.py"
_COMMIT_40 = re.compile(r"^[0-9a-f]{40}$")


def parse_bridge_command(command: str) -> Optional[Dict[str, str]]:
    """BRIDGE-1: read one hook-registration command into the bridge script
    it execs and any HOOK_REPO pin the registration carries. None when the
    command does not name the stop-hook bridge."""

    if not isinstance(command, str):
        return None
    # Recognize only a canonical fixed-launcher candidate. The projection must
    # still verify the bank and compare its provider's exact identity before
    # calling this registration current; parsing grants no runtime authority.
    try:
        portable_words = shlex.split(command)
    except ValueError:
        portable_words = []
    if len(portable_words) == 3 and portable_words[:2] == ["/bin/sh", "-c"]:
        body = portable_words[2]
        matched = re.fullmatch(
            r'HOOK_ROOT="\$HOME/([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)" '
            r'HOOK_NODE=[A-Za-z0-9_-]+ HOOK_SESSION=[A-Za-z0-9_-]+ '
            r'HOOK_REPO="\$HOME/Projects/floati-zcode" '
            r'exec /usr/bin/python3 -I -B "\$HOME/\.local/share/floati-zcode-bridge/launcher\.py"',
            body)
        if (matched and not any(part in {".", ".."} for part in matched[1].split("/"))
                and command == "/bin/sh -c " + shlex.quote(body)):
            return {"script": str(Path.home() / ".local/share/floati-zcode-bridge/launcher.py"),
                    "hook_repo": str(Path.home() / "Projects/floati-zcode"), "adopted": "portable"}
    if _BRIDGE_SCRIPT_NAME not in command:
        return None
    tokens: list[str] = []
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for token in words:
        # The zcode registration wraps its payload in `/bin/sh -c '...'`,
        # so the script path and the HOOK_* pins can arrive inside one
        # quoted token; split that payload open before scanning it.
        if " " in token and _BRIDGE_SCRIPT_NAME in token:
            try:
                tokens.extend(shlex.split(token))
            except ValueError:
                tokens.append(token)
        else:
            tokens.append(token)
    script = next(
        (token for token in tokens if token.endswith(_BRIDGE_SCRIPT_NAME)),
        None,
    )
    if script is None:
        return None
    pin = next(
        (
            token[len("HOOK_REPO="):]
            for token in tokens
            if token.startswith("HOOK_REPO=")
        ),
        None,
    )
    return {"script": script, "hook_repo": pin}


def _bridge_checkout_commit(repo: Path) -> Optional[str]:
    """The floati commit the bridge's checkout runs; read-only rev-parse."""

    try:
        result = subprocess.run(
            fixed_git_command("/usr/bin/git", repo, ("rev-parse", "HEAD")),
            env=fixed_git_environment("/usr/bin/git"),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip().lower()
    return commit if _COMMIT_40.fullmatch(commit) else None


def _bridge_reader_version(repo: Path) -> Optional[str]:
    """The READER_VERSION the bridge's own checkout ships, read by one
    child interpreter bound to that checkout - never by importing it in
    the doctor process."""

    env = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(repo),
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "from floati.records import READER_VERSION as V; print(V)"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def project_zcode_bridge_registrations(
    root: FloatiRoot,
    merged_versions: Dict[str, int],
    *,
    currency_current: bool,
    source_root: Optional[Path] = None,
) -> list[Dict[str, object]]:
    """BRIDGE-1: name every bound zcode seat's bridge checkout and flag a
    reader older than the ledger's newest kind beside `older_readers`.

    The measured deafness ran the bridge from a stale seat checkout whose
    reader died on newer ledger vocabulary while doctor could not see the
    skew. Adopted banks are verified completely before reading a literal
    reader version without execution. Legacy checkouts retain their Git and
    child-interpreter observations alongside an adoption-required finding.
    """

    adapters_root = root.resolve_relative("state/wake-daemon/adapters")
    bindings: list[tuple[str, Path]] = []
    if adapters_root.is_dir() and not adapters_root.is_symlink():
        for node_dir in sorted(adapters_root.iterdir()):
            if node_dir.is_symlink() or not node_dir.is_dir():
                continue
            for binding_path in sorted(node_dir.glob("*.json")):
                if binding_path.is_symlink() or not binding_path.is_file():
                    continue
                if binding_path.stem != "zcode":
                    continue
                bindings.append((node_dir.name, binding_path))
    if not bindings:
        return [_finding(
            "bridge_registrations_absent",
            "ok",
            str(root.path),
            "no zcode seat is bound on this root; no bridge registration to name "
            "(typed absence, not a silent pass)",
        )]

    findings: list[Dict[str, object]] = []
    source_root = source_root or Path(__file__).resolve().parents[1]
    installer = shlex.quote(str(source_root / "scripts/install-zcode-bridge.sh"))
    adoption_remedy = (f"re-apply the private bridge adoption: sh {installer} --dry-run; "
                       f"then sh {installer} --apply PLAN_DIGEST from that dry-run")
    from .bridge_rebank import bridge_rebank_remedy
    rebank_remedy = (bridge_rebank_remedy(source_root) + ". Future fixed-install updates require "
                    "--profile-registry and --fleet-profile so the adopted bank refresh is receipted.")
    for node_name, binding_path in bindings:
        try:
            record = json.loads(binding_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            record = {}
        workspace = record.get("workspace") if isinstance(record, dict) else None
        commands: list[tuple[Path, str]] = []
        if isinstance(workspace, str) and workspace:
            for relative in (".zcode/settings.json", ".zcode/hooks.json", "hooks/hooks.json"):
                candidate = Path(workspace) / relative
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    document = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    continue
                stop_entries = (document or {}).get("hooks", {}).get("Stop") or []
                for entry in stop_entries:
                    for hook in entry.get("hooks") or []:
                        command = hook.get("command")
                        if isinstance(command, str):
                            commands.append((candidate, command))
        # The same registration is often installed in settings.json and
        # hooks.json alike; one command is one bridge, not two.
        seen: set[tuple[str, str]] = set()
        unique: list[tuple[Path, str]] = []
        for source, command in commands:
            key = (str(source), command)
            if key not in seen:
                seen.add(key)
                unique.append((source, command))
        if not unique:
            findings.append(_finding(
                "bridge_registration_unnameable",
                "warning",
                f"{node_name}/zcode",
                "the bound zcode seat carries no readable Stop-hook bridge "
                "registration; the checkout its wake path runs cannot be audited",
                adoption_remedy,
            ))
            continue
        for source, command in unique:
            parsed = parse_bridge_command(command)
            if parsed is None:
                findings.append(_finding(
                    "bridge_adoption_required",
                    "warning",
                    f"{node_name}/zcode",
                    f"the Stop registration in {source} does not select the verified portable bank; "
                    f"seat {node_name} at {workspace} requires adoption",
                    adoption_remedy,
                ))
                continue
            if parsed.get("adopted") == "portable":
                from .bridge_bank import BridgeBankUnavailable, _verify_store
                from .bridge_rebank import BridgeRebankUnavailable, _portable_command
                try:
                    verified = _verify_store(Path.home())
                    expected_command = _portable_command(Path.home(),
                        bridge_payload=verified["files"]["hooks/stop-hook-bridge.py"])
                except (BridgeBankUnavailable, BridgeRebankUnavailable, OSError, ValueError) as exc:
                    findings.append(_finding(
                        "bridge_bank_unavailable", "warning", f"{node_name}/zcode",
                        f"registration {source}: {exc}", rebank_remedy))
                    continue
                if command != expected_command:
                    findings.append(_finding(
                        "bridge_adoption_required", "warning", f"{node_name}/zcode",
                        f"registration {source} differs from the verified private provider identity",
                        adoption_remedy))
                    continue
                repo = verified["runtime"]
                commit = verified["source_sha"]
                reader = verified["reader_schema_version"]
                observation = {"repo": str(repo), "commit": commit,
                    "runtime_source_sha": commit, "runtime_bank": str(verified["bank"]),
                    "authority_repo": parsed["hook_repo"], "reader_schema_version": reader,
                    "hook_repo_pinned": True, "registration": str(source)}
                newest_version = max((int(value) for value in merged_versions.values()), default=None)
                if newest_version is not None and newest_version > int(reader):
                    newest_kind = sorted(kind for kind, value in merged_versions.items()
                                         if int(value) == newest_version)[0]
                    finding = _finding("bridge_older_reader", "warning", f"{node_name}/zcode",
                        f"verified bridge runtime {repo} at source {commit} has reader version {reader}; "
                        f"ledger kind {newest_kind} needs version {newest_version}", adoption_remedy)
                    finding["older_readers"] = [{"reader_schema_version": reader,
                                                  "ledger_newest_kind": newest_kind}]
                else:
                    finding = _finding("bridge_reader_current", "ok", f"{node_name}/zcode",
                        f"verified bridge runtime {repo} at source {commit} (reader version {reader}); "
                        f"authority checkout {parsed['hook_repo']}")
                finding["bridge"] = observation
                findings.append(finding)
                continue
            findings.append(_finding(
                "bridge_adoption_required", "warning", f"{node_name}/zcode",
                f"legacy bridge registration {source} for seat {node_name} at {workspace} "
                "does not select the verified portable bank", adoption_remedy))
            pin = parsed["hook_repo"]
            script = parsed["script"]
            repo = Path(pin) if pin else Path(script).parent.parent
            commit = _bridge_checkout_commit(repo)
            if commit is None:
                findings.append(_finding(
                    "bridge_checkout_unnameable",
                    "warning",
                    f"{node_name}/zcode",
                    f"the bridge checkout {repo} does not answer git rev-parse HEAD",
                    adoption_remedy,
                ))
                continue
            reader = _bridge_reader_version(repo)
            if reader is None or not reader.isdigit():
                findings.append(_finding(
                    "bridge_reader_unnameable",
                    "warning",
                    f"{node_name}/zcode",
                    f"the bridge checkout {repo} (commit {commit[:12]}) does not "
                    "expose a numeric floati.records.READER_VERSION",
                    adoption_remedy,
                ))
                continue
            observation = {
                "repo": str(repo),
                "commit": commit,
                "reader_schema_version": reader,
                "hook_repo_pinned": bool(pin),
                "registration": str(source),
            }
            if merged_versions:
                newest_version = max(int(v) for v in merged_versions.values())
                newest_kind = sorted(
                    kind for kind, version in merged_versions.items()
                    if int(version) == newest_version
                )[0]
            else:
                newest_version, newest_kind = None, None
            if newest_version is not None and newest_version > int(reader):
                finding = _finding(
                    "bridge_older_reader",
                    "warning",
                    f"{node_name}/zcode",
                    f"the zcode bridge runs floati from {repo} at commit "
                    f"{commit[:12]} (reader version {reader}); the ledger carries "
                    f"kind {newest_kind} at version {newest_version}, above this "
                    "reader's vocabulary - the measured BRIDGE-1 deafness",
                    adoption_remedy,
                )
                finding["older_readers"] = [{
                    "reader_schema_version": reader,
                    "ledger_newest_kind": newest_kind,
                }]
            else:
                finding = _finding(
                    "bridge_legacy_reader",
                    "warning",
                    f"{node_name}/zcode",
                    f"the zcode bridge runs floati from {repo} at commit "
                    f"{commit[:12]} (reader version {reader}); private adoption is required",
                    adoption_remedy,
                )
            finding["bridge"] = observation
            findings.append(finding)
    return findings


def _format_cpu_seconds(value: float) -> str:
    return f"{value:.2f}"


def _format_cpu_hour(value: float) -> str:
    rounded = round(value)
    if abs(value - rounded) < 1e-9:
        return str(int(rounded))
    return f"{value:.2f}"


def _wake_daemon_undelivered_count(root: FloatiRoot, node_id: str) -> int:
    """Count undelivered envelopes for one node (WD-5 doctor line).

    Same receipt cut as DeliveryHealthAnalyzer: message_envelope without a
    delivery receipt for that recipient. Measured, never guessed.
    """

    from .delivery_health import _receipt_item_times
    from .events import EventLog
    from .mail_summary import observe
    from .send_prefix import _CACHE_ERRORS

    mail = observe(root)
    if mail is not None:
        try:
            return int(mail.node(node_id)["undelivered_count"])
        except _CACHE_ERRORS:
            pass
    delivered = set(_receipt_item_times(root, "deliveries", node_id))
    count = 0
    for record in EventLog(root).event_records():
        if record.get("kind") != "message_envelope":
            continue
        if record.get("recipient") != node_id:
            continue
        if record.get("id") not in delivered:
            count += 1
    return count


def project_wake_daemon_health(
    root: FloatiRoot, *, currency_current: bool
) -> list[Dict[str, object]]:
    """Derive one wake_daemon_health finding per bound wake daemon coordinate.

    WD-2 GAP 2: a breaker-open daemon was reported nowhere. The runtime the
    daemon already writes carries the whole story; absence of any binding is
    stated as a typed absence, never as silence.
    """

    from .wake_daemon import _BREAKER_THRESHOLD, WAKE_BREAKER_REMEDY, wake_breaker_remedy

    _WAKE_UNRESUMABLE_REMEDY_W = WAKE_BREAKER_REMEDY

    adapters_root = root.resolve_relative("state/wake-daemon/adapters")
    bindings: list[tuple[str, str, Path]] = []
    if adapters_root.is_dir() and not adapters_root.is_symlink():
        for node_dir in sorted(adapters_root.iterdir()):
            if node_dir.is_symlink() or not node_dir.is_dir():
                continue
            for binding_path in sorted(node_dir.glob("*.json")):
                if binding_path.is_symlink() or not binding_path.is_file():
                    continue
                bindings.append((node_dir.name, binding_path.stem, binding_path))
    if not bindings:
        return [_finding(
            "wake_daemon_health",
            "ok",
            str(root.path),
            "no wake daemon is bound for any node/harness (typed absence, not a silent pass)",
        )]

    runtime_dir = root.resolve_relative("state/wake-daemon/runtime")
    findings: list[Dict[str, object]] = []
    for node_name, harness, binding_path in bindings:
        subject = f"{node_name}/{harness}"
        try:
            binding_record = json.loads(binding_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            binding_record = {}
        resume_state = binding_record.get("resume_state")
        bound_session = binding_record.get("session_id")
        coordinate: Optional[object] = None
        try:
            from .wake_daemon_contract import DaemonCoordinate

            coordinate = DaemonCoordinate(root, node_name, harness)
        except ProtocolRefusal as exc:
            findings.append(_exception_finding(
                exc,
                "warning",
                subject,
                WAKE_BREAKER_REMEDY if currency_current else None,
                code="wake_daemon_health",
                detail="binding present but its coordinate no longer resolves (retired node or unsupported harness)",
            ))
            continue
        runtime_path = runtime_dir / f"{coordinate.digest}.json"  # type: ignore[union-attr]
        if not runtime_path.is_file() or runtime_path.is_symlink():
            findings.append(_finding(
                "wake_daemon_health",
                "warning" if resume_state == "resume_suspect" else "ok",
                subject,
                "binding present but the daemon runtime is absent (no cycle has completed); breaker state is not yet derivable"
                + (f"; resume_state={resume_state} session_id={bound_session}" if resume_state else ""),
                _WAKE_UNRESUMABLE_REMEDY_W if resume_state == "resume_suspect" and currency_current else None,
            ))
            continue
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            findings.append(_finding(
                "wake_daemon_health",
                "error",
                subject,
                f"daemon runtime {runtime_path.name} is present but unreadable; durable breaker testimony is malformed",
            ))
            continue
        circuit_state = runtime.get("circuit_state")
        refusals = runtime.get("consecutive_refusals")
        backoff = runtime.get("current_backoff")
        last_state = runtime.get("last_state")
        last_reason = runtime.get("last_reason_code")
        session_digest = str(runtime.get("session_digest", ""))[:12]
        if (
            not isinstance(runtime, dict)
            or not isinstance(circuit_state, str)
            or not isinstance(refusals, int)
            or isinstance(refusals, bool)
            or not isinstance(backoff, int)
            or isinstance(backoff, bool)
        ):
            findings.append(_finding(
                "wake_daemon_health",
                "error",
                subject,
                f"daemon runtime {runtime_path.name} lacks its breaker testimony; durable breaker evidence is malformed",
            ))
            continue
        detail = (
            f"circuit_state={circuit_state} consecutive_refusals={refusals} "
            f"current_backoff={backoff} last_state={last_state} "
            f"last_reason_code={last_reason} session_digest={session_digest}"
            f" resume_state={resume_state if resume_state else 'unrecorded'}"
            + (f" session_id={bound_session}" if resume_state else "")
        )
        node_label = str(runtime.get("node_id") or node_name)
        cpu_window = runtime.get("cpu_seconds_window")
        wall_window = runtime.get("cpu_wall_window")
        extra_lines: list[str] = []
        if (
            isinstance(cpu_window, list)
            and isinstance(wall_window, list)
            and len(cpu_window) == len(wall_window)
            and len(cpu_window) >= 10
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in cpu_window)
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in wall_window)
        ):
            count = len(cpu_window)
            cpu_total = sum(float(item) for item in cpu_window)
            wall_total = sum(float(item) for item in wall_window)
            per_cycle = cpu_total / count
            per_hour = (cpu_total / wall_total * 3600.0) if wall_total else 0.0
            extra_lines.append(
                f"wake daemon {node_label}: {_format_cpu_seconds(per_cycle)} s CPU per cycle, "
                f"{_format_cpu_hour(per_hour)} s per hour, measured over the last {count} cycles."
            )
        elif isinstance(cpu_window, list) and 0 < len(cpu_window) < 10:
            extra_lines.append("not enough cycles to measure")
        if last_reason == "wake_daemon_cycle_over_budget":
            extra_lines.append(
                f"wake daemon {node_label} is over its CPU budget and has backed off to {backoff} s between polls."
            )
        if last_state == "paused":
            extra_lines.append(
                f"wake daemon {node_label} is paused and polls every {backoff} s."
            )
        # WD-4 registration: the idle-retire finding lives in doctor_wd_4.
        extra_lines.extend(doctor_wd_4.idle_retire_lines(node_label, runtime))
        # WD-5: dormant seat holding undelivered mail, from WD-3 holder
        # testimony. Live holder or no undelivered mail: nothing new.
        undelivered = _wake_daemon_undelivered_count(root, node_name)
        if undelivered > 0:
            from .codex_wait_liveness import classify_holder, read_holder_testimony

            testimony = read_holder_testimony(root, node_name)
            holder_class = classify_holder(testimony)
            if holder_class == "released" and testimony is not None:
                extra_lines.append(
                    f"wake daemon {node_label}: {undelivered} undelivered; "
                    f"no live seat since {testimony.timestamp}. "
                    "Restart the seat to drain them."
                )
            elif holder_class == "unproven":
                extra_lines.append(
                    f"Floati cannot tell whether {node_label} has a live seat; "
                    f"{undelivered} messages are undelivered."
                )
        if extra_lines:
            detail = detail + "".join("\n" + line for line in extra_lines)
        if resume_state == "resume_suspect" or circuit_state == "open" or refusals >= _BREAKER_THRESHOLD:
            # F2-TAKEOVER-DAEMON-1: the same act the daemon's own notice names
            # - a Codex seat cannot perform `wake daemon bind`.
            findings.append(_finding(
                "wake_daemon_health",
                "warning",
                subject,
                detail,
                wake_breaker_remedy(harness, last_reason, runtime_path)
                if currency_current
                else None,
            ))
        else:
            findings.append(_finding(
                "wake_daemon_health",
                "ok",
                subject,
                detail,
            ))

    # WD-R8: the hook half of the surface - a bound zcode seat proves its
    # registration by observed dispatches, never by read-back. Reported
    # beside trust and breaker state: trust, shadowing, wakeability.
    # A host-wide bridge installation neither proves nor disproves this
    # root's binding or firing history. Only the root-local records decide.
    for node_name, harness, _binding_path in bindings:
        if harness != "zcode":
            continue
        firings_path = root.resolve_relative(
            Path("state/zcode-hook/firings") / f"{node_name}.jsonl"
        )
        last = None
        if firings_path.is_file() and not firings_path.is_symlink():
            try:
                rows = [
                    json.loads(line)
                    for line in firings_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                last = rows[-1] if rows else None
            except (OSError, json.JSONDecodeError, ValueError):
                last = None
        if last is None:
            findings.append(_finding(
                "hook_unproven",
                "warning",
                f"{node_name}/{harness}",
                "zcode hook registration is bound but no dispatch has ever been observed; "
                "read-back proves what was written, only a firing proves what will run",
                "end a turn with the hook enabled; relaunch the harness session if it "
                "trust-gates the hook" if currency_current else None,
            ))
        else:
            findings.append(_finding(
                "hook_firing_observed",
                "ok",
                f"{node_name}/{harness}",
                f"zcode hook observed firing (last dispatch {last.get('timestamp')}, "
                f"injected={last.get('injected')})",
            ))
    return findings


def project_zcode_installed_hook_findings(root: FloatiRoot) -> list[Dict[str, object]]:
    """Name each zcode Stop hook recorded by hook install, with its hold."""

    wait_root = root.path / "state" / "zcode-wait"
    if not wait_root.is_dir() or wait_root.is_symlink():
        return []
    findings: list[Dict[str, object]] = []
    for node_dir in sorted(path for path in wait_root.iterdir() if not path.is_symlink()):
        if not node_dir.is_dir():
            continue
        installed_path = node_dir / "installed-hook.json"
        if not installed_path.is_file() or installed_path.is_symlink():
            continue
        try:
            installed = json.loads(installed_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(installed, dict):
            continue
        raw_hooks = installed.get("hooks_path")
        if not isinstance(raw_hooks, str) or not raw_hooks:
            continue
        hooks_path = Path(raw_hooks)
        if not hooks_path.is_absolute() or hooks_path.is_symlink() or not hooks_path.is_file():
            continue
        try:
            document = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        stops = document.get("hooks", {}).get("Stop") if isinstance(document.get("hooks"), dict) else None
        if not isinstance(stops, list):
            continue
        for entry in stops:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            for hook in entry["hooks"]:
                if not isinstance(hook, dict) or type(hook.get("timeout")) is not int:
                    continue
                command = hook.get("command")
                if not isinstance(command, str):
                    continue
                try:
                    argv = shlex.split(command)
                except ValueError:
                    continue
                if argv[1:5] != ["wake", "wait", "--harness", "zcode"]:
                    continue
                flags = dict(zip(argv[5::2], argv[6::2]))
                try:
                    deadline = int(flags["--deadline-seconds"])
                except (KeyError, TypeError, ValueError):
                    continue
                timeout = hook["timeout"]
                finding = _finding(
                    "zcode_hook_installed",
                    "ok",
                    f"{node_dir.name}/zcode",
                    f"zcode Stop hook {hooks_path} timeout {timeout} s, waiter deadline {deadline} s",
                )
                finding["hook_timeout_seconds"] = timeout
                finding["wait_deadline_seconds"] = deadline
                finding["hooks_path"] = str(hooks_path)
                findings.append(finding)
    return findings


def project_cursor_hook_workspace_findings(
    root: FloatiRoot,
) -> list[Dict[str, object]]:
    """Name a Cursor seat whose hook workspace differs from last-seen root.

    Typed silence when the journal or the install receipt cannot tell.
    """

    wait_root = root.path / "state" / "cursor-wait"
    if not wait_root.is_dir() or wait_root.is_symlink():
        return []
    findings: list[Dict[str, object]] = []
    for node_dir in sorted(wait_root.iterdir()):
        if node_dir.is_symlink() or not node_dir.is_dir():
            continue
        installed_path = node_dir / "installed-hook.json"
        journal_path = node_dir / "journal.jsonl"
        hook_workspace = None
        if installed_path.is_file() and not installed_path.is_symlink():
            try:
                installed = json.loads(installed_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                installed = {}
            if isinstance(installed, dict):
                value = installed.get("workspace")
                if isinstance(value, str) and value:
                    hook_workspace = value
        last_seen = None
        if journal_path.is_file() and not journal_path.is_symlink():
            try:
                lines = journal_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                value = record.get("last_seen_root")
                if isinstance(value, str) and value:
                    last_seen = value
                    break
        if not hook_workspace or not last_seen:
            continue
        try:
            same = Path(hook_workspace).resolve() == Path(last_seen).resolve()
        except (OSError, RuntimeError):
            same = Path(hook_workspace) == Path(last_seen)
        if same:
            continue
        node_name = node_dir.name
        findings.append(_finding(
            "cursor_hook_workspace_diverged",
            "warning",
            node_name,
            (
                f"Cursor seat {node_name} hook workspace {hook_workspace} "
                f"differs from its journal's last-seen root {last_seen}"
            ),
            "this hook is scoped to the workspace that owns .cursor/hooks.json; "
            "re-rooting the chat leaves it behind — work other directories by absolute path",
        ))
    return findings


def installed_bus_watch_paths() -> Optional[tuple[Path, Path]]:
    """Return the ruled OpenCode plugin path and its install-time source SHA."""

    plugin = Path.home() / ".config" / "opencode" / "plugins" / "floati-bus-watch.ts"
    if not plugin.is_file() or plugin.is_symlink():
        return None
    return plugin, plugin.with_suffix(".sha256")


def _read_sha256_sidecar(path: Path) -> Optional[str]:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value if _SHA256.fullmatch(value) is not None else None


def project_installed_bus_watch_currency(
    source_root: Path, *, currency_current: bool
) -> Dict[str, object]:
    """Observe the host watcher without making host access a root failure."""

    try:
        return _installed_bus_watch_currency(source_root, currency_current=currency_current)
    except (OSError, UnicodeError) as exc:
        return _host_finding(
            "bus_watch_unavailable",
            "bus_watch",
            f"host bus-watch currency is unobserved: {type(exc).__name__}",
        )


def _installed_bus_watch_currency(
    source_root: Path, *, currency_current: bool
) -> Dict[str, object]:
    """Project deployed bus-watch drift without refusing an unnameable copy."""

    paths = installed_bus_watch_paths()
    if paths is None:
        return _host_finding(
            "bus_watch_uninstalled",
            str(Path.home() / ".config/opencode/plugins/floati-bus-watch.ts"),
            "no installed OpenCode bus watcher found; deployed-source currency is absent",
        )
    installed, sidecar = paths
    recorded_sha = _read_sha256_sidecar(sidecar)
    try:
        installed_sha = hashlib.sha256(installed.read_bytes()).hexdigest()
    except OSError:
        installed_sha = None
    if recorded_sha is None or installed_sha is None or recorded_sha != installed_sha:
        return _host_finding(
            "bus_watch_installed_source_unnameable",
            str(installed),
            "installed OpenCode bus watcher is present but its install-time source SHA cannot name these bytes",
            (
                "reinstall the OpenCode bus watcher from a complete source tree so the owner can activate current watcher code"
            ),
        )
    repository_watch = Path(source_root) / BUS_WATCH_SOURCE
    try:
        repository_sha = (
            hashlib.sha256(repository_watch.read_bytes()).hexdigest()
            if repository_watch.is_file() and not repository_watch.is_symlink()
            else None
        )
    except OSError:
        repository_sha = None
    if repository_sha is None:
        return _host_finding(
            "bus_watch_repository_source_unnameable",
            str(repository_watch),
            "repository OpenCode bus-watch source cannot be named for comparison",
            (
                "rerun Doctor with a complete Floati source before claiming deployed-source currency"
            ),
        )
    if recorded_sha == repository_sha:
        return _host_finding(
            "bus_watch_current",
            str(installed),
            f"installed watcher matches the repository (from {recorded_sha[:12]})",
        )
    return _host_finding(
        "bus_watch_drift",
        str(installed),
        (
            f"installed watcher from {recorded_sha[:12]}, "
            f"repository at {repository_sha[:12]}"
        ),
        (
            "reinstall the OpenCode bus watcher from a complete source tree so the owner can activate current watcher code"
            if currency_current
            else None
        ),
    )


def retention_census(root: FloatiRoot) -> Dict[str, int]:
    """Count retained, expired, and keep-forever epochs and measure survivor chunks.

    Expired means removal testimony or a decision that the payload is due for
    removal. Survivor bytes are the regular files in the history chunk directory.
    """
    from .archive_catalog import CHUNK_DIRECTORY
    from .archive_retention import iter_roll_receipts, removed_epoch_ids
    from .archive_retention_policy import read_archive_policy, retention_decision

    policy = read_archive_policy(root)
    removed = removed_epoch_ids(root)
    observed = datetime.now(timezone.utc)
    retained = expired = forever = 0
    seen = set()
    for receipt in iter_roll_receipts(root):
        epoch_id = receipt.get("epoch_id")
        if epoch_id in seen:
            continue
        seen.add(epoch_id)
        decision = retention_decision(receipt, policy, observed)
        if epoch_id in removed or decision["expired"]:
            expired += 1
            continue
        retained += 1
        if decision["keep_forever"]:
            forever += 1
    for epoch_id in removed:
        if epoch_id not in seen:
            expired += 1
    directory = root.tenant_home / CHUNK_DIRECTORY
    measured = 0
    if directory.is_dir() and not directory.is_symlink():
        for path in directory.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            measured += path.stat().st_size
    return {
        "retained": retained,
        "expired": expired,
        "keep_forever": forever,
        "survivor_checkpoint_bytes": measured,
    }


def project_fleet_update(root: FloatiRoot) -> Dict[str, object]:
    """Read one root's complete fleet-update history without opening a lock.

    Receipt files are product-owned evidence.  Every actor file must validate;
    a corrupt sibling is an integrity failure, never an excuse to return a
    healthy partial projection.
    """

    from .fleet_update_receipts import (
        FleetUpdateReceiptLedger,
        _authenticated_recovery_witness,
        _physical_step_kinds,
        _step_spec,
    )

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("fleet_update_root_invalid", "fleet_update_root_invalid")
    # Keep the lexical product-owned coordinate so a dangling symlink is
    # classified as evidence corruption before containment resolution follows it.
    directory = root.path / "receipts" / "fleet-update"
    try:
        if directory.is_symlink():
            raise IntegrityFailure("fleet_update_receipt_invalid", "fleet_update_receipt_invalid")
        if not directory.exists():
            return {"state": "absent", "actors": []}
        if not directory.is_dir():
            raise IntegrityFailure("fleet_update_receipt_invalid", "fleet_update_receipt_invalid")
        actor_paths = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise IntegrityFailure("fleet_update_receipt_invalid", "fleet_update_receipt_invalid") from exc
    histories: list[tuple[str, list[Dict[str, object]]]] = []
    for path in actor_paths:
        if path.name.endswith(".jsonl.lock") and path.is_file() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".jsonl":
            raise IntegrityFailure("fleet_update_receipt_invalid", "fleet_update_receipt_invalid")
        actor = path.stem
        try:
            rows = FleetUpdateReceiptLedger(root).rows(actor)
        except (IntegrityFailure, ProtocolRefusal, OSError) as exc:
            raise IntegrityFailure("fleet_update_receipt_invalid", "fleet_update_receipt_invalid") from exc
        histories.append((actor, rows))
    actors = [actor for actor, _rows in histories]
    operations: list[tuple[str, Dict[str, object], list[Dict[str, object]]]] = []
    for actor, rows in histories:
        keys = sorted({str(row["idempotency_key"]) for row in rows})
        for key in keys:
            related = [row for row in rows if row["idempotency_key"] == key]
            start = next(row for row in related if row["kind"] == "fleet_update_started")
            operations.append((actor, start, related))
    if not operations:
        return {"state": "absent", "actors": actors}
    def operation_projection(
        actor: str, start: Dict[str, object], related: list[Dict[str, object]]
    ) -> Dict[str, object]:
        plan = _authenticated_recovery_witness(start, root)
        completed_steps = [row for row in related if row["kind"] == "fleet_update_step"]
        kinds = _physical_step_kinds(plan)
        if len(completed_steps) > len(kinds):
            raise IntegrityFailure("fleet_update_receipt_invalid", "fleet_update_receipt_invalid")
        expected = [_step_spec(plan, plan["seat_binding_consequences"], ordinal) for ordinal in range(1, len(kinds) + 1)]
        for row, spec in zip(completed_steps, expected):
            if any(row.get(field) != value for field, value in spec.items()):
                raise IntegrityFailure("fleet_update_receipt_invalid", "fleet_update_receipt_invalid")
        completion = next((row for row in related if row["kind"] == "fleet_update_completed"), None)
        return {
            "state": "completed" if completion is not None else "in_progress",
            "actor": actor,
            "started_at": start["timestamp"],
            "idempotency_key": start["idempotency_key"],
            "plan_digest": start["plan_digest"],
            "consent_receipt_id": start["consent_receipt_id"],
            "last_completed_step_receipt_id": completed_steps[-1]["id"] if completed_steps else None,
            "completion_receipt_id": completion["id"] if completion is not None else None,
            "remaining_steps": [{"kind": spec["step_kind"], "ordinal": spec["step_ordinal"], "coordinate": spec["step_coordinate"]} for spec in expected[len(completed_steps):]],
            "epoch_roll_state": completion["epoch_roll_state"] if completion is not None else ("required" if plan["requires_epoch_roll"] else "not_required"),
            "current_transport_pins": plan["current_transport_pins"],
            "target_transport_pins": plan["target_transport_pins"],
            "owner_review_batch_digest": plan["owner_review_batch_digest"],
            "reader_consequences": plan["reader_consequences"],
            "seat_binding_consequences": plan["seat_binding_consequences"],
            "seat_exclusions": plan["seat_exclusions"],
        }
    projected_operations = [operation_projection(*operation) for operation in operations]
    in_progress = [
        item for item in projected_operations if item["state"] == "in_progress"
    ]
    aggregate_state = "in_progress" if in_progress else "completed"
    current = max(
        in_progress if in_progress else projected_operations,
        key=lambda item: (
            str(item["started_at"]), str(item["plan_digest"]),
            str(item["idempotency_key"]), str(item["actor"]),
        ),
    )
    return {
        **current,
        "state": aggregate_state,
        "actors": actors,
        "operations": projected_operations,
    }


def _codex_gateway_finding(
    source_root: Path,
    host_path: Path,
) -> Dict[str, object] | None:
    vendored = source_root / CODEX_GATEWAY_SOURCE
    digest_path = source_root / CODEX_GATEWAY_DIGEST
    if not vendored.exists() and not digest_path.exists():
        return None
    remediation = shlex.quote(str(source_root / "scripts/install-codex-gateway.sh"))
    if (
        vendored.is_symlink()
        or digest_path.is_symlink()
        or not vendored.is_file()
        or not digest_path.is_file()
    ):
        return _finding(
            "codex_gateway_vendored_source_invalid",
            "error",
            str(vendored),
            "vendored gateway source and digest must both be regular non-symlink files",
            "restore the governed vendored gateway source and digest, then rerun doctor",
        )
    try:
        source_data = vendored.read_bytes()
        record = json.loads(digest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _finding(
            "codex_gateway_vendored_source_invalid",
            "error",
            str(vendored),
            "vendored gateway source or digest record is unreadable",
            "restore the governed vendored gateway source and digest, then rerun doctor",
        )
    expected_fields = {"path", "schema_version", "sha256"}
    expected_digest = record.get("sha256") if isinstance(record, dict) else None
    if (
        not isinstance(record, dict)
        or set(record) != expected_fields
        or record.get("schema_version") != 0
        or record.get("path") != CODEX_GATEWAY_SOURCE.as_posix()
        or not isinstance(expected_digest, str)
        or _SHA256.fullmatch(expected_digest) is None
    ):
        return _finding(
            "codex_gateway_vendored_source_invalid",
            "error",
            str(digest_path),
            "vendored gateway digest record violates the version-zero contract",
            "restore the governed vendored gateway source and digest, then rerun doctor",
        )
    source_digest = hashlib.sha256(source_data).hexdigest()
    if source_digest != expected_digest:
        return _finding(
            "codex_gateway_vendored_source_digest_mismatch",
            "error",
            str(vendored),
            f"vendored source digest {source_digest} != recorded {expected_digest}",
            "ratify the source bytes and recorded digest together, then rerun doctor",
        )
    if host_path.is_symlink() or not host_path.is_file():
        return _finding(
            "codex_gateway_vendored_source_missing",
            "ok",
            str(host_path),
            "the optional fixed host gateway is not installed",
        )
    try:
        host_digest = hashlib.sha256(host_path.read_bytes()).hexdigest()
    except OSError:
        return _finding(
            "codex_gateway_vendored_source_unavailable",
            "warning",
            str(host_path),
            "the fixed host gateway digest cannot be read",
            remediation,
        )
    if host_digest != source_digest:
        return _finding(
            "codex_gateway_vendored_source_drift",
            "warning",
            str(host_path),
            (
                "gateway drifted from vendored source: "
                f"host {host_digest}; source {source_digest}"
            ),
            remediation,
        )
    return _finding(
        "codex_gateway_vendored_source_current",
        "ok",
        str(host_path),
        f"host and vendored gateway digests match at {source_digest}",
    )


def project_node_lease_findings(
    root: FloatiRoot, now: datetime
) -> list[Dict[str, object]]:
    """Name each active node whose lease is already past `now`."""

    from .registry import expired_node_leases, read_registry_compatible

    try:
        records, _unrecognized, _versions = read_registry_compatible(root)
        leases = expired_node_leases(root, records, now)
    except (IntegrityFailure, ProtocolRefusal):
        return []
    findings: list[Dict[str, object]] = []
    for lease in leases:
        node_id = str(lease["node_id"])
        expires_at = str(lease["expires_at"])
        findings.append(
            _finding(
                "node_lease_expired",
                "warning",
                node_id,
                f"node {node_id} lease expired at {expires_at}",
                str(lease["retire_command"]),
            )
        )
    return findings


class Doctor:
    """Inspect without creating roots, locks, receipts, or repair writes."""

    def __init__(
        self,
        source: Path | str,
        root: Path | str,
        *,
        ref: str = "origin/main",
        gateway_config: Path | str | None = None,
        destination: Path | str | None = None,
        profile: str | None = None,
        codex_hooks: Path | str | None = None,
        codex_config: Path | str | None = None,
        codex_gateway_host: Path | str | None = None,
        no_sandbox: bool = False,
    ) -> None:
        if profile is not None and profile not in RULED_PROFILES:
            raise ProtocolRefusal("doctor_profile_invalid", DOCTOR_PROFILE_INVALID_DETAIL)
        self.profile = profile
        self.source_arg = Path(source).expanduser()
        self.root_arg = Path(root).expanduser()
        self.ref = ref
        self.gateway_config_arg = (
            None if gateway_config is None else Path(gateway_config).expanduser()
        )
        self.destination_arg = destination
        self.codex_hooks_defaulted = codex_hooks is None and codex_config is None
        self.codex_hooks_arg = (
            _codex_home_path("hooks.json")
            if codex_hooks is None
            else Path(codex_hooks).expanduser()
        )
        self.codex_config_arg = None if codex_config is None else Path(codex_config).expanduser()
        self.codex_gateway_host_defaulted = codex_gateway_host is None
        self.codex_gateway_host_arg = (
            _codex_home_path("bin/codex-fleet-bus")
            if codex_gateway_host is None
            else Path(codex_gateway_host).expanduser()
        )
        self.no_sandbox = no_sandbox

    def _currency(self) -> tuple[Dict[str, object], bool]:
        source = self.source_arg
        if not source.is_absolute() or _is_symlink_entry(source) or not source.is_dir():
            return (
                _finding(
                    "deploy_currency_unavailable",
                    "warning",
                    str(source),
                    "source identity is not an absolute non-symlink directory",
                ),
                False,
            )
        try:
            if is_shallow_repository(source):
                return (
                    _finding(
                        "shallow_repository",
                        "warning",
                        str(source),
                        "source repository is shallow; deployment currency cannot be trusted",
                        "fetch complete history, then rerun doctor",
                    ),
                    False,
                )
        except ProtocolRefusal as exc:
            return _exception_finding(exc, "warning", str(source)), False
        try:
            status = _git(source, "status", "--porcelain=v1", "--untracked-files=all")
            head = _git(source, "rev-parse", "--verify", "HEAD^{commit}")
            target = _git(source, "rev-parse", "--verify", f"{self.ref}^{{commit}}")
        except ProtocolRefusal as exc:
            return _exception_finding(exc, "warning", str(source)), False
        if status:
            return (
                _finding(
                    "deploy_currency_unavailable",
                    "warning",
                    str(source),
                    "source tree is not clean",
                ),
                False,
            )
        if head != target:
            return (
                _finding(
                    "deploy_currency_unavailable",
                    "warning",
                    str(source),
                    f"HEAD {head} is not {self.ref} ({target})",
                ),
                False,
            )
        return (
            _finding(
                "deploy_currency_current",
                "ok",
                str(source),
                f"clean HEAD equals {self.ref} at {head}",
            ),
            True,
        )

    def artifact(self, now: Optional[datetime] = None) -> tuple[Dict[str, object], int]:
        findings: list[Dict[str, object]] = []
        unrecognized_kinds: list[Dict[str, object]] = []
        rc = 0
        if now is None:
            observed = datetime.now(timezone.utc)
        elif now.tzinfo is None or now.utcoffset() is None:
            raise ProtocolRefusal(
                "time_invalid", "lease evaluation requires an aware datetime"
            )
        else:
            observed = now.astimezone(timezone.utc)

        root: Optional[FloatiRoot] = None
        if not self.root_arg.is_absolute():
            findings.append(_finding("root_not_absolute", "error", str(self.root_arg), "root must be absolute"))
            rc = 20
        elif _is_symlink_entry(self.root_arg):
            findings.append(_finding("direct_home_symlinked_entry", "error", str(self.root_arg), "root identity includes a symlink"))
            rc = 20
        else:
            try:
                root = FloatiRoot.open_direct_home(self.root_arg, create=False)
            except ProtocolRefusal as exc:
                findings.append(_exception_finding(exc, "error", str(self.root_arg)))
                rc = 20
            else:
                findings.append(_finding("root_valid", "ok", str(self.root_arg), "direct-home root is valid"))

        if root is not None:
            findings.extend(project_root_layout_findings(root))
            findings.extend(project_seat_declaration_findings(root))
            findings.extend(project_seat_execution_findings(root))
            try:
                from .projection import AdhocThreadStatusProjection
                adhoc = AdhocThreadStatusProjection(root).artifact(_utc_now())
                if adhoc["profiles"]:
                    rendered = json.dumps(adhoc, sort_keys=True, ensure_ascii=True)
                    if len(rendered) > 8192:
                        rendered = json.dumps({"state": "UNKNOWN", "reason": "adhoc_projection_limit"})
                    findings.append(_finding("adhoc_threads", "warning", str(root.path), rendered))
            except (IntegrityFailure, ProtocolRefusal, OSError) as exc:
                findings.append(_finding("adhoc_threads", "warning", str(root.path),
                    json.dumps({"state": "UNKNOWN", "reason": getattr(exc, "code", "adhoc_read_unavailable")})))
            rc = project_disk_watch(root, findings, rc)
            findings.extend(project_hold_findings(root))
            findings.extend(project_inbound_hold_findings(root))
            findings.extend(project_node_lease_findings(root, observed))
            findings.append(project_ledger_roll_policy_finding(root))
            try:
                from .bus_epoch import reconcile_epoch_roll

                epoch_roll = reconcile_epoch_roll(root)
            except (IntegrityFailure, ProtocolRefusal) as exc:
                findings.append(_exception_finding(exc, "error", str(root.path)))
                rc = 33
                # A surviving epoch marker owns the selected-plane truth until
                # reconciliation succeeds.  Do not let later health readers
                # interpret a deliberately partial COMMITTED transition as an
                # ordinary live ledger after recovery has failed closed.
                root = None
            else:
                if epoch_roll is not None:
                    finding = _finding(
                        "bus_epoch_roll_reconciled",
                        "ok",
                        str(root.path.resolve()),
                        "incomplete bus epoch roll was reconciled from durable disk state",
                    )
                    finding["epoch_roll"] = epoch_roll
                    findings.append(finding)

        if root is not None:
            try:
                from .workspace_layout import inspect_workspace_layout

                workspace_findings = [
                    _workspace_layout_finding(row)
                    for row in inspect_workspace_layout(root)
                ]
            except IntegrityFailure as exc:
                findings.append(
                    _exception_finding(exc, "error", str(root.path))
                )
                if rc != 20:
                    rc = 33
            except ProtocolRefusal as exc:
                findings.append(
                    _exception_finding(exc, "error", str(root.path))
                )
                if rc not in (20, 33):
                    rc = 35
            else:
                findings.extend(workspace_findings)
                if (
                    any(
                        row["severity"] in {"warning", "error"}
                        for row in workspace_findings
                    )
                    and rc == 0
                ):
                    rc = 35

        currency_finding, currency_current = self._currency()

        launcher_interpreter_finding = project_launcher_interpreter()
        findings.append(launcher_interpreter_finding)
        if launcher_interpreter_finding["severity"] == "warning" and rc == 0:
            rc = 35

        interpreter_trust_finding = project_effect_reconciliation_interpreter_trust()
        findings.append(interpreter_trust_finding)
        if interpreter_trust_finding["severity"] == "warning" and rc == 0:
            rc = 35

        if self.gateway_config_arg is not None:
            try:
                config = GatewayConfig.load(self.gateway_config_arg)
            except ProtocolRefusal as exc:
                findings.append(
                    _exception_finding(
                        exc,
                        "error",
                        str(self.gateway_config_arg),
                        "provide an exact local stdio gateway v0 config, then rerun doctor"
                        if currency_current
                        else None,
                    )
                )
                rc = 20
            else:
                findings.append(
                    _finding(
                        "gateway_config_valid",
                        "ok",
                        str(config.path),
                        "explicit gateway config is local stdio, network-disabled, and fail-closed",
                    )
                )

        if root is not None:
            try:
                registry, _unrecognized, _versions = read_registry_compatible(root)
                latest: Dict[str, Dict[str, object]] = {}
                for row in registry:
                    if row["kind"] == "registry_entry":
                        latest[str(row["node_id"])] = row
                registered = {
                    node
                    for node, row in latest.items()
                    if row["state"] == "active"
                }
                liveness_dir = root.resolve_relative("liveness-presence")
                live_dirs = {
                    path.stem
                    for path in liveness_dir.glob("*.jsonl")
                    if path.is_file() and not path.is_symlink()
                } if liveness_dir.is_dir() else set()
                if registered == live_dirs:
                    findings.append(_finding("registry_live_dirs_match", "ok", str(root.path), "active registry and liveness directory identities match"))
                elif self.profile == "bus-only" and not live_dirs:
                    findings.append(_finding(
                        "registry_live_dirs_expected_absent", "ok", str(root.path),
                        DOCTOR_LIVE_DIRS_EXPECTED_ABSENT_DETAIL,
                    ))
                elif not live_dirs:
                    from .solo import read_solo

                    solo = read_solo(root, required=False)
                    solo_node = None if solo is None else str(solo["node_id"])
                    solo_row = latest.get(solo_node) if solo_node is not None else None
                    if (
                        solo is not None
                        and registered == {solo_node}
                        and solo_row is not None
                        and solo_row.get("role") == solo.get("harness")
                    ):
                        findings.append(_finding(
                            "registry_live_dirs_expected_idle",
                            "ok",
                            str(root.path),
                            "fresh solo identity is registered and no runtime presence has been claimed",
                        ))
                    else:
                        remediation = (
                            "reconcile registered nodes and liveness-presence files, then rerun doctor"
                            if currency_current else None
                        )
                        findings.append(_finding(
                            "registry_live_dirs_mismatch", "warning", str(root.path),
                            f"registered={sorted(registered)} live_dirs={sorted(live_dirs)}",
                            remediation,
                        ))
                        if rc == 0:
                            rc = 35
                else:
                    remediation = (
                        "reconcile registered nodes and liveness-presence files, then rerun doctor"
                        if currency_current else None
                    )
                    findings.append(_finding(
                        "registry_live_dirs_mismatch", "warning", str(root.path),
                        f"registered={sorted(registered)} live_dirs={sorted(live_dirs)}",
                        remediation,
                    ))
                    if rc == 0:
                        rc = 35

                registry_lineage = set(latest)
                from .cursor_presentations import CursorPresentationLedger
                for node in sorted(registered):
                    try:
                        CursorPresentationLedger(root).read(node)
                    except (IntegrityFailure, ProtocolRefusal) as exc:
                        findings.append(_exception_finding(exc, "error", str(CursorPresentationLedger.relative(node))))
                        if rc != 20:
                            rc = 33
                wake_root = root.resolve_relative("receipts/wake-coordination")
                wake_entries = (
                    list(wake_root.iterdir())
                    if wake_root.is_dir() and not wake_root.is_symlink()
                    else []
                )
                wake_names = {path.name for path in wake_entries}
                unsafe_entries = sorted(
                    path.name for path in wake_entries
                    if path.is_symlink() or not path.is_dir()
                )
                unknown_wake_names = sorted(wake_names - registry_lineage)
                if wake_root.is_symlink() or unsafe_entries or unknown_wake_names:
                    findings.append(_finding(
                        "wake_namespace_registry_mismatch",
                        "warning",
                        str(wake_root),
                        f"outside_registry={unknown_wake_names} unsafe_entries={unsafe_entries}",
                        "remove or quarantine non-registry wake coordinators after closing the minting path, then rerun doctor"
                        if currency_current else None,
                    ))
                    if rc == 0:
                        rc = 35
                else:
                    findings.append(_finding(
                        "wake_namespace_registry_subset",
                        "ok",
                        str(wake_root),
                        f"wake identities are a subset of registry lineage ({len(wake_names)}/{len(registry_lineage)})",
                    ))
            except ProtocolRefusal as exc:
                findings.append(_exception_finding(exc, "error", str(root.path)))
                if rc not in {33, 36}:
                    rc = 20
            except IntegrityFailure as exc:
                findings.append(_exception_finding(exc, "error", str(root.path)))
                rc = 33 if rc != 20 else rc

            # WD-2 wake-doctor health: the daemon's own runtime already knows
            # it has been failing (consecutive_refusals, current_backoff,
            # breaker state, last typed outcome) — surface it here, and keep
            # "nothing bound" a typed absence rather than a silent pass.
            wake_findings = project_wake_daemon_health(root, currency_current=currency_current)
            findings.extend(wake_findings)
            cursor_hook_findings = project_cursor_hook_workspace_findings(root)
            findings.extend(cursor_hook_findings)
            findings.extend(project_zcode_installed_hook_findings(root))
            from .claude_hook_install import project_claude_installed_hook_findings
            claude_hook_findings = project_claude_installed_hook_findings(root)
            findings.extend(claude_hook_findings)
            if any(row["severity"] == "warning" for row in claude_hook_findings) and rc == 0:
                rc = 35
            if any(row["severity"] == "warning" for row in cursor_hook_findings) and rc == 0:
                rc = 35
            bridge_finding = project_installed_bridge_currency(
                self.source_arg, currency_current=currency_current
            )
            findings.append(bridge_finding)
            if any(row["severity"] == "warning" for row in wake_findings) and rc == 0:
                rc = 35
            if any(row["severity"] == "error" for row in wake_findings) and rc not in (20, 33):
                rc = 33

        if root is not None:
            # H2/G2 delivery-health scoreboard: silence is not health — every
            # active node's pending/drain counters and stamped oldest-unread
            # fact are stated; aged pending is RED.
            try:
                from .events import EventLog
                from .mail_summary import observe
                from .send_prefix import _CACHE_ERRORS

                mail_observation = observe(root)
                receipt_facts = None
                if mail_observation is None:
                    events_snapshot, unrecognized_kinds, skew = EventLog(
                        root
                    )._compatible_event_records_with_skew(snapshot=True)
                else:
                    # Admission proves every physical event is understood.
                    # Unknown vocabulary and imported epochs use native history.
                    events_snapshot, unrecognized_kinds, skew = [], [], None
                registry_rows, registry_unrecognized, registry_versions = (
                    read_registry_compatible(root)
                )
                latest_registry: Dict[str, dict] = {}
                for row in registry_rows:
                    if row["kind"] == "registry_entry":
                        latest_registry[str(row["node_id"])] = row
                active_nodes = sorted(
                    node for node, row in latest_registry.items()
                    if row.get("state") == "active"
                )
                if mail_observation is not None:
                    try:
                        events_snapshot, receipt_facts = mail_observation.health_projection(active_nodes)
                    except _CACHE_ERRORS:
                        mail_observation = None
                        events_snapshot, unrecognized_kinds, skew = EventLog(
                            root
                        )._compatible_event_records_with_skew(snapshot=True)
                legacy_findings: list[Dict[str, object]] = []
                role_bindings = _role_bindings(
                    self.source_arg, active_nodes, registry_rows, root=root,
                    legacy_findings=legacy_findings,
                )
                observation_now = _utc_now()
                cadences = {
                    node: template.cadence for node, template in role_bindings.items()
                }
                ack_slas = {
                    node: int(template.ack_sla_minutes)
                    for node, template in role_bindings.items()
                    if template.ack_sla_minutes is not None
                }
                findings.extend(legacy_findings)
                report = DeliveryHealthAnalyzer.analyze(
                    events=events_snapshot,
                    root=root,
                    nodes=active_nodes,
                    now=observation_now,
                    cadences=cadences,
                    ack_slas=ack_slas,
                    receipt_facts=receipt_facts,
                )
                findings.extend(report.findings)
                from .stop_skip import project_stop_skip_findings

                stop_skip_findings = project_stop_skip_findings(
                    events_snapshot, root, active_nodes, observation_now,
                    delivery_times=None if receipt_facts is None else {
                        node: facts["delivery_times"] for node, facts in receipt_facts.items()})
                findings.extend(stop_skip_findings)
                if any(
                    row["severity"] == "warning" for row in stop_skip_findings
                ) and rc == 0:
                    rc = 35
                from .wake_health import WakeHealthProjection

                wake_health = WakeHealthProjection(root)
                for node in active_nodes:
                    try:
                        fact = wake_health.fact(node, observation_now, mail=mail_observation)
                    except _CACHE_ERRORS:
                        fact = wake_health.fact(node, observation_now, mail=False)
                    state = str(fact["state"])
                    severity = (
                        "error"
                        if state in {"stale_claim_with_unread_mail", "breaker_open"}
                        else "warning" if state == "paused" else "ok"
                    )
                    finding = _finding(
                        "wake_health",
                        severity,
                        node,
                        f"wake path projects {state}",
                        str(fact["remedy"]) if severity != "ok" else None,
                    )
                    severity, finding = project_wake_receipt_legacy_read(fact, severity, finding, finding_factory=_finding)
                    finding["wake_health"] = fact
                    findings.append(finding)
                    if severity == "error" and rc == 0:
                        rc = 35
                    try:
                        attention = wake_health.seat_attention(node, observation_now, mail=mail_observation)
                    except _CACHE_ERRORS:
                        attention = wake_health.seat_attention(node, observation_now, mail=False)
                    if attention["state"] == "unknown":
                        finding = _finding("seat_attention_unknown", "warning", node,
                            str(attention["detail"]), str(attention["remedy"]))
                        finding["seat_attention"] = attention
                        findings.append(finding)
                        if rc == 0:
                            rc = 35
                merged_versions = dict(registry_versions)
                if skew is not None:
                    for kind in skew.unknown_kinds:
                        merged_versions[str(kind)] = max(
                            int(merged_versions.get(str(kind), 0)),
                            int(skew.ledger_version),
                        )
                merged_skew = vocabulary_skew_fact(merged_versions)
                if registry_unrecognized:
                    by_kind = {
                        str(row["kind"]): dict(row) for row in unrecognized_kinds
                    }
                    for row in registry_unrecognized:
                        kind = str(row["kind"])
                        existing = by_kind.get(kind)
                        if existing is None:
                            by_kind[kind] = dict(row)
                        else:
                            by_kind[kind] = {
                                "kind": kind,
                                "count": int(existing["count"]) + int(row["count"]),
                                "first_id": str(existing["first_id"]),
                            }
                    unrecognized_kinds = [by_kind[kind] for kind in sorted(by_kind)]
                if merged_skew is not None:
                    finding = _finding(
                        "version_skew",
                        "warning",
                        str(root.path),
                        "installation predates records in this ledger",
                        merged_skew.remedy,
                    )
                    finding["vocabulary_skew"] = merged_skew.artifact()
                    finding["older_readers"] = older_readers(merged_versions)
                    findings.append(finding)
                # BRIDGE-1: the ledger's newest kind names what every reader
                # must speak; the bound zcode seats' bridges are the readers
                # doctor can audit, so they are listed beside older_readers.
                bridge_findings = project_zcode_bridge_registrations(
                    root, merged_versions, currency_current=currency_current,
                    source_root=self.source_arg,
                )
                findings.extend(bridge_findings)
                if any(
                    row["severity"] == "warning" for row in bridge_findings
                ) and rc == 0:
                    rc = 35
                # HARNESS-VER-1 (public issue #18): the harness inventory -
                # per declared harness, the declared executable's measured
                # version and every earlier-on-PATH same-name copy. PATH is
                # an inventory only; it never chooses what floati runs.
                harness_findings = project_harness_binary_inventory(
                    root, currency_current=currency_current
                )
                findings.extend(harness_findings)
                if any(
                    row["severity"] == "warning" for row in harness_findings
                ) and rc == 0:
                    rc = 35
                findings.extend(project_remote_control_worker_findings(root=root))
                findings.extend(project_model_proxy_findings(home=Path.home(), env=dict(os.environ)))
                if report.any_red and rc == 0:
                    rc = 35
            except ProtocolRefusal as exc:
                findings.append(_exception_finding(exc, "error", "events.jsonl"))
                if rc not in {33, 36}:
                    rc = 20
            except IntegrityFailure as exc:
                findings.append(_exception_finding(exc, "error", "events.jsonl", code="delivery_health_unavailable"))
                if rc != 20:
                    rc = 33

        if root is not None and not self.no_sandbox:
            try:
                registry_rows, _unrecognized, _versions = read_registry_compatible(root)
                latest_registry: Dict[str, dict] = {}
                for row in registry_rows:
                    if row["kind"] == "registry_entry":
                        latest_registry[str(row["node_id"])] = row
                for node_id, row in sorted(latest_registry.items()):
                    if row.get("state") != "active":
                        continue
                    harness = str(row.get("role", ""))
                    for fact in probe_write_set(root, node_id, repository=self.source_arg):
                        residue = fact["residue_path"]
                        if fact["verdict"] == "refused":
                            severity = "error"
                        elif fact["verdict"] == "unknown":
                            severity = "warning"
                        elif residue is not None:
                            severity = "warning"
                        else:
                            severity = "ok"
                        detail = (
                            f"{fact['verdict']} path={fact['path'] or '(underivable)'} "
                            f"reason={fact['reason_code']}"
                        )
                        if residue is not None:
                            detail += f" residue_path={residue}"
                        findings.append(
                            _finding(
                                "sandbox_write",
                                severity,
                                fact["coordinate"],
                                detail,
                                None
                                if severity == "ok"
                                else remedy_for(harness, [fact["path"]] if fact["path"] else []),
                            )
                        )
                        if fact["verdict"] == "refused" and rc not in (20, 33):
                            rc = 36
            except ProtocolRefusal as exc:
                findings.append(_exception_finding(exc, "error", "registry/entries.jsonl"))
                if rc not in {33, 36}:
                    rc = 20
            except IntegrityFailure as exc:
                findings.append(_exception_finding(exc, "error", "registry/entries.jsonl"))
                if rc != 20:
                    rc = 33

        manifest_errors = verify_manifest(self.source_arg) if self.source_arg.is_dir() else ["manifest_missing"]
        if manifest_errors:
            findings.append(_finding(
                "manifest_invalid", "error", str(self.source_arg), "; ".join(manifest_errors),
                "restore the governed exact manifest set, then rerun doctor" if currency_current else None,
            ))
            if rc != 20:
                rc = 33
        else:
            findings.append(_finding("manifest_exact_set", "ok", str(self.source_arg), "manifest paths and digests are an exact deployable set"))

        findings.append(currency_finding)
        if currency_finding["severity"] != "ok":
            if rc == 0:
                rc = 35

        bus_watch_finding = project_installed_bus_watch_currency(
            self.source_arg, currency_current=currency_current
        )
        findings.append(bus_watch_finding)
        gateway_finding = _codex_gateway_finding(
            self.source_arg, self.codex_gateway_host_arg
        )
        if gateway_finding is not None:
            # Source-owned failures retain weight even for an ambient host path.
            if self.codex_gateway_host_defaulted and gateway_finding['severity'] != 'error':
                gateway_finding = _host_finding(
                    str(gateway_finding['code']), str(gateway_finding['subject']),
                    str(gateway_finding['detail']), gateway_finding['remediation'],
                )
            findings.append(gateway_finding)
            if gateway_finding["severity"] == "warning" and rc == 0:
                rc = 35
            elif gateway_finding["severity"] == "error" and rc != 20:
                rc = 33

        identity_paths = [
            self.source_arg,
            self.source_arg / "bundle-manifest.v0.json",
            self.source_arg / "scripts/floati",
            self.root_arg,
        ]
        if root is not None:
            identity_paths.extend([
                root.resolve_relative("registry/entries.jsonl"),
                root.resolve_relative("work/items.jsonl"),
            ])
        symlinked = sorted(str(path) for path in identity_paths if _is_symlink_entry(path))
        if symlinked:
            findings.append(_finding("symlink_identity_invalid", "error", str(self.root_arg), f"symlinked identities={symlinked}"))
            rc = 20
        else:
            findings.append(_finding("symlink_identity_valid", "ok", str(self.root_arg), "governed identities are lexical non-symlink paths"))

        if root is not None:
            work_dir = root.resolve_relative("work")
            alternates = sorted(
                path.name
                for path in work_dir.glob("*.jsonl")
                if path.name != "items.jsonl"
            ) if work_dir.is_dir() else []
            if alternates:
                findings.append(_finding(
                    "consumption_coordinate_ambiguous", "warning", str(work_dir),
                    f"work/items.jsonl is authoritative; alternate coordinates={alternates}",
                    "remove or archive alternate work coordinates without consuming them, then rerun doctor" if currency_current else None,
                ))
                if rc == 0:
                    rc = 35
            else:
                try:
                    records = read_records_snapshot(
                        root,
                        ConsumptionLedger(root).relative_path,
                        allowed_kinds={"work_item", "work_transition"},
                    )
                    ConsumptionLedger(root).project(records)
                except ProtocolRefusal as exc:
                    findings.append(_exception_finding(exc, "error", "work/items.jsonl"))
                    if rc not in {33, 36}:
                        rc = 20
                except IntegrityFailure as exc:
                    findings.append(_exception_finding(exc, "error", "work/items.jsonl", code="consumption_state_unavailable"))
                    if rc != 20:
                        rc = 33
                else:
                    findings.append(_finding("consumption_coordinate_valid", "ok", "work/items.jsonl", "sole consumption coordinate is structurally valid"))

        if root is not None:
            from .work_notification import project_notification_findings
            notification_findings = project_notification_findings(root, now=now)
            findings.extend(notification_findings)
            if any(f["severity"] == "error" for f in notification_findings):
                if rc != 20:
                    rc = 33
            elif any(f["severity"] == "warning" for f in notification_findings) and rc == 0:
                rc = 35
            lanes_declaration = root.tenant_home / "state" / "lanes-root.json"
            if lanes_declaration.exists() or lanes_declaration.is_symlink():
                from .lane_workspaces import LaneWorkspaces

                try:
                    lane_inventory = LaneWorkspaces(root).doctor()
                except ProtocolRefusal as exc:
                    findings.append(_exception_finding(
                        exc, "error", str(lanes_declaration),
                        exc.remedy if isinstance(exc.remedy, str) else None,
                    ))
                    if rc not in {33, 36}:
                        rc = 20
                except IntegrityFailure as exc:
                    findings.append(_exception_finding(
                        exc, "error", str(lanes_declaration),
                        "Inspect the named lane ledger before retrying doctor.",
                    ))
                    rc = 33
                else:
                    unmanaged_bytes = lane_inventory["unmanaged_bytes"]
                    warning = unmanaged_bytes is None or unmanaged_bytes > 0
                    byte_detail = "unavailable" if unmanaged_bytes is None else str(unmanaged_bytes)
                    finding = _finding(
                        "lane_workspaces", "warning" if warning else "ok",
                        str(lanes_declaration),
                        f"Recorded lane inventory for {len(lane_inventory['nodes'])} nodes; unmanaged bytes: {byte_detail}.",
                        "Run floati sweep --root with this fleet root to inspect unmanaged paths; removal remains an operator decision."
                        if warning else None,
                    )
                    finding["lane_workspaces"] = lane_inventory
                    findings.append(finding)
                    for node in lane_inventory["nodes"]:
                        age = node["oldest_open_age_seconds"]
                        age_detail = "absent" if age is None else str(age)
                        findings.append(_finding(
                            "lane_workspace_node", "ok", node["node_id"],
                            f"Open lanes: {node['open_lanes']}; oldest open age seconds: {age_detail}.",
                        ))
                    if warning and rc == 0:
                        rc = 35

        from .reader_currency import governed_install_findings
        registry_path = (None if self.codex_config_arg is None else
                         self.codex_config_arg.with_name("fleet-bus-profiles.json"))
        reader_rows = governed_install_findings(
            self.source_arg, self.ref, profile_registry=registry_path,
            hooks=self.codex_hooks_arg,
            gateway_receipt=self.codex_gateway_host_arg.with_name(
                self.codex_gateway_host_arg.name + ".install-receipt.v0.json"),
        )
        for row in reader_rows:
            if self.codex_hooks_defaulted and self.codex_gateway_host_defaulted and row["code"] != "installed_reader_behind":
                row["severity"] = "info"
            findings.append(row)
            if row["severity"] == "warning" and rc == 0:
                rc = 35

        installer_shadow = observe_installer_shadow(
            self.destination_arg,
            source_script=self.source_arg / "scripts" / "floati",
        )
        findings.append(_installer_shadow_finding(installer_shadow, self.destination_arg))
        rc = _fold_shadow_exit(rc, observation_exit_code(installer_shadow))
        if self.destination_arg is not None:
            update_destination = Path(self.destination_arg).expanduser()
            update_metadata = (
                update_destination / INSTALL_METADATA_DIRECTORY / "manifest.v0.json"
            )
            if update_metadata.is_file() or update_metadata.is_symlink():
                try:
                    findings.extend(
                        project_update_findings(
                            update_destination,
                            entrypoint=update_destination / "scripts" / "floati",
                        )
                    )
                except IntegrityFailure as exc:
                    findings.append(
                        _exception_finding(
                            exc,
                            "error",
                            str(update_destination),
                        )
                    )
                    if rc != 20:
                        rc = 33
                except ProtocolRefusal as exc:
                    findings.append(
                        _exception_finding(
                            exc,
                            "error",
                            str(update_destination),
                        )
                    )
                    if rc == 0:
                        rc = 20
        from .adapters.herdr_host import protocol_pins as herdr_protocol_pins

        observation_pin, host_pin = herdr_protocol_pins()
        pin_finding = _finding(
            "herdr_protocol_pins",
            "ok",
            "herdr",
            (
                f"observation pin {observation_pin['protocol']}; "
                f"host pin {host_pin['protocol']} (measured)"
            ),
        )
        findings.append(pin_finding)
        from .codex_hook_trust import observe_codex_waiter_hooks

        def hook_finding(code, severity, subject, detail, remediation=None):
            if self.codex_hooks_defaulted:
                return _host_finding(code, subject, detail, remediation)
            return _finding(code, severity, subject, detail, remediation)

        trust_rows = []
        if self.codex_hooks_defaulted and not self.codex_hooks_arg.exists():
            findings.append(hook_finding(
                "codex_wait_hook_trust",
                "ok",
                str(self.codex_hooks_arg),
                "Codex hooks are absent; no Stop waiter or trust claim exists yet.",
            ))
        else:
            try:
                trust_rows = observe_codex_waiter_hooks(
                    self.codex_hooks_arg, self.codex_config_arg
                )
            except ProtocolRefusal as exc:
                finding = hook_finding(
                    exc.code,
                    "warning",
                    str(self.codex_hooks_arg),
                    exc.detail,
                    "Provide the exact Codex hooks and trust config paths, then rerun doctor.",
                )
                finding["remedy"] = deepcopy(exc.remedy)
                if finding["code"] != exc.code:
                    finding["refusal_code"] = exc.code
                findings.append(finding)
                if not self.codex_hooks_defaulted and rc == 0:
                    rc = 35
            else:
                if not trust_rows:
                    findings.append(hook_finding(
                        "codex_wait_hook_trust",
                        "warning",
                        str(self.codex_hooks_arg),
                        "No Floati Codex Stop waiter was found.",
                        "Install the waiter through the governed path, review trust, and relaunch.",
                    ))
                    if not self.codex_hooks_defaulted and rc == 0:
                        rc = 35
                for trust in trust_rows:
                    finding = hook_finding(
                        "codex_wait_hook_trust",
                        "ok" if trust["hook_armed"] else "warning",
                        str(trust["hook_trust_key"]),
                        (
                            "Hook trust matches and the hook is enabled."
                            if trust["hook_armed"]
                            else "Hook bytes are present but the hook is not armed."
                        ),
                        trust["hook_trust_remediation"],
                    )
                    finding.update(trust)
                    findings.append(finding)
                    if not self.codex_hooks_defaulted and not trust["hook_armed"] and rc == 0:
                        rc = 35
        if root is not None:
            window_findings = project_codex_wait_window_findings(
                root, self.codex_hooks_arg, trust_rows,
                host_scoped=self.codex_hooks_defaulted)
            findings.extend(window_findings)
            if any(row["severity"] == "error" for row in window_findings) and rc not in (20, 33):
                rc = 33
            elif any(row["severity"] == "warning" for row in window_findings) and rc == 0:
                rc = 35
        if root is None:
            fleet_update = {"state": "unavailable"}
        else:
            try:
                fleet_update = project_fleet_update(root)
            except IntegrityFailure as exc:
                fleet_update = {
                    "state": "integrity_failure",
                    "code": exc.code,
                }
                refusal = _captured_refusal(exc)
                if refusal is not None:
                    fleet_update["refusal_code"] = refusal.code
                    fleet_update["remedy"] = deepcopy(refusal.remedy)
                findings.append(_exception_finding(exc, "error", "receipts/fleet-update"))
                rc = 33
        state = {
            0: "healthy",
            1: "degraded",
            2: "degraded",
            20: "refused",
            21: "unknown",
            22: "cannot_speak",
            33: "malformed_evidence",
            35: "degraded",
            36: "sandbox_refused",
        }.get(rc, "degraded")
        report = {
            "schema_version": 1,
            "diagnostic_version": "0",
            "state": state,
            "root": str(self.root_arg),
            "source": str(self.source_arg),
            "ref": self.ref,
            "findings": findings,
            "unrecognized_kinds": unrecognized_kinds,
            "fleet_update": fleet_update,
        }
        if root is not None:
            report["archive_retention"] = retention_census(root)
            from .fleet_presence import presence_status
            report["fleet_presence"] = presence_status(root)
        return report, rc

    def probe(self, budget_seconds: float = 60.0) -> tuple[Dict[str, object], int]:
        """H3 `--probe`: loopback envelope per registered node, drain
        verified inside the budget, per-node PASS/DEAF.

        Unlike the rest of doctor this mode APPENDS one probe envelope per
        node (that is its purpose — productizing the manual incident probe);
        it never drains another node's inbox and never touches real mail.
        """
        from .doctor_probe import DoctorProbe

        try:
            root = FloatiRoot.open_direct_home(self.root_arg, create=False)
        except ProtocolRefusal as exc:
            return (
                {
                    "schema_version": 1,
                    "probe_version": "0",
                    "state": "refused",
                    "root": str(self.root_arg),
                    "detail": exc.detail,
                    "code": exc.code,
                    "remedy": deepcopy(exc.remedy),
                    "findings": [_exception_finding(exc, "error", str(self.root_arg))],
                },
                20,
            )
        registry_rows, _unrecognized, _versions = read_registry_compatible(root)
        latest: Dict[str, dict] = {}
        for row in registry_rows:
            if row["kind"] == "registry_entry":
                latest[str(row["node_id"])] = row
        active_nodes = sorted(
            node for node, row in latest.items() if row.get("state") == "active"
        )
        report = DoctorProbe(root, budget_seconds=budget_seconds).run(active_nodes)
        return (
            {
                "schema_version": 1,
                "probe_version": "0",
                "state": "healthy" if report.rc == 0 else "degraded",
                "root": str(self.root_arg),
                "budget_seconds": budget_seconds,
                "total_budget_seconds": budget_seconds * len(active_nodes),
                "nodes": [
                    {
                        "node": result.node,
                        "verdict": result.verdict,
                        "elapsed_ticks": result.elapsed_ticks,
                    }
                    for result in report.by_node.values()
                ],
                "findings": report.findings,
            },
            report.rc,
        )


def _utc_now() -> datetime:
    """Return the aware datetime required by delivery-health arithmetic."""

    return datetime.now(timezone.utc)
