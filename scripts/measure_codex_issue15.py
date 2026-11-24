"""Issue 15 scratch observer and accelerated product probe.

`observe` owns one interactive native Codex child in a fresh temporary bank;
the owner handles scratch authentication and Stop-hook re-trust in that
terminal. It compares Stop-only and synchronous daemon modes without any
supervisor installation. `capture` runs a fake product probe or imports one
explicit scratch export. Both write JSON, Markdown and optional replay JSON.
`prepare` and `hook` expose the scratch Stop producer separately. Unknown
native exports retain a named cannot-measure result.
"""

import argparse
from contextlib import redirect_stderr
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import (CodexWaitConsentLedger, DEFAULT_HOOK_TIMEOUT_SECONDS,
                                       DEFAULT_WAIT_DEADLINE_SECONDS, resolve_participant)
from floati.events import EventLog
from floati.errors import FloatiError
from floati.ids import uuid7_hex
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_waiter_lifecycle import prepare_waiter
from floati.wake_daemon import WakeDaemon
from floati.wake_daemon_adapters import CODEX_EXECUTABLE, CodexQueueWakeAdapter, adapter_contract_digest
from floati.wake_daemon_contract import AdapterBindingStore, DaemonConsentLedger, DaemonCoordinate
from floati.workers import WorkerRunner
from floati.wake_control import WakeController, validate_session_id


NODE = "issue15-codex"
SCRIPT = Path(__file__).resolve()


def _configuration(hook_timeout_seconds=DEFAULT_HOOK_TIMEOUT_SECONDS,
                   wait_deadline_seconds=DEFAULT_WAIT_DEADLINE_SECONDS):
    if (type(hook_timeout_seconds) is not int or type(wait_deadline_seconds) is not int
            or not 0 < wait_deadline_seconds < hook_timeout_seconds <= 86400):
        raise ValueError("scratch wait deadline must be positive and below its hook timeout, at most 86400")
    return {"hook_timeout_seconds": hook_timeout_seconds, "wait_deadline_seconds": wait_deadline_seconds}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _rows(root, relative, kind):
    return read_records_snapshot(root, Path(relative), allowed_kinds={kind})


def collect_product_probe():
    """Exercise shipped waiter and daemon logic; fake time and queue, no model."""
    with tempfile.TemporaryDirectory(prefix="floati-issue15-", dir=Path(tempfile.gettempdir()).resolve()) as bank:
        scratch = Path(bank).resolve()
        home, workspace = scratch / "home", scratch / "workspace"
        home.mkdir()
        workspace.mkdir()
        receipt = {"artifact_version": 0, "observation_kind": "accelerated_product_probe",
                   "scratch": str(scratch), "home": str(home),
                   "root": str(scratch / "fleet"), "workspace": str(workspace),
                   "defaults": {"hook_timeout_seconds": DEFAULT_HOOK_TIMEOUT_SECONDS,
                                "wait_deadline_seconds": DEFAULT_WAIT_DEADLINE_SECONDS},
                   "stop_windows": []}
        executable = scratch / "fake-codex"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        with mock.patch.dict(os.environ, {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}),\
                mock.patch("floati.wake_daemon_adapters.CODEX_EXECUTABLE", executable):
            root = FloatiRoot.open_direct_home(scratch / "fleet", create=True)
            Registry(root).register(NODE, "Codex")
            Registry(root).register("sender", "Codex")
            prepare_waiter(root, NODE, workspace)
            clock = [0.0]
            for _ in range(2):
                before = clock[0]
                output, errors = io.StringIO(), io.StringIO()
                status = run_stop_waiter(
                    bus_home=root.path, hook_payload={"cwd": str(workspace), "session_id": "issue15-scratch"},
                    stdout=output, stderr=errors, monotonic=lambda: clock[0],
                    sleep=lambda seconds: clock.__setitem__(0, clock[0] + DEFAULT_WAIT_DEADLINE_SECONDS),
                    poll_interval_seconds=1.0)
                receipt["stop_windows"].append({"exit_code": status, "decision": json.loads(output.getvalue()),
                    "elapsed_clock_seconds": clock[0] - before, "stderr": errors.getvalue()})
            receipt["exhaustion_receipts"] = _rows(root, f"receipts/codex-wait-exhaustion/{NODE}.jsonl", "codex_wait_exhaustion_receipt")
            coordinate = DaemonCoordinate(root, NODE, "codex")
            calls = []
            def queue(argv, cwd, timeout):
                import subprocess
                calls.append({"argv": list(argv), "cwd": str(cwd), "timeout": timeout})
                return subprocess.CompletedProcess(argv, 0, "", "")
            adapter = CodexQueueWakeAdapter(coordinate, runner=queue)
            DaemonConsentLedger(root).consent(coordinate, adapter_version="1",
                adapter_digest=adapter_contract_digest("codex"), min_poll_seconds=1,
                max_poll_seconds=4, max_backoff_seconds=8, activation_epoch=1,
                idempotency_key="issue15-probe-consent")
            daemon = WakeDaemon(coordinate, adapter)
            idle = [daemon.run_cycle(100.0), daemon.run_cycle(110.0)]
            before_mail = len(calls)
            EventLog(root).send("sender", NODE, "floati", "a" * 40, "README.md", "scratch issue15 mail",
                                idempotency_key="issue15-mail")
            mail = daemon.run_cycle(120.0)
            receipt["daemon"] = {"adapter_kind": "fake_queue_adapter", "idle_cycles": idle,
                "calls_before_mail": before_mail, "adapter_calls": calls, "mail_cycle": mail,
                "wake_receipts": _rows(root, f"receipts/wakes/{NODE}.jsonl", "wake_attempt_receipt")}
    receipt["scratch_removed"] = not scratch.exists()
    return receipt


