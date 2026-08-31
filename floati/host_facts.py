"""Read-only, non-durable host testimony for maintainer support bundles."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from . import __version__


_UNAVAILABLE = {
    "status": "unavailable",
    "reason_code": "host_fact_unavailable",
}


def _read(probe: Callable[[], object]) -> object:
    try:
        value = probe()
    except Exception:
        return dict(_UNAVAILABLE)
    if not isinstance(value, str) or not value:
        return dict(_UNAVAILABLE)
    return value


def collect_host_facts(
    *,
    install_path: Optional[Path] = None,
    platform_source: object = platform,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
) -> Dict[str, object]:
    """Return bounded local facts without persisting a new record kind."""

    probes: Mapping[str, Callable[[], object]] = {
        "operating_system": getattr(platform_source, "system"),
        "operating_system_release": getattr(platform_source, "release"),
        "machine": getattr(platform_source, "machine"),
        "python_implementation": getattr(platform_source, "python_implementation"),
        "python_version": getattr(platform_source, "python_version"),
    }
    selected_install = (
        Path(__file__).resolve().parents[1]
        if install_path is None
        else Path(install_path).resolve()
    )
    try:
        free_bytes: object = int(getattr(disk_usage(selected_install), "free"))
    except (AttributeError, OSError, TypeError, ValueError):
        try:
            free_bytes = int(disk_usage(selected_install)[2])  # type: ignore[index]
        except (IndexError, OSError, TypeError, ValueError):
            free_bytes = dict(_UNAVAILABLE)
    facts: Dict[str, object] = {
        "disk_free_bytes": free_bytes,
        "floati_version": __version__,
        "install_path": str(selected_install),
        **{name: _read(probe) for name, probe in probes.items()},
    }
    degraded = any(isinstance(value, dict) for value in facts.values())
    return {
        "schema_version": 0,
        "status": "degraded" if degraded else "ok",
        "facts": facts,
    }
