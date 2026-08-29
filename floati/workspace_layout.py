"""Nested per-node workspace convention and read-only layout inspection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from .errors import ProtocolRefusal
from .records import validate_role
from .registry import Registry
from .root import FloatiRoot, validate_identifier


_RETENTION_NOTICE = (
    "Node workspace retained; retirement never deletes working folders."
)


def _workspace_path(root: FloatiRoot, node_id: str) -> Path:
    node = validate_identifier(node_id, "node")
    return root.path / "nodes" / node


def _workspace_state(path: Path) -> str:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        return "invalid"
    if path.is_dir():
        return "present"
    return "absent"


def register_node(
    root: FloatiRoot,
    node_id: str,
    harness: str,
    *,
    create_workspace: bool = False,
) -> Dict[str, Any]:
    """Compose existing registration with an optional nested workspace."""

    node = validate_identifier(node_id, "node")
    role = validate_role(harness)
    if not isinstance(create_workspace, bool):
        raise ProtocolRefusal(
            "node_workspace_option_invalid", "create_workspace must be boolean"
        )
    workspace = _workspace_path(root, node)
    nodes = workspace.parent
    created_nodes = False
    created_workspace = False

    if create_workspace:
        if nodes.is_symlink() or (nodes.exists() and not nodes.is_dir()):
            raise ProtocolRefusal(
                "node_workspace_invalid", "the fleet nodes coordinate is not a directory"
            )
        if workspace.is_symlink() or (workspace.exists() and not workspace.is_dir()):
            raise ProtocolRefusal(
                "node_workspace_invalid", "the node workspace coordinate is not a directory"
            )
        if not nodes.exists():
            nodes.mkdir(mode=0o700)
            created_nodes = True
        if not workspace.exists():
            workspace.mkdir(mode=0o700)
            created_workspace = True

    try:
        registry = Registry(root).register(node, role)
    except Exception:
        if created_workspace:
            try:
                workspace.rmdir()
            except OSError:
                pass
        if created_nodes:
            try:
                nodes.rmdir()
            except OSError:
                pass
        raise

    state = (
        "created"
        if created_workspace
        else "existing"
        if create_workspace
        else "not_requested"
    )
    return {
        "registry": registry,
        "workspace": {
            "path": str(workspace.resolve(strict=False)),
            "state": state,
            "layout": "<root>/nodes/<node-id>",
        },
    }


def retire_node(root: FloatiRoot, node_id: str) -> Dict[str, Any]:
    """Retire through the existing ledger and report the untouched workspace."""

    node = validate_identifier(node_id, "node")
    workspace = _workspace_path(root, node)
    registry = Registry(root).retire(node)
    state = _workspace_state(workspace)
    return {
        "registry": registry,
        "workspace": {
            "path": str(workspace.absolute()),
            "state": "retained" if state == "present" else state,
            "notice": _RETENTION_NOTICE,
        },
    }


def inspect_workspace_layout(root: FloatiRoot) -> List[Dict[str, str]]:
    """Return deterministic doctor findings without creating a path or lock."""

    active = set(Registry(root).active_node_ids())
    nodes = root.path / "nodes"
    present = set()
    invalid = set()
    if nodes.is_symlink() or (nodes.exists() and not nodes.is_dir()):
        return [
            {
                "code": "nodes_root_invalid",
                "node_id": "-",
                "path": str(nodes.absolute()),
                "severity": "error",
            }
        ]
    if nodes.is_dir():
        try:
            children = list(os.scandir(nodes))
        except OSError as exc:
            raise ProtocolRefusal(
                "node_workspace_unavailable", "the fleet nodes directory is unreadable"
            ) from exc
        for child in children:
            try:
                if child.is_dir(follow_symlinks=False):
                    present.add(child.name)
                else:
                    invalid.add(child.name)
            except OSError:
                invalid.add(child.name)

    findings: List[Dict[str, str]] = []
    for node in sorted(active):
        path = nodes / node
        if node in invalid:
            findings.append(
                {
                    "code": "node_workspace_invalid",
                    "node_id": node,
                    "path": str(path.absolute()),
                    "severity": "error",
                }
            )
        elif node not in present:
            findings.append(
                {
                    "code": "node_workspace_missing",
                    "node_id": node,
                    "path": str(path.resolve(strict=False)),
                    "severity": "warning",
                }
            )
    for node in sorted(present - active):
        path = nodes / node
        findings.append(
            {
                "code": "node_workspace_orphan",
                "node_id": node,
                "path": str(path.resolve()),
                "severity": "notice",
            }
        )
    for node in sorted(invalid - active):
        findings.append(
            {
                "code": "node_workspace_orphan_invalid",
                "node_id": node,
                "path": str((nodes / node).absolute()),
                "severity": "warning",
            }
        )
    return findings
