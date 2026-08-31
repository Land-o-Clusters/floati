"""Typed failures shared by Worker setup and isolation code."""

from __future__ import annotations


class WorkerAdapterFailure(RuntimeError):
    def __init__(self, code: str, *, help_dump_present: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.help_dump_present = help_dump_present
