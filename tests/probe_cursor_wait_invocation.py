"""Owned scratch Cursor hook process, optionally launched by a scratch host."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))


def main():
    root, node, runtime, session, mode, control = sys.argv[1:]
    if mode in {"host", "host-chain", "bridge"}:
        child = subprocess.Popen(["/usr/bin/python3", "-B", str(Path(__file__).resolve()),
            root, node, runtime, session, "bridge" if mode == "host-chain" else "wait", control])
        Path(control + (".bridge.pid" if mode == "host-chain" else ".pid")).write_text(str(child.pid))
        if mode == "bridge":
            return child.wait()
        try:
            while not Path(control).exists():
                time.sleep(.02)
        finally:
            # Deliberately model the host exiting without signalling its hook.
            # The supervising test owns and reaps the scratch hook afterwards.
            pass
        return 0
    if mode == "codex":
        from floati.codex_wait import run_stop_waiter
        return run_stop_waiter(bus_home=Path(root),
            hook_payload={"cwd": str(Path.cwd()), "session_id": session},
            stdout=sys.stdout, stderr=sys.stderr, poll_interval_seconds=.05)
    from floati.cursor_wait import run_cursor_stop_wait
    return run_cursor_stop_wait(root=Path(root), node=node, runtime=Path(runtime),
        deadline_seconds=30, poll_seconds=.05, hook_timeout_seconds=60,
        payload={"conversation_id": session, "status": "completed", "loop_count": 0},
        stdout=sys.stdout, stderr=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
