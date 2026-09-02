"""Stable protocol failures used at Floati's filesystem boundary."""

from __future__ import annotations

from typing import Mapping, Union


UNNAMED_REMEDY: dict[str, str] = {
    "kind": "none",
    "why": "no action was named for this refusal",
}

# Action text for the WS-I / M4 drill codes this row owns. Installer
# codes stay off this map so INS-1 can name --ref / shadow without editing
# deploy.py or installer_shadow.py here.
DRILL_REMEDIES: Mapping[str, str] = {
    "arguments_invalid": "retry the command supplying the exact flags or values named in detail",
    "ack_item_unknown": "pass --id of a message that inbox listed for this node and session",
    "solo_identity_ambiguous": "pass --as with the exact active node id",
    "authority_holder_mismatch": "act as the grant's exact holder",
    "work_unknown": "pass --id of a work item that work list shows",
    "run_id_invalid": "pass --run as run-<uuid7>",
}

DOCTOR_CURRENCY_REMEDY = "run doctor --source from a git working tree git can inspect"

RefusalRemedy = Union[str, dict[str, str]]


def bind_refusal_remedy(code: str, remedy: RefusalRemedy | None) -> RefusalRemedy:
    """Populate every refusal's remedy; JSON null is not a default."""

    if isinstance(remedy, str) and remedy.strip():
        return remedy
    if (
        isinstance(remedy, dict)
        and remedy.get("kind") == "none"
        and str(remedy.get("why") or "").strip()
    ):
        return {"kind": "none", "why": str(remedy["why"])}
    mapped = DRILL_REMEDIES.get(code)
    if mapped is not None:
        return mapped
    return dict(UNNAMED_REMEDY)


class FloatiError(RuntimeError):
    """Base error carrying a stable machine-readable reason code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ProtocolRefusal(FloatiError):
    """A requested operation was rejected before its primary mutation."""

    def __init__(
        self,
        code: str,
        detail: str,
        remedy: RefusalRemedy | None = None,
    ) -> None:
        super().__init__(code, detail)
        self.remedy = bind_refusal_remedy(code, remedy)


class IntegrityFailure(FloatiError):
    """Durable evidence is malformed or internally inconsistent."""


class DurabilityFailure(FloatiError):
    """A filesystem failure interrupted or prevented durable evidence access."""


class SnapshotRefusal(FloatiError):
    """Derived snapshot state could not be reconciled with its authority."""
