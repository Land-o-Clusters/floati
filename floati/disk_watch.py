"""Compatibility export. The doctor finding lives in doctor_disk_watch."""

from __future__ import annotations

from .doctor_disk_watch import (
    FLEET_DISK_FLOOR_BYTES,
    TREE_WALK_BUDGET_SECONDS,
    observe_disk_watch,
)

__all__ = [
    "FLEET_DISK_FLOOR_BYTES",
    "TREE_WALK_BUDGET_SECONDS",
    "observe_disk_watch",
]
