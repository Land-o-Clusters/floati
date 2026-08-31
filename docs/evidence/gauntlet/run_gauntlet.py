#!/usr/bin/env python3
"""WS-H gauntlet skeleton runner (grok). Fixture-first; never the live fleet root.

Product source is not edited. Wake drills are not simulated.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

CLONE = Path("\x2fUsers/penguinspecz/Projects/floati-grok").resolve()
if str(CLONE) not in sys.path:
    sys.path.insert(0, str(CLONE))
SCRATCH_PARENT = (CLONE / ".gauntlet-scratch").resolve()
LIVE_FLEET_ROOT = Path("\x2fUsers/penguinspecz/.floati-bus/puddle-fleet").resolve()
CAPTURE_DIR = CLONE / "docs" / "evidence" / "gauntlet" / "captures"
PYTHON = "/usr/bin/python3"
FLOATI = [PYTHON, "-m", "floati"]


class GauntletGuardError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def require_scratch_root(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved == LIVE_FLEET_ROOT or LIVE_FLEET_ROOT in resolved.parents:
        raise GauntletGuardError(
            "live_fleet_forbidden",
            f"refused live fleet root: {resolved}",
        )
    try:
        resolved.relative_to(SCRATCH_PARENT)
    except ValueError as exc:
        raise GauntletGuardError(
            "scratch_containment",
            f"scratch root must live under {SCRATCH_PARENT}: {resolved}",
        ) from exc
    return resolved


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def capture(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout_s: float = 30.0,
    env: Optional[Mapping[str, str]] = None,
    stdin: Optional[str] = None,
) -> Dict[str, Any]:
    started = time.time()
    started_utc = _utc()
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout_s,
            env=dict(os.environ, **(env or {})),
            input=None if stdin is None else stdin.encode("utf-8"),
        )
        timed_out = False
        exit_code = int(proc.returncode)
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
    ended_utc = _utc()
    return {
        "argv": list(argv),
        "cwd": None if cwd is None else str(cwd),
        "timeout_s": timeout_s,
        "timed_out": timed_out,
        "exit": exit_code,
        "started_utc": started_utc,
        "ended_utc": ended_utc,
        "elapsed_s": round(time.time() - started, 3),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stdout_sha256": _sha256_bytes(stdout),
        "stderr_sha256": _sha256_bytes(stderr),
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace"),
    }


def write_json(path: Path, payload: Any) -> Dict[str, Any]:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": str(path), "bytes": len(data), "sha256": _sha256_bytes(data)}


class CompletingAdapter:
    name = "fixture"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        return item["id"]

    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float
    ) -> list:
        time.sleep(min(0.2, max(0.01, deadline_seconds / 2)))
        return []


def _floati_root(scratch: Path):
    from floati.root import FloatiRoot

    return FloatiRoot.open_direct_home(scratch, create=False)


def drill_onboard(scratch: Path, node: str) -> Dict[str, Any]:
    require_scratch_root(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    init = capture(
        FLOATI + ["init", "--root", str(scratch), "--solo", node, "--harness", "Codex"],
        cwd=CLONE,
    )
    register = capture(
        FLOATI
        + ["register", "--root", str(scratch), "peer-h1", "--harness", "Codex"],
        cwd=CLONE,
    )
    return {"init": init, "register": register}


def drill_round_trip(scratch: Path, node: str, sha: str) -> Dict[str, Any]:
    require_scratch_root(scratch)
    send = capture(
        FLOATI
        + [
            "send",
            "--root",
            str(scratch),
            "--from",
            node,
            "--to",
            node,
            "--repo",
            "floati",
            "--sha",
            sha,
            "--doc",
            "docs/status/WEEKEND_PROGRAM_2026-08-28.md",
            "--note",
            "H2 gauntlet round-trip",
        ],
        cwd=CLONE,
    )
    inbox = capture(FLOATI + ["inbox", "--root", str(scratch), "--as", node], cwd=CLONE)
    item_id = None
    try:
        payload = json.loads(inbox["stdout"])
        messages = payload.get("evidence", {}).get("messages") or []
        if messages:
            item_id = messages[0].get("id")
    except json.JSONDecodeError:
        item_id = None
    ack = {"skipped": True, "reason": "no message id"}
    inbox_after = None
    if item_id:
        ack = capture(
            FLOATI + ["ack", "--root", str(scratch), "--as", node, "--id", item_id],
            cwd=CLONE,
        )
        inbox_after = capture(
            FLOATI + ["inbox", "--root", str(scratch), "--as", node], cwd=CLONE
        )
    return {
        "send": send,
        "inbox": inbox,
        "item_id": item_id,
        "ack": ack,
        "inbox_after_ack": inbox_after,
    }


def drill_kill_resume(scratch: Path) -> Dict[str, Any]:
    require_scratch_root(scratch)
    from datetime import timezone as tz

    from floati.errors import ProtocolRefusal
    from floati.orchestrate import DrillAction, FleetOrchestrator, OrchestrationPlan
    from floati.planes import AuthorityGrantStore
    from floati.registry import Registry
    from floati.work import WorkLog
    from floati.workers import WorkerReceipts

    root = _floati_root(scratch)
    current = datetime.now(tz.utc)
    for node in ("lane-a", "lane-b", "lane-c"):
        try:
            Registry(root).register(node, "Codex")
        except ProtocolRefusal:
            pass
        AuthorityGrantStore(root).claim(f"work-{node}", node, 30, 20, current)
    plan_path = scratch / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 0,
                "workers": ["lane-a", "lane-b", "lane-c"],
                "items": [
                    {"key": "a", "title": "Create A.txt", "owner": "lane-a", "needs": []},
                    {"key": "b", "title": "Create B.txt", "owner": "lane-b", "needs": []},
                    {"key": "c", "title": "Create C.txt", "owner": "lane-c", "needs": []},
                    {"key": "d", "title": "Create D.txt", "owner": "lane-a", "needs": ["a", "b", "c"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    plan = OrchestrationPlan.load(plan_path)
    result = FleetOrchestrator(
        root,
        {"fixture": CompletingAdapter()},
        adapter_name="fixture",
        redraw_interval=0.01,
        worker_timeout=1,
    ).run(
        plan,
        deadline_seconds=5,
        drills=(DrillAction("kill_worker", "lane-a"),),
    )
    work_after = list(WorkLog(root).show())
    receipts = list(WorkerReceipts(root).records())
    sessions = list(WorkerReceipts(root).sessions())
    second = None
    try:
        FleetOrchestrator(
            root,
            {"fixture": CompletingAdapter()},
            adapter_name="fixture",
            redraw_interval=0.01,
            worker_timeout=1,
        ).run(plan, deadline_seconds=3)
        second = {"refused": False}
    except ProtocolRefusal as exc:
        second = {"refused": True, "code": exc.code, "detail": exc.detail}
    return {
        "kill_result": result,
        "work_after": work_after,
        "receipt_count": len(receipts),
        "session_count": len(sessions),
        "sessions": sessions,
        "second_orchestrate": second,
    }


def drill_retire(scratch: Path, node: str) -> Dict[str, Any]:
    require_scratch_root(scratch)
    retire = capture(
        FLOATI + ["retire", "--root", str(scratch), node],
        cwd=CLONE,
    )
    return {"retire": retire}


def drill_uninstall_dry_run(scratch: Path) -> Dict[str, Any]:
    require_scratch_root(scratch)
    dest = scratch / "install-dest"
    dest.mkdir(parents=True, exist_ok=True)
    owned = dest / "shipped.txt"
    owned.write_text("gauntlet owned bytes\n", encoding="utf-8")
    from floati import wiring_journal

    digest = hashlib.sha256(owned.read_bytes()).hexdigest()
    wiring_journal.append_entry(
        dest,
        {
            "v": 1,
            "ts": "2026-08-28T00:00:00Z",
            "actor": {"command": "install", "floatiVersion": "gauntlet"},
            "action": "install",
            "kind": "file",
            "path": str(owned),
            "op": "create",
            "sha256": digest,
        },
    )
    wiring_journal.append_entry(
        dest,
        {
            "v": 1,
            "ts": "2026-08-28T00:00:01Z",
            "actor": {"command": "install", "floatiVersion": "gauntlet"},
            "action": "install",
            "kind": "bus_root",
            "path": str(scratch),
            "op": "create",
            "preserved": True,
        },
    )
    before = owned.read_bytes()
    dry = capture(
        FLOATI
        + [
            "uninstall",
            "--destination",
            str(dest),
            "--dry-run",
            "--root",
            str(scratch),
        ],
        cwd=CLONE,
    )
    after = owned.read_bytes()
    empty_dest = scratch / "empty-install"
    empty_dest.mkdir(parents=True, exist_ok=True)
    empty = capture(
        FLOATI + ["uninstall", "--destination", str(empty_dest), "--dry-run"],
        cwd=CLONE,
    )
    return {
        "journaled_dry_run": dry,
        "owned_bytes_before": len(before),
        "owned_bytes_after": len(after),
        "owned_unchanged": before == after,
        "owned_path": str(owned),
        "empty_dest_dry_run": empty,
    }


def drill_wake_skipped() -> Dict[str, Any]:
    return {
        "status": "SKIP",
        "reason": "Wake drills wait for the WS-A controller; this seat does not simulate them.",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--guards-only"]:
        require_scratch_root(SCRATCH_PARENT / "probe")
        return 0
    sha = subprocess.check_output(
        ["/usr/bin/git", "-C", str(CLONE), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    scratch = require_scratch_root(SCRATCH_PARENT / f"h{stamp}")
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    control = capture([PYTHON, "--version"])
    node = "grok-h"
    payload: Dict[str, Any] = {
        "started_utc": _utc(),
        "trunk_sha": sha,
        "scratch": str(scratch),
        "live_fleet_refused": str(LIVE_FLEET_ROOT),
        "control": control,
        "wake": drill_wake_skipped(),
        "H1_onboard": drill_onboard(scratch, node),
        "H2_round_trip": drill_round_trip(scratch, node, sha),
        "H3_kill_resume": drill_kill_resume(scratch),
        "H4_retire": drill_retire(scratch, "peer-h1"),
        "H5_uninstall_dry_run": drill_uninstall_dry_run(scratch),
        "ended_utc": _utc(),
    }
    artifact = write_json(CAPTURE_DIR / "H-skeleton-run.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GauntletGuardError as exc:
        print(
            json.dumps({"status": "refused", "code": exc.code, "detail": exc.detail}),
            file=sys.stderr,
        )
        raise SystemExit(2)
