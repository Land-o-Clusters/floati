"""Stamped testimony for a reader that encounters newer ledger vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence

from .records import READER_VERSION


VERSION_SKEW_REMEDY = (
    "this ledger contains records from a newer floati; update the reading installation"
)


@dataclass(frozen=True)
class VocabularySkewFact:
    reader_version: str
    ledger_version: str
    unknown_kinds: tuple[str, ...]
    remedy: str
    observed_at: str

    def artifact(self) -> dict[str, object]:
        return {
            "state": "version_skew",
            "reader_version": self.reader_version,
            "ledger_version": self.ledger_version,
            "unknown_kinds": list(self.unknown_kinds),
            "remedy": self.remedy,
            "observed_at": self.observed_at,
        }


def vocabulary_skew_fact(
    unknown_versions: Mapping[str, int] | Sequence[tuple[str, int]],
    *,
    observed_at: Optional[datetime] = None,
) -> Optional[VocabularySkewFact]:
    """Return one fact for well-formed kinds that this reader does not ship."""

    items = (
        unknown_versions.items()
        if isinstance(unknown_versions, Mapping)
        else unknown_versions
    )
    versions = {str(kind): int(version) for kind, version in items}
    if not versions:
        return None
    current = datetime.now(timezone.utc) if observed_at is None else observed_at
    return VocabularySkewFact(
        reader_version=READER_VERSION,
        ledger_version=str(max(versions.values())),
        unknown_kinds=tuple(sorted(versions)),
        remedy=VERSION_SKEW_REMEDY,
        observed_at=current.astimezone(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
    )
