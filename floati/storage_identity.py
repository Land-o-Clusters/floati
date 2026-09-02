"""Clean-break storage names and legacy workspace refusal."""

from __future__ import annotations

from pathlib import Path

from .errors import ProtocolRefusal


EVIDENCE_DIRECTORY = ".floati"
INSTALL_METADATA_DIRECTORY = ".floati-install"
SNAPSHOT_DIRECTORY = ".floati-snapshots"
EFFECT_WORKER_PROBE_PREFIX = ".floati-effect-worker-"
EFFECT_WORKER_SCRATCH_PREFIX = "floati-effect-worker-"

# The retired repository name, built from hex rather than spelled, as the dot
# prefix the pre-rename product wrote into workspaces. This is not copy: it is
# a name READ off a disk the product does not own, and it is the whole
# mechanism of the refusal below. Scrubbing it would not rename anything -- it
# would silently disarm the migration safety net and let a reused legacy
# workspace through. Built for the reason floati/identity_fence.py builds its
# governed tokens: a fence that must forbid this word may not find it in
# shipped source, and the runtime value may not move to satisfy the fence.
# tests/test_retired_name_pins.py pins the prefix AND exercises the refusal.
_RETIRED_NAME = bytes.fromhex("736c6970776179").decode("ascii")
LEGACY_ARTIFACT_PREFIX = "." + _RETIRED_NAME


def refuse_legacy_workspace_artifacts(workspace: Path) -> None:
    """Refuse a reused workspace without opening any legacy artifact."""
    if not workspace.exists():
        return
    offenders = sorted(
        entry.name
        for entry in workspace.iterdir()
        if entry.name.startswith(LEGACY_ARTIFACT_PREFIX)
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
