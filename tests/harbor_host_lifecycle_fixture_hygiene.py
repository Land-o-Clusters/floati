"""Opt-in spawn-fixture tracker hygiene, outside default discovery."""
from pathlib import Path
import subprocess
import sys
import unittest


class HostLifecycleFixtureHygieneTests(unittest.TestCase):
    def assert_tracker_lifetime(self, *, preexisting):
        # A fresh interpreter supplies the ownership boundary independently
        # of whichever multiprocessing helpers the suite already uses.
        probe = '''
import os
from multiprocessing import resource_tracker
from unittest import mock
from tests.test_seat_execution_host_lifecycle import HostLifecycleModelTests

tracker = resource_tracker._resource_tracker
assert tracker._pid is None, "fresh probe unexpectedly inherited a tracker"
created = []
real_ensure_running = tracker.ensure_running
def observe_creation():
    real_ensure_running()
    if tracker._pid is not None and tracker._pid not in created:
        created.append(tracker._pid)
case = HostLifecycleModelTests("runTest")
case.setUp()
try:
    if PREEXISTING:
        tracker.ensure_running()
    prior = tracker._pid
    item = case.car()
    case.claim(item)
    with mock.patch.object(tracker, "ensure_running", observe_creation):
        case.native_unclosed_running(item, context="spawn")
    assert created, "probe did not exercise real tracker creation"
    if PREEXISTING:
        assert tracker._pid == prior, "fixture replaced a preexisting tracker"
        assert os.waitpid(prior, os.WNOHANG) == (0, 0), "fixture reaped a preexisting tracker"
    else:
        for pid in created:
            try:
                result = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                continue
            raise AssertionError("fixture-owned resource tracker was not reaped: " + repr((pid, result)))
finally:
    # Close and reap only the helper this probe explicitly owns.
    if tracker._pid in created or (PREEXISTING and tracker._pid == prior):
        tracker._stop()
    case.doCleanups()
'''
        probe = "PREEXISTING = " + repr(preexisting) + "\n" + probe
        completed = subprocess.run(
            [str(Path(sys.executable).resolve()), "-B", "-c", probe],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, timeout=30)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_spawn_fixture_reaps_its_new_resource_tracker_before_return(self):
        self.assert_tracker_lifetime(preexisting=False)

    def test_spawn_fixture_preserves_a_preexisting_resource_tracker(self):
        self.assert_tracker_lifetime(preexisting=True)


def load_tests(loader, standard_tests, pattern):
    return loader.loadTestsFromTestCase(HostLifecycleFixtureHygieneTests)


if __name__ == "__main__":
    unittest.main()
