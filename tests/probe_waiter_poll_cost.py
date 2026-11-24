"""Scratch-only cumulative CPU measurement, with cold startup reported apart."""
from __future__ import annotations

import argparse
import io
import json
import time
from datetime import datetime, timezone

from floati import codex_wait, fixture_ids
from floati.codex_wait_contract import CodexWaitConsentLedger
from floati.events import EventLog, validate_event_records
from floati.ids import uuid7_hex
from floati.jsonl import encode_frame
from tests import test_codex_wait as baseline
from tests.waiter_timing import scripted_readiness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bytes", type=int, default=6_500_000)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--held", action="store_true", help="create native unACKed presentation history before measurement")
    parser.add_argument("--roll", action="store_true", help="roll the native scratch epoch before measurement")
    args = parser.parse_args()
    if not 1000 <= args.bytes <= 8_000_000 or not 3 <= args.seconds <= 30:
        parser.error("bytes must be 1000..8000000 and seconds 3..30")
    f = baseline.CodexWaitRuntimeTests()
    try:
        f.setUp()
        log = EventLog(f.root)
        log.send(fixture_ids.builder("floati"), "architect",
                    "floati", "a" * 40, "docs/evidence/ping.md", "probe unselected mail",
                    idempotency_key="cost-probe-template")
        template = next(row for row in log.event_records() if row["kind"] == "message_envelope")
        frames, records, length = [], [], 0
        while length < args.bytes:
            row = dict(template, id="msg-" + uuid7_hex(),
                       idempotency_key="cost-probe-" + str(len(records)))
            frame = encode_frame(row)
            records.append(row)
            frames.append(frame)
            length += len(frame)
        validate_event_records(records)
        f.root.resolve_relative("events.jsonl").write_bytes(b"".join(frames))
        CodexWaitConsentLedger(f.root).arm(f.participant.binding,
            hook_timeout_seconds=300, wait_deadline_seconds=240,
            idempotency_key="cost-probe-budget")
        if args.held:
            f.send("cost-probe-held")
            prepared, diagnostic = io.StringIO(), io.StringIO()
            codex_wait.run_stop_waiter(bus_home=f.bus_home,
                hook_payload={"cwd": str(f.workspace), "session_id": "cost-probe"},
                stdout=prepared, stderr=diagnostic)
            if "1 new message(s)" not in prepared.getvalue() or diagnostic.getvalue():
                raise RuntimeError("probe did not prepare native held history")
        if args.roll:
            from floati.bus_epoch import ROLL_AUTHORITY_SUBJECT, roll_bus_epoch
            from floati.planes import AuthorityGrantStore
            AuthorityGrantStore(f.root).grant_exact(ROLL_AUTHORITY_SUBJECT, "architect", 1,
                datetime.now(timezone.utc))
            roll_bus_epoch(f.root, actor="architect", idempotency_key="cost-probe-roll")
        output, error = io.StringIO(), io.StringIO()
        started = (time.monotonic_ns(), time.process_time_ns())
        steady = []
        samples = []
        startup_polls = []

        class ProbeComplete(Exception):
            pass

        def sleep(seconds):
            now = (time.monotonic_ns(), time.process_time_ns())
            if not startup_polls:
                # Native startup testimony can invalidate the initial watch.
                # Include its first reconciliation in cold startup, separately
                # from the sustained idle hold; subsequent dirty polls count.
                startup_polls.append(now)
                time.sleep(seconds)
                return
            if not steady:
                steady.append(now)
            wall = (now[0] - steady[0][0]) / 1e9
            cpu = (now[1] - steady[0][1]) / 1e9
            samples.append({"wall_seconds": wall, "cpu_seconds": cpu})
            if wall >= args.seconds:
                raise ProbeComplete()
            time.sleep(seconds)

        try:
            codex_wait.run_stop_waiter(bus_home=f.bus_home,
                hook_payload={"cwd": str(f.workspace), "session_id": "cost-probe"},
                stdout=output, stderr=error, wait_readable=scripted_readiness((sleep), interval=(1.0)), sleep=sleep)
        except ProbeComplete:
            pass
        if not steady or len(samples) < 2 or output.getvalue() or error.getvalue():
            raise RuntimeError("probe did not complete an ordinary idle hold: " + error.getvalue())
        last = samples[-1]
        print(json.dumps({"ledger_bytes": length, "ledger_records": len(records),
            "held_history": args.held, "rolled_epoch": args.roll,
            "poll_interval_seconds": 1.0, "samples": samples,
            "startup_reconciliation_polls": 1,
            "cold_wall_seconds": (steady[0][0] - started[0]) / 1e9,
            "cold_cpu_seconds": (steady[0][1] - started[1]) / 1e9,
            "steady_cpu_percent": 100 * last["cpu_seconds"] / last["wall_seconds"]}, sort_keys=True))
    finally:
        f.doCleanups()


if __name__ == "__main__":
    main()
