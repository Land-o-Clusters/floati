"""Typed coexistence census for Cursor Remote Control workers.

F2-REMOTE-CONTROL-COEXIST-1, detection half. With Remote Control on, Cursor's
builtin worker auto-spawns `cursor-agent … worker start --worker-dir <open
workspace>` for every open window, and that worker is an anonymous git actor.
doctor classifies the census against the root's leased worktrees: 0 silent,
1 workers counted-only, 2 a worker inside a leased worktree (named, warning).
Foreign paths are counted, never named, at every class; the census observes
and never interferes. Tests inject a ProcessSource; the default shells out.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence

FINDING_CODE = "remote_control_workers"
PS_EXECUTABLE = "/bin/ps"


class PsProcessSource:
    """The default census: one snapshot of the host process table."""

    name = "ps"

    def snapshot(self) -> List[Dict[str, object]]:
        completed = subprocess.run(
            [PS_EXECUTABLE, "-eo", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=15, check=False,
        )
        rows: List[Dict[str, object]] = []
        for line in completed.stdout.splitlines():
            stripped = line.lstrip()
            if not stripped:
                continue
            head = stripped.split(None, 2)
            if len(head) < 3:
                continue
            try:
                rows.append({"pid": int(head[0]), "ppid": int(head[1]),
                             "command": head[2]})
            except ValueError:
                continue
        return rows


DEFAULT_SOURCE = PsProcessSource()


def _worker_observations(rows: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    """Rows whose argv carries the Am.6 spawn shape: worker start --worker-dir DIR."""
    workers: List[Dict[str, object]] = []
    for row in rows:
        command = str(row.get("command", ""))
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        for index, token in enumerate(tokens[:-2]):
            if token == "worker" and tokens[index + 1] == "start"\
                    and tokens[index + 2] == "--worker-dir":
                workers.append({
                    "pid": row.get("pid"),
                    "worker_dir": tokens[index + 3],
                })
                break
    return workers


def _leased_workspaces(root) -> List[str]:
    from .registry import read_registry_compatible

    records, _unknown, _versions = read_registry_compatible(root)
    workspaces = []
    for record in records:
        if record.get("kind") != "node_lease" or record.get("state") != "active":
            continue
        workspace = record.get("workspace")
        if isinstance(workspace, str) and workspace:
            workspaces.append(workspace)
    return workspaces


def _in_seat(worker_dir: str, leased: Sequence[str]) -> Optional[str]:
    resolved = os.path.realpath(worker_dir)
    for workspace in leased:
        seat = os.path.realpath(workspace)
        if resolved == seat or resolved.startswith(seat + os.sep):
            return workspace
    return None


def project_remote_control_worker_findings(root, source=None) -> List[Dict[str, object]]:
    """One typed doctor line classifying Remote Control workers (0/1/2)."""
    from .doctor import _finding

    census = source if source is not None else DEFAULT_SOURCE
    workers = _worker_observations(census.snapshot())
    if not workers:
        return []
    leased = _leased_workspaces(root)
    seats = []
    for worker in workers:
        workspace = _in_seat(str(worker["worker_dir"]), leased)
        if workspace is not None:
            seats.append({"workspace": workspace, "pid": worker["pid"]})
    foreign = len(workers) - len(seats)
    if not seats:
        finding = _finding(
            FINDING_CODE,
            "info",
            "host",
            f"{len(workers)} Remote Control worker(s) detected, none inside a "
            "leased seat worktree. Awareness only: coexist, never interfere; "
            "foreign paths are counted, never named.",
            None,
        )
        finding["remote_control_workers"] = {
            "classification": 1, "workers": len(workers), "in_seat": 0,
        }
        return [finding]
    named = "; ".join(f"{seat['workspace']} (pid {seat['pid']})" for seat in seats)
    finding = _finding(
        FINDING_CODE,
        "warning",
        "host",
        f"{len(seats)} Remote Control worker(s) inside leased seat worktree(s): "
        f"{named}. An anonymous git actor is driving leased work; foreign "
        f"workers counted: {foreign}. Awareness only: coexist, never interfere.",
        "keep Remote Control sessions out of leased seat worktrees; the "
        "supported non-leased checkout is documented for phone use",
    )
    finding["remote_control_workers"] = {
        "classification": 2,
        "workers": len(workers),
        "in_seat": len(seats),
        "seats": seats,
        "foreign_workers": foreign,
    }
    return [finding]
