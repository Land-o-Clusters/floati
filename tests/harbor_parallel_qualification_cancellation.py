"""Cancellation boundaries of qualification, never performance evidence.

The direct capture and paired run use tiny real owned children. The recursive
probe constructor control uses a synthetic post-spawn Popen seam: no process
is created by that control, so demonstrating lost ownership cannot orphan one.
No installed profile or product source is changed.
"""
from __future__ import annotations

import copy
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from types import ModuleType
import unittest
from unittest import mock

from tests import harbor_parallel_qualification as qualification_fixtures


class QualificationCancellationTests(unittest.TestCase):
    def fixture(self):
        # Compose the existing fixture; importing/inheriting its TestCase class
        # here would duplicate its unrelated tests during discovery.
        fixture = qualification_fixtures.ParallelQualificationTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.fixture()
        return fixture, fixture.helper()

    def test_signal_delivery_error_cannot_skip_retained_direct_child_collection(self):
        fixture, helper = self.fixture()
        owner = helper._Cancellation()
        release = fixture.bank / "capture-release"
        output = fixture.bank / "capture.log"
        record, children, waits, delivery = {}, [], [], []
        original_init, original_wait = subprocess.Popen.__init__, subprocess.Popen.wait
        original_signal = subprocess.Popen.send_signal
        child_code = (
            "import os,sys,time\nfrom pathlib import Path\n"
            "release=Path(sys.argv[1]); started=time.monotonic(); deadline=started+120\n"
            "while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
            "assert release.exists(), ('capture child release: readiness bound=120s '"
            "'elapsed=%.3fs host_load=%r' % (time.monotonic()-started,os.getloadavg()))\n"
            "print('owned capture child completed',flush=True)\n"
        )

        def initialized(process, *arguments, **keywords):
            original_init(process, *arguments, **keywords)
            children.append(process)

            def refused(number):
                delivery.append((process.pid, number))
                # Even with delivery refused, the test child exits by its own
                # bounded protocol. Test cleanup never needs a blind signal.
                release.write_text("test-owned child may exit\n")
                raise PermissionError(errno.EPERM, "controlled signal delivery refusal")

            process.send_signal = refused
            owner.receive(signal.SIGINT, None)

        def waited(process, *arguments, **keywords):
            result = original_wait(process, *arguments, **keywords)
            waits.append((process.pid, result))
            return result

        failure = None
        try:
            with output.open("xb") as stream, owner.handlers(),\
                    mock.patch.object(subprocess.Popen, "__init__", initialized),\
                    mock.patch.object(subprocess.Popen, "wait", waited):
                try:
                    helper._capture([sys.executable, "-B", "-c", child_code, str(release)],
                        cwd=fixture.bank, environment=dict(os.environ), cancellation=owner,
                        log=stream, record=record)
                except BaseException as error:
                    failure = {"type": type(error).__name__, "detail": str(error)}
            # Freeze observations BEFORE this test's safety collection. That
            # cleanup cannot turn a missing helper reap into a passing test.
            observed = {"record": copy.deepcopy(record), "waits": list(waits),
                        "delivery": list(delivery), "owner_released": owner.process is None,
                        "child_returncodes": [process.returncode for process in children],
                        "failure": failure, "native_execution": True, "performance_evidence": False}
        finally:
            release.write_text("test-owned safety release\n")
            for process in children:
                # Restore the real retained-handle signaling seam for safety
                # collection; the controlled EPERM must not affect cleanup.
                process.send_signal = original_signal.__get__(process, type(process))
                try:
                    original_wait(process, timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        original_wait(process, timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        original_wait(process, timeout=2)
        (fixture.bank / "capture-boundary.json").write_text(json.dumps(observed, sort_keys=True) + "\n")
        self.assertEqual(1, len(children))
        self.assertTrue(observed["delivery"], "control never reached the failing signal syscall seam")
        self.assertTrue(observed["record"].get("collected"),
                        "signal delivery OSError skipped collection of the retained child: " + repr(observed))
        self.assertEqual(0, observed["record"]["exit_code"])
        self.assertIn((children[0].pid, 0), observed["waits"])
        self.assertTrue(observed["owner_released"])
        self.assertEqual(signal.SIGINT, owner.number)

    def test_recursive_probe_retains_constructor_child_until_definitive_collection(self):
        fixture, helper = self.fixture()
        trace, children = [], []

        class PostSpawnFailure:
            def __new__(cls, *arguments, **keywords):
                process = object.__new__(cls)
                children.append(process)
                return process

            def __init__(self, *arguments, **keywords):
                self.pid, self.returncode, self._child_created = 100_000_001, None, True
                trace.append({"event": "synthetic_os_spawn", "pid": self.pid})
                raise OSError(errno.EIO, "controlled nested post-spawn constructor failure")

            def send_signal(self, number):
                trace.append({"event": "owned_signal", "pid": self.pid, "number": number})

            def wait(self, *arguments, **keywords):
                self.returncode = 0
                trace.append({"event": "definitive_collection", "pid": self.pid, "exit_code": 0})
                return 0

        packages = {}
        for name in ("PIL", "jsonschema"):
            package = ModuleType(name)
            package.__file__ = str(fixture.imports / name / "__init__.py")
            package.__version__ = "routing-fixture-only"
            packages[name] = package
        environment = helper.make_environment(fixture.profile, {}, fixture.home, fixture.temporary)
        argv = ["qualification-probe", str(fixture.imports), "0",
                json.dumps([environment] * 4), sys.executable, helper._PROBE]
        failure = None
        prior_handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        try:
            with mock.patch.dict(sys.modules, packages), mock.patch.object(sys, "argv", argv),\
                    mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(os.environ, environment, clear=True),\
                    mock.patch.object(subprocess, "Popen", PostSpawnFailure):
                try:
                    exec(helper._PROBE, {"__name__": "__qualification_probe_control__"})
                except BaseException as error:
                    failure = error
        finally:
            for number, handler in prior_handlers.items():
                signal.signal(number, handler)
        artifact = {"trace": trace, "failure": repr(failure), "native_execution": False,
                    "kind": "synthetic_post_spawn_constructor_ownership_control",
                    "performance_evidence": False, "real_children_created": 0}
        (fixture.bank / "nested-constructor-boundary.json").write_text(json.dumps(artifact, sort_keys=True) + "\n")
        self.assertIsInstance(failure, OSError)
        self.assertEqual(errno.EIO, failure.errno)
        self.assertEqual(1, len(children))
        self.assertEqual([{"event": "definitive_collection", "pid": children[0].pid, "exit_code": 0}],
                         [row for row in trace if row["event"] == "definitive_collection"],
                         "recursive probe abandoned the child when Popen construction failed after spawn")

    def test_sigint_during_final_verification_retains_interrupted_receipt_and_cleanup(self):
        fixture, helper = self.fixture()
        output = fixture.bank / "cancelled-pair"
        original_registry = helper._read_registry
        original_handler = signal.getsignal(signal.SIGINT)
        # A test-owned outer handler makes an escaped signal a local assertion
        # failure. The production helper must retain its own handler until the
        # final verification/cleanup boundary instead of restoring this early.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, original_handler)
        observed = {"injected": False, "native_execution": True, "performance_evidence": False}

        def registry_at_final_verification():
            result = original_registry()
            caller = sys._getframe(1)
            receipt = caller.f_locals.get("receipt")
            # This retained native field is assigned only by final verification;
            # no ordinal assumption about ordinary registry reads is needed.
            if not observed["injected"] and isinstance(receipt, dict) and "source_after" in receipt:
                launches = copy.deepcopy(receipt["launches"])
                self.assertEqual(2, len(launches))
                self.assertTrue(all(row["collected"] for row in launches))
                observed.update(injected=True, launches_before_signal=launches,
                                source_after=receipt["source_after"],
                                scratch=str(caller.f_locals["scratch"]))
                # Both native phase leaders are already collected. Register
                # cleanup before signaling, including the pre-fix exit path.
                self.addCleanup(shutil.rmtree, observed["scratch"], True)
                os.kill(os.getpid(), signal.SIGINT)
            return result

        escaped = None
        with fixture.registry_patch(helper), mock.patch.object(helper, "_read_registry", registry_at_final_verification):
            try:
                helper.run_pair(fixture.root, output)
            except BaseException as error:
                escaped = {"type": type(error).__name__, "detail": str(error)}
        observed["escaped"] = escaped
        (fixture.bank / "final-verification-boundary.json").write_text(json.dumps(observed, sort_keys=True) + "\n")
        self.assertTrue(observed["injected"], "control did not reach actual final verification after two native phases")
        for phase in ("serial", "parallel"):
            native = json.loads((output / phase / "final.json").read_text())
            self.assertTrue(native["cleanup"]["successful"], native)
        path = output / "qualification.json"
        self.assertTrue(path.is_file(),
                        "SIGINT escaped final verification and lost the qualification receipt: " + repr(observed))
        receipt = json.loads(path.read_text())
        self.assertEqual("interrupted", receipt["status"])
        self.assertFalse(receipt["qualified"])
        self.assertFalse(receipt["successful"])
        self.assertEqual(128 + signal.SIGINT, receipt["exit_code"])
        self.assertTrue(receipt["cleanup"]["direct_children_collected"])
        self.assertTrue(receipt["cleanup"]["native_cleanup_proved"])
        self.assertTrue(receipt["cleanup"]["successful"])
        self.assertFalse(receipt["cleanup"]["scratch_retained"])
