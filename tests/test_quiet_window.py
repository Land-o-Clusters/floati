"""NET-QUIET-1: a fleet-wide quiet window is a product verb.

The row (FLOATI-NETWORK-2026-10-16 §3): *a fleet-wide quiet window settles
load, holds every seat, releases itself on a touch or a cap, and writes a
per-seat exposure receipt (what ran inside the window, with CPU deltas).*

The properties below are the lessons the hand-run windows paid for:

- a window settles BEFORE it holds: it waits for the 15-minute load to fall
  under a bound and gives up without holding anything if it never does;
- it pauses only seats whose process tree is building or testing, never a
  seat that is only writing, never the operator's own seat, an exempt seat or
  the window's own process tree;
- every hold is released by the window's own exit path (a touch, the cap, a
  signal) and the window resumes only what it paused itself; a crashed window
  is visible and its recorded pauses can still be released;
- CPU is the delta of cumulative CPU time over the timed interval, never a
  sampled percentage, and a delta the window could not observe whole says it
  is a lower bound;
- Floati holds only its own enrolled seats and tells everyone else: the
  window's state is a pull read (CLI ``--json`` and the MCP read surface) any
  agent can make without a hook, and work outside every enrolled seat is
  listed as observed and not held, never silently dropped;
- the hold carries the command patterns it covers, so a reader can ask what
  it means for the command it is about to run;
- measured memory pressure (Linux PSI, macOS memory pressure level) can set
  the hold at an operator-declared threshold, and the hold releases itself,
  receipted, when the pressure falls. The pressure source is an interface;
  these tests drive it from a fixture source.

No new module existed for this: tests/test_holds.py covers project hold
records, which pause nothing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ACTOR = "architect-a"
BUSY = "while True:\n    pass\n"


def _quiet_api():
    """Import the verb's API inside the test, so an absent verb is a failure."""
    try:
        from floati import quiet_window
    except ImportError as exc:  # pragma: no cover - the RED state
        raise AssertionError("no quiet window verb exists: " + str(exc)) from None
    return quiet_window


def _host_bound(base: float) -> float:
    """A wait bound scaled by this host's own load, never a fixed wall clock."""
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    return base * max(1.0, load / max(1, os.cpu_count() or 1))


def _wait_until(predicate, base: float, what: str):
    bound, started = _host_bound(base), time.monotonic()
    while True:
        value = predicate()
        if value:
            return value
        elapsed = time.monotonic() - started
        if elapsed > bound:
            try:
                load = os.getloadavg()
            except OSError:
                load = None
            raise AssertionError("%s: not seen within bound=%.2fs elapsed=%.2fs load=%s"
                                 % (what, bound, elapsed, load))
        time.sleep(0.01)


