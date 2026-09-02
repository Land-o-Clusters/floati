"""Physically read-only typed diagnostics for a Floati source and root."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .consumption import ConsumptionLedger
from .copy import (
    DOCTOR_LIVE_DIRS_EXPECTED_ABSENT_DETAIL,
    DOCTOR_PROFILE_INVALID_DETAIL,
)
from .delivery_health import DeliveryHealthAnalyzer
from .errors import DOCTOR_CURRENCY_REMEDY, IntegrityFailure, ProtocolRefusal
from .gateway import GatewayConfig
from .jsonl import read_records_compatible_snapshot, read_records_snapshot
from .installer_shadow import observe_installer_shadow, observation_exit_code
from .manifest import verify_manifest
from .registry import REGISTRY_KINDS
from .root import FloatiRoot
from .git_process import is_shallow_repository
from .sandbox_probe import probe_write_set
from .sandbox_remedy import remedy_for
from .storage_identity import INSTALL_METADATA_DIRECTORY
from .update_status import project_update_findings


RULED_PROFILES = ("bus-only", "orchestration")
CODEX_GATEWAY_SOURCE = Path("tools/codex/codex-fleet-bus.py")
CODEX_GATEWAY_DIGEST = Path("tools/codex/codex-fleet-bus.sha256.json")
CODEX_GATEWAY_HOST = Path.home() / ".codex/bin/codex-fleet-bus"
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
        "subject": subject,
        "detail": detail,
        "remediation": remediation,
    }


def _git(source: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
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
        "cannot_speak": "error",
    }[outcome]
    finding = _finding(
        "installer_shadow",
        severity,
        "FLOATI_INSTALL_DESTINATION" if destination is None else str(destination),
        str(artifact["reason"]),
    )
    finding["installer_shadow"] = artifact
    return finding


def _fold_shadow_exit(current: int, shadow: int) -> int:
    """Preserve doctor’s existing aggregate diagnostic precedence."""

    if current in {20, 33, 35}:
        return current
    return shadow


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
    """HT-R6: the Am.2-ruled installed bridge path and its source-SHA sidecar.
    A deployed copy must carry the coordinate it was deployed from, or its
    staleness is unobservable."""

    root = Path.home() / ".local" / "share" / "floati" / "hooks"
    bridge = root / "stop-hook-bridge.py"
    sidecar = root / "stop-hook-bridge.sha256"
    if not bridge.is_file():
        return None
    return bridge, sidecar


def project_installed_bridge_currency(
    source_root: Path, *, currency_current: bool
) -> Dict[str, object]:
    """Report installed-wake-bridge drift: installed bridge from <sha>,
    repository at <sha>. Informational, never a refusal - a stale bridge
    still runs; the user is simply told."""

    paths = installed_bridge_paths()
    if paths is None:
        return _finding(
            "wake_bridge_uninstalled",
            "ok",
            "no installed wake bridge found",
            "no stop-hook bridge at the ruled install path; the seat wakes by its registration alone",
        )
    bridge, sidecar = paths
    installed_sha = hashlib.sha256(bridge.read_bytes()).hexdigest()
    try:
        recorded_sha = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else None
    except OSError:
        recorded_sha = None
    repository_bridge = source_root / "hooks" / "stop-hook-bridge.py"
    repository_sha = (
        hashlib.sha256(repository_bridge.read_bytes()).hexdigest()
        if repository_bridge.is_file() else None
    )
    if repository_sha is None:
        return _finding(
            "wake_bridge_repository_source_unnameable",
            "warning",
            str(repository_bridge),
            "repository wake-bridge source cannot be named for comparison",
            (
                "rerun Doctor with a complete Floati source containing "
                "hooks/stop-hook-bridge.py before claiming deployed-source currency"
            ),
        )
    if installed_sha == repository_sha:
        return _finding(
            "wake_bridge_current",
            "ok",
            str(bridge),
            f"installed wake bridge matches the repository (from {installed_sha[:12]})",
        )
    drift_note = (
        f"installed wake bridge from {installed_sha[:12]}"
        + (f" (sidecar {recorded_sha[:12]})" if recorded_sha else " (no source-SHA sidecar)")
        + f", repository at {repository_sha[:12]}"
    )
    return _finding(
        "wake_bridge_drift",
        "warning",
        str(bridge),
        drift_note,
        "reinstall the installed bridge from the repository copy so the hook runs current code"
        if currency_current else None,
    )


def project_wake_daemon_health(
    root: FloatiRoot, *, currency_current: bool
) -> list[Dict[str, object]]:
    """Derive one wake_daemon_health finding per bound wake daemon coordinate.

    WD-2 GAP 2: a breaker-open daemon was reported nowhere. The runtime the
    daemon already writes carries the whole story; absence of any binding is
    stated as a typed absence, never as silence.
    """

    from .wake_daemon import _BREAKER_THRESHOLD, WAKE_BREAKER_REMEDY

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
        except ProtocolRefusal:
            findings.append(_finding(
                "wake_daemon_health",
                "warning",
                subject,
                "binding present but its coordinate no longer resolves (retired node or unsupported harness)",
                WAKE_BREAKER_REMEDY if currency_current else None,
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
        if resume_state == "resume_suspect" or circuit_state == "open" or refusals >= _BREAKER_THRESHOLD:
            findings.append(_finding(
                "wake_daemon_health",
                "warning",
                subject,
                detail,
                WAKE_BREAKER_REMEDY if currency_current else None,
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
    # ZC1-C-R1: an uninstalled bridge has no writer that can produce firing
    # evidence. Its typed absence is projected separately and must not become
    # a permanent hook_unproven warning for every bound zcode coordinate.
    if installed_bridge_paths() is None:
        return findings
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
    """Project deployed bus-watch drift without refusing an unnameable copy."""

    paths = installed_bus_watch_paths()
    if paths is None:
        return _finding(
            "bus_watch_uninstalled",
            "ok",
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
        return _finding(
            "bus_watch_installed_source_unnameable",
            "warning",
            str(installed),
            "installed OpenCode bus watcher is present but its install-time source SHA cannot name these bytes",
            (
                "reinstall with scripts/bus-watch/install-floati-bus-watch.py so the owner can activate current watcher code"
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
        return _finding(
            "bus_watch_repository_source_unnameable",
            "warning",
            str(repository_watch),
            "repository OpenCode bus-watch source cannot be named for comparison",
            (
                "rerun Doctor with a complete Floati source containing scripts/bus-watch/floati-bus-watch.ts before claiming deployed-source currency"
            ),
        )
    if recorded_sha == repository_sha:
        return _finding(
            "bus_watch_current",
            "ok",
            str(installed),
            f"installed watcher matches the repository (from {recorded_sha[:12]})",
        )
    return _finding(
        "bus_watch_drift",
        "warning",
        str(installed),
        (
            f"installed watcher from {recorded_sha[:12]}, "
            f"repository at {repository_sha[:12]}"
        ),
        (
            "reinstall with scripts/bus-watch/install-floati-bus-watch.py so the owner can activate current watcher code"
            if currency_current
            else None
        ),
    )


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
    remediation = (
        "run scripts/install-codex-gateway.sh explicitly, then rerun doctor"
    )
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
            "warning",
            str(host_path),
            "the fixed host gateway is absent or not a regular non-symlink file",
            remediation,
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
        self.codex_hooks_defaulted = codex_hooks is None
        self.codex_hooks_arg = (
            Path.home() / ".codex" / "hooks.json"
            if codex_hooks is None
            else Path(codex_hooks).expanduser()
        )
        self.codex_config_arg = None if codex_config is None else Path(codex_config).expanduser()
        self.codex_gateway_host_arg = (
            CODEX_GATEWAY_HOST
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
            return _finding(exc.code, "warning", str(source), exc.detail), False
        try:
            status = _git(source, "status", "--porcelain=v1", "--untracked-files=all")
            head = _git(source, "rev-parse", "--verify", "HEAD^{commit}")
            target = _git(source, "rev-parse", "--verify", f"{self.ref}^{{commit}}")
        except ProtocolRefusal as exc:
            return _finding(exc.code, "warning", str(source), exc.detail), False
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

    def artifact(self) -> tuple[Dict[str, object], int]:
        findings: list[Dict[str, object]] = []
        unrecognized_kinds: list[Dict[str, object]] = []
        rc = 0

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
                findings.append(_finding(exc.code, "error", str(self.root_arg), exc.detail))
                rc = 20
            else:
                findings.append(_finding("root_valid", "ok", str(self.root_arg), "direct-home root is valid"))

        if root is not None:
            try:
                from .bus_epoch import reconcile_epoch_roll

                epoch_roll = reconcile_epoch_roll(root)
            except (IntegrityFailure, ProtocolRefusal) as exc:
                findings.append(_finding(exc.code, "error", str(root.path), exc.detail))
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

        currency_finding, currency_current = self._currency()

        interpreter_trust_finding = project_effect_reconciliation_interpreter_trust()
        findings.append(interpreter_trust_finding)
        if interpreter_trust_finding["severity"] == "warning" and rc == 0:
            rc = 35

        if self.gateway_config_arg is not None:
            try:
                config = GatewayConfig.load(self.gateway_config_arg)
            except ProtocolRefusal as exc:
                findings.append(
                    _finding(
                        exc.code,
                        "error",
                        str(self.gateway_config_arg),
                        exc.detail,
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
                registry = read_records_snapshot(
                    root, "registry/entries.jsonl", allowed_kinds=set(REGISTRY_KINDS)
                )
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
            except IntegrityFailure as exc:
                findings.append(_finding(exc.code, "error", str(root.path), exc.detail))
                rc = 33 if rc != 20 else rc

            # WD-2 wake-doctor health: the daemon's own runtime already knows
            # it has been failing (consecutive_refusals, current_backoff,
            # breaker state, last typed outcome) — surface it here, and keep
            # "nothing bound" a typed absence rather than a silent pass.
            wake_findings = project_wake_daemon_health(root, currency_current=currency_current)
            wake_findings.append(project_installed_bridge_currency(
                self.source_arg, currency_current=currency_current
            ))
            findings.extend(wake_findings)
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

                events_snapshot, unrecognized_kinds, skew = EventLog(
                    root
                )._compatible_event_records_with_skew(
                    snapshot=True
                )
                registry_rows = read_records_snapshot(
                    root, "registry/entries.jsonl",
                    allowed_kinds=set(REGISTRY_KINDS),
                )
                latest_registry: Dict[str, dict] = {}
                for row in registry_rows:
                    if row["kind"] == "registry_entry":
                        latest_registry[str(row["node_id"])] = row
                active_nodes = sorted(
                    node for node, row in latest_registry.items()
                    if row.get("state") == "active"
                )
                report = DeliveryHealthAnalyzer.analyze(
                    events=events_snapshot,
                    root=root,
                    nodes=active_nodes,
                    now=_utc_now(),
                )
                findings.extend(report.findings)
                from .wake_health import WakeHealthProjection

                wake_health = WakeHealthProjection(root)
                for node in active_nodes:
                    fact = wake_health.fact(node, _utc_now())
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
                    finding["wake_health"] = fact
                    findings.append(finding)
                    if severity == "error" and rc == 0:
                        rc = 35
                if skew is not None:
                    finding = _finding(
                        "version_skew",
                        "warning",
                        str(root.path),
                        "installation predates records in this ledger",
                        skew.remedy,
                    )
                    finding["vocabulary_skew"] = skew.artifact()
                    findings.append(finding)
                if report.any_red and rc == 0:
                    rc = 35
            except IntegrityFailure as exc:
                findings.append(_finding("delivery_health_unavailable", "error", "events.jsonl", exc.detail))
                if rc != 20:
                    rc = 33

        if root is not None and not self.no_sandbox:
            try:
                registry_rows = read_records_snapshot(
                    root, "registry/entries.jsonl", allowed_kinds=set(REGISTRY_KINDS)
                )
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
            except IntegrityFailure as exc:
                findings.append(_finding(exc.code, "error", "registry/entries.jsonl", exc.detail))
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
        if bus_watch_finding["severity"] == "warning" and rc == 0:
            rc = 35

        gateway_finding = _codex_gateway_finding(
            self.source_arg, self.codex_gateway_host_arg
        )
        if gateway_finding is not None:
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
                except IntegrityFailure as exc:
                    findings.append(_finding("consumption_state_unavailable", "error", "work/items.jsonl", exc.detail))
                    if rc != 20:
                        rc = 33
                else:
                    findings.append(_finding("consumption_coordinate_valid", "ok", "work/items.jsonl", "sole consumption coordinate is structurally valid"))

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
                        _finding(
                            exc.code,
                            "error",
                            str(update_destination),
                            exc.detail,
                        )
                    )
                    if rc != 20:
                        rc = 33
                except ProtocolRefusal as exc:
                    findings.append(
                        _finding(
                            exc.code,
                            "error",
                            str(update_destination),
                            exc.detail,
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

        if self.codex_hooks_defaulted and not self.codex_hooks_arg.exists():
            findings.append(_finding(
                "codex_wait_hook_trust",
                "warning",
                str(self.codex_hooks_arg),
                "Codex hooks are absent; no Floati Stop waiter is configured.",
                "Install the waiter through the governed path when this host uses Codex.",
            ))
        else:
            try:
                trust_rows = observe_codex_waiter_hooks(
                    self.codex_hooks_arg, self.codex_config_arg
                )
            except ProtocolRefusal as exc:
                findings.append(_finding(
                    exc.code,
                    "warning",
                    str(self.codex_hooks_arg),
                    exc.detail,
                    "Provide the exact Codex hooks and trust config paths, then rerun doctor.",
                ))
                if rc == 0:
                    rc = 35
            else:
                if not trust_rows:
                    findings.append(_finding(
                        "codex_wait_hook_trust",
                        "warning",
                        str(self.codex_hooks_arg),
                        "No Floati Codex Stop waiter was found.",
                        "Install the waiter through the governed path, review trust, and relaunch.",
                    ))
                    if rc == 0:
                        rc = 35
                for trust in trust_rows:
                    finding = _finding(
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
                    if not trust["hook_armed"] and rc == 0:
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
                rc = 33
        state = {
            0: "healthy",
            20: "refused",
            21: "unknown",
            22: "cannot_speak",
            33: "malformed_evidence",
            35: "degraded",
            36: "sandbox_refused",
        }.get(rc, "degraded")
        return (
            {
                "schema_version": 1,
                "diagnostic_version": "0",
                "state": state,
                "root": str(self.root_arg),
                "source": str(self.source_arg),
                "ref": self.ref,
                "findings": findings,
                "unrecognized_kinds": unrecognized_kinds,
                "fleet_update": fleet_update,
            },
            rc,
        )

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
                },
                20,
            )
        registry_rows = read_records_snapshot(
            root, "registry/entries.jsonl", allowed_kinds=set(REGISTRY_KINDS)
        )
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
