from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import time_bounds


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WATCH_REPRO_TRACE_VARIABLE = "FLOATI_WATCH_TRACE"


def watch_liveness_bound_seconds() -> float:
    """TIMEOUT-PUB-1: the child liveness backstop, derived from this host.

    CI-GREEN-24 called this a LIVENESS backstop, not a latency budget, and
    held it in a module constant; full-suite runs collected that constant
    six times in six trees in one night (a shipped test passes alone and
    fails inside the suite run, so the constant was a bet on the suite's
    own subprocess contention). The bound is DERIVED each process from
    what the suite process charges, right then, to start the same CLI
    import graph; the assumption the derivation makes is stated in
    ``tests.time_bounds.derived_watch_liveness_bound_seconds``, whose
    floor is the 30 s constant this derivation replaced.  A miss is not a
    verdict: ``await_with_one_extension`` takes a fresh measurement and
    extends exactly once before any failure, and says so in what it
    prints.  Latencies inside the bound are measured and printed, never
    asserted; only deafness fails, naming the loadavg it failed under.
    """

    return time_bounds.derived_watch_liveness_bound_seconds()


def await_with_one_extension(attempt):
    """Run one condition-wait against the derived bound; extend once on a miss.

    Am.1 (ruling 2026-09-07-a-derived-bound-that-lands-below-the-constant-
    it-replaced): prefer measuring over promising.  ``attempt(seconds) -> bool``
    must wait for the test's actual CONDITION - a streamed delta, a marker,
    an exit - and return whether it was met inside ``seconds``.  The first
    wait uses the process's derived bound; on a miss a FRESH measurement is
    taken (the domain just proved hotter than the first draw saw) and the
    condition is awaited once more.  Returns
    ``(bound, extension_seconds, met)``: ``extension_seconds`` is 0.0 when
    the bound itself sufficed, so a caller can print - and a failure can
    name - exactly how long was really granted and which wait carried it.
    """

    bound = watch_liveness_bound_seconds()
    if attempt(bound):
        return bound, 0.0, True
    fresh = time_bounds.fresh_watch_liveness_bound_seconds()
    if attempt(fresh):
        return bound, fresh, True
    return bound, fresh, False


