"""Injected budget ceilings — THE NO-SECOND-BUDGET-TABLE LAW.

No default table, no fallback numbers: the canonical ceilings live on the
governed side of the repo and are injected here with a mandatory
sourceCitation that renders beside any violation.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetTable:
    max_wakes_per_node: int
    max_idle_burn_wakes: int
    max_chain_depth: int
    coalesce_window_seconds: int
    source_citation: str

    def __post_init__(self):
        if not isinstance(self.source_citation, str) or not self.source_citation.strip():
            raise ValueError(
                "budget_citation_required: ceiling provenance is law"
            )


@dataclass(frozen=True)
class BudgetViolation:
    dimension: str          # max_wakes | idle_burn | coalesce_missed
    observed: str
    ceiling: str
    citation: str