def _process_state(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            text = Path("/proc/%d/stat" % pid).read_text()
        except OSError:
            return None
        return text.rpartition(") ")[2].split()[0]
    result = subprocess.run(["/bin/ps", "-o", "state=", "-p", str(pid)],
                            capture_output=True, text=True)
    return result.stdout.strip()[:1] or None


def _process_cpu_ticks(pid: int) -> float | None:
    if sys.platform.startswith("linux"):
        try:
            cells = Path("/proc/%d/stat" % pid).read_text().rpartition(") ")[2].split()
        except OSError:
            return None
        return float(int(cells[11]) + int(cells[12]))
    return None


class _Fleet(unittest.TestCase):
    """One fleet root: an operator seat, two mapped seats and one unmapped seat."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        self.builder, self.writer, self.unmapped, self.exempt = (
            public_ids.builder("a"), public_ids.builder("b"),
            public_ids.builder("c"), public_ids.builder("d"))
        for node in (ACTOR, self.builder, self.writer, self.unmapped, self.exempt):
            registry.register(node, "codex")
        from floati.codex_wait_contract import write_workspace_map
        self.workspaces = {}
        for node in (ACTOR, self.builder, self.writer, self.exempt):
            workspace = self.base / ("ws-" + node)
            workspace.mkdir()
            write_workspace_map(self.home, workspace, node)
            self.workspaces[node] = workspace.resolve()


class _FakeHost:
    """Injected host: a fake clock, fake load and a scripted process table.

    ``table(clock)`` returns the rows visible at that clock. Signals are
    recorded with the clock they were sent at and change the visible state.
    """

    DRIVER = 4000

    def __init__(self, module, table, loads=lambda clock: (1.0, 1.0, 1.0), pressure=None):
        self.module, self.table, self.loads = module, table, loads
        self.driver_in_table = any(row["pid"] == self.DRIVER for row in table(0.0))
        self.pressure_source = pressure
        self.clock = 0.0
        self.signals: list[tuple[float, int, int]] = []
        self.stopped: dict[int, str] = {}

    def loadavg(self):
        return self.loads(self.clock)

    def monotonic(self):
        return self.clock

    def wall(self):
        return 1_800_000_000.0 + self.clock

    def wait(self, seconds):
        self.clock += seconds
        return False

    def self_pid(self):
        return self.DRIVER

    def snapshot(self):
        rows = []
        for row in self.table(self.clock):
            token = self.stopped.get(row["pid"])
            state = "T" if token is not None and token == row["start_token"] else row.get("state", "R")
            rows.append(self.module.ProcessRow(
                pid=row["pid"], ppid=row.get("ppid", 1), state=state,
                cpu_seconds=row.get("cpu", 0.0), started_at=row.get("started_at", self.wall() - 3600),
                start_resolution=0.01, start_token=row["start_token"], cwd=row.get("cwd"),
                executable=row.get("executable", "python3"), argv=tuple(row.get("argv", ("python3",)))))
        return rows

    def pressure(self, metric):
        """The fixture pressure source: a value, or None for a typed absence."""
        return None if self.pressure_source is None else self.pressure_source(metric, self.clock)

    def identity(self, pid):
        for row in self.table(self.clock):
            if row["pid"] == pid:
                return row["start_token"]
        return "driver" if pid == self.DRIVER and not self.driver_in_table else None

    def matches(self, pid, token):
        return self.identity(pid) == token

    def signal(self, pid, expected, number):
        if not self.matches(pid, expected):
            return False
        self.signals.append((self.clock, pid, number))
        token = self.identity(pid)
        if number == signal.SIGSTOP and token is not None:
            self.stopped[pid] = token
        elif number == signal.SIGCONT:
            self.stopped.pop(pid, None)
        return True


class QuietWindowSettleTests(_Fleet):
    def test_an_exited_target_has_a_resolution_receipt_and_receives_no_continue(self):
        module = _quiet_api()
        host = _FakeHost(module, lambda clock: self._heavy(clock) if clock < 10 else [
            dict(self._heavy(clock)[0], start_token="replacement", executable="vim", argv=("vim",))])
        self._window(host).run()
        self.assertFalse(any(sig == signal.SIGCONT for _, _, sig in host.signals))
        rows, _ = module._read(self.root)
        self.assertTrue(any(row.get("phase") == "pause_resolved"
                            and row["facts"].get("outcome") == "resolved_by_exit"
                            and row["facts"]["pauses"][0]["start_token"] == "s501" for row in rows))

    def test_operator_resolution_continues_the_selected_identity_once(self):
        from unittest.mock import patch
        from floati.cli import main
        import shlex
        module = _quiet_api()
        window, host, target = self._interrupted_stop()
        status = module.quiet_status(self.root, host=host)["quiet"]
        command = shlex.split(status["ambiguous_targets"][0]["resolve_command"])
        with patch.object(module, "NativeHost", return_value=host):
            for replayed in (False, True):
                with self.subTest(replayed=replayed):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = main(command[1:])
                    self.assertEqual(code, 0, output.getvalue())
                    evidence = json.loads(output.getvalue())["evidence"]
                    self.assertEqual((evidence["outcome"], evidence["replayed"]), ("operator_resumed", replayed))
        self.assertEqual([(pid, sig) for _, pid, sig in host.signals], [(target["pid"], signal.SIGCONT)])
        finished = module.release_orphan(self.root, ACTOR, window, "finish", host=host)
        self.assertEqual(finished["state"], "released")

    def test_operator_resolution_refuses_a_replacement_and_never_continues_it(self):
        from unittest.mock import patch
        from floati.cli import main
        module = _quiet_api()
        window, host, target = self._interrupted_stop()
        pid, token = target["pid"], target["start_token"]
        target["start_token"] = "replacement"
        host.stopped[pid] = "replacement"
        output = io.StringIO()
        with patch.object(module, "NativeHost", return_value=host), contextlib.redirect_stdout(output):
            try:
                code = main(["quiet", "release", "--root", str(self.home), "--window", window,
                             "--as", ACTOR, "--idempotency-key", "resolve-target",
                             "--resolve-pid", str(pid), "--start-token", token])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 20, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["evidence"]["code"], "quiet_target_changed")
        self.assertEqual(host.signals, [])
        self.assertEqual(host.stopped[pid], "replacement")

    def _interrupted_stop(self, interrupted_signal=signal.SIGSTOP):
        """Restore the durable prefix visible when the driver submitted its stop."""
        from unittest.mock import patch
        module = _quiet_api()
        target = self._heavy(0)[0]
        token = target["start_token"]
        driver = {"pid": _FakeHost.DRIVER, "start_token": "driver"}
        live = [target, driver]
        host = _FakeHost(module, lambda clock: live)
        ledger = self.home / module.LEDGER_RELATIVE
        before_delivery = []
        original = host.signal

        def capture(*args):
            if args[-1] == interrupted_signal:
                before_delivery.append(ledger.read_bytes())
            return original(*args)

        with patch.object(host, "signal", side_effect=capture):
            window = self._window(host).run()["window_id"]
        ledger.write_bytes(before_delivery[0])
        live.remove(driver)
        host.stopped[target["pid"]] = token
        host.signals.clear()
        return window, host, target

    def test_retry_after_interrupted_continue_never_releases_a_later_stop(self):
        module = _quiet_api()
        window, host, target = self._interrupted_stop(signal.SIGCONT)
        # The previous continue took effect; an independent actor has since stopped this PID.
        recovered = module.release_orphan(self.root, ACTOR, window, "retry", host=host)
        self.assertEqual(host.signals, [])
        self.assertEqual([p["pid"] for p in recovered["ambiguous_targets"]], [target["pid"]])
        self.assertNotEqual(recovered["state"], "released")

    def test_failed_release_from_an_older_writer_can_be_recovered(self):
        from floati.framing import encode_frame
        module = _quiet_api()
        window, host, target = self._interrupted_stop()
        rows, _ = module._read(self.root)
        intent = rows[-1]
        pause = intent["facts"]["pauses"][0]
        del pause["intent_id"]
        intent["phase"] = "paused"
        rows.append(module._window_record(self.root, window, ACTOR, "released", "old-close", None,
                                          {"not_resumed_count": 1, "not_resumed": [
                                              {"pid": target["pid"], "reason": "signal_refused"}]}, host.wall()))
        (self.home / module.LEDGER_RELATIVE).write_bytes(b"".join(encode_frame(row) for row in rows))
        recovered = module.release_orphan(self.root, ACTOR, window, "old-close", host=host)
        self.assertEqual([p["pid"] for p in recovered.get("ambiguous_targets", [])], [target["pid"]])
        self.assertNotEqual(recovered["state"], "released")
        self.assertEqual(host.signals, [])

    def test_unconfirmed_stop_names_the_target_and_its_resolution_command(self):
        import shlex
        from floati.errors import ProtocolRefusal
        module = _quiet_api()
        window, host, target = self._interrupted_stop()
        status = module.quiet_status(self.root, host=host)["quiet"]
        try:
            recovered = module.release_orphan(self.root, ACTOR, window, "recover", host=host)
        except ProtocolRefusal as exc:
            recovered = getattr(exc, "refusal_detail", {})
        for evidence in (status, recovered):
            with self.subTest(evidence=evidence):
                self.assertNotEqual(evidence.get("state"), "released")
                rows = evidence.get("ambiguous_targets", [])
                self.assertEqual([row["pid"] for row in rows], [target["pid"]])
                row = rows[0]
                self.assertEqual((row["start_token"], row["executable"], row["node_id"], row["window_id"]),
                                 (target["start_token"], "python3", self.builder, window))
                command = shlex.split(row["resolve_command"])
                self.assertEqual(command[:3], ["floati", "quiet", "release"])
                args = dict(zip(command[3::2], command[4::2]))
                self.assertEqual((args["--root"], args["--as"], args["--window"], args["--resolve-pid"],
                                  args["--start-token"]), (str(self.home), ACTOR, window,
                                  str(target["pid"]), target["start_token"]))
                self.assertTrue(args["--idempotency-key"])
        self.assertEqual(host.signals, [])

    def test_each_stop_has_a_durable_intent_and_no_premature_confirmation(self):
        from unittest.mock import patch
        module = _quiet_api()
        path = self.root.resolve_relative(module.LEDGER_RELATIVE)
        durable = []
        sync = os.fsync

        def capture_sync(fd):
            sync(fd)
            if path.exists() and os.path.samestat(os.fstat(fd), path.stat()):
                durable[:] = module._read(self.root)[0]

        class Host(_FakeHost):
            def signal(inner, pid, *args):
                if args[-1] == signal.SIGSTOP:
                    phases = [row["phase"] for row in durable
                              if any(p["pid"] == pid for p in row.get("facts", {}).get("pauses", []))]
                    self.assertEqual(phases, ["pause_intent"])
                return super().signal(pid, *args)

        host = Host(module, self._heavy)
        with patch.object(os, "fsync", side_effect=capture_sync):
            self._window(host).run()
        self.assertIn("paused", [r.get("phase") for r in durable])

    def test_failed_resume_leaves_only_failed_targets_for_retry(self):
        module = _quiet_api()

        class Host(_FakeHost):
            fail = True

            def signal(inner, pid, *args):
                if pid == 501 and args[-1] == signal.SIGCONT and inner.fail:
                    raise PermissionError()
                return super().signal(pid, *args)

        host = Host(module, lambda clock: self._heavy(clock) + [
            dict(self._heavy(clock)[0], pid=502, start_token="s502"),
            {"pid": _FakeHost.DRIVER, "start_token": "driver"}])
        window = self._window(host)
        result = window.run()
        self.assertNotEqual(result["state"], "released")
        self.assertEqual(host.stopped, {501: "s501"})
        host.fail = False
        recovered = module.release_orphan(self.root, ACTOR, window.window_id, "retry", host=host)
        self.assertEqual(recovered["state"], "released")
        self.assertEqual(host.stopped, {})
        self.assertEqual([pid for _, pid, sig in host.signals if sig == signal.SIGCONT].count(502), 1)

    def test_record_capacity_refuses_a_hold_before_its_recovery_becomes_unreadable(self):
        from unittest.mock import patch
        from floati.errors import ProtocolRefusal

        module = _quiet_api()
        host = _FakeHost(module, self._heavy)
        with patch.object(module, "MAX_LEDGER_RECORDS", 2):
            with self.assertRaises(ProtocolRefusal):
                self._window(host).run()
            self.assertEqual(module.show_windows(self.root, host=host)["partial_records"], 0)
        self.assertEqual(host.signals, [])

    def test_recovery_requires_the_complete_pause_history(self):
        from unittest.mock import patch
        from floati.errors import ProtocolRefusal

        module = _quiet_api()
        host = _FakeHost(module, lambda clock: self._heavy(clock) + [
            {"pid": _FakeHost.DRIVER, "start_token": "driver"}])
        result = self._window(host).run()
        host.table = self._heavy
        with patch.object(module, "MAX_LEDGER_RECORDS", 2):
            with self.assertRaises(ProtocolRefusal) as caught:
                module.release_orphan(self.root, ACTOR, result["window_id"], "partial", host=host)
            self.assertEqual(caught.exception.code, "quiet_history_uncertain")

    def test_failed_driver_probe_cannot_release_a_live_window(self):
        from unittest.mock import patch
        from floati.errors import ProtocolRefusal

        module = _quiet_api()
        host = _FakeHost(module, lambda clock: [{"pid": _FakeHost.DRIVER, "start_token": "driver"}])
        window = self._window(host)
        wait = host.wait

        def while_open(seconds):
            observer = module.NativeHost()
            with patch.object(module.sys, "platform", "darwin"),\
                    patch.object(module, "_darwin_status", side_effect=PermissionError("probe unavailable")):
                with self.assertRaises(ProtocolRefusal):
                    module.release_orphan(self.root, ACTOR, window.window_id, "recover", host=observer)
            return wait(seconds)

        host.wait = while_open
        self.assertEqual(window.run()["cause"], "cap")

    def test_a_build_first_seen_at_release_is_never_paused(self):
        for cause in ("cap", "touch"):
            with self.subTest(cause=cause):
                release = self.base / "release"

                class ReleaseOnWait(_FakeHost):
                    def wait(inner, seconds):
                        interrupted = super().wait(seconds)
                        if cause == "touch":
                            release.touch()
                            os.utime(release, (inner.wall(), inner.wall()))
                        return interrupted

                host = ReleaseOnWait(_quiet_api(), lambda clock: self._heavy(clock) if clock >= 10 else [])
                result = self._window(host, key="boundary-" + cause, release_file=str(release),
                                      cap_seconds=10 if cause == "cap" else 30).run()
                self.assertEqual(result["cause"], cause)
                self.assertEqual(host.signals, [])

    def test_an_exempt_subtree_stays_running_even_when_a_descendant_changes_workspace(self):
        ws = self.workspaces
        for protected in (self.exempt, ACTOR):
            with self.subTest(protected=protected):
                rows = self._heavy(0) + [
                    {"pid": 502, "start_token": "child", "ppid": 501, "cwd": str(ws[protected])},
                    {"pid": 503, "start_token": "grandchild", "ppid": 502,
                     "cwd": str(ws[self.builder]), "argv": ("python3", "-m", "pytest")},
                    {"pid": 504, "start_token": "sibling", "ppid": 501, "cwd": str(ws[self.builder])},
                ]
                host = _FakeHost(_quiet_api(), lambda _: rows)
                self._window(host, key="exemption-" + protected, exempt=(self.exempt,)).run()
                self.assertEqual({pid for _, pid, sig in host.signals if sig == signal.SIGSTOP},
                                 {501, 504})

    def _window(self, host, key="quiet-key-1", **overrides):
        module = _quiet_api()
        parameters = dict(load_bound=8.0, settle_cap_seconds=30.0, cap_seconds=30.0,
                          interval_seconds=10.0, release_file=None, exempt=())
        parameters.update(overrides)
        return module.QuietWindow(self.root, ACTOR, key,
                                  module.QuietParameters(**parameters), host=host)

    def _heavy(self, clock):
        return [{"pid": 501, "start_token": "s501", "cwd": str(self.workspaces[self.builder]),
                 "argv": ("python3", "-m", "unittest")}]

    def test_a_window_settles_before_it_holds_and_gives_up_without_holding(self) -> None:
        module = _quiet_api()
        from floati.errors import ProtocolRefusal
        for case, loads, expect_open in (
            ("never_settles", lambda clock: (9.0, 9.0, 9.0), False),
            ("settles_at_twenty", lambda clock: (9.0, 9.0, 9.0) if clock < 20 else (2.0, 2.0, 7.5), True),
        ):
            with self.subTest(case=case):
                host = _FakeHost(module, self._heavy, loads)
                window = self._window(host, key="settle-" + case)
                if not expect_open:
                    with self.assertRaises(ProtocolRefusal) as caught:
                        window.run()
                    self.assertEqual(caught.exception.code, "quiet_settle_cap_reached")
                    self.assertEqual(host.signals, [], "nothing is held when load never settles")
                    shown = module.show_windows(self.root)["windows"]
                    self.assertEqual(shown[-1]["state"], "not_opened")
                    continue
                result = window.run()
                stops = [clock for clock, _pid, number in host.signals if number == signal.SIGSTOP]
                self.assertTrue(stops, "a settled window holds the building seat")
                self.assertGreaterEqual(min(stops), 20.0, "no hold before the load settled")
                self.assertEqual(result["cause"], "cap")

    def test_cpu_is_a_cumulative_delta_and_says_when_it_is_a_lower_bound(self) -> None:
        module = _quiet_api()
        workspace = str(self.workspaces[self.writer])
        opened_wall = 1_800_000_000.0

        def steady(clock):
            return [{"pid": 601, "start_token": "s601", "cwd": workspace,
                     "cpu": 10.0 + clock * 0.25, "started_at": opened_wall - 100}]

        def late_but_old(clock):
            rows = steady(clock)
            if clock >= 10:
                rows.append({"pid": 602, "start_token": "s602", "cwd": workspace,
                             "cpu": 3.0 + (clock - 10) * 0.05, "started_at": opened_wall - 50})
            return rows

        def born_inside(clock):
            rows = steady(clock)
            if clock >= 10:
                rows.append({"pid": 603, "start_token": "s603", "cwd": workspace,
                             "cpu": 3.0 + (clock - 10) * 0.05, "started_at": opened_wall + 5})
            return rows

        def exited(clock):
            rows = steady(clock)
            if clock < 20:
                rows.append({"pid": 604, "start_token": "s604", "cwd": workspace,
                             "cpu": 1.0 + clock * 0.1, "started_at": opened_wall - 100})
            return rows

        for case, table, seconds, bound in (
            ("steady", steady, 7.5, "exact"),
            ("appeared_after_open_born_before", late_but_old, 7.5 + 1.0, "lower"),
            ("born_inside_window", born_inside, 7.5 + 4.0, "exact"),
            ("exited_before_release", exited, 7.5 + 1.0, "lower"),
        ):
            with self.subTest(case=case):
                host = _FakeHost(module, table)
                result = self._window(host, key="cpu-" + case).run()
                exposure = {row["node_id"]: row for row in result["exposures"]}[self.writer]
                self.assertEqual(exposure["cpu"]["method"], "cumulative_cpu_time_delta")
                self.assertAlmostEqual(exposure["cpu"]["seconds"], seconds, places=6)
                self.assertEqual(exposure["cpu"]["bound"], bound)
                self.assertFalse(exposure["held"], "a writing-only seat is never paused")
                self.assertEqual(exposure["hold_reason"], "writing_only")
                self.assertAlmostEqual(exposure["interval"]["seconds"], 30.0, places=6)
                self.assertEqual(host.signals, [])

    def test_measured_memory_pressure_sets_the_hold_and_its_fall_releases_it(self) -> None:
        module = _quiet_api()
        from floati.errors import ProtocolRefusal

        def rising(metric, clock):
            self.assertEqual(metric, "psi_memory_some_avg10")
            return 42.0 if 20 <= clock < 50 else 3.0

        host = _FakeHost(module, self._heavy, loads=lambda clock: (99.0, 99.0, 99.0), pressure=rising)
        result = self._window(host, key="pressure-1", load_bound=None, trigger="memory_pressure",
                              pressure_metric="psi_memory_some_avg10", pressure_threshold=30.0,
                              settle_cap_seconds=600.0, cap_seconds=600.0).run()
        stops = [clock for clock, _pid, number in host.signals if number == signal.SIGSTOP]
        conts = [clock for clock, _pid, number in host.signals if number == signal.SIGCONT]
        self.assertTrue(stops and min(stops) >= 20.0, "nothing is held before pressure crosses the threshold")
        self.assertTrue(conts and min(conts) >= 50.0, "the hold lasts while pressure stays high")
        self.assertEqual(result["cause"], "pressure_fell")
        self.assertEqual(result["trigger"]["metric"], "psi_memory_some_avg10")
        self.assertEqual(result["trigger"]["set_reading"], 42.0)
        self.assertEqual(result["trigger"]["release_reading"], 3.0)
        shown = module.show_windows(self.root, result["window_id"])["window"]
        self.assertEqual((shown["state"], shown["cause"]), ("released", "pressure_fell"))
        for case, source in (("source_absent", lambda metric, clock: None),):
            with self.subTest(case=case):
                host = _FakeHost(module, self._heavy, pressure=source)
                with self.assertRaises(ProtocolRefusal) as caught:
                    self._window(host, key="pressure-" + case, load_bound=None, trigger="memory_pressure",
                                 pressure_metric="psi_memory_some_avg10", pressure_threshold=30.0).run()
                self.assertEqual(caught.exception.code, "quiet_pressure_unavailable")
                self.assertEqual(host.signals, [])

    def test_a_window_resumes_only_what_it_paused_itself(self) -> None:
        module = _quiet_api()
        ws = self.workspaces

        def table(clock):
            rows = [
                # Already stopped by someone else before the window: not ours.
                {"pid": 701, "start_token": "s701", "cwd": str(ws[self.builder]), "state": "T",
                 "argv": ("swift-build",), "executable": "swift-build"},
                # Paused by the window; its PID is reused before release.
                {"pid": 702, "start_token": "s702", "cwd": str(ws[self.builder]),
                 "argv": ("python3", "-m", "pytest")} if clock < 20 else
                {"pid": 702, "start_token": "reused", "cwd": str(ws[self.builder]), "argv": ("vim",),
                 "executable": "vim"},
                # The operator's own seat, an exempt seat and the window's own child.
                {"pid": 703, "start_token": "s703", "cwd": str(ws[ACTOR]), "argv": ("python3", "-m", "unittest")},
                {"pid": 704, "start_token": "s704", "cwd": str(ws[self.exempt]), "argv": ("cargo", "build"),
                 "executable": "cargo"},
                {"pid": 705, "start_token": "s705", "ppid": _FakeHost.DRIVER, "cwd": str(ws[self.builder]),
                 "argv": ("python3", "-m", "unittest")},
                {"pid": _FakeHost.DRIVER, "start_token": "driver", "cwd": str(ws[ACTOR]),
                 "argv": (sys.executable, "-m", "floati", "quiet", "run")},
            ]
            return rows

        host = _FakeHost(module, table)
        result = self._window(host, exempt=(self.exempt,)).run()
        signalled = {(pid, number) for _clock, pid, number in host.signals}
        self.assertEqual(signalled, {(702, signal.SIGSTOP)},
                         "only the building seat's own process is stopped, and a reused PID is never continued")
        exposures = {row["node_id"]: row for row in result["exposures"]}
        self.assertEqual(exposures[ACTOR]["hold_reason"], "actor")
        self.assertEqual(exposures[self.exempt]["hold_reason"], "exempt")
        self.assertEqual(exposures[self.unmapped]["observation"], "no_workspace")
        self.assertTrue(exposures[self.builder]["held"])
        self.assertEqual(result["resumed"], [])
        self.assertEqual([row["pid"] for row in result["not_resumed"]], [702])


class QuietWindowLiveTests(_Fleet):
    """Real processes, the real CLI and the real process table of this host."""

    def _spawn(self, node, *extra, cwd=None):
        child = subprocess.Popen([sys.executable, "-c", BUSY, *extra],
                                 cwd=self.workspaces[node] if cwd is None else cwd,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def reap():
            try:
                os.kill(child.pid, signal.SIGCONT)
            except OSError:
                pass
            child.kill()
            child.wait()
        self.addCleanup(reap)
        return child

    def test_native_identity_can_hold_and_release_its_selected_child(self):
        child = self._spawn(self.builder, "unittest")
        host = _quiet_api().NativeHost()
        row = next(row for row in host.snapshot() if row.pid == child.pid)
        self.assertEqual(row.cwd, str(self.workspaces[self.builder]))
        self.assertEqual(row.start_token, host.identity(child.pid))
        self.assertTrue(host.signal(child.pid, row.start_token, signal.SIGSTOP))
        _wait_until(lambda: _process_state(child.pid) == "T", 2.0, "the selected child paused")
        self.assertTrue(host.signal(child.pid, row.start_token, signal.SIGCONT))

    def _driver(self, release_file: Path, key: str):
        driver = subprocess.Popen(
            [sys.executable, "-m", "floati", "quiet", "run", "--root", str(self.home), "--as", ACTOR,
             "--idempotency-key", key, "--load-bound", "1000000", "--settle-cap-seconds", "0",
             "--cap-seconds", "120", "--interval-seconds", "0.05", "--release-file", str(release_file)],
            cwd=REPOSITORY_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

        def stop():
            if driver.poll() is None:
                driver.kill()
            driver.communicate()
        self.addCleanup(stop)
        return driver

    def _cli(self, *arguments):
        """The real CLI entry point, in process: the reads a seat would make."""
        from floati.cli import main
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                code = main(list(arguments))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        try:
            return code, json.loads(output.getvalue())
        except json.JSONDecodeError:
            return code, {"stdout": output.getvalue()[-2000:]}

    def _open_window(self, driver):
        def opened():
            if driver.poll() is not None:
                out, err = driver.communicate()
                raise AssertionError("the window driver exited before it opened: rc=%s %s %s"
                                     % (driver.returncode, out[-2000:], err[-2000:]))
            code, artifact = self._cli("quiet", "show", "--root", str(self.home))
            windows = artifact.get("evidence", {}).get("windows", []) if code == 0 else []
            return next((w for w in windows if w.get("state") == "open"), None)
        return _wait_until(opened, 20.0, "an open quiet window")

    def test_a_touch_releases_a_window_that_paused_only_the_building_seat(self) -> None:
        building = self._spawn(self.builder, "unittest")
        writing = self._spawn(self.writer)
        elsewhere = self.base / "not-a-seat"
        elsewhere.mkdir()
        outside = self._spawn(self.writer, "unittest", cwd=elsewhere)
        release = self.base / "release"
        code, idle = self._cli("quiet", "status", "--root", str(self.home), "--json")
        self.assertEqual((code, idle["evidence"]["quiet"]["active"]), (0, False), idle)
        driver = self._driver(release, "live-touch-1")
        self._open_window(driver)
        _wait_until(lambda: _process_state(building.pid) == "T", 20.0, "the building seat paused")
        self.assertNotEqual(_process_state(writing.pid), "T", "a writing-only seat keeps running")
        self.assertNotEqual(_process_state(outside.pid), "T", "work outside every seat is never held")
        code, told = self._cli("quiet", "status", "--root", str(self.home), "--json")
        self.assertEqual(code, 0, told)
        quiet = told["evidence"]["quiet"]
        self.assertEqual((quiet["active"], quiet["state"]), (True, "open"))
        self.assertEqual(quiet["outside_reach"], "observed_not_held")
        self.assertIn(self.builder, quiet["holdable_seats"])
        self.assertIn("unittest", quiet["covered_commands"]["interpreter_runner_words"])
        for command, waits in (("python3 -m unittest discover", True), ("cargo test", True),
                               ("git commit -m notes", False), ("vim notes.md", False)):
            with self.subTest(command=command):
                code, asked = self._cli("quiet", "status", "--root", str(self.home), "--command", command)
                self.assertEqual(code, 0, asked)
                self.assertEqual(asked["evidence"]["command"]["waits"], waits)
        before = _process_cpu_ticks(writing.pid)
        if before is not None:
            _wait_until(lambda: (_process_cpu_ticks(writing.pid) or 0) >= before + 3, 20.0,
                        "the writing seat accrued CPU inside the window")
        release.touch()
        out, err = driver.communicate(timeout=_host_bound(30.0))
        self.assertEqual(driver.returncode, 0, out[-2000:] + err[-2000:])
        artifact = json.loads(out)
        self.assertEqual(artifact["status"], "ok")
        evidence = artifact["evidence"]
        self.assertEqual(evidence["cause"], "touch")
        _wait_until(lambda: _process_state(building.pid) not in (None, "T"), 20.0,
                    "the building seat resumed by the window's own release")
        exposures = {row["node_id"]: row for row in evidence["exposures"]}
        held = exposures[self.builder]
        self.assertTrue(held["held"])
        self.assertEqual(held["hold_reason"], "building_or_testing")
        self.assertIn(building.pid, [p["pid"] for p in held["processes"] if p["paused"]])
        writer = exposures[self.writer]
        self.assertFalse(writer["held"])
        self.assertEqual(writer["cpu"]["method"], "cumulative_cpu_time_delta")
        self.assertGreater(writer["cpu"]["seconds"], 0.0)
        self.assertLessEqual(writer["cpu"]["seconds"], writer["interval"]["seconds"] + 0.1)
        self.assertEqual(exposures[self.unmapped]["observation"], "no_workspace")
        self.assertEqual(exposures[ACTOR]["hold_reason"], "actor")
        unheld = exposures[None]
        self.assertEqual((unheld["scope"], unheld["held"], unheld["hold_reason"]),
                         ("outside_reach", False, "not_enrolled"))
        listed = {p["pid"]: p for p in unheld["processes"]}
        self.assertIn(outside.pid, listed, "work outside every seat is listed, not dropped")
        self.assertTrue(listed[outside.pid]["build_or_test"])
        self.assertFalse(listed[outside.pid]["paused"])
        code, after = self._cli("quiet", "status", "--root", str(self.home), "--json")
        self.assertEqual((code, after["evidence"]["quiet"]["active"]), (0, False), after)
        code, shown = self._cli("quiet", "show", "--root", str(self.home), "--window", evidence["window_id"])
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["evidence"]["window"]["state"], "released")

    def test_a_crashed_window_is_visible_and_its_pauses_can_be_released(self) -> None:
        building = self._spawn(self.builder, "unittest")
        driver = self._driver(self.base / "release", "live-crash-1")
        window = self._open_window(driver)
        _wait_until(lambda: _process_state(building.pid) == "T", 20.0, "the building seat paused")
        _wait_until(lambda: _quiet_api().show_windows(self.root, window["window_id"])
                    ["window"]["paused_processes"] > 0, 5.0, "the stop delivery was recorded")
        driver.kill()
        driver.communicate()
        self.assertEqual(_process_state(building.pid), "T")
        code, shown = self._cli("quiet", "show", "--root", str(self.home))
        self.assertEqual(code, 0, shown)
        state = {w["window_id"]: w for w in shown["evidence"]["windows"]}[window["window_id"]]
        self.assertEqual(state["state"], "orphaned")
        code, released = self._cli("quiet", "release", "--root", str(self.home), "--as", ACTOR,
                                   "--window", window["window_id"], "--idempotency-key", "orphan-1")
        self.assertEqual(code, 0, released)
        self.assertIn(building.pid, [row["pid"] for row in released["evidence"]["resumed"]])
        _wait_until(lambda: _process_state(building.pid) not in (None, "T"), 20.0,
                    "the orphaned pause resumed")
        code, replay = self._cli("quiet", "release", "--root", str(self.home), "--as", ACTOR,
                                 "--window", window["window_id"], "--idempotency-key", "orphan-1")
        self.assertEqual((code, replay["evidence"]["window_id"]), (0, window["window_id"]))


class QuietWindowProcessTableTests(unittest.TestCase):
    def test_linux_signals_require_the_kernel_and_procfs_to_agree_on_the_target(self):
        from unittest.mock import patch
        module = _quiet_api()
        host = module.NativeHost()
        real_read = Path.read_bytes
        for scenario in ("hidden_pid", "foreign_proc_namespace"):
            with self.subTest(scenario=scenario), contextlib.ExitStack() as mocks:
                def read(path):
                    if str(path) == "/proc/self/status":
                        fields = "123 7" if scenario == "foreign_proc_namespace" else str(os.getpid())
                        return ("NSpid:\t" + fields + "\n").encode()
                    if str(path) == "/proc/722/stat":
                        if scenario == "hidden_pid":
                            raise FileNotFoundError()
                        return b"722 (member) R " + b"0 " * 18 + b"10"
                    return real_read(path)

                mocks.enter_context(patch.object(module.sys, "platform", "linux"))
                mocks.enter_context(patch.object(Path, "read_bytes", read))
                mocks.enter_context(patch.object(module, "_linux_boot_id", return_value="fixture"))
                mocks.enter_context(patch.object(module.os, "stat", return_value=os.stat_result(
                    (0, 123, 0, 0, 0, 0, 0, 0, 0, 0))))
                # A pipe models a live pidfd for the readiness probe.
                rd, wr = os.pipe()
                self.addCleanup(os.close, rd)
                self.addCleanup(os.close, wr)
                mocks.enter_context(patch.object(module.os, "pidfd_open", create=True,
                                                  side_effect=lambda pid: os.dup(rd)))
                delivery = mocks.enter_context(patch.object(module.signal, "pidfd_send_signal", create=True))
                with self.assertRaises(OSError):
                    host.signal(722, "linux:fixture:123:10", signal.SIGCONT)
                delivery.assert_not_called()

    def test_recycled_pid_after_identity_read_never_receives_the_continue(self):
        from unittest.mock import patch

        module = _quiet_api()
        host = module.NativeHost()
        occupants = {722: "linux:fixture:123:1"}
        delivered = []

        def read_identity(pid, scope):
            old = occupants[pid]
            occupants[pid] = "replacement"
            return old, "T"

        def by_pid(pid, number):
            delivered.append((occupants[pid], number))

        # The descriptor pins the original occupant before its last identity
        # read. A later numeric-PID signal would reach the replacement.
        with patch.object(module.sys, "platform", "linux"),\
                patch.object(module, "_linux_scope", return_value="linux:fixture:123"),\
                patch.object(module, "_linux_status", side_effect=read_identity),\
                patch.object(module.os, "pidfd_open", return_value=927, create=True),\
                patch.object(module.signal, "pidfd_send_signal", create=True,
                             side_effect=ProcessLookupError()),\
                patch.object(module.os, "close"), patch.object(module.os, "kill", side_effect=by_pid):
            host.signal(722, "linux:fixture:123:1", signal.SIGCONT)
        self.assertNotIn(("replacement", signal.SIGCONT), delivered)

    def test_the_window_state_is_an_mcp_read_and_holding_is_not(self) -> None:
        from floati import cli
        from floati.command_contract import project_mcp_surface
        surface = project_mcp_surface(cli._parser())
        tools = {tool["name"]: tool["_meta"]["floati"]["exposure"] for tool in surface["tools"]}
        self.assertEqual(tools.get("quiet_status"), "read")
        self.assertIn(["quiet", "run"], surface["denied_paths"])
        self.assertIn(["quiet", "release"], surface["denied_paths"])

    def test_darwin_process_table_rows_parse_cumulative_cpu_and_start(self) -> None:
        module = _quiet_api()
        for case, cell, seconds in (
            ("minutes", "0:01.25", 1.25),
            ("hours", "1:02:03.50", 3723.5),
            ("days", "2-01:00:00", 176400.0),
            ("long_minutes", "125:00.00", 7500.0),
        ):
            with self.subTest(case=case):
                self.assertAlmostEqual(module.parse_darwin_cpu_time(cell), seconds, places=6)
        for bad in ("", "1:2:3:4", "x:00", "-1:00"):
            with self.subTest(bad=bad):
                self.assertIsNone(module.parse_darwin_cpu_time(bad))

    def test_the_building_or_testing_classifier(self) -> None:
        module = _quiet_api()
        for argv, heavy in (
            (("python3", "-m", "unittest", "discover"), True),
            (("/usr/bin/python3", "-m", "pytest", "-q"), True),
            (("swift-frontend", "-frontend"), True),
            (("xcodebuild", "test"), True),
            (("cargo", "build"), True),
            (("go", "test", "./..."), True),
            (("make",), True),
            (("npm", "run", "build"), True),
            (("python3", "-c", BUSY), False),
            (("git", "commit", "-m", "x"), False),
            (("vim", "notes.md"), False),
            (("go", "fmt"), False),
            (("codex", "exec"), False),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(module.builds_or_tests(Path(argv[0]).name, argv), heavy)


if __name__ == "__main__":
    unittest.main()
