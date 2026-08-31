"""Stable protocol failures used at Floati's filesystem boundary."""

from __future__ import annotations


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
        remedy: str | None = None,
    ) -> None:
        super().__init__(code, detail)
        self.remedy = remedy


class IntegrityFailure(FloatiError):
    """Durable evidence is malformed or internally inconsistent."""


class DurabilityFailure(FloatiError):
    """A filesystem failure interrupted or prevented durable evidence access."""


class SnapshotRefusal(FloatiError):
    """Derived snapshot state could not be reconciled with its authority."""
