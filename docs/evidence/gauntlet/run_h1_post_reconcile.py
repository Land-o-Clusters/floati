#!/usr/bin/env python3
"""Post-reconcile H1: onboard via the landed `floati node add` verb."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_gauntlet import (  # noqa: E402
    CAPTURE_DIR,
    CLONE,
    FLOATI,
    SCRATCH_PARENT,
    capture,
    require_scratch_root,
    write_json,
)


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    scratch = require_scratch_root(SCRATCH_PARENT / f"h1r{stamp}")
    scratch.mkdir(parents=True, exist_ok=True)
    init = capture(FLOATI + ["init", "--root", str(scratch)], cwd=CLONE)
    help_node = capture(FLOATI + ["node", "--help"], cwd=CLONE)
    help_add = capture(FLOATI + ["node", "add", "--help"], cwd=CLONE)
    permanent = capture(
        FLOATI
        + [
            "node",
            "add",
            "--root",
            str(scratch),
            "--node",
            "grok-h1",
            "--harness",
            "Cursor",
            "--lifetime",
            "permanent",
        ],
        cwd=CLONE,
    )
    temporary = capture(
        FLOATI
        + [
            "node",
            "add",
            "--root",
            str(scratch),
            "--node",
            "temp-h1",
            "--harness",
            "Codex",
            "--lifetime",
            "temporary",
            "--lease-minutes",
            "30",
        ],
        cwd=CLONE,
    )
    refused = capture(
        FLOATI
        + [
            "node",
            "add",
            "--root",
            str(scratch),
            "--node",
            "../escape",
            "--harness",
            "Codex",
            "--lifetime",
            "permanent",
        ],
        cwd=CLONE,
    )
    payload = {
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scratch": str(scratch),
        "init": init,
        "node_help": help_node,
        "node_add_help": help_add,
        "permanent_add": permanent,
        "temporary_add": temporary,
        "invalid_node_refused": refused,
        "workspaces": {
            "permanent_exists": (scratch / "nodes" / "grok-h1").is_dir(),
            "temporary_exists": (scratch / "nodes" / "temp-h1").is_dir(),
            "escape_absent": not (scratch / "nodes" / "../escape").exists(),
        },
    }
    artifact = write_json(CAPTURE_DIR / "H1-post-reconcile-run.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    from run_gauntlet import GauntletGuardError

    try:
        raise SystemExit(main())
    except GauntletGuardError as exc:
        print(json.dumps({"status": "refused", "code": exc.code, "detail": exc.detail}))
        raise SystemExit(2)
