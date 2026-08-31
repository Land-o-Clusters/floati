from __future__ import annotations


def builder(label: str) -> str:
    """Derive one public example builder identifier from a semantic label."""

    return f"builder-{label}"


def worker(label: str) -> str:
    """Derive one public example worker identifier from a semantic label."""

    return f"worker-{label}"


def reviewer() -> str:
    """Return the public review-role identifier used by examples."""

    return "reviewer"


def verifier() -> str:
    """Return the public verification-role identifier used by examples."""

    return "verifier"


def ledger(node_id: str) -> str:
    """Derive the receipt-ledger filename for one public example node."""

    return f"{node_id}.jsonl"


def compose(*parts: str) -> str:
    """Compose example text from reviewed prose and derived identifiers."""

    return "".join(parts)
