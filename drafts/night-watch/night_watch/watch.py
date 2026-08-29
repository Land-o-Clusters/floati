"""The Night Watch — pure fold over the typed night-log.

Implements drafts/night-watch/docs/NIGHT_WATCH_SPEC.md. Injected clock
windows and an injected, cited budget table (no-second-budget-table law).
Directives are advisory records; this package owns no processes, sockets,
or leases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from night_watch.budget import BudgetTable, BudgetViolation
from night_watch.events import EVENT_KINDS, NightEvent

def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _resolve(moment: str, start: datetime) -> datetime:
    """Resolve a full instant or a bare HH:MM against the window start date
    (an overnight window crossing midnight rolls forward one day)."""
    if "T" in moment:
        return _parse(moment)
    resolved = datetime.fromisoformat(
        f"{start.date().isoformat()}T{moment}+00:00"
    )
    if resolved.time() < start.time():
        resolved += timedelta(days=1)
    return resolved


@dataclass(frozen=True)
class LoopFinding:
    kind: str               # cycle | depth
    chain: List[str]


@dataclass
class NodeNight:
    node: str
    wakes: int = 0
    idle_burns: int = 0
    mails: int = 0
    work_items: int = 0
    pauses: List[str] = field(default_factory=list)
    resumes: List[str] = field(default_factory=list)
    refusals: List[str] = field(default_factory=list)
    paused: bool = False
    paused_reason: Optional[str] = None
    last_wake_moment: Optional[datetime] = None
    mail_since_last_wake: bool = False
    first_mail_after_wake: Optional[datetime] = None
    wake_moments: List[datetime] = field(default_factory=list)


@dataclass(frozen=True)
class Directive:
    node: str
    directive: str          # pause | resume
    reason: str


class NightWatch:
    def __init__(self, window_start: str, window_end: str, budget: BudgetTable):
        start, end = _parse(window_start), _parse(window_end)
        if end <= start:
            raise ValueError("window_inverted: end must be after start")
        self.start = start
        self.end = end
        self.budget = budget
        self.nodes: Dict[str, NodeNight] = {}
        self.edges: List[tuple] = []
        self.loops: List[LoopFinding] = []
        self.violations: List[BudgetViolation] = []
        self.directives: List[Directive] = []

    # -- folding -------------------------------------------------------------

    def fold(self, event: NightEvent):
        moment = _resolve(event.moment, self.start)
        if moment < self.start or moment > self.end:
            raise ValueError(
                "window_inverted: event %s outside night window" % event.moment
            )
        node = self._node(event.node)

        if event.kind == "loop_edge":
            other = self._node(event.to_node)
            self.edges.append((event.node, other.node))
            self._check_loops()
            return

        if event.kind == "mail_landed":
            node.mails += 1
            if not node.mail_since_last_wake:
                node.first_mail_after_wake = _resolve(event.moment, self.start)
            node.mail_since_last_wake = True
            return

        if event.kind in ("pause_directive", "resume_directive"):
            self._apply_directive(event, node, moment)
            return

        if event.kind == "reset_observed":
            if node.paused and node.paused_reason == "quota":
                self._resume(node, "reset")
            return

        if event.kind == "quota_ceiling_hit":
            self._violation(node, "max_wakes", str(node.wakes), moment)
            self._pause(node, "quota", moment)
            return

        if event.kind == "work_completed":
            node.work_items += 1
            return

        if event.kind == "wake_delivered":
            return

        if event.kind == "wake_requested":
            if node.paused:
                node.refusals.append(
                    "node_paused(%s) at %s" % (node.paused_reason, event.moment)
                )
                return
            justified = node.mail_since_last_wake
            if not justified:
                node.idle_burns += 1
                if node.idle_burns >= self.budget.max_idle_burn_wakes:
                    self._violation(
                        node, "idle_burn",
                        "%d wakes without cause" % node.idle_burns, moment,
                    )
                    self._pause(node, "idle_burn", moment)
            else:
                if (
                    node.last_wake_moment is not None
                    and node.first_mail_after_wake is not None
                    and (node.first_mail_after_wake - node.last_wake_moment).total_seconds()
                    <= self.budget.coalesce_window_seconds
                ):
                    self.violations.append(BudgetViolation(
                        dimension="coalesce_missed",
                        observed="second wake for mail %.0fs after the previous one"
                        % (node.first_mail_after_wake - node.last_wake_moment).total_seconds(),
                        ceiling="1 wake per %.0fs coalesce window"
                        % self.budget.coalesce_window_seconds,
                        citation=self.budget.source_citation,
                    ))
                node.mail_since_last_wake = False
                node.first_mail_after_wake = None
                previous = node.wake_moments[-1] if node.wake_moments else None
                now = moment
                if (
                    previous is not None
                    and (now - previous).total_seconds()
                    <= self.budget.coalesce_window_seconds
                ):
                    self.violations.append(BudgetViolation(
                        dimension="coalesce_missed",
                        observed="2 wakes / %.0fs" % (now - previous).total_seconds(),
                        ceiling="1 wake per %.0fs coalesce window"
                        % self.budget.coalesce_window_seconds,
                        citation=self.budget.source_citation,
                    ))
            node.wake_moments.append(moment)
            node.last_wake_moment = moment
            node.wakes += 1
            if node.wakes > self.budget.max_wakes_per_node:
                self._violation(node, "max_wakes", str(node.wakes), moment)
                self._pause(node, "quota", moment)

    # -- directives ----------------------------------------------------------

    def _pause(self, node: NodeNight, reason: str, moment: datetime):
        if node.paused:
            return
        node.paused = True
        node.paused_reason = reason
        node.pauses.append("%s at %s" % (reason, moment.isoformat()))
        self.directives.append(Directive(node.node, "pause", reason))

    def _resume(self, node: NodeNight, reason: str):
        if not node.paused:
            return
        node.paused = False
        node.paused_reason = None
        node.resumes.append(reason)
        self.directives.append(Directive(node.node, "resume", reason))

    def _apply_directive(self, event: NightEvent, node: NodeNight, moment):
        if event.kind == "pause_directive":
            if not node.paused:
                self._pause(node, "operator", moment)
        else:  # resume_directive is the explicit operator act
            self._resume(node, "operator")

    def _violation(self, node: NodeNight, dimension: str, observed: str, moment):
        ceilings = {
            "max_wakes": str(self.budget.max_wakes_per_node),
            "idle_burn": str(self.budget.max_idle_burn_wakes),
            "coalesce_missed": "1 per coalesce window",
        }
        self.violations.append(BudgetViolation(
            dimension=dimension,
            observed=observed,
            ceiling=ceilings.get(dimension, "?"),
            citation=self.budget.source_citation,
        ))
        if dimension == "max_wakes":
            self._pause(node, "quota", moment)

    def _node(self, name: str) -> NodeNight:
        if name not in self.nodes:
            self.nodes[name] = NodeNight(node=name)
        return self.nodes[name]

    # -- loops ---------------------------------------------------------------

    def _check_loops(self):
        adjacency: Dict[str, List[str]] = {}
        for src, dst in self.edges:
            adjacency.setdefault(src, []).append(dst)
        # depth check: longest simple chain from any origin
        longest: List[str] = []
        for origin in list(adjacency):
            path = self._longest(origin, adjacency)
            if len(path) > len(longest):
                longest = path
        if len(longest) - 1 > self.budget.max_chain_depth:
            self.loops.append(LoopFinding(kind="depth", chain=longest))
            for member in longest:
                self._node_pause_if_present(member, "loop")
        # cycle check: edge whose target can reach its source
        reported_cycles = set()
        for src, dst in self.edges:
            if self._reaches(dst, src, adjacency):
                chain = [src, dst]
                seen = {src}
                current = dst
                while current not in seen:
                    chain.append(current)
                    seen.add(current)
                    nxt = adjacency.get(current, [src])[0]
                    if nxt in seen:
                        break
                    current = nxt
                key = frozenset(chain)
                if key in reported_cycles:
                    continue
                reported_cycles.add(key)
                self.loops.append(LoopFinding(kind="cycle", chain=sorted(set(chain))))
                for member in chain:
                    self._node_pause_if_present(member, "loop")
                break

    def _node_pause_if_present(self, name: str, reason: str):
        node = self.nodes.get(name)
        if node is not None and not node.paused:
            self._pause(node, reason, _parse(self.end_iso()))

    def end_iso(self) -> str:
        return self.end.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _reaches(start: str, goal: str, adjacency: Dict[str, List[str]]) -> bool:
        stack, seen = [start], set()
        while stack:
            current = stack.pop()
            if current == goal:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adjacency.get(current, []))
        return False

    @staticmethod
    def _longest(origin: str, adjacency: Dict[str, List[str]]) -> List[str]:
        best = [origin]

        def walk(path: List[str]):
            last = path[-1]
            improved = False
            for nxt in adjacency.get(last, []):
                if nxt in path:
                    continue
                candidate = path + [nxt]
                if len(candidate) > len(best):
                    best[:] = candidate
                walk(candidate)
                improved = True
            return improved

        walk([origin])
        return best

    # -- report ---------------------------------------------------------------

    def morning_report(self):
        healthy = sorted(
            node.node
            for node in self.nodes.values()
            if node.mails == 0 and node.wakes == 0
        )
        per_node = {
            name: node
            for name, node in sorted(self.nodes.items())
        }
        from night_watch.report import MorningReport

        return MorningReport(
            start=self.start.isoformat().replace("+00:00", "Z"),
            end=self.end.isoformat().replace("+00:00", "Z"),
            per_node=per_node,
            loops=list(self.loops),
            violations=list(self.violations),
            directives=list(self.directives),
            healthy_silence_nodes=healthy,
        )
