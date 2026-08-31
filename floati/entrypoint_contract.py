"""Closed, discoverable invocation contracts for installed operator entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Tuple


@dataclass(frozen=True)
class ArgumentContract:
    operation: str
    required: Tuple[str, ...]
    optional: Tuple[str, ...]
    full_shape: str

    def __post_init__(self) -> None:
        if not self.operation or not self.full_shape:
            raise ValueError("argument contracts require an operation and full shape")
        if (
            len(set(self.required)) != len(self.required)
            or len(set(self.optional)) != len(self.optional)
            or set(self.required) & set(self.optional)
            or any(not flag.startswith("--") for flag in self.required + self.optional)
        ):
            raise ValueError("argument contract flags must be unique long options")

    def refuse(
        self, *, missing: Iterable[str], unknown: Iterable[str]
    ) -> Dict[str, object]:
        absent: FrozenSet[str] = frozenset(missing)
        foreign: FrozenSet[str] = frozenset(unknown)
        if not absent <= set(self.required):
            raise ValueError("missing flags must belong to the required contract")
        if foreign & (set(self.required) | set(self.optional)):
            raise ValueError("unknown flags cannot belong to the contract")
        missing_rows = sorted(absent)
        unknown_rows = sorted(foreign)
        clauses = []
        if missing_rows:
            clauses.append("missing " + ", ".join(missing_rows))
        if unknown_rows:
            clauses.append("unknown " + ", ".join(unknown_rows))
        detail = (
            f"{self.operation} malformed invocation: "
            + ("; ".join(clauses) if clauses else "argument shape mismatch")
            + f"; required shape: {self.full_shape}"
        )
        return {
            "operation": self.operation,
            "class": "malformed_invocation",
            "missing": missing_rows,
            "unknown": unknown_rows,
            "required": list(self.required),
            "optional": list(self.optional),
            "full_shape": self.full_shape,
            "detail": detail,
        }


ENTRYPOINT_CONTRACTS = {
    "codex-stop-waiter": ArgumentContract(
        operation="codex stop waiter",
        required=("--root",),
        optional=(),
        full_shape="scripts/floati-codex-wait --root ROOT",
    )
}
