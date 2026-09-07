"""HARNESS-VER-1 (public issue #18): harness binaries installed twice.

The measured incident: a harness existed as two copies - the PATH copy
and a user-local copy - disagreeing on version, and nothing reported
it, so a terminal ran one harness while floati ran the other. The
executable-provenance policy (IN-4) still holds: PATH is consulted ONLY
as an operator inventory, never to choose. The declared executable is
the one floati runs; the PATH walk here only NAMES what an ambient
shell would resolve to first, with each copy's measured version.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Mapping, Optional

_PROBE_TIMEOUT_SECONDS = 10


def declared_harness_executables(root) -> List[Dict[str, str]]:
    """Per bound seat: node, harness, and the DECLARED executable path.

    The bindings under state/wake-daemon/adapters are the coordinates
    the operator declared; nothing on PATH ever declares itself.
    """

    adapters_root = root.resolve_relative("state/wake-daemon/adapters")
    declared: List[Dict[str, str]] = []
    if not adapters_root.is_dir() or adapters_root.is_symlink():
        return declared
    for node_dir in sorted(adapters_root.iterdir()):
        if node_dir.is_symlink() or not node_dir.is_dir():
            continue
        for binding_path in sorted(node_dir.glob("*.json")):
            if binding_path.is_symlink() or not binding_path.is_file():
                continue
            try:
                record = json.loads(binding_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            executable = (
                record.get("executable") if isinstance(record, dict) else None
            )
            if isinstance(executable, str) and executable:
                declared.append({
                    "node": node_dir.name,
                    "harness": binding_path.stem,
                    "executable": executable,
                })
    return declared


def probe_version(executable: str) -> Optional[str]:
    """The first line of `<executable> --version`.

    None when the version cannot be measured - an unmeasurable copy is
    named, never guessed. The argv is a plain variable plus a literal:
    no built expression, no network shape, inventory only.
    """

    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = completed.stdout.strip().splitlines()
    if not lines or not lines[0].strip():
        return None
    return lines[0].strip()


def path_inventory(declared_path: str, environ: Mapping[str, str]) -> Dict[str, object]:
    """Every same-named copy EARLIER on PATH than the declared binary.

    An ambient shell resolves the first PATH hit, so a copy earlier than
    the declared executable is what the operator's terminal actually
    runs. When the declared binary is not on PATH at all, every PATH
    copy shadows it. Entries that cannot be read are named, never a
    crash (the INS-1 shape). PATH is an inventory only: this never
    chooses what floati runs.
    """

    name = Path(declared_path).name
    entries = [
        entry
        for entry in environ.get("PATH", "").split(os.pathsep)
        if entry
    ]
    resolved_entries = [os.path.abspath(entry) for entry in entries]
    declared_dir = os.path.dirname(os.path.abspath(declared_path))
    if declared_dir in resolved_entries:
        position = resolved_entries.index(declared_dir)
    else:
        position = len(resolved_entries)
    shadows: List[Dict[str, Optional[str]]] = []
    unreadable: List[str] = []
    for index, entry in enumerate(entries):
        if index >= position:
            continue
        resolved = Path(resolved_entries[index])
        if not resolved.is_dir():
            unreadable.append(entry)
            continue
        candidate = resolved / name
        if not candidate.is_file():
            continue
        shadows.append({"path": str(candidate), "version": probe_version(str(candidate))})
    return {"shadows": shadows, "unreadable_entries": unreadable}


def harness_version_artifact(
    root, *, environ: Optional[Mapping[str, str]] = None
) -> Dict[str, object]:
    """The harness inventory: per declared harness, the declared
    executable with its measured version, every earlier-on-PATH same-name
    copy with its measured version, the unreadable PATH entries, and the
    resulting status. `shadowed` requires a MEASURED difference; a copy
    whose version cannot be measured is `unmeasurable` - fail-safe,
    because identical cannot be proven. `identical` and `no_copies` are
    notes, not warnings."""

    if environ is None:
        environ = os.environ
    declared_rows: List[Dict[str, object]] = []
    for row in declared_harness_executables(root):
        entry: Dict[str, object] = dict(row)
        entry["version"] = probe_version(row["executable"])
        inventory = path_inventory(row["executable"], environ)
        entry["shadow_inventory"] = inventory
        shadows = inventory["shadows"]
        measured = [
            shadow["version"]
            for shadow in shadows
            if shadow["version"] is not None
        ]
        unmeasurable = len(shadows) - len(measured)
        if entry["version"] is None:
            # The declared binary cannot be measured, so no comparison is
            # possible: the inventory names that honestly instead of
            # warning about a disagreement it cannot see.
            entry["status"] = "inventory"
        elif unmeasurable:
            # A comparison is possible but blocked: identical cannot be
            # proven, so fail safe.
            entry["status"] = "unmeasurable"
        elif any(version != entry["version"] for version in measured):
            entry["status"] = "shadowed"
        elif shadows:
            entry["status"] = "identical"
        else:
            entry["status"] = "no_copies"
        declared_rows.append(entry)
    return {"declared": declared_rows}
