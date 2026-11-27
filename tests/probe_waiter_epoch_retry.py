"""Visible private native Stop diagnostic; no test runner or live roots.

Only coordination seams are instrumented: an actual tenant flock starts at the
maintenance boundary, or a genuine native append extends the selected bytes
after the real carry planner returns. Native validation, decisions and writes run.
"""
from contextlib import ExitStack, contextmanager
from tests.readiness import wait_event, wait_join
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
from tests.waiter_timing import scripted_readiness


CASES = ("lock_deadline", "lock_release", "lock_retry_release", "lock_short_map",
         "member_once", "member_corrupt",
         "member_pause", "planner_refusal", "member_integrity")


@contextmanager
def replace_attribute(owner, name, value):
    original = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, original)


def run_case(case, base):
    import fcntl
    from floati import bus_epoch, epoch_carry, epoch_rotation
    from floati.bus_epoch import observe_epoch_roll_state
    from floati.codex_wait import run_stop_waiter
    from floati.codex_wait_contract import (WORKSPACE_MAP_RELATIVE, CodexWaitConsentLedger,
        CodexWaitSessionLedger, resolve_participant)
    from floati.errors import IntegrityFailure, ProtocolRefusal
    from floati.events import EventLog
    from floati.jsonl import read_records_snapshot
    from floati.records import WAKE_HOLD_KINDS
    from floati.registry import Registry
    from floati.root import FloatiRoot
    from floati.wake_control import WakeController
    if case not in CASES:
        raise ValueError("unknown diagnostic case")
    base.mkdir(parents=True)
    root = FloatiRoot.open_direct_home(base / "fleet", create=True)
    workspace = base / "workspace"
    workspace.mkdir()
    Registry(root).register("seat", "Codex")
    Registry(root).register("sender", "worker")
    budget = 1 if case == "lock_deadline" else (7 if case == "lock_retry_release" else 3)
    arm = __import__("floati.waiter_consent", fromlist=["arm_waiter_consent"])
    arm.arm_waiter_consent(root.path, workspace, "seat", "Codex", 10, budget)
    participant = resolve_participant(root.path, workspace)
    if participant is None:
        raise AssertionError("native participant missing")
    consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
    CodexWaitSessionLedger(root).arm(participant.binding, consent, "session",
        idempotency_key="epoch-retry-session")
    message = EventLog(root).send("sender", "seat", "repo", "a" * 40,
        "docs/proof.md", "native pending notification")
    (root.path / "state").mkdir(exist_ok=True)
    (root.path / "state/ledger-policy.json").write_text(json.dumps({
        "schema_version": 0, "max_bytes": 1, "max_age_days": 7}))
    output, errors, outcome = io.StringIO(), io.StringIO(), {}
    ready, release = threading.Event(), threading.Event()
    thread_errors, children, mutations = [], [], []
    native_maintenance = epoch_rotation.stop_maintenance
    native_plan = epoch_carry.build_carry_plan
    calls = []
    force_acquisition_timeout = case in {"lock_deadline", "lock_retry_release"}
    clock = [0.0]
    acquisition_timeouts = []

    def monotonic():
        return clock[0]

    def hold_epoch():
        descriptor = None
        try:
            descriptor = os.open(root.tenant_home, os.O_RDONLY | os.O_DIRECTORY)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            ready.set()
            wait_event(release, "release the owned epoch lock")
        except BaseException as failure:
            thread_errors.append(repr(failure))
            ready.set()
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def maintenance(*args, **kwargs):
        calls.append(None)
        if case.startswith("lock_") and len(calls) == 1:
            child = threading.Thread(target=hold_epoch)
            children.append(child)
            child.start()
            if not wait_event(ready, "probe_waiter_epoch_retry ready") or thread_errors:
                raise AssertionError("private tenant lock holder did not start")
        try:
            return native_maintenance(*args, **kwargs)
        except ProtocolRefusal as failure:
            if force_acquisition_timeout and failure.code == "epoch_lock_timeout":
                acquisition_timeouts.append(monotonic())
                if case == "lock_retry_release":
                    # Release only after native flock acquisition actually
                    # timed out, then let the original waiter retry it.
                    release.set()
                    wait_join(children[0], "epoch lock holder exits before retry")
            raise

    def planner(current, payloads):
        if case == "planner_refusal":
            raise ProtocolRefusal("epoch_selected_member_changed", "invalid planner coordinates")
        if case == "member_integrity":
            raise IntegrityFailure("epoch_selected_member_changed", "invalid native testimony")
        plan = native_plan(current, payloads)
        if case in {"member_once", "member_corrupt", "member_pause"} and not mutations:
            mutations.append(case)
            path = current.tenant_home / "events.jsonl"
            if case == "member_corrupt":
                path.write_bytes(b"not-json\n")
            else:
                # The same EX owner may perform a nested native append. This
                # models the old writer's completion before selected exclusion,
                # without inventing a decoder result or member-change error.
                EventLog(current).send("sender", "sender", "repo", "b" * 40,
                    "docs/proof.md", "native append after carry planning")
        return plan

    def sleep(seconds):
        if (case in {"lock_release", "lock_short_map"} and ready.is_set()
                and not release.is_set()):
            # The native lock attempt has reached contention. Publish the
            # controlled map change before allowing the next acquisition.
            if case == "lock_short_map":
                mutations.append(case)
                (root.path / WORKSPACE_MAP_RELATIVE).write_bytes(b"not-json\n")
            release.set()
            wait_join(children[0], "epoch lock holder releases after contention")
        if case == "member_pause" and mutations and not release.is_set():
            release.set()
            WakeController(root).pause("seat", "session", idempotency_key="race-pause")
        clock[0] += seconds

    began = time.monotonic()
    window_started = monotonic()
    try:
        with ExitStack() as changes:
            changes.enter_context(replace_attribute(epoch_rotation, "stop_maintenance", maintenance))
            changes.enter_context(replace_attribute(epoch_carry, "build_carry_plan", planner))
            # The real flock, retry, and delivery are exercised on the
            # original consent window. Advance its clock only when native
            # code requests a wait, independent of fixture scheduling.
            changes.enter_context(replace_attribute(bus_epoch, "time",
                SimpleNamespace(monotonic=monotonic, sleep=sleep)))
            if force_acquisition_timeout:
                changes.enter_context(replace_attribute(bus_epoch, "EPOCH_LOCK_TIMEOUT_SECONDS", 0.1))
            status = run_stop_waiter(bus_home=root.path,
                hook_payload={"cwd": str(workspace), "session_id": "session"},
                stdout=output, stderr=errors, monotonic=monotonic,
                wait_readable=scripted_readiness((sleep), interval=(0.01)), sleep=sleep,
                poll_interval_seconds=0.01, _waiter_result=outcome)
    finally:
        release.set()
        for child in children:
            wait_join(child, "owned epoch holder exits")
    elapsed = time.monotonic() - began
    window_elapsed = monotonic() - window_started
    all_collected = all(not child.is_alive() for child in children)
    if not all_collected or thread_errors:
        raise AssertionError("owned native holder collection failed: " + repr(thread_errors))
    deliveries = read_records_snapshot(root, "receipts/deliveries/seat.jsonl",
        allowed_kinds=set(WAKE_HOLD_KINDS))
    exits = read_records_snapshot(root, "receipts/wake-waiter-exit/seat.jsonl",
        allowed_kinds={"wake_waiter_exit_receipt"})
    decision = json.loads(output.getvalue()) if output.getvalue() else {}
    checks = {"native_return_zero": status == 0, "owned_holders_collected": all_collected}
    if case in {"lock_release", "lock_retry_release", "member_once"}:
        checks.update(fresh_exact_mail=outcome.get("kind") == "fresh_work"
            and outcome.get("item_ids") == [message["id"]],
            one_exact_delivery=len(deliveries) == 1 and deliveries[0]["item_ids"] == [message["id"]],
            no_integrity_exit=all(row["reason_code"] != "integrity_failure" for row in exits),
            original_window=window_elapsed < budget)
        if case == "lock_retry_release":
            checks["acquisition_timeout_retried"] = bool(acquisition_timeouts) and len(calls) >= 3
    elif case == "lock_deadline":
        checks.update(rearm=outcome.get("kind") == "exhausted"
            and "re-arm" in decision.get("reason", ""),
            no_delivery=not deliveries,
            exhausted_testimony=[row["reason_code"] for row in exits] == ["exhausted"],
            original_window=window_elapsed == budget)
    elif case == "member_pause":
        checks.update(native_pause=outcome.get("kind") == "authority_changed"
            and outcome.get("reason") == "paused", no_delivery=not deliveries)
    else:
        expected = ("consumption_state_unavailable" if case == "member_corrupt" else (
            "codex_wait_workspace_map_invalid" if case == "lock_short_map"
            else "epoch_selected_member_changed"))
        checks.update(precise_refusal=outcome.get("kind") == "integrity_failure"
            and outcome.get("code") == expected, no_delivery=not deliveries,
            corruption_or_planner_not_silenced=outcome.get("kind") != "exhausted")
    if not case.startswith("lock_"):
        checks["transition_attempt_observed"] = bool(mutations) if case not in {
            "planner_refusal", "member_integrity"} else True
    return {"case": case, "checks": checks, "outcome": outcome,
        "elapsed_seconds": elapsed, "window_seconds": window_elapsed,
        "acquisition_timeouts": acquisition_timeouts, "stop_maintenance_calls": len(calls),
        "exit_reasons": [row["reason_code"] for row in exits],
        "pending_state": observe_epoch_roll_state(root)["classification"],
        "stdout": output.getvalue(), "stderr": errors.getvalue()}