def _native(trace, receipt):
    """Reduce timestamped evidence; missing, duplicated, or foreign evidence is unknown."""
    if not isinstance(trace, dict) or trace.get("source") != "instrumented_codex_stop_and_native_turns":
        return None
    events = trace.get("events")
    if not isinstance(events, list) or len(events) < 2:
        return None
    try:
        configuration = receipt.get("scratch_configuration")
        if configuration is None and ("scratch_configuration" in trace or
                                      any("scratch_configuration" in row for row in events)):
            return None
        if configuration is not None:
            if (_configuration(**configuration) != configuration or trace.get("scratch_configuration") != configuration
                    or any(row.get("scratch_configuration") != configuration for row in events)):
                return None
        sessions = {row["session_id"] for row in events}
        if len(sessions) != 1 or not all(isinstance(s, str) and s for s in sessions):
            return None
        times = [datetime.fromisoformat(row["at"].replace("Z", "+00:00")) for row in events]
        if any(value.tzinfo is None for value in times) or times != sorted(times):
            return None
        if any(row["scratch_root"] != receipt["root"] or row["scratch_home"] != receipt["home"] for row in events):
            return None
        if events[0]["type"] != "observation_started" or events[-1]["type"] != "observation_ended":
            return None
        seconds = (times[-1] - times[0]).total_seconds()
        if seconds <= 0:
            return None
        observer = trace.get("observer_window")
        if observer is not None:
            elapsed = observer.get("elapsed_monotonic_seconds")
            # observe samples monotonic then wall at startup, wall then monotonic
            # at shutdown. Budget 100 ms for endpoint sampling/scheduling and
            # 100 ppm for rate divergence over the window, capped at 1 s: at
            # least 300x below a 300 s scratch Stop. These are bounded acceptance
            # allowances, not a measured clock guarantee; larger skew is unknown.
            clock_tolerance = min(1.0, 0.1 + seconds * 1e-4)
            if (observer.get("source") != "owned_native_child_observer" or
                    type(elapsed) not in (int, float) or elapsed <= 0 or abs(elapsed - seconds) > clock_tolerance):
                return None
        turns = [row["turn_id"] for row in events if row["type"] == "model_turn_started"]
        stops = [row["invocation_id"] for row in events if row["type"] == "stop_started"]
        if any(not isinstance(value, str) or not value for value in turns + stops):
            return None
        if len(turns) != len(set(turns)) or len(stops) != len(set(stops)):
            return None
        rearm = "unknown"
        for i, row in enumerate(events):
            if row["type"] != "stop_exhausted":
                continue
            if row["invocation_id"] not in ({event.get("invocation_id") for event in events[:i] if event["type"] == "stop_started"} |
                                            set(trace.get("context_stop_invocations", []))):
                return None
            for j in range(i + 1, len(events)):
                if events[j]["type"] == "stop_started":
                    if any(event["type"] == "model_turn_started" for event in events[i + 1:j]):
                        rearm = "observed_with_intervening_turn"
                    else:
                        rearm = "observed_without_intervening_turn"
                    break
        production_configuration = _configuration()
        return {"basis": "measured" if seconds >= 86400 and
                    (configuration is None or configuration == production_configuration) else "derived",
                "value": len(turns) * 86400 / seconds,
                "observed_turns": len(turns), "observed_seconds": seconds}, rearm
    except (ValueError, TypeError, KeyError):
        return None


