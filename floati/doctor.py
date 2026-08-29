"""Physically read-only typed diagnostics for a Floati source and root."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .consumption import ConsumptionLedger
from .copy import (
    DOCTOR_LIVE_DIRS_EXPECTED_ABSENT_DETAIL,
    DOCTOR_PROFILE_INVALID_DETAIL,
)
from .delivery_health import DeliveryHealthAnalyzer
from .errors import IntegrityFailure, ProtocolRefusal
from .gateway import GatewayConfig
from .jsonl import read_records_compatible_snapshot, read_records_snapshot
from .installer_shadow import observe_installer_shadow, observation_exit_code
from .manifest import verify_manifest
from .registry import REGISTRY_KINDS
from .root import FloatiRoot


RULED_PROFILES = ("bus-only", "orchestration")


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
        raise ProtocolRefusal("deployment_currency_unavailable", str(exc)) from exc
    if result.returncode != 0:
        raise ProtocolRefusal(
            "deployment_currency_unavailable",
            result.stderr.strip() or "git inspection failed",
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
        self.codex_hooks_arg = None if codex_hooks is None else Path(codex_hooks).expanduser()
        self.codex_config_arg = None if codex_config is None else Path(codex_config).expanduser()

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

        currency_finding, currency_current = self._currency()

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

        if root is not None:
            # H2 delivery-health scoreboard (hardening intake @09283dc7):
            # silence is not health — every active node's pending/drain
            # counters are stated; aged pending is RED.
            try:
                from .events import EVENT_KINDS

                events_snapshot, unrecognized_kinds = read_records_compatible_snapshot(
                    root, "events.jsonl", allowed_kinds=set(EVENT_KINDS)
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
                if report.any_red and rc == 0:
                    rc = 35
            except IntegrityFailure as exc:
                findings.append(_finding("delivery_health_unavailable", "error", "events.jsonl", exc.detail))
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
        if self.codex_hooks_arg is not None:
            from .codex_hook_trust import observe_codex_waiter_hooks

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
        state = {
            0: "healthy",
            20: "refused",
            21: "unknown",
            22: "cannot_speak",
            33: "malformed_evidence",
            35: "degraded",
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
