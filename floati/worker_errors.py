"""Typed failures shared by Worker setup and isolation code."""

from __future__ import annotations

from typing import Optional


class WorkerAdapterFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        detail: Optional[str] = None,
        help_dump_present: bool = False,
    ) -> None:
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code = code
        self.detail = code if detail is None else detail
        self.help_dump_present = help_dump_present
