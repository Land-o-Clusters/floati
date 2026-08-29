"""Registry-backed, per-worktree opt-in committer identity fence."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, Optional, Sequence

from .errors import ProtocolRefusal
from .jsonl import read_records_snapshot
from .registry import REGISTRY_KINDS, Registry
from .root import FloatiRoot, validate_identifier


def validate_seat_fence(
    root: FloatiRoot,
    node_id: str,
    *,
    committer_name: Optional[str],
    committer_email: Optional[str],
) -> Dict[str, Any]:
    """Require the configured seat to be active and to own this commit."""

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("seat_fence_root_invalid", "seat fence requires a validated root")
    node = validate_identifier(node_id, "seat_node")
    latest = None
    try:
        for record in read_records_snapshot(
            root, Registry(root).relative_path, allowed_kinds=REGISTRY_KINDS
        ):
            if record.get("kind") == "registry_entry" and record.get("node_id") == node:
                latest = record
    except ProtocolRefusal as exc:
        raise ProtocolRefusal(
            "seat_fence_node_inactive",
            "configured worktree seat registry evidence is invalid",
        ) from exc
    if latest is None or latest.get("state") != "active":
        raise ProtocolRefusal(
            "seat_fence_node_inactive",
            "configured worktree seat is not active in the selected registry",
        )
    expected_email = f"{node}@{root.tenant_id}"
    if committer_name != node or committer_email != expected_email:
        raise ProtocolRefusal(
            "seat_fence_identity_mismatch",
            f"commit requires GIT_COMMITTER_NAME={node} and GIT_COMMITTER_EMAIL={expected_email}",
        )
    return {
        "node_id": node,
        "registry_entry_id": latest["id"],
        "identity_source": "active_registry_entry",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("--node", required=True)
    try:
        args = parser.parse_args(argv)
        root = FloatiRoot.open_direct_home(args.root)
        validate_seat_fence(
            root,
            args.node,
            committer_name=os.environ.get("GIT_COMMITTER_NAME"),
            committer_email=os.environ.get("GIT_COMMITTER_EMAIL"),
        )
    except (ProtocolRefusal, SystemExit) as exc:
        detail = exc.detail if isinstance(exc, ProtocolRefusal) else "seat fence arguments are invalid"
        print(f"SEAT FENCE: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
