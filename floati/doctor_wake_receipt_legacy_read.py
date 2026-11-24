"""Pure Doctor finding for unavailable wake-attempt testimony."""

from __future__ import annotations

from typing import Callable, Dict, Mapping, Optional, Tuple


def project_wake_receipt_legacy_read(
    fact: Mapping[str, object], severity: str, finding: Dict[str, object],
    *, finding_factory: Callable[[str, str, str, str, Optional[str]], Dict[str, object]],
) -> Tuple[str, Dict[str, object]]:
    """Replace the generic finding only for the named read observation."""
    read_finding = fact.get("read_finding")
    if read_finding is None:
        return severity, finding

    metadata = {
        key: read_finding[key]
        for key in ("code", "ledger", "record_id", "source_code")
    }
    projected = dict(finding)
    projected.update(finding_factory(
        "wake_receipt_legacy_unreadable", "error", metadata["ledger"],
        (
            "Wake-attempt testimony is unavailable because a decision reference "
            "failed strict validation. No receipts from this ledger were used "
            "as wake evidence."
        ),
        (
            "Retain this ledger unchanged. Inspect an owned clone and rehearse "
            "any quarantine with the actual Stop waiter before live repair."
        ),
    ))
    projected["read_finding"] = metadata
    return "error", projected
