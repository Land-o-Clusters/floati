#!/usr/bin/env python3
"""Wake family hook+controller drills on a clone-local scratch root.

Does not mark the family green. Daemon longevity is a later half.
Never the live fleet root. Never a synthetic woke row.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_gauntlet import (  # noqa: E402
    CAPTURE_DIR,
    CLONE,
    FLOATI,
    LIVE_FLEET_ROOT,
    PYTHON,
    SCRATCH_PARENT,
    capture,
    require_scratch_root,
    write_json,
)


SESSION_A = "session-h-wake-a"
SESSION_B = "session-h-wake-b"
SESSION_IDLE = "session-h-wake-idle"
HOOK = CLONE / ".githooks" / "pre-commit"


def _jsonl_rows(path: Path) -> List[dict]:
    if not path.is_file():
        return []
    rows: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _waiter_cli(scratch: Path, session_id: str, workspace: Path) -> Dict[str, Any]:
    payload = json.dumps({"cwd": str(workspace), "session_id": session_id})
    return capture(
        [PYTHON, "-m", "floati.codex_wait", "--root", str(scratch)],
        cwd=CLONE,
        stdin=payload,
        timeout_s=8.0,
    )


def _exhaustion_cycle(scratch: Path, workspace: Path, session_id: str) -> Dict[str, Any]:
    from floati.codex_wait import run_stop_waiter

    clock = [0.0]

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    stdout = io.StringIO()
    stderr = io.StringIO()
    status = run_stop_waiter(
        bus_home=scratch,
        hook_payload={"cwd": str(workspace), "session_id": session_id},
        stdout=stdout,
        stderr=stderr,
        monotonic=monotonic,
        sleep=sleep,
        poll_interval_seconds=1.0,
    )
    return {
        "exit": status,
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "clock": clock[0],
    }


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    scratch = require_scratch_root(SCRATCH_PARENT / f"hwake{stamp}")
    scratch.mkdir(parents=True, exist_ok=True)
    hook_before = HOOK.read_bytes() if HOOK.is_file() else b""

    init = capture(FLOATI + ["init", "--root", str(scratch)], cwd=CLONE)
    add_a = capture(
        FLOATI
        + [
            "node",
            "add",
            "--root",
            str(scratch),
            "--node",
            "lane-a",
            "--harness",
            "Codex",
            "--lifetime",
            "permanent",
        ],
        cwd=CLONE,
    )
    add_b = capture(
        FLOATI
        + [
            "node",
            "add",
            "--root",
            str(scratch),
            "--node",
            "lane-b",
            "--harness",
            "Codex",
            "--lifetime",
            "permanent",
        ],
        cwd=CLONE,
    )
    workspace = scratch / "nodes" / "lane-a"
    from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
    from floati.root import FloatiRoot

    root = FloatiRoot.open_direct_home(scratch, create=False)
    map_path = scratch / "codex-wait" / "workspaces.v0.json"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(
        json.dumps(
            {
                "schema_version": 0,
                "tenant_id": scratch.name,
                "mappings": [{"workspace": str(workspace), "node_id": "lane-a"}],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    participant = resolve_participant(scratch, workspace)
    if participant is None:
        raise RuntimeError("scratch workspace map did not resolve")
    CodexWaitConsentLedger(root).arm(
        participant.binding,
        hook_timeout_seconds=10,
        wait_deadline_seconds=2,
        idempotency_key="h-wake-consent",
    )

    wildcards = {}
    for session in ("*", "all", "global", ""):
        wildcards[session or "empty"] = capture(
            FLOATI
            + [
                "wake",
                "pause",
                "--root",
                str(scratch),
                "--as",
                "lane-a",
                "--session",
                session,
            ],
            cwd=CLONE,
        )

    exhaustion = [_exhaustion_cycle(scratch, workspace, SESSION_IDLE) for _ in range(3)]
    exhaustion_rows = _jsonl_rows(
        scratch / "receipts" / "codex-wait-exhaustion" / "lane-a.jsonl"
    )

    pause_a = capture(
        FLOATI
        + [
            "wake",
            "pause",
            "--root",
            str(scratch),
            "--as",
            "lane-a",
            "--session",
            SESSION_A,
        ],
        cwd=CLONE,
    )
    status_paused = capture(
        FLOATI
        + [
            "wake",
            "status",
            "--root",
            str(scratch),
            "--as",
            "lane-a",
            "--session",
            SESSION_A,
        ],
        cwd=CLONE,
    )
    send_one = capture(
        FLOATI
        + [
            "send",
            "--root",
            str(scratch),
            "--from",
            "lane-b",
            "--to",
            "lane-a",
            "--repo",
            "floati",
            "--sha",
            "a" * 40,
            "--doc",
            "docs/status/WEEKEND_PROGRAM_2026-08-28.md",
            "--note",
            "H-wake pause silence ping",
        ],
        cwd=CLONE,
    )
    wakes_path = scratch / "receipts" / "wakes" / "lane-a.jsonl"
    waiter_paused = _waiter_cli(scratch, SESSION_A, workspace)
    wakes_during_pause = _jsonl_rows(wakes_path)

    resume_a = capture(
        FLOATI
        + [
            "wake",
            "resume",
            "--root",
            str(scratch),
            "--as",
            "lane-a",
            "--session",
            SESSION_A,
        ],
        cwd=CLONE,
    )
    status_active = capture(
        FLOATI
        + [
            "wake",
            "status",
            "--root",
            str(scratch),
            "--as",
            "lane-a",
            "--session",
            SESSION_A,
        ],
        cwd=CLONE,
    )
    waiter_resumed = _waiter_cli(scratch, SESSION_A, workspace)
    wakes_after_resume = _jsonl_rows(wakes_path)

    pause_a_again = capture(
        FLOATI
        + [
            "wake",
            "pause",
            "--root",
            str(scratch),
            "--as",
            "lane-a",
            "--session",
            SESSION_A,
        ],
        cwd=CLONE,
    )
    send_two = capture(
        FLOATI
        + [
            "send",
            "--root",
            str(scratch),
            "--from",
            "lane-b",
            "--to",
            "lane-a",
            "--repo",
            "floati",
            "--sha",
            "b" * 40,
            "--doc",
            "docs/status/WEEKEND_PROGRAM_2026-08-28.md",
            "--note",
            "H-wake exact-session isolation ping",
        ],
        cwd=CLONE,
    )
    waiter_b_while_a_paused = _waiter_cli(scratch, SESSION_B, workspace)
    waiter_a_still_paused = _waiter_cli(scratch, SESSION_A, workspace)
    resume_a_final = capture(
        FLOATI
        + [
            "wake",
            "resume",
            "--root",
            str(scratch),
            "--as",
            "lane-a",
            "--session",
            SESSION_A,
        ],
        cwd=CLONE,
    )

    hook_after = HOOK.read_bytes() if HOOK.is_file() else b""
    control_receipts = _jsonl_rows(
        scratch / "receipts" / "wake-control" / "lane-a.jsonl"
    )

    payload = {
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scratch": str(scratch),
        "live_fleet_refused": str(LIVE_FLEET_ROOT),
        "family_green": False,
        "daemon_half": "queued; family must not close on hook-only evidence",
        "init": init,
        "node_add_lane_a": add_a,
        "node_add_lane_b": add_b,
        "wildcard_refusals": wildcards,
        "pause_a": pause_a,
        "status_paused": status_paused,
        "send_one": send_one,
        "waiter_paused": waiter_paused,
        "wakes_during_pause_count": len(wakes_during_pause),
        "wakes_during_pause_outcomes": [row.get("outcome") for row in wakes_during_pause],
        "resume_a": resume_a,
        "status_active": status_active,
        "waiter_resumed": waiter_resumed,
        "wakes_after_resume": wakes_after_resume,
        "pause_a_again": pause_a_again,
        "send_two": send_two,
        "waiter_b_while_a_paused": waiter_b_while_a_paused,
        "waiter_a_still_paused": waiter_a_still_paused,
        "resume_a_final": resume_a_final,
        "deadline_cycles": exhaustion,
        "exhaustion_receipts": exhaustion_rows,
        "control_receipts": [
            {
                "id": row.get("id"),
                "operation": row.get("operation"),
                "state": row.get("state"),
            }
            for row in control_receipts
        ],
        "hook_bytes_unchanged": hook_before == hook_after,
        "hook_sha256": hashlib.sha256(hook_after).hexdigest() if hook_after else None,
    }
    artifact = write_json(CAPTURE_DIR / "H-wake-hook-run.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    from run_gauntlet import GauntletGuardError

    try:
        raise SystemExit(main())
    except GauntletGuardError as exc:
        print(json.dumps({"status": "refused", "code": exc.code, "detail": exc.detail}))
        raise SystemExit(2)
