"""Bounded scratch-only Stop waiter perturbation, explicitly invoked.

The guarded arm splits EventLog's canonical append while preserving its real
root and ledger locks. The negative control deliberately bypasses both locks;
it is a planted protocol violation, not evidence of a production writer race.
No live fleet, hook registration, installed bundle, or account HOME is opened.
"""
from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

# tests.__init__ pins a suite HOME on import. Worker coordinates are passed
# explicitly by this probe's parent and must be saved before that import.
PARENT_SCRATCH_HOME = os.environ.get("HOME")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.readiness import READY_SECONDS, communicate, wait_until
from tests.waiter_timing import scripted_readiness


def wait_for(path: Path, timeout: float = READY_SECONDS) -> None:
    wait_until(path.exists, "probe handshake: " + path.name, timeout=timeout)


def setup(base: Path) -> None:
    from floati.codex_wait_contract import (
        CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant,
    )
    from floati.events import EventLog
    from floati.registry import Registry
    from floati.root import FloatiRoot
    from floati.waiter_consent import arm_waiter_consent

    root = FloatiRoot.open_direct_home(base / "probe-fleet", create=True)
    registry = Registry(root)
    registry.register("probe-reader", "Codex")
    registry.register("probe-writer", "architect")
    registry.register("probe-other", "worker")
    workspace = base / "workspace"
    workspace.mkdir()
    arm_waiter_consent(root.path, workspace, "probe-reader", "Codex", 90, 70)
    participant = resolve_participant(root.tenant_home, workspace)
    assert participant is not None
    consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
    CodexWaitSessionLedger(root).arm(
        participant.binding, consent, "probe-session",
        idempotency_key="probe-arm", take_over=False,
    )
    # Retain a validated nonempty prefix before the writer perturbs its tail.
    EventLog(root).send("probe-writer", "probe-other", "floati", "a" * 40,
        "docs/evidence/probe.md", "scratch seed", idempotency_key="probe-seed")


def lock_identities(root):
    """Observe only the two explicit private fixture lock coordinates."""
    paths = (root.tenant_home, root.resolve_relative("events.jsonl.lock"))
    return {(status.st_dev, status.st_ino) for status in (path.stat() for path in paths)}


def writer(base: Path, guarded: bool) -> dict:
    from floati import jsonl
    from floati.events import EventLog
    from floati.root import FloatiRoot

    root = FloatiRoot.open_direct_home(base / "probe-fleet")
    original_append = jsonl._append_frame
    def split_append(path, encoded, **kwargs):
        if path.name != "events.jsonl":
            return original_append(path, encoded, **kwargs)
        split = len(encoded) // 2
        with path.open("ab", buffering=0) as handle:
            handle.write(encoded[:split])
            os.fsync(handle.fileno())
            (base / "writer-half-frame").touch()
            wait_for(base / "reader-attempt")
            if guarded:
                # Negative observation: a protected read cannot finish while
                # the writer still owns its lock and an incomplete frame.
                time.sleep(0.2)
            else:
                wait_for(base / "reader-terminal")
            blocked = not (base / "reader-terminal").exists()
            handle.write(encoded[split:])
            os.fsync(handle.fileno())
        (base / "writer-complete").touch()
        (base / "writer-observation.json").write_text(json.dumps({
            "reader_blocked_while_partial": blocked,
            "split_offset_in_frame": split,
        }))

    def send():
        return EventLog(root).send("probe-writer", "probe-reader", "floati",
            "a" * 40, "docs/evidence/probe.md", "scratch concurrent ping",
            idempotency_key="probe-appended")

    if guarded:
        jsonl._append_frame = split_append
        return {"message_id": send()["id"]}

    # Obtain the product's exact canonical batch without publishing it. The
    # sentinel unwinds every native guard before the explicitly unguarded
    # raw append; no production lock seam is silently replaced or bypassed.
    class Captured(BaseException):
        pass
    captured = []
    path = root.resolve_relative("events.jsonl")
    before = path.read_bytes()
    def capture(selected, encoded, **kwargs):
        if selected == path:
            captured.append(encoded)
            raise Captured()
        return original_append(selected, encoded, **kwargs)
    jsonl._append_frame = capture
    try:
        send()
    except Captured:
        pass
    finally:
        jsonl._append_frame = original_append
    assert len(captured) == 1 and path.read_bytes() == before
    split_append(path, captured[0])
    return {"message_id": json.loads(captured[0].splitlines()[0])["id"]}


def waiter(base: Path) -> dict:
    from floati import jsonl
    from floati.codex_wait import run_stop_waiter

    from floati.root import FloatiRoot
    root = FloatiRoot.open_direct_home(base / "probe-fleet")
    owned_locks = lock_identities(root)
    native_flock = fcntl.flock

    def observed_flock(descriptor, operation):
        status = os.fstat(descriptor)
        selected = ((status.st_dev, status.st_ino) in owned_locks
            and operation & (fcntl.LOCK_SH | fcntl.LOCK_EX)
            and (base / "writer-half-frame").exists())
        if selected:
            (base / "reader-attempt").touch()
        result = native_flock(descriptor, operation)
        return result

    def observed_sleep(seconds):
        (base / "waiter-sleeping").touch()
        time.sleep(seconds)

    fcntl.flock = observed_flock
    output, errors = io.StringIO(), io.StringIO()
    code = run_stop_waiter(bus_home=base / "probe-fleet",
        hook_payload={"cwd": str(base / "workspace"), "session_id": "probe-session"},
        stdout=output, stderr=errors, wait_readable=scripted_readiness((observed_sleep), interval=(0.025)), sleep=observed_sleep,
        poll_interval_seconds=0.025)
    (base / "reader-terminal").touch()
    return {"code": code, "stdout": output.getvalue(), "stderr": errors.getvalue()}


