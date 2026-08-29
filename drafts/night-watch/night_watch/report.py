"""Morning-report value object."""

from dataclasses import dataclass, field
from typing import Dict, List

from night_watch.budget import BudgetViolation
from night_watch.watch import Directive, LoopFinding, NodeNight


@dataclass
class MorningReport:
    start: str
    end: str
    per_node: Dict[str, NodeNight] = field(default_factory=dict)
    loops: List[LoopFinding] = field(default_factory=list)
    violations: List[BudgetViolation] = field(default_factory=list)
    directives: List[Directive] = field(default_factory=list)
    healthy_silence_nodes: List[str] = field(default_factory=list)

    def any_paused(self) -> bool:
        return any(node.paused for node in self.per_node.values())
