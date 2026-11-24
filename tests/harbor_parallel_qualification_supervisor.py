"""Public supervisor decisions must include the payload's completed interval.

These are tiny native routing fixtures, not passing host-performance evidence.
No protocol frames, process outcomes or native receipts are fabricated. The
policy clock reaches each boundary only after the actual qualification payload exists.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import signal
import threading
import time
import unittest
from unittest import mock

from tests import harbor_parallel_qualification as qualification_tests
from tests.readiness import Readiness, wait_join


TIMING_FIELDS = {
    "parallel_native_seconds", "payload_post_validation_seconds",
    "payload_publication_seconds", "publication_to_payload_exit_seconds",
    "supervisor_startup_seconds", "observer_publication_seconds",
}


class ParallelQualificationSupervisorTests(unittest.TestCase):
    def require_supervisor(self, helper):
        self.assertTrue(callable(helper.supervise_pair),
                        "external supervisor is absent; the payload cannot adjudicate its own publication cost")
        self.assertTrue(callable(helper.supervisor_clock),
                        "supervisor has no independent external-clock boundary")

    def fixture(self):
        # Compose rather than inherit or import a TestCase class: the existing
        # qualification module must not be discovered a second time here.
        owner = qualification_tests.ParallelQualificationTests("runTest")
        self.addCleanup(owner.doCleanups)
        owner.fixture()
        source = qualification_tests.SOURCE / "scripts/parallel_qualification.py"
        text = source.read_text()
        declared_registry = 'PROFILE_REGISTRY = _QUALIFIED_PLATFORM_REGISTRIES["darwin"]'
        self.assertEqual(1, text.count(declared_registry),
                         "private fixture must bind exactly the existing fixed registry declaration")
        text = text.replace(declared_registry, "PROFILE_REGISTRY = Path(%r)" % str(owner.registry))
        helper_path = owner.root / "scripts/parallel_qualification.py"
        helper_path.write_text(text)
        sibling = qualification_tests.SOURCE / "scripts/parallel_supervisor.py"
        if sibling.is_file():
            shutil.copyfile(sibling, owner.root / "scripts/parallel_supervisor.py")
        cases = "import unittest\n"
        for ordinal in range(8):
            cases += ("class Case%d(unittest.TestCase):\n"
                      " def test_one(self): pass\n") % ordinal
        (owner.root / "test_cases.py").write_text(cases)
        owner.commit()
        specification = importlib.util.spec_from_file_location("owned_supervisor_fixture", helper_path)
        helper = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(helper)
        self.require_supervisor(helper)
        return owner, helper

    def assert_nonnegative_seconds(self, value):
        self.assertIn(type(value), (int, float), "seconds must be numeric, not boolean or absent")
        self.assertTrue(math.isfinite(value))
        self.assertGreaterEqual(value, 0)

    def completed_payload(self, output):
        path = output / "qualification.json"
        raw = path.read_bytes()
        payload = json.loads(raw)
        if payload.get("scratch_root"):
            self.addCleanup(shutil.rmtree, payload["scratch_root"], True)
        self.assertEqual("awaiting_external_gate", payload["status"])
        self.assertFalse(payload["qualified"], "payload asserted authority over its unmeasured exit/publication")
        self.assertTrue(payload["cleanup"]["successful"])
        self.assertEqual(["serial", "parallel"], [row["phase"] for row in payload["launches"]])
        for launch in payload["launches"]:
            self.assertTrue(launch["collected"])
            self.assertEqual(0, launch["exit_code"])
        serial = json.loads((output / "serial/final.json").read_bytes())
        parallel = json.loads((output / "parallel/final.json").read_bytes())
        for native in (serial, parallel):
            self.assertEqual("complete", native["status"])
            self.assertTrue(native["successful"])
            self.assertTrue(native["cleanup"]["successful"])
            self.assertEqual(8, native["aggregate"]["tests_run"])
            self.assertEqual(payload["source"], native["source"])
            if native.get("scratch_root"):
                self.addCleanup(shutil.rmtree, native["scratch_root"], True)
        self.assertEqual(serial["source"], parallel["source"])
        self.assertEqual(serial["interpreter"], parallel["interpreter"])
        self.assertEqual(serial["aggregate"]["selected_ids"], parallel["census"]["ids"])
        self.assertEqual(8, parallel["workers"])
        self.assertTrue(parallel["qualified"])
        self.assertTrue(parallel["comparison"]["equivalent"])
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def assert_observer(self, output, observer, *, qualified):
        binding = self.completed_payload(output)
        published = json.loads((output / "observer.json").read_bytes())
        for evidence in (observer, published):
            self.assertIs(qualified, evidence["qualified"])
            self.assertEqual(binding, evidence["payload_artifact"])
            self.assertEqual(set(binding), set(evidence["payload_artifact"]))
            self.assertEqual(int, type(evidence["payload_artifact"]["bytes"]))
            self.assert_nonnegative_seconds(evidence["parallel_wall_seconds"])
        # Its own disk publication is outside the authoritative interval.
        # Returned/stdout timing may report that completed write, so do not
        # equate all disk and returned timing figures or demand a completed
        # self-publication duration inside the first disk publication.
        self.assertEqual(TIMING_FIELDS, set(observer["timing"]))
        for seconds in observer["timing"].values():
            self.assert_nonnegative_seconds(seconds)
        return published

    def test_public_supervisor_api_is_separate_from_payload(self):
        owner = qualification_tests.ParallelQualificationTests("runTest")
        self.require_supervisor(owner.helper())

    def test_parallel_wall_deadline_is_a_typed_collected_refusal(self):
        owner, helper = self.fixture()
        sibling = owner.root / 'scripts/parallel_supervisor.py'
        text = sibling.read_text()
        # A private shortened real clock budget, never a fabricated outcome.
        text += '\n_PARALLEL_WALL_SECONDS = 0.1\n'
        sibling.write_text(text)
        output = owner.bank / 'pair'
        cases = 'import time, unittest\nfrom pathlib import Path\n'
        for ordinal in range(8):
            cases += ('class Case%d(unittest.TestCase):\n'
                      ' def test_one(self):\n'
                      '  if Path(%r).is_file(): time.sleep(1)\n') % (
                          ordinal, str(output / 'serial/final.json'))
        (owner.root / 'test_cases.py').write_text(cases)
        owner.commit()
        with owner.registry_patch(helper):
            observer = helper.supervise_pair(owner.root, output)
        self.assertEqual('invalid', observer['status'])
        self.assertEqual('qualification_parallel_wall_deadline', observer.get('refusal_reason'))
        self.assertFalse(observer['qualified'])
        self.assertTrue(observer['cleanup']['payload_collected'])
        self.assertEqual(observer['refusal_reason'],
                         json.loads((output / 'observer.json').read_bytes())['refusal_reason'])
        for path in (output / 'qualification.json', output / 'serial/final.json',
                     output / 'parallel/final.json'):
            if path.is_file():
                record = json.loads(path.read_bytes())
                if record.get('scratch_root'):
                    self.addCleanup(shutil.rmtree, record['scratch_root'], True)
        if observer.get('scratch_root'):
            self.addCleanup(shutil.rmtree, observer['scratch_root'], True)

    def test_tiny_native_pair_publishes_authoritative_observer_and_provisional_payload(self):
        owner, helper = self.fixture()
        output = owner.bank / "pair"
        with owner.registry_patch(helper):
            observer = helper.supervise_pair(owner.root, output)
        self.assertEqual("complete", observer["status"])
        self.assertTrue(observer["successful"])
        self.assertLessEqual(observer["parallel_wall_seconds"], 600.0)
        published = self.assert_observer(output, observer, qualified=True)
        self.assertTrue(published["successful"])

    def test_wall_deadline_includes_exit_after_protocol_close(self):
        owner, helper = self.fixture()
        sibling = owner.root / 'scripts/parallel_supervisor.py'
        with sibling.open('a') as stream:
            stream.write('''
_PARALLEL_WALL_SECONDS = 5.0
# Only this committed fixture module sees the controlled deadline clock.
# Observe actual protocol EOF before advancing it; the product still creates
# the deadline, waits on its remaining budget and raises its typed refusal.
_native_os = os
_native_read = os.read
_protocol_closed = False
_payload_transmit = None
_payload_marker = None
class _DeadlineClock:
    def monotonic(self):
        return 10.0 if _protocol_closed else 0.0
time = _DeadlineClock()
def _read_protocol(descriptor, size):
    global _protocol_closed
    part = _native_read(descriptor, size)
    if not part:
        _protocol_closed = True
    return part
class _ProtocolOS:
    read = staticmethod(_read_protocol)
    def close(self, descriptor):
        if descriptor == _payload_transmit:
            # Publish readiness before EOF can release the observing parent.
            _payload_marker.touch()
        return _native_os.close(descriptor)
    def __getattr__(self, name):
        return getattr(_native_os, name)
os = _ProtocolOS()
_real_payload = _payload
def _payload(engine, root, output, transmit, acknowledge):
    global _payload_transmit, _payload_marker
    _payload_transmit = transmit
    _payload_marker = Path(output) / 'post_protocol_exit_wait'
    result = _real_payload(engine, root, output, transmit, acknowledge)
    import threading
    threading.Event().wait(30)
    return result
''')
        owner.commit()
        output = owner.bank / 'pair'
        with owner.registry_patch(helper):
            observer = helper.supervise_pair(owner.root, output)
        self.assertTrue((output / 'post_protocol_exit_wait').is_file())
        self.completed_payload(output)
        self.assertEqual('invalid', observer['status'])
        self.assertEqual('qualification_parallel_wall_deadline', observer.get('refusal_reason'))
        self.assertTrue(observer['cleanup']['payload_collected'])
        self.assertFalse(observer['qualified'])
        if observer.get('scratch_root'):
            self.addCleanup(shutil.rmtree, observer['scratch_root'], True)

    def test_external_total_crossing_after_payload_publication_refuses_native_success_pair(self):
        for target_wall, expected in ((600.0, True), (601.0, False)):
            with self.subTest(wall=target_wall):
                owner, helper = self.fixture()
                output = owner.bank / "pair"
                boundary_observed = []

                def external_clock():
                    # Deterministic policy control only: all native child clocks and
                    # timeout behavior remain unchanged in their fresh processes.
                    if (output / "qualification.json").is_file():
                        boundary_observed.append(True)
                        return target_wall
                    return 0.0

                with owner.registry_patch(helper), mock.patch.object(helper, "supervisor_clock", external_clock):
                    observer = helper.supervise_pair(owner.root, output)
                self.assertTrue(boundary_observed, "supervisor never measured past actual payload publication")
                self.assertEqual(target_wall, observer["parallel_wall_seconds"])
                self.assertEqual(600.0, observer["parallel_limit_seconds"])
                if expected:
                    self.assertNotIn("gate_reason", observer)
                else:
                    self.assertEqual("external_parallel_total_exceeded_600_seconds", observer["gate_reason"])
                published = self.assert_observer(output, observer, qualified=expected)
                self.assertEqual(target_wall, published["parallel_wall_seconds"])
                self.assertEqual(600.0, published["parallel_limit_seconds"])


    def test_native_serial_failure_retains_committed_payload_without_supervisor_interrupt(self):
        owner, helper = self.fixture()
        output = owner.bank / "pair"
        with (owner.root / "test_cases.py").open("a") as stream:
            stream.write("\nclass NativeSerialFailure(unittest.TestCase):\n"
                         " def test_serial_failure(self):\n"
                         "  self.fail('owned serial assertion failure')\n")
        owner.commit()
        with owner.registry_patch(helper):
            observer = helper.supervise_pair(owner.root, output)
        if observer.get("scratch_root"):
            self.addCleanup(shutil.rmtree, observer["scratch_root"], True)
        payload_path = output / "qualification.json"
        raw = payload_path.read_bytes()
        payload = json.loads(raw)
        native = json.loads((output / "serial/final.json").read_bytes())
        for evidence in (payload, native):
            if evidence.get("scratch_root"):
                self.addCleanup(shutil.rmtree, evidence["scratch_root"], True)
        published = json.loads((output / "observer.json").read_bytes())
        binding = {"path": str(payload_path), "sha256": hashlib.sha256(raw).hexdigest(),
                   "bytes": len(raw)}
        self.assertEqual("complete", native["status"])
        self.assertFalse(native["successful"])
        self.assertTrue(native["cleanup"]["successful"])
        self.assertEqual(9, native["aggregate"]["tests_run"])
        self.assertEqual(["test_cases.NativeSerialFailure.test_serial_failure"],
                         [event["test_id"] for event in native["aggregate"]["events"]
                          if event["kind"] == "failure"])
        self.assertEqual("invalid", payload["status"])
        self.assertFalse(payload["successful"])
        self.assertFalse(payload["qualified"])
        self.assertEqual(["serial"], [row["phase"] for row in payload["launches"]])
        self.assertTrue(payload["launches"][0]["collected"])
        self.assertEqual(1, payload["launches"][0]["exit_code"])
        self.assertFalse((output / "parallel").exists())
        self.assertFalse((output / "parallel.log").exists())
        for evidence in (observer, published):
            # This is the missing admission branch: real failed serial evidence
            # remains bound after natural payload collection, without a ready.
            self.assertEqual(binding, evidence.get("payload_artifact"))
            self.assertEqual("invalid", evidence["status"])
            self.assertFalse(evidence["successful"])
            self.assertFalse(evidence["qualified"])
            self.assertEqual(1, evidence["payload_exit_code"])
            self.assertEqual(payload["source"], evidence["source"])
            self.assertEqual(payload["source_after"], evidence["source_after"])
            self.assertEqual(payload["registry"], evidence["registry"])
            self.assertEqual(payload["registry_after"], evidence["registry_after"])
            self.assertIn("native phase status, source or cleanup could not qualify",
                          evidence.get("error", ""))
            self.assertNotIn("out-of-order", evidence.get("error", ""))
            self.assertNotIn("parallel_wall_seconds", evidence)
            self.assertNotIn("timing", evidence)
            self.assertEqual(600.0, evidence["parallel_limit_seconds"])
            self.assertTrue(evidence["cleanup"]["payload_collected"])
            self.assertFalse(evidence["cleanup"]["payload_native_cleanup_proved"])
            self.assertFalse(evidence["cleanup"]["successful"])
            self.assertTrue(evidence["cleanup"]["scratch_retained"])
            self.assertTrue(Path(evidence["scratch_root"]).is_dir())

    def test_real_success_pair_without_ready_cannot_enter_early_failure_admission(self):
        owner, _helper = self.fixture()
        helper_path = owner.root / "scripts/parallel_qualification.py"
        # Adversarial committed fixture source suppresses only the parallel
        # handshake hooks. Both native phases, outcomes, cleanup and the final
        # publication still happen for real. No passing receipt is fabricated.
        with helper_path.open("a") as stream:
            stream.write('''

_real_fixture_run_pair = run_pair
def run_pair(root, output, *, _supervision=None):
    if _supervision is None:
        raise AssertionError("owned adversarial fixture requires supervision")
    altered = dict(_supervision)
    altered["parallel_start"] = lambda: None
    altered["parallel_collected"] = lambda seconds: None
    return _real_fixture_run_pair(root, output, _supervision=altered)
''')
        owner.commit()
        specification = importlib.util.spec_from_file_location("owned_no_ready_fixture", helper_path)
        helper = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(helper)
        output = owner.bank / "pair"
        with owner.registry_patch(helper):
            observer = helper.supervise_pair(owner.root, output)
        if observer.get("scratch_root"):
            self.addCleanup(shutil.rmtree, observer["scratch_root"], True)
        # This checks eight actual native methods in each phase, native parity,
        # collected child exits and cleanup before examining the rejection.
        self.completed_payload(output)
        payload = json.loads((output / "qualification.json").read_bytes())
        self.assertTrue(payload["successful"])
        self.assertEqual("awaiting_external_gate", payload["status"])
        published = json.loads((output / "observer.json").read_bytes())
        for evidence in (observer, published):
            self.assertEqual("invalid", evidence["status"])
            self.assertFalse(evidence["successful"])
            self.assertFalse(evidence["qualified"])
            self.assertEqual(payload["source"], evidence["source"])
            self.assertNotIn("parallel_wall_seconds", evidence)
            self.assertNotIn("timing", evidence)
            self.assertEqual(600.0, evidence["parallel_limit_seconds"])
            self.assertTrue(evidence["cleanup"]["payload_collected"])
            self.assertFalse(evidence["cleanup"]["payload_native_cleanup_proved"])
            self.assertFalse(evidence["cleanup"]["successful"])
            self.assertTrue(evidence["cleanup"]["scratch_retained"])
            self.assertTrue(Path(evidence["scratch_root"]).is_dir())

    def test_native_parallel_failure_retains_external_interval_without_qualification(self):
        owner, helper = self.fixture()
        output = owner.bank / "pair"
        # This real method passes in the serial process, then fails in its
        # parallel process after the complete serial receipt has been saved.
        cases = owner.root / "test_cases.py"
        with cases.open("a") as stream:
            stream.write("\nfrom pathlib import Path\n"
                         "class NativeFailure(unittest.TestCase):\n"
                         " def test_serial_pass_parallel_fail(self):\n"
                         "  self.assertFalse(Path(%r).exists())\n" % str(output / "serial/final.json"))
        owner.commit()
        with owner.registry_patch(helper):
            observer = helper.supervise_pair(owner.root, output)
        if observer.get("scratch_root"):
            self.addCleanup(shutil.rmtree, observer["scratch_root"], True)
        published = json.loads((output / "observer.json").read_bytes())
        native = json.loads((output / "parallel/final.json").read_bytes())
        self.assertFalse(native["successful"])
        self.assertTrue(native["cleanup"]["successful"])
        for receipt in (observer, published):
            self.assertEqual("invalid", receipt["status"])
            self.assertFalse(receipt["successful"])
            self.assertFalse(receipt["qualified"])
            self.assertTrue(receipt["cleanup"]["payload_collected"])
            self.assertFalse(receipt["cleanup"]["payload_native_cleanup_proved"])
            self.assert_nonnegative_seconds(receipt.get("parallel_wall_seconds"))
            self.assertGreaterEqual(receipt["parallel_wall_seconds"], native["wall_seconds"])

    def test_committed_wrong_source_cannot_authorize_supervisor_scratch_cleanup(self):
        owner, _helper = self.fixture()
        helper_path = owner.root / "scripts/parallel_qualification.py"
        # The private payload still performs every native phase and the real
        # publication. Alter only the published document, never the receipt
        # object used for the final protocol status/cleanup hooks. The existing
        # published callback then commits the actual changed bytes/hash.
        with helper_path.open("a") as stream:
            stream.write('''

# Test-owned committed corruption control, not a native source observation.
_real_fixture_publish = _publish
def _publish(path, value):
    result = _real_fixture_publish(path, value)
    if path.name == "qualification.json":
        import copy as _fixture_copy
        import json as _fixture_json
        document = _fixture_copy.deepcopy(_fixture_json.loads(path.read_bytes()))
        document["source"]["digest"] = "0" * 64
        with path.open("w") as stream:
            _fixture_json.dump(document, stream, sort_keys=True)
            stream.write("\\n")
            stream.flush()
            os.fsync(stream.fileno())
    return result
''')
        owner.commit()
        specification = importlib.util.spec_from_file_location("owned_wrong_source_fixture", helper_path)
        helper = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(helper)
        output = owner.bank / "pair"
        with owner.registry_patch(helper):
            observer = helper.supervise_pair(owner.root, output)
        # Register owned scratch cleanup before any assertion, including the
        # repaired path which deliberately retains evidence on bad admission.
        if observer.get("scratch_root"):
            self.addCleanup(shutil.rmtree, observer["scratch_root"], True)
        payload_path = output / "qualification.json"
        raw = payload_path.read_bytes()
        payload = json.loads(raw)
        if payload.get("scratch_root"):
            self.addCleanup(shutil.rmtree, payload["scratch_root"], True)
        published = json.loads((output / "observer.json").read_bytes())
        self.assertEqual({"path": str(payload_path), "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": len(raw)}, observer["payload_artifact"])
        self.assertEqual("awaiting_external_gate", payload["status"])
        self.assertTrue(payload["successful"])
        self.assertTrue(payload["cleanup"]["successful"])
        self.assertTrue(all(row["collected"] and row["exit_code"] == 0 for row in payload["launches"]))
        serial = json.loads((output / "serial/final.json").read_bytes())
        parallel = json.loads((output / "parallel/final.json").read_bytes())
        for native in (serial, parallel):
            self.assertTrue(native["successful"])
            self.assertTrue(native["cleanup"]["successful"])
            self.assertEqual(8, native["aggregate"]["tests_run"])
        self.assertTrue(parallel["qualified"])
        self.assertTrue(parallel["comparison"]["equivalent"])
        self.assertEqual(serial["source"], parallel["source"])
        self.assertEqual(parallel["source"], payload["source_after"])
        expected_source = dict(parallel["source"], digest="0" * 64)
        self.assertNotEqual(parallel["source"]["digest"], expected_source["digest"])
        self.assertEqual(expected_source, payload["source"])
        for evidence in (observer, published):
            self.assertFalse(evidence["qualified"])
            self.assertEqual("invalid", evidence["status"])
            self.assertFalse(evidence["cleanup"]["payload_native_cleanup_proved"],
                             "unadmitted payload source authorized cleanup")
            self.assertFalse(evidence["cleanup"]["successful"])
            self.assertTrue(evidence["cleanup"]["scratch_retained"])
            self.assertTrue(Path(evidence["scratch_root"]).is_dir())

    def test_actual_sigint_after_parallel_method_starts_is_interrupted_and_collected(self):
        owner, helper = self.fixture()
        cases = '''import os, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent
class Base:
 def test_one(self):
  if (ROOT.parent / 'pair/serial/final.json').is_file():
   (ROOT.parent / 'parallel_started').touch()
   release = ROOT.parent / 'parallel_release'
   started = time.monotonic()
   deadline = started + 120
   while not release.exists() and time.monotonic() < deadline: time.sleep(.01)
   assert release.exists(), ('parallel fixture release: readiness bound=120s '
       'elapsed=%.3fs host_load=%r' % (time.monotonic()-started, os.getloadavg()))
'''
        for ordinal in range(8):
            cases += "class Case%d(Base, unittest.TestCase): pass\n" % ordinal
        (owner.root / "test_cases.py").write_text(cases)
        owner.commit()
        output = owner.bank / "pair"
        marker, release = owner.bank / "parallel_started", owner.bank / "parallel_release"
        stop = threading.Event()
        observed = {"parallel_marker": False, "signal_sent": False, "errors": []}
        test_pid = os.getpid()

        def cancel_actual_parallel():
            try:
                Readiness("parallel test method marker").until(
                    lambda: stop.is_set() or marker.is_file())
                if stop.is_set():
                    return
                observed["parallel_marker"] = True
                observed["serial_published"] = (output / "serial/final.json").is_file()
                # Signal only this test process. The real supervisor
                # handler must forward through its retained payload.
                os.kill(test_pid, signal.SIGINT)
                observed["signal_sent"] = True
                release.write_text("test-owned parallel methods may exit\n")
            except BaseException as error:
                observed["errors"].append(repr(error))
            finally:
                release.write_text("test-owned sender safety release\n")

        sender = threading.Thread(target=cancel_actual_parallel, name="owned-qualification-cancel")
        observer, escaped = None, None
        sender.start()
        try:
            with owner.registry_patch(helper):
                observer = helper.supervise_pair(owner.root, output)
        except BaseException as error:
            escaped = {"type": type(error).__name__, "detail": str(error)}
        finally:
            stop.set()
            release.write_text("test-owned final safety release\n")
            wait_join(sender, "test-owned cancellation sender exit")
            # These paths come only from this real, private fixture's payload
            # and observer. No unrelated process or directory is targeted.
            records = [observer] if isinstance(observer, dict) else []
            payload_path = output / "qualification.json"
            if payload_path.is_file():
                records.append(json.loads(payload_path.read_bytes()))
            for record in records:
                if record.get("scratch_root"):
                    self.addCleanup(shutil.rmtree, record["scratch_root"], True)
        self.assertFalse(sender.is_alive(), "test-owned cancellation sender was not joined")
        self.assertEqual([], observed["errors"])
        self.assertTrue(observed["parallel_marker"], "control never reached an actual parallel test method")
        self.assertTrue(observed.get("serial_published"), "control cancelled before serial reference publication")
        self.assertTrue(observed["signal_sent"])
        self.assertIsNone(escaped, "operator cancellation escaped the supervisor: " + repr(escaped))
        self.assertIsInstance(observer, dict)
        self.assertEqual("interrupted", observer["status"],
                         "actual operator cancellation was mislabeled as invalid evidence")
        self.assertFalse(observer["qualified"])
        self.assertFalse(observer["successful"])
        self.assertEqual(130, observer["exit_code"])
        self.assertTrue(observer["cleanup"]["payload_collected"])


if __name__ == "__main__":
    unittest.main()