def run_case(base: Path, guarded: bool) -> dict:
    setup(base)
    command = [sys.executable, "-B", str(Path(__file__).resolve())]
    env = dict(os.environ)
    reader = subprocess.Popen(command + ["--worker", "waiter", "--base", str(base)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    appender = None
    try:
        sleeping = base / "waiter-sleeping"
        wait_until(lambda: sleeping.exists() or reader.poll() is not None,
                   "probe waiter first sleep")
        if not sleeping.exists():
            out, errors = communicate(reader, "probe early reader exit")
            raise RuntimeError("reader exited before sleeping: " + out + errors)
        appender = subprocess.Popen(command + ["--worker", "writer", "--base", str(base)]
            + (["--guarded"] if guarded else []), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        writer_out, writer_err = communicate(appender, "probe writer result", timeout=120)
        reader_out, reader_err = communicate(reader, "probe reader result", timeout=120)
        if appender.returncode or reader.returncode:
            raise RuntimeError(json.dumps({"writer_returncode": appender.returncode,
                "reader_returncode": reader.returncode,
                "writer_error": writer_err, "reader_error": reader_err}))
        writer_result = json.loads(writer_out)
        reader_result = json.loads(reader_out)
        observation = json.loads((base / "writer-observation.json").read_text())
        receipt_path = base / "probe-fleet/receipts/wake-waiter-exit/probe-reader.jsonl"
        exits = ([json.loads(line) for line in receipt_path.read_text().splitlines()]
            if receipt_path.exists() else [])
        deliveries = base / "probe-fleet/receipts/deliveries/probe-reader.jsonl"
        delivered = deliveries.read_text() if deliveries.exists() else ""
        result = {"guarded": guarded, **observation, **writer_result,
            "waiter_code": reader_result["code"],
            "waiter_stdout": reader_result["stdout"],
            "waiter_stderr": reader_result["stderr"],
            "exit_receipt": exits[-1] if exits else None,
            "appended_message_presented": writer_result["message_id"] in delivered}
        assert reader_result["code"] == 0, result
        if guarded:
            assert observation["reader_blocked_while_partial"], result
            decision = json.loads(reader_result["stdout"])
            assert decision["decision"] == "block", result
            assert writer_result["message_id"] in decision["reason"], result
            assert result["appended_message_presented"], result
            assert not any(row["reason_code"] == "integrity_failure" for row in exits), result
        else:
            assert not observation["reader_blocked_while_partial"], result
            assert exits[-1]["reason_code"] == "integrity_failure", result
            assert not result["appended_message_presented"], result
        return result
    finally:
        for child in (appender, reader):
            if child is not None and child.poll() is None:
                child.terminate()
                child.communicate(timeout=4)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--worker", choices=("waiter", "writer"), help=argparse.SUPPRESS)
    parser.add_argument("--base", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--guarded", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        # Child coordinates are created and passed only by this script.
        home = Path(PARENT_SCRATCH_HOME)
        from tests.temp_roots import REAL_TEMP_ROOT
        # tests.__init__ creates a separate suite HOME on import. Restore the
        # explicit parent-owned scratch HOME before opening this probe fleet.
        os.environ["HOME"] = str(home)
        os.environ["CODEX_HOME"] = str(home / "codex")
        assert home.parent == Path(REAL_TEMP_ROOT) and home.name.startswith("f2-append-probe-"), (
            str(home), REAL_TEMP_ROOT)
        assert home.resolve() == home and home.is_dir()
        assert args.base is not None and args.base.parent == home
        print(json.dumps(waiter(args.base) if args.worker == "waiter"
            else writer(args.base, args.guarded), sort_keys=True))
        return 0
    if not 1 <= args.repetitions <= 10:
        parser.error("repetitions must be between 1 and 10")
    from tests.temp_roots import REAL_TEMP_ROOT
    with tempfile.TemporaryDirectory(prefix="f2-append-probe-", dir=REAL_TEMP_ROOT) as temp:
        home = Path(temp)
        os.environ["HOME"] = str(home)
        os.environ["CODEX_HOME"] = str(home / "codex")
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in ("CODEX_THREAD_ID", "CODEX_TURN_ID", "AGENT_BUS_ROOT", "FLOATI_BUS_ROOT"):
            os.environ.pop(name, None)
        results = []
        for ordinal in range(args.repetitions):
            for guarded in (True, False):
                base = home / (str(ordinal) + ("-guarded" if guarded else "-unlocked-fault"))
                base.mkdir()
                results.append(run_case(base, guarded))
        print(json.dumps({"probe": "F2-WAIT-INTEGRITY-EXIT-1 concurrent append",
            "repetitions": args.repetitions, "cases": results,
            "historical_cause": "not established",
            "conclusion": "guarded appends exclude torn reads; bypassing both owned root and ledger locks exposes the planted partial frame"},
            sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
