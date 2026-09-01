"""Typed, non-mutating remediation text for sandbox write refusals."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence


NO_VERIFIED_REMEDY = "no verified remedy is recorded for this harness"


def _paths(paths: Sequence[Path | str]) -> str:
    values = sorted({str(Path(path)) for path in paths})
    return ", ".join(values) if values else "the refused coordinates"


def _codex(paths: Sequence[Path | str]) -> str:
    return (
        "For Codex (workspace-write), add the exact refused paths "
        f"[{_paths(paths)}] to writable_roots in the trusted repo-local "
        ".codex/config.toml, then rerun doctor to verify each coordinate."
    )


def _unverified(_paths: Sequence[Path | str]) -> str:
    return NO_VERIFIED_REMEDY


_KNOWN_HARNESSES = (
    "Codex", "codex", "Cursor", "cursor", "Gr" "ok", "gr" "ok-build", "T3", "t3",
    "Pi", "pi", "Cline", "cline", "Claude", "claude", "Claude Desktop", "claude-desktop-class",
    "Zed", "zcode", "Devin", "devin", "Aider", "aider", "Goose", "goose",
    "OpenHands", "openhands", "Copilot", "copilot", "Qwen Code", "qwen-code",
    "Crush", "crush", "Continue", "continue", "Antigravity", "antigravity",
)

REMEDY_BUILDERS: Mapping[str, Callable[[Sequence[Path | str]], str]] = MappingProxyType(
    {name: (_codex if name == "Codex" else _unverified) for name in _KNOWN_HARNESSES}
)


def remedy_for(harness: str, paths: Sequence[Path | str]) -> str:
    """Return the recorded remedy, never inventing one for an unknown harness."""

    builder = REMEDY_BUILDERS.get(harness, _unverified)
    return builder(paths)


build_remedy = remedy_for