class WatchTests(unittest.TestCase):
    def test_help_names_initial_and_changed_streaming_observations(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "floati", "watch", "--help"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("initial observation", result.stdout)
        self.assertIn("each changed observation", result.stdout)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        subprocess.run(
            [sys.executable, "-m", "floati", "init", "--root", str(self.home)],
            cwd=REPOSITORY_ROOT, check=True, capture_output=True, text=True,
        )
        self.destination = Path(self.temp.name) / "installed"
        destination_scripts = self.destination / "scripts"
        destination_scripts.mkdir(parents=True)
        (destination_scripts / "floati").write_bytes(b"installed\n")
        self.shadow = Path(self.temp.name) / "shadow"
        self.shadow.mkdir()
        (self.shadow / "floati").write_bytes(b"shadow\n")
        self.environment = {
            **os.environ,
            "PATH": os.pathsep.join((
                str(self.shadow),
                str(destination_scripts),
            )),
        }

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "floati", *args], cwd=REPOSITORY_ROOT,
            check=False, capture_output=True, text=True, env=self.environment,
        )

    def test_unchanged_poll_iterations_emit_only_the_initial_delta(self) -> None:
        result = self.run_cli(
            "watch", "--root", str(self.home), "--destination", str(self.destination),
            "--interval", "0.05", "--iterations", "3",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(1, len(lines))
        artifact = json.loads(lines[0])
        self.assertEqual("watch", artifact["command"])
        self.assertEqual("initial", artifact["evidence"]["delta"]["kind"])
        self.assertEqual(1, artifact["schema_version"])
        self.assertEqual(
            "found",
            artifact["evidence"]["delta"]["snapshot"]["installer_shadow"]["outcome"],
        )

    def test_unbounded_watch_streams_initial_and_changed_artifacts_before_exit(self) -> None:
        child = subprocess.Popen(
            [
                sys.executable, "-m", "floati", "watch", "--root", str(self.home),
                "--destination", str(self.destination), "--interval", "0.05",
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.environment,
        )
        def close_child() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

        self.addCleanup(close_child)
        self.assertIsNotNone(child.stdout)

        ready, _, _ = select.select([child.stdout], [], [], 1.0)
        self.assertTrue(ready, "initial watch artifact must stream before the unbounded loop exits")
        initial = json.loads(child.stdout.readline())
        self.assertEqual("initial", initial["evidence"]["delta"]["kind"])

        registered = self.run_cli(
            "register", "--root", str(self.home), public_ids.builder('a'), "--harness", "Codex"
        )
        self.assertEqual(0, registered.returncode, registered.stderr)
        ready, _, _ = select.select([child.stdout], [], [], 1.0)
        self.assertTrue(ready, "changed watch artifact must stream while the loop remains active")
        changed = json.loads(child.stdout.readline())
        self.assertEqual("change", changed["evidence"]["delta"]["kind"])
        self.assertIsNone(child.poll())

        child.terminate()
        child.wait(timeout=2)

    def test_interval_below_floor_refuses_with_artifact_exit(self) -> None:
        result = self.run_cli(
            "watch", "--root", str(self.home), "--destination", str(self.destination),
            "--interval", "0.01", "--iterations", "1",
        )

        self.assertEqual(20, result.returncode)
        artifact = json.loads(result.stdout)
        self.assertEqual("", result.stderr)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("watch_interval_invalid", artifact["evidence"]["code"])

    def _streaming_child(
        self,
        trace: Path | None,
        *,
        iter_ready: Path | None = None,
        hold_flush: Path | None = None,
    ) -> subprocess.Popen:
        environment = dict(self.environment)
        if trace is not None:
            environment["FLOATI_WATCH_TRACE"] = str(trace)
        else:
            environment.pop("FLOATI_WATCH_TRACE", None)
        if iter_ready is not None:
            environment["FLOATI_WATCH_ITER_READY"] = str(iter_ready)
        else:
            environment.pop("FLOATI_WATCH_ITER_READY", None)
        if hold_flush is not None:
            environment["FLOATI_WATCH_HOLD_FLUSH"] = str(hold_flush)
        else:
            environment.pop("FLOATI_WATCH_HOLD_FLUSH", None)
        child = subprocess.Popen(
            [
                sys.executable, "-m", "floati", "watch", "--root", str(self.home),
                "--destination", str(self.destination), "--interval", "0.05",
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

        def close_child() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=watch_liveness_bound_seconds())
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

        self.addCleanup(close_child)
        began = time.monotonic()

        def attempt(seconds: float) -> bool:
            ready, _, _ = select.select([child.stdout], [], [], seconds)
            return bool(ready)

        bound, extension, met = await_with_one_extension(attempt)
        waited = time.monotonic() - began
        if not met:
            self.fail(
                "the watch child streamed no delta within derived bound "
                f"{bound:g}s plus one re-measured extension of {extension:g}s "
                f"(waited {waited:.3f}s total); host loadavg "
                f"{[round(value, 2) for value in os.getloadavg()]}"
            )
        if extension:
            print(
                f"[TIMEOUT-PUB-1] {self.id()}: missed the derived bound "
                f"{bound:g}s, re-measured {extension:g}s and extended once; "
                f"the child streamed at {waited:.3f}s; host loadavg "
                f"{[round(value, 2) for value in os.getloadavg()]}"
            )
        self.assertEqual(
            "initial", json.loads(child.stdout.readline())["evidence"]["delta"]["kind"]
        )
        return child

    def _wait_for_marker(self, path: Path, needle: str) -> str:
        found: list[str] = []
        began = time.monotonic()

        def attempt(seconds: float) -> bool:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if path.is_file():
                    recorded = path.read_text(encoding="utf-8")
                    if needle in recorded:
                        found.append(recorded)
                        return True
                select.select([], [], [], 0.05)
            return False

        bound, extension, met = await_with_one_extension(attempt)
        waited = time.monotonic() - began
        if not met:
            self.fail(
                f"marker {path.name} never carried {needle!r} within derived "
                f"bound {bound:g}s plus one re-measured extension of "
                f"{extension:g}s (waited {waited:.3f}s total); host loadavg "
                f"{[round(value, 2) for value in os.getloadavg()]}"
            )
        if extension:
            print(
                f"[TIMEOUT-PUB-1] {self.id()}: missed the derived bound "
                f"{bound:g}s, re-measured {extension:g}s and extended once; "
                f"the marker landed at {waited:.3f}s; host loadavg "
                f"{[round(value, 2) for value in os.getloadavg()]}"
            )
        return found[-1]

    def test_watch_trace_records_where_the_child_was_when_sigint_arrived(self) -> None:
        """WATCH-1: the trace must NAME the frame, not merely exist.

        Constructed rather than raced: the child signals from inside
        ``iter_deltas`` before SIGINT is sent, so the recorded stack is that
        frame and never the ``_watch_loop`` flush that beat it on a loaded
        Linux runner.
        """

        trace = Path(self.temp.name) / "armed-trace.txt"
        iter_ready = Path(tempfile.gettempdir()) / (
            f"floati-watch-iter-ready-{os.getpid()}-{Path(self.temp.name).name}"
        )
        self.addCleanup(lambda: iter_ready.unlink(missing_ok=True))
        child = self._streaming_child(trace, iter_ready=iter_ready)
        self._wait_for_marker(iter_ready, "WATCH_ITER_DELTAS_READY")
        child.send_signal(signal.SIGINT)

        def attempt(seconds: float) -> bool:
            try:
                child.wait(timeout=seconds)
                return True
            except subprocess.TimeoutExpired:
                return False

        bound, extension, met = await_with_one_extension(attempt)
        if not met:
            self.fail(
                "the watch child did not honour SIGINT within derived bound "
                f"{bound:g}s plus one re-measured extension of {extension:g}s; "
                f"host loadavg {[round(value, 2) for value in os.getloadavg()]}"
            )
        if extension:
            print(
                f"[TIMEOUT-PUB-1] {self.id()}: missed the derived bound "
                f"{bound:g}s, re-measured {extension:g}s and extended once; "
                f"the child then honoured SIGINT; host loadavg "
                f"{[round(value, 2) for value in os.getloadavg()]}"
            )

        self.assertEqual(0, child.returncode)
        recorded = trace.read_text(encoding="utf-8")
        self.assertIn("WATCH_TRACE_ARMED", recorded)
        self.assertIn("SIGINT_HANDLER_ENTERED", recorded)
        self.assertIn("SIGINT_HANDLER_RERAISING", recorded)
        # The frame, not just the fact: the handler must report a stack that
        # runs through the watch loop and the delta iterator it was blocked in.
        self.assertIn("_watch_loop", recorded)
        self.assertIn("iter_deltas", recorded)

    def test_early_sigint_during_flush_is_not_claimed_as_iter_deltas(self) -> None:
        """WATCH-1-F1 RED: SIGINT during stdout.flush must not satisfy iter_deltas."""

        trace = Path(self.temp.name) / "early-trace.txt"
        hold_flush = Path(tempfile.gettempdir()) / (
            f"floati-watch-hold-flush-{os.getpid()}-{Path(self.temp.name).name}"
        )
        self.addCleanup(lambda: hold_flush.unlink(missing_ok=True))
        child = self._streaming_child(trace, hold_flush=hold_flush)
        self._wait_for_marker(hold_flush, "WATCH_FLUSH_HOLD")
        child.send_signal(signal.SIGINT)

        def attempt(seconds: float) -> bool:
            try:
                child.wait(timeout=seconds)
                return True
            except subprocess.TimeoutExpired:
                return False

        bound, extension, met = await_with_one_extension(attempt)
        if not met:
            self.fail(
                "the watch child did not honour SIGINT within derived bound "
                f"{bound:g}s plus one re-measured extension of {extension:g}s; "
                f"host loadavg {[round(value, 2) for value in os.getloadavg()]}"
            )
        if extension:
            print(
                f"[TIMEOUT-PUB-1] {self.id()}: missed the derived bound "
                f"{bound:g}s, re-measured {extension:g}s and extended once; "
                f"the child then honoured SIGINT; host loadavg "
                f"{[round(value, 2) for value in os.getloadavg()]}"
            )

        self.assertEqual(0, child.returncode)
        recorded = trace.read_text(encoding="utf-8")
        self.assertIn("WATCH_TRACE_ARMED", recorded)
        self.assertIn("SIGINT_HANDLER_ENTERED", recorded)
        self.assertIn("_watch_loop", recorded)
        self.assertIn("wrapped_flush", recorded)
        self.assertNotIn(
            "iter_deltas",
            recorded,
            "an early SIGINT during flush was reported as the iter_deltas wait",
        )

    def test_watch_signal_behaviour_is_unchanged_when_no_trace_is_requested(self) -> None:
        """WATCH-1: the diagnostic is OFF unless asked for - checked at the GATE.

        A diagnostic that changes the thing it observes is worth nothing here,
        because the hang under investigation is a signal-handling hang. The
        first version of this control asserted that a named file did not appear,
        and a perturbation that armed the trace unconditionally still PASSED it
        - the file simply landed somewhere the test was not looking. A control
        that can only see one path is not a control, so this asserts the
        property itself: with the variable unset the context manager must leave
        SIGINT exactly as it found it, and with it set it must not.
        """

        from floati.cli import WATCH_TRACE_VARIABLE, _watch_signal_trace

        original = signal.getsignal(signal.SIGINT)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(WATCH_TRACE_VARIABLE, None)
            with _watch_signal_trace():
                unarmed = signal.getsignal(signal.SIGINT)
        self.assertIs(original, unarmed, "an unarmed watch must not touch SIGINT")
        self.assertIs(original, signal.getsignal(signal.SIGINT))

        armed_trace = Path(self.temp.name) / "gate-armed.txt"
        with mock.patch.dict(
            os.environ, {WATCH_TRACE_VARIABLE: str(armed_trace)}, clear=False
        ):
            with _watch_signal_trace():
                armed = signal.getsignal(signal.SIGINT)
        self.assertIsNot(original, armed, "an armed watch must install its recorder")
        self.assertIs(original, signal.getsignal(signal.SIGINT))
        self.assertIn("WATCH_TRACE_ARMED", armed_trace.read_text(encoding="utf-8"))

    def test_unarmed_watch_child_writes_no_trace_anywhere_in_its_tree(self) -> None:
        """WATCH-1: the reproducer's trace stays outside the scanned tree.

        The direct context-manager test above owns the unarmed product gate.
        This child is the load-shaped reproducer that has timed out four times.
        It may be armed at the external test-only seam while retaining its
        original assertion: no trace may be manufactured inside the fleet
        fixture tree.
        """

        trace = self._watch_reproduction_trace()
        child = self._streaming_child(trace)
        child.send_signal(signal.SIGINT)

        def attempt(seconds: float) -> bool:
            try:
                child.wait(timeout=seconds)
                return True
            except subprocess.TimeoutExpired:
                return False

        bound, extension, met = await_with_one_extension(attempt)
        if not met:
            diagnostic = (
                self._watch_trace(child, trace)
                if trace is not None
                else "WATCH-1 reproduction trace was not armed"
            )
            self.fail(
                "the watch child did not honour SIGINT within derived bound "
                f"{bound:g}s plus one re-measured extension of {extension:g}s; "
                f"host loadavg {os.getloadavg()}\n"
                + diagnostic
            )
        if extension:
            print(
                f"[TIMEOUT-PUB-1] {self.id()}: missed the derived bound "
                f"{bound:g}s, re-measured {extension:g}s and extended once; "
                f"the child then honoured SIGINT; host loadavg "
                f"{[round(value, 2) for value in os.getloadavg()]}"
            )

        self.assertEqual(0, child.returncode)
        self.assertEqual("", child.stderr.read())
        traces = [
            path
            for path in Path(self.temp.name).rglob("*")
            if path.is_file()
            and "WATCH_TRACE_ARMED" in path.read_text(encoding="utf-8", errors="replace")
        ]
        self.assertEqual([], traces)

    def _watch_trace(self, child: subprocess.Popen, trace: Path) -> str:
        """WATCH-1: make the child say where it was, then read what it said.

        Called only when SIGINT has already gone unanswered. SIGUSR1 reaches
        `faulthandler`, which dumps every thread FROM INSIDE the signal handler
        - the one mechanism that still reports when the interpreter itself is
        stuck. The trace also carries whether the child's own SIGINT handler
        was ever entered, and those two facts together say which hang this is:
        handler entered means something after it blocked; handler absent means
        the signal never got to Python at all.
        """

        if hasattr(signal, "SIGUSR1") and child.poll() is None:
            try:
                child.send_signal(signal.SIGUSR1)
            except OSError:
                pass
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if trace.exists() and "Thread" in trace.read_text(encoding="utf-8", errors="replace"):
                    break
                time.sleep(0.1)
        recorded = (
            trace.read_text(encoding="utf-8", errors="replace")
            if trace.exists()
            else "(the child wrote no trace)"
        )
        entered = "SIGINT_HANDLER_ENTERED" in recorded
        return (
            f"SIGINT handler entered: {entered}\n"
            f"--- child trace ---\n{recorded}\n--- end child trace ---"
        )

    def _watch_reproduction_trace(self, *, required: bool = False) -> Path | None:
        """Resolve a retained trace path outside this test's scanned tree.

        An investigator may arm the load-shaped reproducer with one exact path.
        Ordinary runs leave it genuinely unarmed.  Tests that directly assert
        the trace contract request a collision-free default beneath the host's
        temporary root.  Neither form is beneath ``self.temp``: the reproducer
        recursively scans that tree and must not mistake its own instrument for
        evidence that an unrequested trace was manufactured there.
        """

        supplied = os.environ.get(WATCH_REPRO_TRACE_VARIABLE)
        if not supplied and not required:
            return None
        trace = (
            Path(supplied)
            if supplied
            else Path(tempfile.gettempdir())
            / f"floati-watch-repro-{os.getpid()}-{Path(self.temp.name).name}.txt"
        )
        self.assertTrue(
            trace.is_absolute(), f"{WATCH_REPRO_TRACE_VARIABLE} must be absolute"
        )
        self.assertNotIn(
            Path(self.temp.name),
            (trace, *trace.parents),
            "the WATCH-1 reproduction trace must be outside the scanned test tree",
        )
        return trace

    def test_interrupt_after_streamed_delta_exits_cleanly_without_traceback(self) -> None:
        trace = self._watch_reproduction_trace(required=True)
        self.assertIsNotNone(trace)
        environment = dict(self.environment)
        environment["FLOATI_WATCH_TRACE"] = str(trace)
        child = subprocess.Popen(
            [
                sys.executable, "-m", "floati", "watch", "--root", str(self.home),
                "--destination", str(self.destination), "--interval", "0.05",
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        def close_child() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=watch_liveness_bound_seconds())
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

        self.addCleanup(close_child)
        self.assertIsNotNone(child.stdout)
        self.assertIsNotNone(child.stderr)
        # CI-GREEN-24. This test's CLAIM is in its name - the child exits with
        # rc 0 and an empty stderr. The old 1 s select and 2 s wait were not
        # part of that claim, they were bets that a loaded host would schedule
        # the child promptly, and the second one collected: it raised
        # TimeoutExpired under a concurrent suite and once on a runner, killing
        # the test BEFORE either real assertion ran. Measured on a quiet Mac the
        # first line arrives in ~200 ms and SIGINT-to-exit takes ~10 ms, so the
        # budget that failed had a 190x margin - a margin that wide does not
        # erode, it stalls, and a stall is the host's, not the product's. The
        # bounds are now a liveness backstop and the latencies are MEASURED and
        # printed, so a genuinely deaf SIGINT still reds, naming its number.
        began = time.monotonic()

        def attempt(seconds: float) -> bool:
            ready, _, _ = select.select([child.stdout], [], [], seconds)
            return bool(ready)

        bound, extension, met = await_with_one_extension(attempt)
        first_line_seconds = time.monotonic() - began
        if not met:
            self.fail(
                "the watch child streamed no delta within derived bound "
                f"{bound:g}s plus one re-measured extension of {extension:g}s "
                f"(waited {first_line_seconds:.3f}s total); host loadavg "
                f"{[round(value, 2) for value in os.getloadavg()]}"
            )
        if extension:
            print(
                f"[TIMEOUT-PUB-1] {self.id()}: missed the derived bound "
                f"{bound:g}s, re-measured {extension:g}s and extended once; "
                f"the child streamed at {first_line_seconds:.3f}s; host loadavg "
                f"{[round(value, 2) for value in os.getloadavg()]}"
            )
        self.assertEqual("initial", json.loads(child.stdout.readline())["evidence"]["delta"]["kind"])

        signalled = time.monotonic()
        child.send_signal(signal.SIGINT)

        def exit_attempt(seconds: float) -> bool:
            try:
                child.wait(timeout=seconds)
                return True
            except subprocess.TimeoutExpired:
                return False

        exit_bound, exit_extension, exit_met = await_with_one_extension(exit_attempt)
        exit_seconds = time.monotonic() - signalled
        if not exit_met:
            self.fail(
                "the watch child did not honour SIGINT within derived bound "
                f"{exit_bound:g}s plus one re-measured extension of "
                f"{exit_extension:g}s; first line took "
                f"{first_line_seconds:.3f}s, host loadavg {os.getloadavg()}\n"
                + self._watch_trace(child, trace)
            )
        print(
            f"[CI-GREEN-24] {self.id()}: first delta {first_line_seconds:.3f}s, "
            f"SIGINT to exit {exit_seconds:.3f}s, backstop {exit_bound:g}s"
            + (
                f" extended once by {exit_extension:g}s"
                if exit_extension
                else ""
            )
            + f"; host loadavg {[round(value, 2) for value in os.getloadavg()]}"
        )
        self.assertEqual(0, child.returncode)
        self.assertEqual("", child.stderr.read())


if __name__ == "__main__":
    unittest.main()
