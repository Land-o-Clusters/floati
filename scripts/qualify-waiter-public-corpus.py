#!/usr/bin/python3
"""Measure sealed native history through the October 27 settled endpoint.

Every arm is a fresh system-Python process with a scratch HOME. Construction
uses native holds, presentations, ACKs and rolls; no production root is read.
An explicitly supplied caught-up snapshot is copied before any native write.
The parent owns and collects its one child at a time. Private banks are retained
for inspection. CPU starts at process birth, with no baseline subtraction.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve()
CASES = {
    "sealed-baseline": (8, 16, 1, False),
    "sealed-depth-five": (8, 16, 5, False),
    "sealed-imported-baseline": (8, 16, 2, True),
    "sealed-holds-tenfold": (80, 16, 2, True),
    "sealed-cuts-tenfold": (8, 160, 2, True),
}
BORROWED_CASES = ("borrowed-absent-ten-mb", "borrowed-prepared-ten-mb")
SEALED_EVENT_BYTES = 10 * 1024 * 1024
SECONDS = 180


class EndpointComplete(BaseException):
    """Unwind the owned experiment after a qualified native watch."""


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def inventory(path):
    """Hash only the explicitly selected tree; refuse aliases in a snapshot."""
    result = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError("selected tree contains a symlink: " + str(item.relative_to(path)))
        if item.is_file() and "__pycache__" not in item.parts:
            result[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    return result


def provenance():
    return {
        "git_sha": subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=REPO, text=True).strip(),
        "product": inventory(REPO / "floati"), "schemas": inventory(REPO / "schemas"),
        "instrument_sha256": hashlib.sha256(HERE.read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256((REPO / "tests/probe_waiter_projection_memo.py").read_bytes()).hexdigest(),
        "interpreter": "/usr/bin/python3", "controller_python": sys.version,
    }


def bind(root, workspace, node, session):
    from floati.codex_wait_contract import (write_workspace_map, resolve_participant,
        CodexWaitConsentLedger, CodexWaitSessionLedger)
    write_workspace_map(root.tenant_home, workspace, node)
    participant = resolve_participant(root.tenant_home, workspace)
    if participant is None:
        raise ValueError("private fixture participant did not resolve")
    consent = CodexWaitConsentLedger(root).arm(participant.binding,
        hook_timeout_seconds=SECONDS + 120, wait_deadline_seconds=SECONDS,
        idempotency_key=session + "-consent")
    CodexWaitSessionLedger(root).arm(participant.binding, consent, session,
        idempotency_key=session + "-claim", take_over=True)


def legacy_fixture_roll(fixture):
    """Place the pre-v3 byte-exact carry behind a genuine manual receipt.

    This is fixture installation, like EpochCarryTests.install_fixture_carry;
    the copied rows, outgoing archive and all later admission remain native.
    It deliberately does not exercise the current roll's v3 carry writer.
    """
    from floati import bus_epoch
    from floati.epoch_carry import build_carry_plan
    from floati.framing import encode_frame
    with bus_epoch.epoch_guard(fixture.root, exclusive=True):
        selected = bus_epoch._preflight_selected_members(fixture.root)
        old = bus_epoch._read_selected_epoch_payloads(fixture.root, selected)
        plan = build_carry_plan(fixture.root, old)
    fixture.rolls += 1
    result = bus_epoch.roll_bus_epoch(fixture.root, actor="sender",
        idempotency_key="public-legacy-fixture-" + str(fixture.rolls))
    with bus_epoch.epoch_guard(fixture.root, exclusive=True):
        for relative, _plane, raw in plan.live_payloads:
            path = fixture.root.path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((encode_frame(result["receipt"])
                if relative == "events.jsonl" else b"") + raw)


def build(bank, shape):
    from floati.cursor import SparseCursor
    from floati.events import EventLog
    from floati.snapshot import _owned_epoch_archives
    from floati.wake_hold import WakeHoldController
    from tests.probe_waiter_projection_memo import NativeFixture, SESSION, private_runtime

    holds, cuts, depth, imported = shape
    with private_runtime(bank / "construction"):
        fixture = NativeFixture(bank / "run", "codex")
        bind(fixture.root, fixture.workspace, fixture.node, SESSION)
        def held_batch(epoch, *, carry=False):
            held = []
            for ordinal in range(holds):
                key = "sealed-%d-hold-%d" % (epoch, ordinal)
                message = fixture.send(key)
                held.append(message["id"])
                issued = WakeHoldController(fixture.root).evaluate(fixture.node,
                    idempotency_key=key, limit=1)
                if issued.get("receipt", {}).get("item_ids") != [message["id"]]:
                    raise AssertionError("native fixture did not issue its distinct hold")
            anchor = fixture.send("sealed-imported-anchor") if carry else None
            expected = held + ([anchor["id"]] if anchor else [])
            rows, receipt = EventLog(fixture.root).present(fixture.node)
            if receipt is None or {row["id"] for row in rows} != set(expected):
                raise AssertionError("native held population was not presented exactly")
            SparseCursor(fixture.root).ack(fixture.node, held, acting_session_id=SESSION)
            return anchor

        def cut_batch(epoch):
            for ordinal in range(cuts):
                message = fixture.send("sealed-%d-cut-%d" % (epoch, ordinal))
                fixture.publish()
                SparseCursor(fixture.root).ack(fixture.node, [message["id"]],
                    acting_session_id=SESSION)

        if imported:
            # Preserve the banked corpus's actual expensive shape: a mixed
            # delivery carries closed hold origins with one outstanding anchor,
            # and the next epoch's cuts validate those imported holds. Only
            # after that work is complete do we ACK the anchor and seal it all.
            anchor = held_batch(0, carry=True)
            legacy_fixture_roll(fixture)
            source = WakeHoldController(fixture.root)._read(fixture.node, None)
            admitted_imported = sum(row["kind"] == "wake_hold_receipt" and (
                row["delivery_prefix_digest"] != source[3][ordinal]
                or row["event_prefix_digest"] not in source[1]
                or row["acknowledgment_prefix_digest"] not in source[5])
                for ordinal, row in enumerate(source[2]))
            if admitted_imported != holds:
                raise AssertionError("native corpus did not retain its imported hold origins")
            cut_batch(1)
            SparseCursor(fixture.root).ack(fixture.node, [anchor["id"]],
                acting_session_id=SESSION)
            fixture.roll(legacy=False)
        else:
            for epoch in range(depth):
                held_batch(epoch)
                cut_batch(epoch)
                # No borrowed or live imported item may confound depth arms.
                fixture.roll(legacy=False)
        archives = _owned_epoch_archives(fixture.root)
        if len(archives) != depth or EventLog(fixture.root).unacked_ids(fixture.node):
            raise AssertionError("sealed fixture is not the requested caught-up depth")
        bind(fixture.root, fixture.workspace, fixture.node, SESSION)
        return {"root": str(fixture.root.tenant_home), "workspace": str(fixture.workspace),
            "node": fixture.node, "session": SESSION,
            "population": {"sealed_epochs": depth,
                "distinct_hold_origins": holds if imported else holds * depth,
                "sealed_imported_hold_origins": holds if imported else 0,
                "imported_fixture_installation": "manual receipt and byte-exact retained frames" if imported else None,
                "sealed_presentation_cuts": cuts if imported else cuts * depth,
                "current_outstanding": 0,
                "archive_member_bytes": sum(item.stat().st_size for archive in archives
                    for item in archive.rglob("*") if item.is_file())}}


def build_borrowed_source(bank):
    """Prepare one borrowed item behind >=10 MiB of admitted sealed events.

    Padding is canonical schema-validated message/retraction pairs in a private
    fixture, batched under EX. The roll does its complete ordinary admission;
    neither its reader nor the measured waiter has a substituted source.
    """
    from floati import bus_epoch
    from floati.events import EventLog
    from floati.framing import encode_frame
    from floati.ids import uuid7_hex
    from floati.records import validate_record
    from floati.snapshot import _owned_epoch_archives
    from tests.probe_waiter_projection_memo import NativeFixture, SESSION, private_runtime
    with private_runtime(bank / "construction"):
        fixture = NativeFixture(bank / "run", "codex")
        bind(fixture.root, fixture.workspace, fixture.node, SESSION)
        borrowed = fixture.send("borrowed-prepared")
        padding = fixture.send("padding-template", recipient="other",
            worker_session_id="sealed-padding")
        retraction = EventLog(fixture.root).retract(padding["id"],
            worker_session_id="sealed-padding", reason="sent_in_error", author="sender")
        events = fixture.root.tenant_home / "events.jsonl"
        with bus_epoch.epoch_guard(fixture.root, exclusive=True):
            size = events.stat().st_size
            pairs = 0
            with events.open("ab") as output:
                while size < SEALED_EVENT_BYTES:
                    message = dict(padding, id="msg-" + uuid7_hex(),
                        idempotency_key="sealed-padding-" + str(pairs), note="界" * 1024)
                    removed = dict(retraction, id="ret-" + uuid7_hex(),
                        retracted_message_id=message["id"])
                    payload = b"".join(encode_frame(validate_record(row,
                        fixture.root.tenant_id, {row["kind"]}, integrity=False))
                        for row in (message, removed))
                    output.write(payload)
                    size += len(payload)
                    pairs += 1
        prepared = fixture.publish(stage="prepared")
        if prepared["item_ids"] != [borrowed["id"]]:
            raise AssertionError("borrowed fixture prepared a different message selection")
        result = fixture.roll(legacy=False)
        archives = _owned_epoch_archives(fixture.root)
        if len(archives) != 1:
            raise AssertionError("borrowed fixture did not seal exactly one epoch")
        sealed_bytes = (archives[0] / "events.jsonl").stat().st_size
        if sealed_bytes < SEALED_EVENT_BYTES:
            raise AssertionError("borrowed fixture's sealed events are below 10 MiB")
        return {"root": str(fixture.root.tenant_home), "workspace": str(fixture.workspace),
            "node": fixture.node, "session": SESSION, "borrowed_id": borrowed["id"],
            "population": {"sealed_epochs": 1, "sealed_events_bytes": sealed_bytes,
                "padding_pairs": pairs, "padding_source": "batched canonical schema-validated retracted frames",
                "archive_member_bytes": sum(path.stat().st_size for path in archives[0].rglob("*")
                    if path.is_file()), "roll_receipt_id": result["receipt"]["id"],
                "source_archive_sha256": result["receipt"]["archive_sha256"]}}


def copy_borrowed_arm(bank, source, *, prepared):
    """Pair identical sealed history with/without an active carried item."""
    from floati.cursor import SparseCursor
    from floati.events import EventLog
    from floati.root import FloatiRoot
    from tests.probe_waiter_projection_memo import private_runtime
    home = bank / Path(source["root"]).name
    shutil.copytree(source["root"], home)
    root = FloatiRoot.open_direct_home(home)
    # Keep the original workspace/session: those are the prepared row's exact
    # identity. Only the selected root is copied; no original fixture is written.
    with private_runtime(bank / "construction"):
        bind(root, Path(source["workspace"]), source["node"], source["session"])
        if not prepared:
            rows, receipt = EventLog(root).present(source["node"])
            if receipt is None or [row["id"] for row in rows] != [source["borrowed_id"]]:
                raise AssertionError("borrowed control did not present its exact carried item")
            SparseCursor(root).ack(source["node"], [source["borrowed_id"]],
                acting_session_id=source["session"])
    return {"root": str(home), "workspace": source["workspace"], "node": source["node"],
        "session": source["session"], "expected_held_ids": [source["borrowed_id"]] if prepared else [],
        "expected_presentation_state": "presentation_uncertain" if prepared else "caught_up",
        "population": {**source["population"], "current_outstanding": 1 if prepared else 0,
            "active_borrowed_item": prepared, "control": "native ACK of the same carried item"}}


def measure(config):
    """Delegate native wait; use the unchanged October 27 endpoint predicates.

    Adapted from run-waiter-arm-remeasure-2026-10-27.py::measure. A first wait
    is never settlement. Require the same complete watch to finish its entire
    native idle tick, then an authorized unchanged idle decision with no output.
    """
    from floati import codex_wait
    output, errors = io.StringIO(), io.StringIO()
    previous, endpoint = None, None
    native = codex_wait._WaiterInputsWatch.wait

    def qualified(frame):
        values = frame.f_locals
        artifact = values.get("idle_artifact") or {}
        selection = artifact.get("_selection") or {}
        expected_ids = config.get("expected_held_ids", [])
        held = selection.get("held_items", [])
        native_idle_state = "held_only" if expected_ids else "caught_up"
        participant = values.get("participant")
        return (getattr(values.get("classification"), "state", None) == "authorized"
            and participant is not None
            and str(participant.root.tenant_home) == config["root"]
            and str(participant.binding.workspace) == config["workspace"]
            and participant.binding.node_id == config["node"]
            and values.get("session_id") == config["session"]
            # Native conversation artifacts call a prepared-only reservation
            # held_only; the presentation evidence calls it uncertain. Require
            # its exact IDs and phase rather than relaxing the endpoint to any
            # no-output state.
            and artifact.get("state") == native_idle_state
            and selection.get("held_total") == len(expected_ids)
            and [item["item_id"] for item in held] == expected_ids
            and (not expected_ids or all(item["phase"] == "prepared" for item in held))
            and artifact.get("wake_required") is False and not artifact.get("fresh_messages")
            and values.get("dirty") is False and values.get("invalidate") is False
            and not output.getvalue() and not errors.getvalue())

    def observed(watch, *args, **kwargs):
        nonlocal previous, endpoint
        frame = sys._getframe(1)
        # Sample before marker I/O or host-load reads, charging all imports.
        cpu = time.process_time()
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        child_cpu = usage.ru_utime + usage.ru_stime
        if (previous and previous["qualified"] and qualified(frame)
                and watch is previous["watch"] and watch.complete and watch.source is not None
                and previous["ready"] is False
                and previous["blocked"] >= previous["requested"] >= 0.99):
            endpoint = {"qualified_endpoint": True,
                "endpoint": "settled_complete_native_watch", "cpu_seconds": cpu + child_cpu,
                "self_cpu_seconds": cpu, "collected_child_cpu_seconds": child_cpu,
                "host_load_after": list(os.getloadavg()),
                "active_descendant_cpu_included": False,
                "native_tick_requested_seconds": previous["requested"],
                "native_tick_blocked_seconds": previous["blocked"],
                "presentation_state": config.get("expected_presentation_state", "caught_up"),
                "held_total": len(config.get("expected_held_ids", [])),
                "sealed_read": list((frame.f_locals["idle_artifact"].get("_selection") or {}).get("sealed_read", []))}
            raise EndpointComplete()
        row = {"watch": watch, "qualified": qualified(frame),
            "requested": min(float(args[0]), 60.0)}
        began = time.monotonic()
        ready = native(watch, *args, **kwargs)
        row.update(ready=ready, blocked=time.monotonic() - began)
        previous = row
        return ready

    native_code, termination, result = None, "native_return_without_endpoint", {}
    try:
        codex_wait._WaiterInputsWatch.wait = observed
        native_code = codex_wait.run_stop_waiter(bus_home=Path(config["root"]),
            hook_payload={"cwd": config["workspace"], "session_id": config["session"]},
            stdout=output, stderr=errors, _waiter_result=result)
    except EndpointComplete:
        termination = "observer_endpoint_unwind"
    finally:
        codex_wait._WaiterInputsWatch.wait = native
    return {"status": "measured" if endpoint else "unqualified",
        "endpoint": endpoint, "native_return_code": native_code, "termination": termination,
        "native_result_kind": result.get("kind"), "native_result_code": result.get("code"),
        "native_output_characters": len(output.getvalue()),
        "native_error_characters": len(errors.getvalue())}


def worker(config_path):
    config = json.loads(config_path.read_bytes())
    sys.path.insert(0, str(REPO))
    result = measure(config)
    save(config_path.with_name("measurement.json"), result)
    return 0 if result["endpoint"] else 1


@contextmanager
def controller_termination():
    """Route controller termination through owned-child interrupt and reap."""
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_child(bank, config):
    config_path = bank / "config.json"
    save(config_path, config)
    home, temporary = bank / "home", bank / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home),
        "TMPDIR": str(temporary), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTHONUSERBASE": "\x2fUsers/penguinspecz/Library/Python/3.9"}
    with controller_termination(), (bank / "stdout").open("w") as output, (bank / "stderr").open("w") as errors:
        child = subprocess.Popen(["/usr/bin/python3", "-B", str(HERE), "--worker", str(config_path)],
            cwd=bank, env=environment, stdout=output, stderr=errors, start_new_session=True)
        try:
            child.wait(timeout=SECONDS + 300)
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                # Keep the original handle until collection. A slow unwind or
                # ignored interrupt must never orphan a worker then launch the
                # next arm. One interrupt is the only signal this driver sends.
                while child.poll() is None:
                    try:
                        child.wait(timeout=60)
                    except (subprocess.TimeoutExpired, KeyboardInterrupt):
                        print(json.dumps({"phase": "await_owned_child_collection",
                            "pid": child.pid, "bank": str(bank)}), file=sys.stderr, flush=True)
    path = bank / "measurement.json"
    result = json.loads(path.read_bytes()) if path.is_file() else {"status": "missing_measurement", "endpoint": None}
    return {**result, "child_returncode": child.returncode,
        "child_collected": child.poll() is not None, "bank": str(bank)}


def run(args):
    sys.path.insert(0, str(REPO))
    os.umask(0o077)
    bank = Path(tempfile.mkdtemp(prefix="waiter-sealed-public-", dir="\x2fprivate/tmp"))
    print(json.dumps({"bank": str(bank), "phase": "created"}), file=sys.stderr, flush=True)
    before = provenance()
    selected = ["snapshot"] if args.snapshot else (args.case or list(CASES) + list(BORROWED_CASES))
    if len(selected) != len(set(selected)):
        raise ValueError("public case selection must not repeat a case")
    if "borrowed-prepared-ten-mb" in selected and "borrowed-absent-ten-mb" not in selected:
        selected.insert(0, "borrowed-absent-ten-mb")
    measurements = {}
    borrowed_sources = {}
    snapshot_before = inventory(args.snapshot) if args.snapshot else None
    for label in selected:
        trials = []
        for repetition in range(1, args.repetitions + 1):
            trial = bank / (label + "-" + str(repetition))
            trial.mkdir(mode=0o700)
            print(json.dumps({"case": label, "repetition": repetition, "phase": "construct"}),
                file=sys.stderr, flush=True)
            if args.snapshot:
                from floati.root import FloatiRoot
                from tests.probe_waiter_projection_memo import private_runtime
                root = trial / args.snapshot.name
                shutil.copytree(args.snapshot, root, symlinks=True)
                if inventory(root) != snapshot_before or inventory(args.snapshot) != snapshot_before:
                    raise ValueError("offline snapshot changed during copy")
                workspace = trial / "workspace"
                workspace.mkdir(mode=0o700)
                config = {"root": str(root), "workspace": str(workspace), "node": args.node,
                    "session": "sealed-snapshot-" + str(repetition)}
                with private_runtime(trial / "construction"):
                    bind(FloatiRoot.open_direct_home(root), workspace, args.node, config["session"])
            elif label in BORROWED_CASES:
                if repetition not in borrowed_sources:
                    source_bank = bank / ("borrowed-source-" + str(repetition))
                    source_bank.mkdir(mode=0o700)
                    borrowed_sources[repetition] = build_borrowed_source(source_bank)
                source = borrowed_sources[repetition]
                sealed_before = inventory(Path(source["root"]))
                config = copy_borrowed_arm(trial, source,
                    prepared=label == "borrowed-prepared-ten-mb")
                if inventory(Path(source["root"])) != sealed_before:
                    raise AssertionError("paired arm construction changed its shared source")
            else:
                config = build(trial, CASES[label])
            observed = run_child(trial, config)
            observed.update(repetition=repetition, population=config.get("population"))
            observed["source_unchanged"] = provenance() == before
            trials.append(observed)
            print(json.dumps({"case": label, "repetition": repetition,
                "endpoint": observed["endpoint"], "phase": "collected"}), file=sys.stderr, flush=True)
        measurements[label] = trials
    growth = {}
    for label, trials in measurements.items():
        if label in {"snapshot", "sealed-baseline", "sealed-imported-baseline", *BORROWED_CASES}:
            continue
        baseline_label = "sealed-baseline" if label == "sealed-depth-five" else "sealed-imported-baseline"
        baseline = measurements.get(baseline_label, [])
        growth[label] = [
            large["endpoint"]["cpu_seconds"] / small["endpoint"]["cpu_seconds"]
            if large["endpoint"] and small["endpoint"] else None
            for small, large in zip(baseline, trials)]
    borrowed_overhead = [
        carried["endpoint"]["cpu_seconds"] - absent["endpoint"]["cpu_seconds"]
        if carried["endpoint"] and absent["endpoint"] else None
        for absent, carried in zip(measurements.get("borrowed-absent-ten-mb", []),
                                  measurements.get("borrowed-prepared-ten-mb", []))]
    snapshot_unchanged = args.snapshot is None or inventory(args.snapshot) == snapshot_before
    complete = all(row.get("endpoint") and row["child_collected"]
        and row["child_returncode"] == 0 and row["source_unchanged"]
        for trials in measurements.values() for row in trials)
    low_load = complete and all(row["endpoint"]["host_load_after"][2] <= 8
        for trials in measurements.values() for row in trials)
    passing = complete and snapshot_unchanged and low_load and all(
        row["endpoint"]["cpu_seconds"] <= 2.0
        for trials in measurements.values() for row in trials) and all(
        values and all(value is not None and value <= 1.5 for value in values)
        for values in growth.values()) and (
        "borrowed-prepared-ten-mb" not in measurements or borrowed_overhead and all(
            value is not None and value <= 0.1 for value in borrowed_overhead))
    result = {"classification": "MEASURED_OFFLINE_SNAPSHOT" if args.snapshot else "MEASURED_PUBLIC_SYNTHETIC_NATIVE",
        "bank": str(bank), "provenance": before, "measurements": measurements,
        "growth_ratios": growth, "qualified_endpoints": complete,
        "borrowed_overhead_seconds": borrowed_overhead, "borrowed_overhead_bound_seconds": 0.1,
        "snapshot_unchanged": snapshot_unchanged, "load15_at_most_8": low_load,
        "cpu_bound_seconds": 2.0, "growth_bound": 1.5, "verdict": "PASS" if passing else "FAIL",
        "scope": "selected sealed history cells and explicit paired prepared-only carried-item arms"}
    save(bank / "qualification.json", result)
    print(json.dumps({key: value for key, value in result.items() if key != "provenance"},
                     sort_keys=True), flush=True)
    return 0 if passing else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--case", action="append", choices=tuple(CASES) + BORROWED_CASES)
    parser.add_argument("--snapshot", type=Path, help="explicit caught-up offline root; copied, never written")
    parser.add_argument("--node", help="snapshot's explicit measured recipient")
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if args.snapshot:
        if args.case:
            parser.error("snapshot and public case selection are separate runs")
        if not args.node:
            parser.error("snapshot requires --node")
        args.snapshot = args.snapshot.resolve(strict=True)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
