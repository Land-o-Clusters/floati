"""BL-10-F2: real held Git maintenance, never an argv lookalike."""

from tests.readiness import READY_SECONDS, wait_event, wait_until
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from tests import test_gate_z2_of_fl_1 as gate

_RETAINED_MAINTENANCE = {}


@contextmanager
def held_maintenance():
    """Release/reap our synchronous Git and hook before removing its bank."""
    bank = Path(tempfile.mkdtemp(prefix="fl1-held-"))
    repository = bank / "projection"
    ready, release = bank / "ready", bank / "release"
    process = None
    cleaned = False
    try:
        repository.mkdir()
        marker = uuid.uuid4().hex
        (bank / "projection.fl1-attribution.marker").write_text(marker)
        environment = gate._git_environment()
        environment.update(FLOATI_FL1_ATTRIBUTION=marker,
                           BL10_READY=str(ready), BL10_RELEASE=str(release))
        subprocess.run(["/usr/bin/git", "init", "-q"], cwd=repository,
                       env=environment, check=True, capture_output=True)
        for key, value in (("gc.auto", "1"), ("gc.autoDetach", "false"),
                           ("maintenance.autoDetach", "false")):
            subprocess.run(["/usr/bin/git", "config", "--local", key, value],
                           cwd=repository, env=environment, check=True,
                           capture_output=True)
        # No commit runs after these loose objects are made: only the held
        # maintenance command can consume them or launch auto-GC.
        gate._write_gate_objects(bank, repository, environment)
        hook = repository / ".git" / "hooks" / "pre-auto-gc"
        hook.write_text('#!/bin/sh\nprintf "%s\\n" "$$" > "$BL10_READY"\n'
                        'i=0\nwhile [ ! -f "$BL10_RELEASE" ]; do\n'
                        ' i=$((i + 1))\n [ "$i" -le 1800 ] || exit 1\n'
                        ' /bin/sleep 0.05\ndone\nexit 0\n')
        hook.chmod(0o700)
        # Both detach controls are explicit: old Git knows gc.autoDetach;
        # newer Git also knows maintenance.autoDetach. Unknown config keys
        # are harmless on old Git, unlike maintenance --no-detach.
        process = subprocess.Popen(
            ["/usr/bin/git", "maintenance", "run", "--task=gc", "--auto"],
            cwd=repository, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        owner = {'pid': process.pid, 'identity': None, 'repository': str(repository),
                 'argv': process.args, 'cleanup': 'pending', 'bank': str(bank)}
        _RETAINED_MAINTENANCE[process.pid] = (process, bank)
        (bank / 'owner.json').write_text(json.dumps(owner, indent=2) + '\n')
        owner['identity'] = gate.ProcessQuery().identity(process.pid)
        (bank / 'owner.json').write_text(json.dumps(owner, indent=2) + '\n')
        wait_until(lambda: ready.exists() or process.poll() is not None,
            "native maintenance private hook")
        if process.poll() is not None:
            raise AssertionError("native maintenance exited before observation")
        yield repository, process
    finally:
        release.touch()
        if process is not None:
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                owner.update(cleanup='unproved: communicate timeout after release')
                (bank / 'owner.json').write_text(json.dumps(owner, indent=2) + '\n')
                raise
            _RETAINED_MAINTENANCE.pop(process.pid, None)
            if process.returncode:
                raise AssertionError("owned maintenance failed after release: "
                                     + stderr.decode(errors="replace")[:1000])
        cleaned = True
        if cleaned:
            shutil.rmtree(bank)


def contains_pid(hits, pid):
    return any(line.split(maxsplit=1)[0] == str(pid) for line in hits)


class NativeAttributionTests(unittest.TestCase):
    def test_mid_watch_unrelated_native_maintenance_is_not_attributed(self):
        """Not-alive-at-start is not a witness of fixture ownership."""
        with tempfile.TemporaryDirectory(prefix="fl1-midwatch-") as temporary, ExitStack() as stack:
            projection = gate._projection_repository(Path(temporary), gate.SHIPPED_KNOBS)
            observed = threading.Event()
            planted = []
            native_run = subprocess.run
            native_census = gate._maintenance_children_under

            def census(prefix):
                result = native_census(prefix)
                observed.set()
                return result

            def run(argv, *args, **kwargs):
                if "add" in argv and kwargs.get("cwd") == projection and not planted:
                    wait_event(observed, "maintenance watch before foreign plant")
                    planted.append(stack.enter_context(held_maintenance())[1])
                return native_run(argv, *args, **kwargs)

            with patch.object(gate, '_maintenance_children_under', census),\
                    patch.object(subprocess, 'run', run):
                returned_at, samples = gate._run_commit_and_watch(projection, gate.SHIPPED_KNOBS)
            process = planted[0]
            self.assertIsNone(process.poll())
            after = [hit for moment, hits in samples if moment > returned_at for hit in hits]
            self.assertTrue(any(t > returned_at for t, _ in samples))
            self.assertFalse(contains_pid(after, process.pid), "mid-watch foreign maintenance counted")

    def test_unrelated_native_maintenance_is_not_a_fixture_child(self):
        """A sibling's real, live maintenance must not red our FL-1 gate."""
        with tempfile.TemporaryDirectory(prefix="fl1-experiment-") as temporary:
            projection = gate._projection_repository(
                Path(temporary), gate.SHIPPED_KNOBS)
            with held_maintenance() as (foreign, process):
                native = subprocess.run(
                    ["/bin/ps", "-p", str(process.pid), "-o", "pid=,command="],
                    check=True, capture_output=True, text=True,
                ).stdout
                self.assertIn("maintenance run", native)
                self.assertEqual(str(process.pid), native.split()[0])
                returned_at, samples = gate._run_commit_and_watch(
                    projection, gate.SHIPPED_KNOBS)
                after = [hit for moment, hits in samples
                         if moment > returned_at for hit in hits]
                print(json.dumps({"control": "unrelated-native-maintenance",
                                  "pid": process.pid,
                                  "after_return_samples": sum(t > returned_at for t, _ in samples),
                                  "counted_foreign_pid": contains_pid(after, process.pid)}))
                self.assertIsNone(process.poll(), "plant must remain live across the sample")
                self.assertTrue(any(t > returned_at for t, _ in samples),
                                "absence needs a post-return sample")
                self.assertFalse(contains_pid(after, process.pid),
                                 "census counted actual unrelated Git maintenance")

    def test_matching_marker_native_maintenance_still_counts(self):
        """An always-empty census cannot satisfy the negative property."""
        with held_maintenance() as (projection, process):
            hits = gate._maintenance_children_under(projection)
            self.assertIsNone(process.poll())
            self.assertTrue(contains_pid(hits, process.pid),
                            "census missed its own matching-marker maintenance")

    def test_live_marker_lookup_failure_is_not_an_empty_census(self):
        with held_maintenance() as (projection, process):
            native_run = subprocess.run

            def fail_marker(argv, *args, **kwargs):
                if "eww" in argv and str(process.pid) in argv:
                    return subprocess.CompletedProcess(argv, 1, "", "private failure")
                return native_run(argv, *args, **kwargs)

            with patch.object(subprocess, 'run', fail_marker):
                with self.assertRaisesRegex(RuntimeError, "live maintenance marker unavailable"):
                    gate._maintenance_children_under(projection)
            self.assertIsNone(process.poll())


class ObservationBoundaryTests(unittest.TestCase):
    def test_empty_census_crossing_return_requires_a_fresh_census(self):
        with tempfile.TemporaryDirectory() as temporary:
            projection = gate._projection_repository(Path(temporary), gate.SHIPPED_KNOBS)
            started = threading.Event()
            release = threading.Event()
            timers, calls = [], []
            native_run = subprocess.run

            def census(prefix):
                calls.append(time.monotonic())
                if len(calls) == 1:
                    started.set()
                    wait_event(release, 'empty census release', timeout=READY_SECONDS * 2)
                return []

            def run(argv, *args, **kwargs):
                if 'commit' in argv:
                    wait_event(started, 'empty census handshake')
                    value = native_run(argv, *args, **kwargs)
                    timer = threading.Timer(.05, release.set)
                    timers.append(timer)
                    timer.start()
                    return value
                return native_run(argv, *args, **kwargs)

            try:
                with patch.object(gate, '_maintenance_children_under', census),\
                        patch.object(gate, '_WATCH_SECONDS', .001),\
                        patch.object(subprocess, 'run', run):
                    returned_at, samples = gate._run_commit_and_watch(projection, gate.SHIPPED_KNOBS)
                self.assertGreaterEqual(len(calls), 2, 'empty crossing census suppressed fresh observation')
                self.assertTrue(any(moment > returned_at for moment in calls))
                self.assertTrue(any(moment > returned_at for moment, _ in samples))
            finally:
                release.set()
                for timer in timers:
                    timer.join()

    def test_inherited_marker_is_stripped_and_sibling_markers_differ(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary) / 'first', Path(temporary) / 'second'
            with patch.dict(os.environ, {'FLOATI_FL1_ATTRIBUTION': 'foreign'}):
                self.assertNotIn('FLOATI_FL1_ATTRIBUTION', gate._git_environment())
                one = gate._git_environment(projection=first)['FLOATI_FL1_ATTRIBUTION']
                two = gate._git_environment(projection=second)['FLOATI_FL1_ATTRIBUTION']
                self.assertNotEqual(one, two)
                self.assertEqual(one, gate._git_environment(projection=first)['FLOATI_FL1_ATTRIBUTION'])
                self.assertEqual('foreign', os.environ['FLOATI_FL1_ATTRIBUTION'])

    def test_watcher_failure_cannot_become_no_child_green(self):
        with tempfile.TemporaryDirectory() as temporary:
            projection = gate._projection_repository(Path(temporary), gate.SHIPPED_KNOBS)
            with patch.object(gate, '_maintenance_children_under', side_effect=OSError('unreadable')):
                with self.assertRaisesRegex(RuntimeError, 'watcher observation failed'):
                    gate._run_commit_and_watch(projection, gate.SHIPPED_KNOBS)

    def test_expired_precommit_watch_still_has_a_postreturn_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            projection = gate._projection_repository(Path(temporary), gate.SHIPPED_KNOBS)
            with patch.object(gate, '_WATCH_SECONDS', 0):
                returned_at, samples = gate._run_commit_and_watch(projection, gate.SHIPPED_KNOBS)
            self.assertTrue(any(moment > returned_at for moment, _ in samples))

    def test_commit_failure_joins_the_watcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            projection = gate._projection_repository(Path(temporary), gate.SHIPPED_KNOBS)
            hooks = Path(temporary) / 'failing-hooks'
            hooks.mkdir()
            hook = hooks / 'pre-commit'
            hook.write_text('#!/bin/sh\nexit 1\n')
            hook.chmod(0o700)
            created = []
            native_thread = threading.Thread

            def thread(*args, **kwargs):
                value = native_thread(*args, **kwargs)
                created.append(value)
                return value

            with patch.object(gate.threading, 'Thread', thread):
                with self.assertRaises(subprocess.CalledProcessError):
                    gate._run_commit_and_watch(projection, ('-c', 'core.hooksPath=' + str(hooks)))
            self.assertTrue(created)
            self.assertFalse(any(item.is_alive() for item in created))

    def test_straddling_hit_cannot_be_discarded_as_pre_return_absence(self):
        with tempfile.TemporaryDirectory() as temporary:
            projection = gate._projection_repository(Path(temporary), gate.SHIPPED_KNOBS)
            first = threading.Event()
            release = threading.Event()
            timers = []
            native_run = subprocess.run

            def census(prefix):
                if not first.is_set():
                    first.set()
                    wait_event(release, 'straddling fixture release', timeout=READY_SECONDS * 2)
                    return ['999 git maintenance run --auto --detach']
                return []

            def run(argv, *args, **kwargs):
                if 'commit' in argv:
                    wait_event(first, 'straddling census handshake')
                    value = native_run(argv, *args, **kwargs)
                    timer = threading.Timer(.05, release.set)
                    timers.append(timer)
                    timer.start()
                    return value
                return native_run(argv, *args, **kwargs)

            try:
                with patch.object(gate, '_maintenance_children_under', census),\
                        patch.object(subprocess, 'run', run):
                    with self.assertRaisesRegex(RuntimeError, 'crossed commit return'):
                        gate._run_commit_and_watch(projection, gate.SHIPPED_KNOBS)
            finally:
                release.set()
                for timer in timers:
                    timer.join()


if __name__ == "__main__":
    unittest.main()
