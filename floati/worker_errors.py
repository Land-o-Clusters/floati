"""Typed failures shared by Worker setup and isolation code."""

from __future__ import annotations


class WorkerAdapterFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
