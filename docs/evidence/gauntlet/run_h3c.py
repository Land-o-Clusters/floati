#!/usr/bin/env python3
"""H3c: does live sequencer/worker/supervise restart complete surviving work?"""

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
    drill_kill_resume,
    require_scratch_root,
    write_json,
)


def _readiness(work_show: dict) -> list:
    try:
        items = json.loads(work_show.get("stdout") or "{}")["evidence"]["items"]
    except (KeyError, json.JSONDecodeError, TypeError):
        return []
    return [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "owner": item.get("owner"),
            "readiness": item.get("readiness"),
        }
        for item in items
    ]


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    scratch = require_scratch_root(SCRATCH_PARENT / f"h3c{stamp}")
    scratch.mkdir(parents=True, exist_ok=True)
    from floati.root import FloatiRoot

    FloatiRoot.open_direct_home(scratch, create=True)
    kill = drill_kill_resume(scratch)
    before = capture(FLOATI + ["work", "show", "--root", str(scratch)], cwd=CLONE)
    seq_status = capture(
        FLOATI + ["sequencer", "status", "--root", str(scratch)], cwd=CLONE
    )
    seq_direct = capture(
        FLOATI
        + ["sequencer", "direct", "--root", str(scratch), "--as", "operator-h3c"],
        cwd=CLONE,
        timeout_s=8.0,
    )
    seq_serve = capture(
        FLOATI
        + [
            "sequencer",
            "serve",
            "--root",
            str(scratch),
            "--as",
            "seq-h3c",
            "--takeover",
        ],
        cwd=CLONE,
        timeout_s=3.0,
    )
    supervise = capture(
        FLOATI + ["supervise", "--root", str(scratch)], cwd=CLONE, timeout_s=8.0
    )
    worker_run = capture(
        FLOATI
        + [
            "worker",
            "run",
            "--root",
            str(scratch),
            "--as",
            "lane-a",
            "--adapter",
            "pi",
        ],
        cwd=CLONE,
        timeout_s=8.0,
    )
    after = capture(FLOATI + ["work", "show", "--root", str(scratch)], cwd=CLONE)
    payload = {
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scratch": str(scratch),
        "fleet_md": "docs/FLEET.md has no sequencer, worker-run, or supervise restart flow",
        "kill": {
            "state": kill["kill_result"].get("state"),
            "return_code": kill["kill_result"].get("return_code"),
            "second_orchestrate": kill["second_orchestrate"],
        },
        "work_before": _readiness(before),
        "sequencer_status": seq_status,
        "sequencer_direct": seq_direct,
        "sequencer_serve_takeover": seq_serve,
        "supervise": supervise,
        "worker_run": worker_run,
        "work_after": _readiness(after),
        "done_before": sum(1 for row in _readiness(before) if row["readiness"] == "done"),
        "done_after": sum(1 for row in _readiness(after) if row["readiness"] == "done"),
    }
    artifact = write_json(CAPTURE_DIR / "H3c-live-resume-run.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