def classify_receipt(receipt):
    defaults = receipt.get("defaults", {})
    window = defaults.get("wait_deadline_seconds")
    windows, rows = receipt.get("stop_windows", []), receipt.get("exhaustion_receipts", [])
    proved = (window == 86300 and defaults.get("hook_timeout_seconds") == 86400 and
        len(windows) == len(rows) == 2 and len({row.get("id") for row in rows}) == 2 and
        all(row.get("waited_seconds") == window for row in rows) and
        all(row.get("elapsed_clock_seconds") == window and row.get("exit_code") == 0 and
            row.get("decision", {}).get("decision") == "block" and
            "end this turn to re-arm" in row.get("decision", {}).get("reason", "") for row in windows))
    daemon = receipt.get("daemon", {})
    daemon_proved = (len(daemon.get("idle_cycles", [])) >= 2 and
        all(row.get("state") == "idle" for row in daemon.get("idle_cycles", [])) and
        daemon.get("calls_before_mail") == 0 and len(daemon.get("adapter_calls", [])) == 1 and
        daemon.get("mail_cycle", {}).get("state") == "queued" and
        [row.get("outcome") for row in daemon.get("wake_receipts", [])] == ["queued"])
    native = _native(receipt.get("native_trace"), receipt)
    return {"product_rearm": "new_stop_invocation_has_a_fresh_window" if proved else "cannot-measure:product_rearm_unproved",
        "daemon": "no_request_without_mail_queued_for_mail" if daemon_proved else "cannot-measure:daemon_unproved",
        "derived_stop_windows_per_day": {"basis": "derived", "value": 86400 / window if type(window) is int and window > 0 else None,
                                         "formula": f"86400 / {window}; assumes immediate invocation renewal"},
        "model_turns_per_day": native[0] if native else {"basis": "unknown", "value": None},
        "native_rearm_needs_turn": native[1] if native else "unknown",
        "native_rearm_needs_turn_basis": "measured" if native and native[1] != "unknown" else "unknown",
        "native_rearm_configuration": receipt.get("scratch_configuration", _configuration()) if native else None,
        "native_measurement_reason": None if native else "native_trace_source_unavailable"}


def _new_json(path, value):
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def _bank(path):
    path = Path(path)
    temporary_roots = {Path(tempfile.gettempdir()).resolve(), Path("\x2fprivate/tmp").resolve()}
    if not path.is_absolute() or path.resolve() != path or not any(root in path.parents for root in temporary_roots):
        raise ValueError("bank must be a canonical strict descendant of the private temporary directory")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("bank cannot contain symlinks")
    return path


def _metadata(bank):
    bank = _bank(bank)
    result = json.loads((bank / "bank.json").read_text())
    expected = {"bank": bank, "home": bank / "home", "root": bank / "fleet", "workspace": bank / "workspace"}
    if any(result.get(key) != str(value) or value.resolve() != value for key, value in expected.items()):
        raise ValueError("bank metadata must name only its exact owned scratch children")
    if "scratch_configuration" in result:
        configuration = _configuration(**result["scratch_configuration"])
        root = FloatiRoot.open_direct_home(Path(result["root"]))
        participant = resolve_participant(root.path, Path(result["workspace"]))
        if participant is None:
            raise ValueError("bank scratch waiter binding is absent")
        consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
        hooks = json.loads((Path(result["home"]) / ".codex/hooks.json").read_text())
        if (any(consent.get(key) != value for key, value in configuration.items())
                or hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"] != configuration["hook_timeout_seconds"]):
            raise ValueError("bank configuration must match its actual scratch waiter consent and Stop hook")
    return result


def prepare(bank, *, hook_timeout_seconds=DEFAULT_HOOK_TIMEOUT_SECONDS,
            wait_deadline_seconds=DEFAULT_WAIT_DEADLINE_SECONDS):
    configuration = _configuration(hook_timeout_seconds, wait_deadline_seconds)
    bank = _bank(bank)
    bank.mkdir(mode=0o700)
    home, workspace = bank / "home", bank / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    root = FloatiRoot.open_direct_home(bank / "fleet", create=True)
    Registry(root).register(NODE, "Codex")
    prepare_waiter(root, NODE, workspace, **configuration)
    codex = home / ".codex"
    codex.mkdir(mode=0o700)
    command = shlex.join(["/usr/bin/python3", "-B", str(SCRIPT), "hook", "--bank", str(bank)])
    _new_json(codex / "hooks.json", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command, "timeout": hook_timeout_seconds}]}]}})
    metadata = {"artifact_version": 0, "bank": str(bank), "home": str(home), "root": str(root.path),
                "workspace": str(workspace), "created_at": stamp(), "hook_trace": str(bank / "stop-trace.jsonl"),
                "scratch_configuration": configuration}
    _new_json(bank / "bank.json", metadata)
    return dict(metadata, owner_step="Open native Codex in this scratch HOME/workspace, authenticate only there and explicitly re-trust the prepared Stop hook. Keep the seat idle for the chosen observation interval; export its native rollout JSONL inside this scratch HOME. No native launch or trust was performed.")


