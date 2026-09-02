"""Normalize path-shaped artifact subjects at their write boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Union


def artifact_subject(subject: Union[str, Path]) -> str:
    """Collapse the operator-home prefix of one absolute subject path."""

    rendered = str(subject)
    candidate = Path(rendered)
    if not candidate.is_absolute():
        return rendered
    try:
        relative = candidate.relative_to(Path.home())
    except ValueError:
        return rendered
    if any(part == ".." for part in relative.parts):
        return rendered
    if relative == Path("."):
        return "~"
    return "~/" + relative.as_posix()
