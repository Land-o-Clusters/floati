"""H3 — `floati doctor --probe`: loopback envelope per node, drain verified
inside a budget, per-node PASS/DEAF (hardening intake, launch cut
@09283dc7).

The probe appends ONE self-addressed loopback envelope per node via the
standard send path (both parties are the node; no other mail is touched),
then watches that node's own drain receipts for the probe id inside the
budget. PASS = the node's wake layer is alive. DEAF = the budget expired
with no drain — the incident class this exists to catch in one command.

Deterministic by construction: the budget is a TICK BUDGET of fixed 1 s
polls with an injected sleeper, so tests drive it without real time and
production uses real seconds.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List

from .bus_epoch import shared_epoch_operation
from .events import EVENT_KINDS, EventLog
from .jsonl import read_records_snapshot
from .registry import Registry
from .root import FloatiRoot
from .snapshot import _owned_epoch_archives

POLL_INTERVAL_SECONDS = 1.0
PROBE_NOTE_MARKER = "doctor-probe"


@dataclass(frozen=True)
class ProbeNodeResult:
    node: str
    verdict: str          # "PASS" | "DEAF"
    elapsed_ticks: int


@dataclass(frozen=True)
class ProbeReport:
    by_node: Dict[str, ProbeNodeResult]
    findings_by_node: Dict[str, dict] = field(default_factory=dict)

    @property
    def findings(self) -> List[dict]:
        return [self.findings_by_node[node] for node in sorted(self.by_node)]

    @property
    def rc(self) -> int:
        return 35 if any(r.verdict == "DEAF" for r in self.by_node.values()) else 0


# dataclasses field imported above


class DoctorProbe:
    def __init__(
        self,
        root: FloatiRoot | str,
        *,
        budget_seconds: float = 60.0,
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.root = root if isinstance(root, FloatiRoot) else FloatiRoot.open_direct_home(root)
        self.budget_seconds = budget_seconds
        self.sleeper = sleeper
        self.poll_interval_seconds = poll_interval_seconds
        self.log = EventLog(self.root)
        self.registry = Registry(self.root)

    def run(self, nodes: List[str]) -> ProbeReport:
        results: Dict[str, ProbeNodeResult] = {}
        findings: Dict[str, dict] = {}
        for node in nodes:
            self.registry.require_active(node)  # typed refusal when unknown/retired
            result = self._probe_node(node)
            results[node] = result
            if result.verdict == "PASS":
                findings[node] = {
                    "code": "delivery_probe",
                    "severity": "ok",
                    "subject": node,
                    "detail": (
                        f"PASS: loopback probe drained in "
                        f"{result.elapsed_ticks} tick(s)"
                    ),
                }
            else:
                findings[node] = {
                    "code": "delivery_probe",
                    "severity": "error",
                    "subject": node,
                    "detail": (
                        f"DEAF: loopback probe not drained within "
                        f"{self.budget_seconds:g}s budget"
                    ),
                    "remediation": (
                        "check the node's wake path, then drain its inbox "
                        "(floati inbox --root <root> --as %s)" % node
                    ),
                }
        return ProbeReport(by_node=results, findings_by_node=findings)

    def _probe_node(self, node: str) -> ProbeNodeResult:
        envelope = self._send_probe(node)
        probe_id = envelope["id"]
        ticks_allowed = max(1, int(self.budget_seconds / self.poll_interval_seconds))
        for tick in range(1, ticks_allowed + 1):
            self.sleeper(self.poll_interval_seconds)
            if self._probe_was_archived(str(probe_id)):
                envelope = self._send_probe(node)
                probe_id = envelope["id"]
            if self._drained(node, probe_id):
                return ProbeNodeResult(node=node, verdict="PASS",
                                       elapsed_ticks=tick)
        return ProbeNodeResult(node=node, verdict="DEAF",
                               elapsed_ticks=ticks_allowed)

    def _send_probe(self, node: str) -> dict:
        moment = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
        return self.log.send(
            node, node, "probe", "0" * 64, "doctor-probe",
            f"{PROBE_NOTE_MARKER} {stamp}",
        )

    @shared_epoch_operation
    def _probe_was_archived(self, probe_id: str) -> bool:
        """Recognize only receipt-owned archives, without locking or mutating them."""

        for archive in _owned_epoch_archives(self.root):
            relative = archive.relative_to(self.root.tenant_home) / "events.jsonl"
            records = read_records_snapshot(
                self.root, relative, allowed_kinds=set(EVENT_KINDS)
            )
            if any(
                row.get("kind") == "message_envelope" and row.get("id") == probe_id
                for row in records
            ):
                return True
        return False

    @shared_epoch_operation
    def _drained(self, node: str, probe_id: str) -> bool:
        base = Path(self.root.resolve_relative("receipts/deliveries"))
        candidates: List[Path] = []
        plain = base / f"{node}.jsonl"
        if plain.is_file():
            candidates.append(plain)
        session_dir = base / node
        if session_dir.is_dir():
            candidates.extend(sorted(p for p in session_dir.glob("*.jsonl") if p.is_file()))
        for path in candidates:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    item_ids = row.get("item_ids")
                    if isinstance(item_ids, list) and probe_id in item_ids:
                        return True
        return False
