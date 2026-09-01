"""Closed schema-v1 validation for derived run-manifest facts."""

from __future__ import annotations

from typing import Dict, Mapping

from .errors import ProtocolRefusal
from .records import _SPECS, validate_record


__all__ = ("validate_run_manifest_fact",)

RUN_MANIFEST_FACT_KINDS = frozenset({"run_manifest_fact"})
RUN_MANIFEST_FACT_FIELDS = _SPECS["run_manifest_fact"][1]


def validate_run_manifest_fact(
    fact: Mapping[str, object], expected_tenant: str
) -> Dict[str, object]:
    """Validate one complete derived fact without writing or projecting it."""

    if type(fact) is not dict or set(fact) != RUN_MANIFEST_FACT_FIELDS:
        raise ProtocolRefusal(
            "run_manifest_fields_invalid",
            "run manifest fact requires the exact closed schema-v1 field set",
        )
    return validate_record(
        dict(fact),
        expected_tenant,
        RUN_MANIFEST_FACT_KINDS,
        integrity=False,
    )