def hook(bank):
    bank = _bank(bank)
    invocation = "issue15-stop-" + uuid7_hex()
    diagnostics = bank / "hook-stderr"
    diagnostics.mkdir(mode=0o700, exist_ok=True)
    if diagnostics.is_symlink():
        raise ValueError("hook diagnostics cannot cross a symlink")
    descriptor = os.open(diagnostics / (invocation + ".log"),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    terminal = sys.stderr
    with os.fdopen(descriptor, "w", encoding="utf-8") as retained:
        class Stderr:
            def write(self, value):
                # Persist first, including partial writes before a crash.
                retained.write(value)
                retained.flush()
                return terminal.write(value)

            def flush(self):
                retained.flush()
                terminal.flush()

        with redirect_stderr(Stderr()):
            try:
                return _hook(bank, invocation)
            except BaseException:
                traceback.print_exc()
                raise


def _hook(bank, invocation):
    metadata = _metadata(bank)
    payload = json.load(sys.stdin)
    if (not isinstance(payload, dict) or os.environ.get("HOME") != metadata["home"]
            or payload.get("cwd") != metadata["workspace"]):
        raise ValueError("Stop observer requires the exact prepared scratch HOME and workspace")
    session = validate_session_id(payload.get("session_id"))
    # Codex 0.164.0 starts command hooks in a new session. Group equality
    # rejects a genuine Stop; scratch identity and a held lease bind cleanup.
    # Consume the documented fields and leave future native fields alone.
    def event(kind):
        row = {"type": kind, "at": stamp(), "invocation_id": invocation,
               "session_id": session, "scratch_root": metadata["root"], "scratch_home": metadata["home"],
               "scratch_configuration": metadata.get("scratch_configuration", _configuration())}
        with (bank / "stop-trace.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(row) + "\n")
    leases = bank / "hook-leases"
    leases.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(leases / invocation, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w+") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        lease.write(json.dumps({"session_id": session}))
        lease.flush()
        if (bank / "observer-stopping").exists():
            sys.stdout.write("{}\n")
            return 0
        event("stop_started")
        output = io.StringIO()
        result = run_stop_waiter(bus_home=Path(metadata["root"]), hook_payload=payload,
                                 stdout=output, stderr=sys.stderr)
        sys.stdout.write(output.getvalue())
        try:
            decision = json.loads(output.getvalue())
        except ValueError:
            decision = {}
        if result == 0 and decision.get("decision") == "block" and "end this turn to re-arm" in decision.get("reason", ""):
            event("stop_exhausted")
        return result


def settle_native_hooks(metadata, *, timeout=5.0):
    """Release this bank's parked hooks through scratch-only wake pause.

    Native hooks have their own groups, so native-group quiescence alone says
    nothing about them. A released kernel lease proves each waiter returned.
    No PID discovery or signaling is needed.
    """
    bank = _bank(Path(metadata["bank"]))
    (bank / "observer-stopping").touch(mode=0o600, exist_ok=True)
    controller = WakeController(FloatiRoot.open_direct_home(Path(metadata["root"])))
    paused = set()
    deadline = time.monotonic() + timeout
    try:
        while True:
            active = False
            for path in (bank / "hook-leases").glob("issue15-stop-*"):
                with path.open("r") as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        active = True
                        try:
                            session = validate_session_id(json.load(lease).get("session_id"))
                        except ValueError:
                            continue  # Registration is still being written.
                        if session not in paused:
                            if controller.status(NODE, session)["state"] != "paused":
                                controller.pause(NODE, session,
                                    idempotency_key="issue15-cleanup-" + hashlib.sha256(session.encode()).hexdigest())
                            paused.add(session)
            if not active:
                return {"owned_hooks_quiescent": True}
            if time.monotonic() >= deadline:
                return {"owned_hooks_quiescent": False}
            time.sleep(0.01)
    except (OSError, ValueError, FloatiError):
        return {"owned_hooks_quiescent": False}


def read_native_trace(bank, native_turn_log, *, observation=None):
    """Read one explicit Codex export; never search HOME or discover sessions."""
    bank = _bank(bank)
    metadata = _metadata(bank)
    native_turn_log = Path(native_turn_log)
    if (not native_turn_log.is_absolute() or native_turn_log.resolve() != native_turn_log or
            Path(metadata["home"]) not in native_turn_log.parents or native_turn_log.is_symlink()):
        raise ValueError("native export must be an ordinary explicit file inside the prepared scratch HOME")
    encoded_stops = (bank / "stop-trace.jsonl").read_bytes()
    stops = [json.loads(line) for line in encoded_stops.splitlines()]
    encoded_native = native_turn_log.read_bytes()
    native = []
    lines = encoded_native.splitlines(keepends=True)
    for index, line in enumerate(lines):
        try:
            native.append(json.loads(line))
        except ValueError:
            return None  # An unfinished native tail might contain another turn.
    sessions = {row.get("payload", {}).get("id") for row in native if row.get("type") == "session_meta"}
    if (len(sessions) != 1 or not stops or {row.get("session_id") for row in stops} != sessions or
            any(row.get("scratch_root") != metadata["root"] or row.get("scratch_home") != metadata["home"] for row in stops)):
        return None
    session = next(iter(sessions))
    configuration = metadata.get("scratch_configuration", _configuration())
    # Older default-bank exports predate configuration testimony. Shortened
    # arms require the producer to name it; no unchecked label can shorten one.
    if any(row.get("scratch_configuration", _configuration()) != configuration for row in stops):
        return None
    origin = stops[0]["at"] if observation is None else observation["started_at"]
    try:
        origin_at = datetime.fromisoformat(origin.replace("Z", "+00:00"))
        stamps = [datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                  for row in native if isinstance(row.get("timestamp"), str)]
        if not stamps or any(value.tzinfo is None for value in stamps):
            return None
        end_at = max(stamps) if observation is None else datetime.fromisoformat(observation["ended_at"])
        if end_at <= origin_at:
            return None
    except (ValueError, TypeError):
        return None
    # A delayed export does not extend the observed interval. Its final native
    # timestamp bounds coverage; capture wall time can never manufacture idle.
    events = [{"type": "observation_started", "at": origin}]
    # Context predating the bounded observer is preserved separately; it is
    # never counted as an invocation starting during the observed interval.
    context_stops = []
    for row in stops:
        at = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
        if origin_at <= at <= end_at:
            events.append(dict(row))
        elif at < origin_at and row["type"] == "stop_started":
            context_stops.append(row["invocation_id"])
    recognized_turns = 0
    for row in native:
        payload = row.get("payload", {})
        if row.get("type") == "event_msg" and payload.get("type") == "task_started":
            if not payload.get("turn_id") or not row.get("timestamp"):
                return None
            recognized_turns += 1
            at = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if origin_at <= at <= end_at:
                events.append({"type": "model_turn_started", "at": row["timestamp"], "turn_id": payload["turn_id"]})
    if not recognized_turns:
        return None  # Unsupported native turn schema is not measured zero.
    events.append({"type": "observation_ended", "at": end_at.isoformat()})
    for row in events:
        if row["type"] in {"observation_started", "observation_ended", "model_turn_started"}:
            row.update(session_id=session, scratch_root=metadata["root"], scratch_home=metadata["home"],
                       scratch_configuration=configuration)
        elif "scratch_configuration" not in row:
            row["scratch_configuration"] = configuration
    events.sort(key=lambda row: datetime.fromisoformat(row["at"].replace("Z", "+00:00")))
    return {"source": "instrumented_codex_stop_and_native_turns", "events": events,
            "scratch_configuration": configuration,
            "context_stop_invocations": context_stops, "observer_window": observation,
            "sources": [{"path": str(path), "sha256": hashlib.sha256(encoded).hexdigest()}
                        for path, encoded in ((native_turn_log, encoded_native), (bank / "stop-trace.jsonl", encoded_stops))]}


def capture(output, *, bank=None, native_turn_log=None, fixture=None):
    output = Path(output)
    human = output.with_suffix(".md")
    destinations = [output, human] + ([Path(fixture)] if fixture else [])
    if len(set(destinations)) != len(destinations) or any(path.exists() for path in destinations):
        raise FileExistsError("receipt/fixture already exists; refusing overwrite")
    receipt = collect_product_probe()
    if bank is not None and native_turn_log is not None:
        metadata = _metadata(bank)
        receipt["probe_scratch"] = {key: receipt[key] for key in ("scratch", "home", "root", "workspace", "scratch_removed")}
        receipt.update({key: metadata[key] for key in ("home", "root", "workspace")})
        receipt["scratch_configuration"] = metadata.get("scratch_configuration", _configuration())
        receipt["native_trace"] = read_native_trace(bank, native_turn_log)
        receipt["native_bank_retained"] = str(bank)
    receipt["classification"] = classify_receipt(receipt)
    _new_json(output, receipt)
    if fixture:
        _new_json(fixture, receipt)
    with human.open("x", encoding="utf-8") as report:
        report.write("# Codex issue 15 observation\n\n")
        report.write("The product probe uses accelerated clocks and a fake queue. Queue acceptance does not prove a model turn.\n\n")
        report.write("```json\n" + json.dumps(receipt["classification"], indent=2) + "\n```\n\n")
        report.write("Owner observations require the prepared scratch HOME, native Stop re-trust, and an explicit supported native rollout export. A short interval yields a derived daily rate. An observed intervening turn establishes sequence, not that such a turn is logically necessary. Native banks remain available for owner inspection.\n")
    return receipt


def _launch_codex(metadata, environment):
    """Attached terminal, established argv only; the owner handles native trust/login."""
    if not sys.stdin.isatty():
        raise OSError("owner observer requires an attached interactive terminal")
    terminal = sys.stdin.fileno()
    previous_foreground = os.tcgetpgrp(terminal)
    # A separate foreground group preserves /dev/tty for native authentication
    # while keeping the child's Stop descendants out of our observer's group.
    process = subprocess.Popen([str(CODEX_EXECUTABLE), "--cd", metadata["workspace"]],
        cwd=metadata["workspace"], env=environment, preexec_fn=os.setpgrp)
    process._issue15_terminal = terminal
    process._issue15_previous_foreground = previous_foreground
    try:
        if os.getpgid(process.pid) != process.pid or process.pid == os.getpgrp():
            raise OSError("native child did not establish its exact owned process group")
        process._issue15_owned_group = process.pid
        _new_json(Path(metadata["bank"]) / "owner-process-group.json", {"process_group": process.pid})
        _foreground(terminal, process.pid)
        os.killpg(process.pid, signal.SIGCONT)
        return process
    except BaseException:
        _cleanup_native_child(process)
        raise


def _foreground(terminal, group):
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(terminal, group)
    finally:
        signal.signal(signal.SIGTTOU, previous)


def _cleanup_native_child(process):
    """Quiesce the saved owned group even after its leader has already exited."""
    owned_group = vars(process).get("_issue15_owned_group")
    group_owned = type(owned_group) is int and owned_group == process.pid and owned_group != os.getpgrp()
    try:
        if group_owned:
            WorkerRunner._terminate_process_group(owned_group)
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if group_owned:
                WorkerRunner._terminate_process_group(owned_group)
            else:
                process.kill()
            process.wait(timeout=1)
        deadline = time.monotonic() + 1
        while group_owned and WorkerRunner._process_group_alive(owned_group) and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"owned_child_reaped": process.poll() is not None,
                "owned_process_group": owned_group if group_owned else None,
                "owned_group_quiescent": group_owned and not WorkerRunner._process_group_alive(owned_group)}
    finally:
        terminal = vars(process).get("_issue15_terminal")
        if terminal is not None:
            _foreground(terminal, process._issue15_previous_foreground)


def _native_log(bank, session):
    """Inspect only this newly owned scratch Codex session directory."""
    home = Path(_metadata(bank)["home"])
    session_root = home / ".codex/sessions"
    if not session_root.exists():
        return None
    if session_root.resolve() != session_root:
        raise ValueError("scratch session root cannot be symlinked")
    found = []
    for path in session_root.rglob("*.jsonl"):
        if path.resolve() != path:
            raise ValueError("scratch native log cannot cross a symlink")
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # A writer's unfinished tail is not evidence.
                if row.get("type") == "session_meta" and row.get("payload", {}).get("id") == session:
                    found.append(path)
                    break
    return found[0] if len(found) == 1 else None


def observe(bank, output, *, fixture=None, duration_seconds=86400, mode="stop",
            startup_seconds=600, hook_timeout_seconds=DEFAULT_HOOK_TIMEOUT_SECONDS,
            wait_deadline_seconds=DEFAULT_WAIT_DEADLINE_SECONDS,
            clock=time.monotonic, sleep=time.sleep, wall_time=time.time):
    """Own one attached native child and bounded synchronous scratch observation.

    Native authentication and re-trust happen in the owner's terminal. No
    credentials are copied and no supervisor service is installed or started.
    Both modes use explicit scratch Stop budgets; `daemon` additionally cycles
    the landed daemon against only this child's trusted scratch binding.
    """
    if mode not in {"stop", "daemon"} or type(duration_seconds) is not int or not 1 <= duration_seconds <= 172800:
        raise ValueError("mode must be stop/daemon and duration must be 1..172800 whole seconds")
    bank, output = _bank(bank), Path(output)
    destinations = [output, output.with_suffix(".md")] + ([Path(fixture)] if fixture else [])
    if len(set(destinations)) != len(destinations) or any(path.exists() for path in destinations):
        raise FileExistsError("receipt/fixture already exists; refusing overwrite")
    metadata = prepare(bank, hook_timeout_seconds=hook_timeout_seconds, wait_deadline_seconds=wait_deadline_seconds)
    (bank / "tmp").mkdir(mode=0o700)
    # An allowlist prevents provider keys, proxies, auth aliases, XDG paths,
    # and arbitrary configuration redirectors from piercing scratch identity.
    environment = {"HOME": metadata["home"], "CODEX_HOME": str(Path(metadata["home"]) / ".codex"),
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": str(bank / "tmp"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"), "TERM": os.environ.get("TERM", "xterm-256color")}
    result = {"mode": mode, "bank": str(bank), "session_id": None, "cycles": [],
              "queue_receipts": [], "state": "cannot-measure", "reason": "native_trace_source_unavailable",
              "native_bank_retained": True, "child_launched": False, "owned_child_reaped": False}
    process, trace = None, None
    print("Owner sitting: authenticate in the scratch Codex HOME, explicitly trust its prepared Stop hook, then ask it to finish and idle. Observation starts at trusted Stop participation.", file=sys.stderr, flush=True)
    try:
        with mock.patch.dict(os.environ, environment, clear=True):
            process = _launch_codex(metadata, environment)
            result["child_launched"] = True
            setup_start = clock()
            root = FloatiRoot.open_direct_home(Path(metadata["root"]))
            coordinate = DaemonCoordinate(root, NODE, "codex")
            binding = None
            while clock() - setup_start < startup_seconds:
                if process.poll() is not None:
                    result["reason"] = "native_child_exited_before_trusted_stop"
                    break
                try:
                    binding = AdapterBindingStore(root).read(coordinate)
                except Exception as exc:
                    if getattr(exc, "code", None) != "wake_daemon_binding_absent":
                        raise
                if binding is not None and (bank / "stop-trace.jsonl").exists():
                    break
                sleep(min(1.0, max(0.0, startup_seconds - (clock() - setup_start))))
            if binding is None:
                if result["reason"] == "native_trace_source_unavailable":
                    result["reason"] = "native_trusted_stop_not_observed_before_startup_deadline"
            else:
                result["session_id"] = binding["session_id"]
                adapter = CodexQueueWakeAdapter(coordinate)
                daemon = None
                if mode == "daemon":
                    DaemonConsentLedger(root).consent(coordinate, adapter_version=binding["adapter_version"],
                        adapter_digest=binding["adapter_digest"], min_poll_seconds=1,
                        max_poll_seconds=60, max_backoff_seconds=60, activation_epoch=1,
                        idempotency_key="issue15-owner-observer")
                    daemon = WakeDaemon(coordinate, adapter)
                started, wall_started = clock(), wall_time()
                observation = {"started_at": datetime.fromtimestamp(wall_started, timezone.utc).isoformat(),
                               "source": "owned_native_child_observer"}
                complete = True
                while clock() - started < duration_seconds:
                    if process.poll() is not None:
                        result["reason"], complete = "native_child_exited_during_observation", False
                        break
                    if AdapterBindingStore(root).read(coordinate)["session_id"] != result["session_id"]:
                        result["reason"], complete = "native_session_changed_during_observation", False
                        break
                    remaining = duration_seconds - (clock() - started)
                    delay = min(60.0, remaining)
                    if daemon is not None:
                        now = wall_started + clock() - started
                        cycle = daemon.run_cycle(now)
                        if cycle["state"] != "backpressure":
                            result["cycles"].append({"at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                                "session_id": result["session_id"], "artifact": cycle})
                        next_at = cycle.get("next_poll_at", now + 1)
                        delay = min(delay, max(0.05, float(next_at) - now))
                    sleep(max(0.001, delay))
                # A child exit or takeover during the final sleep is not a
                # fully observed interval. Recheck both at the actual endpoint.
                if process.poll() is not None:
                    result["reason"], complete = "native_child_exited_during_observation", False
                elif AdapterBindingStore(root).read(coordinate)["session_id"] != result["session_id"]:
                    result["reason"], complete = "native_session_changed_during_observation", False
                observation.update(ended_at=datetime.fromtimestamp(wall_time(), timezone.utc).isoformat(),
                                   elapsed_monotonic_seconds=clock() - started)
                result["observation_window"] = observation
                native_log = _native_log(bank, result["session_id"])
                if complete and native_log is not None:
                    trace = read_native_trace(bank, native_log, observation=observation)
                    if trace is not None and _native(trace, metadata) is not None:
                        result["state"], result["reason"] = "observed", None
                if daemon is not None:
                    result["queue_receipts"] = _rows(root, f"receipts/wakes/{NODE}.jsonl", "wake_attempt_receipt")
    except KeyboardInterrupt:
        result["reason"] = "owner_interrupted_observation"
    except (OSError, ValueError, FloatiError) as exc:
        result["reason"] = "native_observer_unavailable"
        result["detail"] = type(exc).__name__
    finally:
        if process is not None:
            try:
                result.update(settle_native_hooks(metadata))
            finally:
                result.update(_cleanup_native_child(process))
            result["child_exit_code"] = process.returncode
            if not result["owned_group_quiescent"] or not result["owned_hooks_quiescent"]:
                result["state"], result["reason"], trace = "cannot-measure", "native_owned_group_cleanup_unproved", None
    receipt = collect_product_probe()
    receipt["probe_scratch"] = {key: receipt[key] for key in ("scratch", "home", "root", "workspace", "scratch_removed")}
    receipt.update({key: metadata[key] for key in ("home", "root", "workspace")})
    receipt["scratch_configuration"] = metadata["scratch_configuration"]
    receipt.update(scratch=str(bank), scratch_removed=False, native_observer=result, native_trace=trace)
    receipt["classification"] = classify_receipt(receipt)
    _new_json(output, receipt)
    if fixture:
        _new_json(fixture, receipt)
    with output.with_suffix(".md").open("x", encoding="utf-8") as report:
        report.write("# Codex issue 15 owner observation\n\nMode: " + mode + ". The native child was owned and reaped; the scratch bank is retained. Queue acceptance is separate from model execution.\n\n```json\n" +
            json.dumps({"classification": receipt["classification"], "native_observer": result}, indent=2) + "\n```\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "hook", "capture", "classify", "observe"))
    parser.add_argument("--bank", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--native-turn-log", type=Path)
    parser.add_argument("--duration-seconds", type=int, default=86400)
    parser.add_argument("--startup-seconds", type=int, default=600)
    parser.add_argument("--hook-timeout-seconds", type=int, default=DEFAULT_HOOK_TIMEOUT_SECONDS)
    parser.add_argument("--wait-deadline-seconds", type=int, default=DEFAULT_WAIT_DEADLINE_SECONDS)
    parser.add_argument("--mode", choices=("stop", "daemon"), default="stop")
    args = parser.parse_args()
    if args.phase in {"prepare", "hook", "observe"} and args.bank is None:
        parser.error("--bank is required")
    if args.phase == "prepare":
        result = prepare(args.bank, hook_timeout_seconds=args.hook_timeout_seconds,
                         wait_deadline_seconds=args.wait_deadline_seconds)
    elif args.phase == "hook":
        return hook(args.bank)
    else:
        if args.output is None:
            parser.error("--output is required")
        if args.phase == "observe":
            observation = observe(args.bank, args.output, fixture=args.fixture,
                duration_seconds=args.duration_seconds, startup_seconds=args.startup_seconds,
                hook_timeout_seconds=args.hook_timeout_seconds, wait_deadline_seconds=args.wait_deadline_seconds,
                mode=args.mode)
            print(json.dumps({"status": "ok" if observation["native_observer"]["state"] == "observed" else "cannot-measure",
                              "classification": observation["classification"], "native_observer": observation["native_observer"]}, indent=2))
            return 0 if observation["native_observer"]["state"] == "observed" else 20
        elif args.phase == "classify":
            result = classify_receipt(json.loads(args.output.read_text()))
        else:
            if bool(args.bank) != bool(args.native_turn_log):
                parser.error("native capture requires both --bank and --native-turn-log")
            result = capture(args.output, bank=args.bank, native_turn_log=args.native_turn_log, fixture=args.fixture)["classification"]
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
