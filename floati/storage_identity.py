"""Clean-break storage names and legacy workspace refusal."""

from __future__ import annotations

from pathlib import Path

from .errors import ProtocolRefusal


EVIDENCE_DIRECTORY = ".floati"
INSTALL_METADATA_DIRECTORY = ".floati-install"
SNAPSHOT_DIRECTORY = ".floati-snapshots"
EFFECT_WORKER_PROBE_PREFIX = ".floati-effect-worker-"
EFFECT_WORKER_SCRATCH_PREFIX = "floati-effect-worker-"


def refuse_legacy_workspace_artifacts(workspace: Path) -> None:
    """Refuse a reused workspace without opening any legacy artifact."""
    if not workspace.exists():
        return
    offenders = sorted(
        entry.name
        for entry in workspace.iterdir()
        if entry.name.startswith(".slipway")
    )
    if not offenders:
        return
    first = offenders[0]
    others = len(offenders) - 1
    if others == 0:
        detail = (
            f"workspace refused: legacy artifact {first!r} predates the Floati rename; "
            "nothing was read, migrated, or deleted; start a fresh root, or archive "
            "the legacy artifacts yourself and run again"
        )
    else:
        detail = (
            f"workspace refused: legacy artifact {first!r} and {others} more predate "
            "the Floati rename; nothing was read, migrated, or deleted; start a fresh "
            "root, or archive the legacy artifacts yourself and run again"
        )
    raise ProtocolRefusal("legacy_workspace_artifacts", detail)
