#!/usr/bin/env python3
"""H3b: documented resume after kill. Fixture-first; never the live fleet."""

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
    PYTHON,
    SCRATCH_PARENT,
    capture,
    drill_kill_resume,
    require_scratch_root,
    write_json,
)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    scratch = require_scratch_root(SCRATCH_PARENT / f"h3b{stamp}")
    scratch.mkdir(parents=True, exist_ok=True)
    from floati.root import FloatiRoot

    FloatiRoot.open_direct_home(scratch, create=True)
    help_topics = ["", "log", "orchestrate", "sequencer", "sequencer serve", "worker", "worker run"]
    helps = {}
    for topic in help_topics:
        argv = FLOATI + (["--help"] if topic == "" else topic.split() + ["--help"])
        helps[topic or "root"] = capture(argv, cwd=CLONE)
    kill = drill_kill_resume(scratch)
    replay = capture(
        FLOATI + ["log", "--root", str(scratch), "--replay", "--plain"],
        cwd=CLONE,
        timeout_s=20.0,
    )
    log_plain = capture(FLOATI + ["log", "--root", str(scratch)], cwd=CLONE)
    work_show = capture(FLOATI + ["work", "show", "--root", str(scratch)], cwd=CLONE)
    payload = {
        "started_utc": _utc(),
        "scratch": str(scratch),
        "help": {
            name: {
                "exit": row["exit"],
                "stdout_bytes": row["stdout_bytes"],
                "stdout_sha256": row["stdout_sha256"],
                "stdout": row["stdout"],
                "resume_count": row["stdout"].lower().count("resume"),
                "replay_count": row["stdout"].lower().count("replay"),
            }
            for name, row in helps.items()
        },
        "kill": {
            "state": kill["kill_result"].get("state"),
            "return_code": kill["kill_result"].get("return_code"),
            "drills": kill["kill_result"].get("drills"),
            "work_n": len(kill["work_after"]),
            "receipt_count": kill["receipt_count"],
            "second_orchestrate": kill["second_orchestrate"],
        },
        "log_replay_plain": replay,
        "log_without_replay": log_plain,
        "work_show": work_show,
        "ended_utc": _utc(),
    }
    artifact = write_json(CAPTURE_DIR / "H3b-resume-run.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