def main():
    source = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != source:
        raise SystemExit("run from exact diagnostic source worktree")
    bank = Path(tempfile.mkdtemp(prefix="bus-roll-waiter-native-", dir="\x2fprivate/tmp"))
    for name in ("home", "tmp", "xdg"):
        (bank / name).mkdir()
    os.environ.update(HOME=str(bank / "home"), TMPDIR=str(bank / "tmp"),
        XDG_CONFIG_HOME=str(bank / "xdg"), PYTHONDONTWRITEBYTECODE="1")
    import sys
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    paths = ("floati/codex_wait.py", "floati/waiter_lock_retry.py", "floati/bus_epoch.py",
        "floati/epoch_rotation.py", "tests/probe_waiter_epoch_retry.py")
    def pins():
        return {path: hashlib.sha256((source / path).read_bytes()).hexdigest() for path in paths}
    before = pins()
    sha = subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    results = []
    for case in CASES:
        try:
            results.append(run_case(case, bank / case))
        except Exception as failure:
            results.append({"case": case, "checks": {"harness_completed": False},
                "error": repr(failure)})
    after = pins()
    receipt = {"source_sha": sha, "source": str(source), "before": before, "after": after,
        "source_unchanged": before == after, "cases": results}
    (bank / "result.private.json").write_text(json.dumps(receipt, indent=2) + "\n")
    passed = sum(sum(case["checks"].values()) for case in results)
    count = sum(len(case["checks"]) for case in results)
    print(json.dumps({"bank": str(bank), "passed": passed, "checks": count,
        "source_unchanged": before == after}))
    return 0 if passed == count and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
