"""Real parent-ledger / real-child shared protocol tests, not run_exact success."""
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import inspect
import json
import multiprocessing
import os
import queue
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

from floati.adhoc_journal import AdhocJournal, AdhocJournalController, active_dispatch_references
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.root import FloatiRoot
from floati.seat_declaration import SeatDeclaration
from floati.worker_bootstrap_protocol import BootstrapChannel, NATIVE_THREAD_BOUNDS
from tests.fixtures.adhoc_stage1 import AdhocFixture
from tests.temp_roots import REAL_TEMP_ROOT
from tests.readiness import Readiness, wait_event, wait_join, wait_process
from tests.test_native_io import HARNESS, required_api

BINDING_FIELDS = ("profile_id", "profile_digest", "dispatch_id", "dispatch_digest",
                  "node_id", "node_lease_id", "work_item_id", "effect_binding")


def literal_native_context():
    """Wire validation needs closed data, not a live admitted parent."""
    token = "019f6ed3cc2870008000000000000001"
    base = Path(REAL_TEMP_ROOT) / "native-wire"
    return {"schema_version": 1,
        "profile_id": "adhoc-profile-" + token, "profile_digest": "1" * 64,
        "dispatch_id": "adhoc-dispatch-" + token, "dispatch_digest": "2" * 64,
        "node_id": "node-a", "node_lease_id": "lease-" + token,
        "work_item_id": "work-" + token, "operation": "start", "provider_coordinate": None,
        "workspace_binding": {"profile_id": "adhoc-profile-" + token,
            "lane_opening_id": "lane-workspace-" + token, "node_lease_id": "lease-" + token,
            "workspace": str(base / "workspace")},
        "provider_state_binding": {"path": str(base / "provider"), "dev": 1, "ino": 1},
        "permission": {"mode": "on_request_deny_unattended", "approval_policy": "on-request",
            "reviewer": "user", "sandbox": "workspace-write", "network": False},
        "bounds": dict(NATIVE_THREAD_BOUNDS),
        "effect_binding": {"run_id": "run-" + token, "item_id": "work-" + token,
                           "attempt_id": "attempt-" + token}}


def fixture_frame(record):
    """Independent hostile scratch framing; never a lawful product producer."""
    return json.dumps(record, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode() + b"\n"


@contextmanager
def fresh_native_fixture(owner):
    case = SynchronousNativeThreadProtocolTests("test_opened_record_preserves_admission_and_exclusive_owner")
    owner.addCleanup(case.doCleanups)
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def opened_contender(root_path, policy_path, context, event, now, start, results):
    """Construct all parent owners in this child; race the real journal guard."""
    from floati import adhoc_journal
    from floati.approvals import ApprovalLedger
    from floati.effects import EffectController, EffectLedger
    from floati.native_thread_protocol import NativeThreadProtocol
    from floati.policy import RepositoryPolicy
    from floati.runtruth import RunLedger
    try:
        root = FloatiRoot.open_direct_home(root_path)
        effects = EffectController(EffectLedger(root), RunLedger(root),
            RepositoryPolicy.load(policy_path), ApprovalLedger(root))
        journal = AdhocJournal(root)
        writer = AdhocJournalController(journal)
        protocol = NativeThreadProtocol(context, adapter_kind="codex",
            journal_controller=writer, effect_controller=effects, now=now)
        real_guard = adhoc_journal.car_acceptance_guard
        reached = False
        @contextmanager
        def at_writer_boundary(*args, **kwargs):
            nonlocal reached
            if not reached:
                reached = True
                results.put(("ready", os.getpid()))
                wait_event(start, "contender start barrier")
            with real_guard(*args, **kwargs) as value:
                yield value
        with mock.patch.object(adhoc_journal, "car_acceptance_guard", at_writer_boundary):
            ack = protocol.accept(event, now=now)
        results.put(("ack", ack))
    except ProtocolRefusal as exc:
        results.put(("refused", exc.code))
    except BaseException as exc:
        results.put(("unexpected", type(exc).__name__, str(exc)[:256]))


class _NativeThreadProtocolFixture(AdhocFixture):
    """Shared helpers only; concrete classes own disjoint test methods."""

    def setUp(self):
        if self._testMethodName in {
            "test_wire_resume_identity_and_bounded_metadata",
            "test_claude_terminal_only_wire_validation_never_invents_an_open_ack",
        }:
            self.context = literal_native_context()
            self.base = Path(REAL_TEMP_ROOT) / "native-wire"
            self.coordinate = {"provider": "codex_local",
                "session_id": "019f6ed3-cc28-7000-8000-000000000001", "session_file": None}
            return
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        root = FloatiRoot.open_direct_home(Path(temporary.name).resolve() / "alpha", create=True)
        super().setUp(root=root)
        self.registry.register("node-a", "Codex")
        lease = self.add_lease("node-a")
        SeatDeclaration.create(self.root.path / "nodes/node-a", "node-a", self.root,
                               self.registry.governance())
        opened = self.lanes.open(actor="node-a", row="borrowed", repo="fixture")
        AuthorityGrantStore(self.root).grant_exact("borrowed-work", "node-a", 1, self.now)
        profile = self.profiles().create("node-a", "codex", opened["record"]["id"],
            "fixture", "coordinator", idempotency_key="node-a-profile", now=self.now)
        requested = self.work.add("native shared protocol fixture", "node-a", [],
            adhoc_profile_id=profile["id"], now=self.now)
        dispatch = self.dispatch(profile, node_id="node-a", node_lease_id=lease["id"],
            lane_opening_id=profile["lane_opening_id"], work_item_id=requested["id"],
            authority_subject="borrowed-work")
        with self.launch_sender():
            envelope = self.envelope(dispatch, recipient="node-a")
        self.journal = AdhocJournal(self.root)
        self.writer = AdhocJournalController(self.journal)
        self.admitted = self.writer.admit(profile["id"], envelope["id"], dispatch,
            actor="node-a", acting_session_id="fixture-parent",
            idempotency_key="node-a-dispatch", now=self.now)
        self.effects, effect_binding = self.effect_fixture(requested["id"])
        self.context = {"schema_version": 1, "profile_id": profile["id"],
            "profile_digest": profile["digest"], "dispatch_id": self.admitted["dispatch_id"],
            "dispatch_digest": self.admitted["dispatch_digest"], "node_id": "node-a",
            "node_lease_id": lease["id"], "work_item_id": requested["id"],
            "operation": "start", "provider_coordinate": None,
            "workspace_binding": deepcopy(requested["adhoc_binding"]),
            "provider_state_binding": {"path": str(self.root.tenant_home / profile["provider_state"]),
                **profile["filesystem_bindings"]["provider_state"]},
            "permission": deepcopy(dispatch["permission"]),
            "bounds": dict(NATIVE_THREAD_BOUNDS), "effect_binding": effect_binding}
        self.profile_record = profile
        self.requested = requested
        self.dispatch_record = dispatch
        self.envelope_record = envelope
        self.lease_record = lease
        self.coordinate = {"provider": "codex_local",
                           "session_id": "019f6ed3-cc28-7000-8000-000000000001",
                           "session_file": None}

    @contextmanager
    def launch_sender(self):
        authenticated = self._testMethodName == "test_confluence_attributes_only_authenticated_native_launches"
        if not authenticated and self._testMethodName != "test_confluence_marks_a_launch_without_sender_testimony_incomplete":
            yield
            return
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant,
        )
        from floati.waiter_consent import arm_waiter_consent

        self.launcher_session = "019f6ed3-cc28-7000-8000-000000000002"
        workspace = self.root.path / "nodes/coordinator"
        SeatDeclaration.create(workspace, "coordinator", self.root, self.registry.governance())
        arm_waiter_consent(self.root.path, workspace, "coordinator", "Codex", 1800, 1700)
        binding = resolve_participant(self.root.path, workspace).binding
        consent = CodexWaitConsentLedger(self.root).require_armed(binding)
        CodexWaitSessionLedger(self.root).arm(binding, consent, self.launcher_session,
            idempotency_key="launcher-session")
        self.now = datetime.now(timezone.utc)
        runtime = self.launcher_session if authenticated else "019f6ed3-cc28-7000-8000-000000000003"
        with mock.patch.object(Path, "cwd", return_value=workspace), mock.patch.dict(
            os.environ, {"CODEX_THREAD_ID": runtime}
        ):
            yield

    def controller(self, context=None):
        api = required_api(self, "floati.native_thread_protocol", "NativeThreadProtocol")
        return api(self.context if context is None else context, adapter_kind="codex",
                   journal_controller=self.writer, effect_controller=self.effects,
                   now=self.now)

    def event(self, kind="opened", coordinate=None):
        result = {"schema_version": 1, "event": kind,
                  "binding": {key: deepcopy(self.context[key]) for key in BINDING_FIELDS},
                  "provider_coordinate": deepcopy(self.coordinate if coordinate is None else coordinate)}
        if kind == "terminal":
            result["outcome"] = "success"
        return result

    def replay(self, *, writer=None, key="node-a-dispatch"):
        return (self.writer if writer is None else writer).admit(self.profile_record["id"],
            self.envelope_record["id"], self.dispatch_record, actor="node-a",
            acting_session_id="fixture-parent", idempotency_key=key, now=self.now)

    def _assert_authentication_to_append_transition_is_serialized(self, transition):
        """A real writer either wins before authentication or follows durable open.

        Pause at the actual authentication return, then let a second thread
        enter the canonical authority/scheduler writer. A retained owner fence
        may lawfully make opened testimony win. If the writer wins first, no
        stale authenticated opened record may subsequently be accepted.
        """
        from floati.scheduler import RunScheduler
        protocol = self.controller()
        authenticate = protocol._authenticate
        attempted, finished = threading.Event(), threading.Event()
        observations, failures = [], []
        path = self.root.tenant_home / self.journal.relative_path
        before = path.read_bytes()
        def mutate():
            try:
                attempted.set()
                if transition == "authority":
                    AuthorityGrantStore(self.root).revoke_exact("borrowed-work", "node-a", 1, self.now)
                else:
                    binding = self.context["effect_binding"]
                    RunScheduler(self.effects.run_ledger).terminal_attempt(
                        binding["run_id"], binding["item_id"], binding["attempt_id"],
                        "failed", "permanent", "malformed_evidence", "idempotent", now=self.now)
                # This is actual parent-ledger state after the competing write,
                # rather than an invented ordering receipt or a mocked result.
                rows = AdhocJournal(self.root).records()
                observations.append(any(row["kind"] == "adhoc_native_thread_opened_record" for row in rows))
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()
        contender = threading.Thread(target=mutate)
        def cleanup():
            if contender.ident is not None:
                wait_join(contender, "collect the canonical transition writer")
            self.assertFalse(contender.is_alive(), "canonical transition writer outlived the fixture")
        self.addCleanup(cleanup)
        visits = []
        def at_authenticated_boundary(now):
            result = authenticate(now)
            visits.append("authenticated")
            if contender.ident is None:
                contender.start()
                wait_event(attempted, "canonical competing writer starts")
                # A proper retained fence blocks the writer here. An unprotected
                # source lets the real transition finish before append is released.
                finished.wait(0.2)
            return result
        ack, refusal = None, None
        with mock.patch.object(protocol, "_authenticate", side_effect=at_authenticated_boundary):
            try:
                ack = protocol.accept(self.event(), now=self.now)
            except ProtocolRefusal as exc:
                refusal = exc
        wait_join(contender, "canonical transition writer exits after acceptance")
        self.assertFalse(contender.is_alive(), "competing writer remained blocked after accept returned")
        self.assertTrue(visits, "test must reach the actual authentication boundary")
        self.assertEqual([], failures, failures)
        self.assertEqual(1, len(observations), "real transition did not complete")
        if observations[0]:
            # Opened testimony committed first; the later transition is legal.
            self.assertIsNotNone(ack)
            self.assertIsNone(refusal)
            self.assertEqual("opened_ack", ack["event"])
            self.assertEqual(2, len(AdhocJournal(self.root).records()))
        else:
            # Revocation/terminal transition committed first; accepting the
            # earlier sampled authority would create false durable testimony.
            self.assertIsNone(ack, "opened ACK accepted authority invalidated before append")
            self.assertIsNotNone(refusal, "invalidated authentication was not refused")
            expected = "adhoc_dispatch_authentication_failed" if transition == "authority" else "effect_attempt_terminal"
            self.assertEqual(expected, refusal.code)
            self.assertEqual(before, path.read_bytes())

    def launch_opened_child(self, duration=None, hold_after_prompt=False, saturate_channel=False):
        if duration is None:
            duration = 60.0
        readiness = Readiness("native emitter child clock handshake", timeout=duration)
        required_api(self, "floati.native_thread_protocol", "NativeThreadEmitter")
        parent, child = socket.socketpair()
        channel = BootstrapChannel(parent.detach())
        self.addCleanup(channel.close)
        process = None
        def reap():
            if process is None:
                return
            try:
                # The opened fixture creates no descendants. Control only its
                # owned Popen child, and collect that child's actual exit.
                if process.poll() is None:
                    process.terminate()
                try:
                    wait_process(process, "opened emitter fixture exits")
                except AssertionError:
                    process.kill()
                    wait_process(process, "collect killed opened emitter fixture")
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
        # Keep the original pre-spawn deadline: explicit durations exercise
        # one shared product budget. Default framing controls get 60 seconds.
        parent_started = time.monotonic()
        deadline = parent_started + duration
        try:
            process = subprocess.Popen([sys.executable, "-I", "-B", str(HARNESS), "opened",
                                        str(Path(__file__).resolve().parents[1]), str(child.fileno())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(child.fileno(),), start_new_session=True)
            self.addCleanup(reap)  # Before any config write or other fallible step.
        except BaseException:
            channel.close()
            raise
        finally:
            child.close()
        clock_path = self.base / ("native-clock-" + str(process.pid) + ".json")
        process.fixture_clock_path = clock_path
        metadata = {"schema_version": 1, "pid": process.pid,
            "parent_monotonic_before_spawn": parent_started, "parent_original_deadline": deadline,
            "readiness_bound": readiness.bound}
        with clock_path.open("x") as output:
            json.dump(metadata, output, sort_keys=True)
        raw = json.dumps({"context": self.context,
            "coordinate": self.coordinate,
            "handshake_budget": max(0, deadline - time.monotonic()),
            "hold_after_prompt": hold_after_prompt,
            "saturate_channel": saturate_channel}).encode() + b"\n"
        # Fixed small fixture configuration, not provider output/production transport.
        self.assertLessEqual(len(raw), 4096)
        process.stdin.write(raw)
        process.stdin.flush()
        process.stdin.close()
        try:
            verb, clock = channel.recv(timeout=min(readiness.remaining(), max(0, deadline - time.monotonic())))
        except Exception as error:
            raise readiness.failure() from error
        self.assertEqual("fixture_clock", verb)
        self.assertEqual({"schema_version", "monotonic"}, set(clock))
        self.assertEqual(1, clock["schema_version"])
        child_sample = clock["monotonic"]
        self.assertIs(type(child_sample), float)
        parent_now = time.monotonic()
        remaining = deadline - parent_now
        translated = child_sample + remaining
        metadata.update(parent_monotonic_received=parent_now, child_monotonic_sample=child_sample,
            remaining_parent_budget=remaining, child_local_deadline=translated,
            parent_minus_child_monotonic=parent_now - child_sample)
        clock_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        self.assertGreater(remaining, 0, metadata)
        # The child sample precedes this parent sample: using the already
        # consumed parent budget conservatively excludes handshake latency.
        channel.send(("fixture_clock_ack", {"schema_version": 1, "child_deadline": translated}),
                     deadline=deadline)
        return process, channel, deadline

    def observed_child_output(self, process, deadline):
        pending = {process.stdout.fileno(): "stdout", process.stderr.fileno(): "stderr"}
        data = {"stdout": bytearray(), "stderr": bytearray()}
        # Drain both pipes while the real child runs. Waiting before reading
        # could hide an uncaught traceback blocked on a full diagnostic pipe.
        # Keep the original child-deadline-plus-one-second exit allowance.
        observation_deadline = max(time.monotonic() + 0.001, deadline) + 1
        def diagnostic():
            result = {"pid": process.pid, "exit_code": process.poll(),
                      **{key: bytes(value) for key, value in data.items()}}
            if hasattr(process, "fixture_clock_path"):
                result["clock_metadata_path"] = str(process.fixture_clock_path)
                result["clock_metadata"] = json.loads(process.fixture_clock_path.read_text())
            return result
        while pending:
            remaining = observation_deadline - time.monotonic()
            self.assertGreater(remaining, 0, diagnostic())
            ready, _, _ = select.select(list(pending), [], [], remaining)
            self.assertTrue(ready, diagnostic())
            for fd in ready:
                chunk = os.read(fd, 512)
                if not chunk:
                    del pending[fd]
                    continue
                target = data[pending[fd]]
                target.extend(chunk)
                self.assertLessEqual(len(target), 4096, diagnostic())
        try:
            process.wait(timeout=max(0.001, observation_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.fail("fixture process did not exit after closing both pipes: " + repr(diagnostic()))
        return {key: bytes(value) for key, value in data.items()}

    def assert_emitter_failure(self, process, deadline, code):
        data = self.observed_child_output(process, deadline)
        self.assertEqual(42, process.returncode, data)
        self.assertEqual(b"", data["stdout"], data)
        self.assertEqual({"fixture_failure": {"type": "WorkerAdapterFailure", "code": code}},
                         json.loads(data["stderr"]))

    def ready_opened_frame(self, channel, deadline):
        readiness = Readiness("native emitter publishes opened frame",
                              timeout=max(0, deadline - time.monotonic()))
        try:
            return channel.recv(timeout=readiness.remaining())
        except Exception as error:
            raise readiness.failure() from error



class NativeThreadProtocolTests(_NativeThreadProtocolFixture, unittest.TestCase):
    """Real contention and transport deadline controls remain exclusive."""

    def test_two_real_parent_contenders_have_one_atomic_opened_winner(self):
        required_api(self, "floati.native_thread_protocol", "NativeThreadProtocol")
        self.assertIn("fork", multiprocessing.get_all_start_methods(), "registered host requires POSIX fork")
        context = multiprocessing.get_context("fork")
        start, results = context.Event(), context.Queue()
        other = {**self.coordinate, "session_id": "019f6ed3-cc28-7000-8000-000000000002"}
        parents = [context.Process(target=opened_contender, args=(str(self.root.path),
            str(self.base / "FLOATI.toml"), self.context, self.event(coordinate=coordinate),
            self.now, start, results)) for coordinate in (self.coordinate, other)]
        def cleanup():
            start.set()
            for child in parents:
                if child.pid is not None:
                    try:
                        wait_join(child, "collect the opened contender")
                    except AssertionError:
                        child.terminate()
                        try:
                            wait_join(child, "collect terminated opened contender")
                        except AssertionError:
                            child.kill()
                            wait_join(child, "collect killed opened contender")
            results.close()
            results.join_thread()
        self.addCleanup(cleanup)
        path = self.root.tenant_home / self.journal.relative_path
        original = path.read_bytes()
        foreign = self.tree_bytes(Path(self.foreign_opened["workspace"]))
        for child in parents:
            child.start()
        def next_result(description):
            readiness = Readiness(description)
            try:
                return results.get(timeout=readiness.remaining())
            except queue.Empty:
                raise readiness.failure() from None
        ready = [next_result("first contender ready"), next_result("second contender ready")]
        self.assertEqual(["ready", "ready"], [row[0] for row in ready], ready)
        self.assertEqual(2, len({row[1] for row in ready}))
        start.set()
        outcomes = [next_result("first contender outcome"), next_result("second contender outcome")]
        winners = [row[1] for row in outcomes if row[0] == "ack"]
        self.assertEqual(1, len(winners), outcomes)
        self.assertEqual([("refused", "native_session_mismatch")],
                         [row for row in outcomes if row[0] != "ack"])
        for child in parents:
            wait_join(child, "native opened contender exits after publication")
            self.assertEqual(0, child.exitcode)
        rows = AdhocJournal(self.root).records()
        self.assertEqual(2, len(rows))
        self.assertEqual(winners[0]["opened_record_id"], rows[-1]["id"])
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertEqual(foreign, self.tree_bytes(Path(self.foreign_opened["workspace"])))
        self.assertEqual([self.context["dispatch_id"]],
                         [row["dispatch_id"] for row in active_dispatch_references(self.root)])

    def test_authority_transition_between_authentication_and_open_append_is_serialized(self):
        self._assert_authentication_to_append_transition_is_serialized("authority")

    def test_run_transition_between_authentication_and_open_append_is_serialized(self):
        self._assert_authentication_to_append_transition_is_serialized("run")

    def test_opened_identity_waits_for_durable_parent_ack(self):
        controller = self.controller()
        process, channel, deadline = self.launch_opened_child()
        verb, event = self.ready_opened_frame(channel, deadline)
        self.assertEqual("native_thread", verb)
        self.assertEqual([], select.select([process.stdout], [], [], 0.1)[0])
        self.assertEqual(1, len(AdhocJournal(self.root).records()))
        ack = controller.accept(event, now=self.now)
        # Fresh read of the actual journal must precede the socket ACK.
        persisted = AdhocJournal(self.root).records()[-1]
        self.assertEqual(ack["opened_record_id"], persisted["id"])
        self.assertEqual(self.coordinate, persisted["evidence"]["provider_coordinate"])
        self.assertEqual([], select.select([process.stdout], [], [], 0.1)[0])
        channel.send(("native_thread_ack", ack), deadline=deadline)
        data = self.observed_child_output(process, deadline)
        self.assertEqual(0, process.returncode, data)
        self.assertEqual(b"", data["stderr"])
        self.assertEqual({"prompt_sent": True}, json.loads(data["stdout"]))

    def test_no_ack_exhausts_original_deadline_without_prompt(self):
        self.controller()
        process, channel, deadline = self.launch_opened_child(duration=3)
        self.assertEqual("native_thread", channel.recv(timeout=max(0, deadline - time.monotonic()))[0])
        self.assert_emitter_failure(process, deadline, "process_timeout")
        self.assertLess(time.monotonic(), deadline + 1.5)
        self.assertEqual([self.admitted], self.journal.records())

    def test_substituted_ack_cannot_release_prompt(self):
        controller = self.controller()
        lawful = controller.accept(self.event(), now=self.now)
        for variant, code in (("binding", "native_session_mismatch"),
                              ("coordinate", "native_session_mismatch"),
                              ("record-id", "protocol_error"),
                              ("extra-field", "protocol_error"),
                              ("eof", "protocol_error")):
            with self.subTest(variant=variant):
                process, channel, deadline = self.launch_opened_child()
                verb, event = self.ready_opened_frame(channel, deadline)
                self.assertEqual("native_thread", verb)
                self.assertEqual(self.event(), event)
                ack = deepcopy(lawful)
                if variant == "binding":
                    ack["binding"]["dispatch_id"] = "adhoc-dispatch-" + uuid7_hex()
                elif variant == "coordinate":
                    ack["provider_coordinate"]["session_id"] = "019f6ed3-cc28-7000-8000-000000000002"
                elif variant == "record-id":
                    ack["opened_record_id"] = "not-a-record-id"
                elif variant == "extra-field":
                    ack["unreviewed"] = True
                if variant == "eof":
                    channel.close()
                else:
                    channel.send(("native_thread_ack", ack), deadline=deadline)
                self.assert_emitter_failure(process, deadline, code)

    def test_one_absolute_deadline_covers_ack_wait_and_final_drain(self):
        from floati.worker_errors import WorkerAdapterFailure
        io_api = required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        controller = self.controller()
        process, channel, deadline = self.launch_opened_child(duration=3, hold_after_prompt=True)
        verb, event = channel.recv(timeout=max(0, deadline - time.monotonic()))
        self.assertEqual("native_thread", verb)
        self.assertEqual([], select.select([process.stdout], [], [], 0.15)[0])
        ack = controller.accept(event, now=self.now)
        channel.send(("native_thread_ack", ack), deadline=deadline)
        reader = io_api(process, deadline=deadline, bounds=dict(NATIVE_THREAD_BOUNDS))
        self.assertEqual({"prompt_sent": True}, reader.read_record())
        with self.assertRaises(WorkerAdapterFailure) as caught:
            reader.drain_to_exit()
        self.assertEqual("process_timeout", caught.exception.code)
        self.assertLess(time.monotonic(), deadline + 1)

    def test_emitter_backpressure_failure_does_not_retry_unbounded_reporting(self):
        process, _channel, deadline = self.launch_opened_child(duration=1.5, saturate_channel=True)
        # Peer intentionally neither reads nor ACKs: real child send must time out.
        data = self.observed_child_output(process, deadline)
        self.assertEqual(42, process.returncode, data)
        self.assertEqual(b"", data["stdout"])
        self.assertEqual({"fixture_failure": {"type": "WorkerAdapterFailure",
                                              "code": "process_timeout"},
                          "transport_prefilled": True}, json.loads(data["stderr"]))
        self.assertLess(time.monotonic(), deadline + 1)



class SynchronousNativeThreadProtocolTests(_NativeThreadProtocolFixture, unittest.TestCase):
    """Private-root synchronous protocol cases without real timing budgets."""

    def test_confluence_attributes_only_authenticated_native_launches(self):
        from floati.confluence import ConfluenceGrantLedger, materialize_bundle
        from floati.framing import encode_frame
        from floati.jsonl import _append_frame
        from tests.schema_validation import validate_json_schema

        envelope = self.envelope_record
        replay = self.events.send(envelope["sender"], envelope["recipient"],
            envelope["repo"], envelope["sha"], envelope["doc"], envelope["note"],
            idempotency_key=envelope["idempotency_key"])
        self.assertEqual(envelope["id"], replay["id"])
        ack = self.controller().accept(self.event(), now=self.now)
        ConfluenceGrantLedger(self.root).grant("puddle", "launch-projection")
        output = materialize_bundle(self.root, consumer="puddle",
                                    out=self.base / "receipts-read.json", version=2)
        sidecar = output.with_name("codex-launches.json")
        self.assertTrue(sidecar.is_file(), "Confluence must publish native launch testimony")
        projection = json.loads(sidecar.read_text())
        schema = Path(__file__).resolve().parents[1] / "schemas/v0/codex-headless-launch-projection.schema.json"
        validate_json_schema(projection, schema)
        record, = projection["records"]
        launcher, = [row for row in self.events.event_records()
                     if row["kind"] == "native_dispatch_launcher_record"]
        self.refuses("native_dispatch_launcher_controller_only", lambda: _append_frame(
            self.root.tenant_home / "events.jsonl", encode_frame(launcher)))
        self.assertEqual({
            "schema_version": 0, "kind": "codex_headless_launch_projection",
            "complete": True, "records": [record],
        }, projection)
        self.assertEqual({
            "schema_version": 0, "kind": "codex_headless_launch",
            "id": ack["opened_record_id"],
            "dispatch_message_id": self.envelope_record["id"],
            "launcher_record_id": launcher["id"],
            "launcher_node_id": "coordinator",
            "launcher_provider": "codex",
            "launcher_session_id": self.launcher_session,
            "child_session_id": self.coordinate["session_id"],
            "cwd": self.profile_record["workspace"],
            "launch_requested_at": self.admitted["timestamp"],
            "launch_observed_at": self.admitted["timestamp"],
        }, record)

    def test_confluence_directory_export_preserves_private_matching_files(self):
        from floati.confluence import ConfluenceGrantLedger, materialize_bundle
        from floati.root import FloatiRoot

        self.controller().accept(self.event(), now=self.now)
        ConfluenceGrantLedger(self.root).grant("puddle", "source-fleet")
        output = self.base / "receipts-read.json"
        sidecar = output.with_name("codex-launches.json")
        previous_umask = os.umask(0o022)
        try:
            materialize_bundle(self.root, consumer="puddle", out=output, version=2)
        finally:
            os.umask(previous_umask)
        for path in (output, sidecar):
            with self.subTest(path=path.name):
                self.assertEqual(0o600, path.stat().st_mode & 0o777)

        before = {path: path.read_bytes() for path in (output, sidecar)}
        second = FloatiRoot.open_direct_home(self.base / "second-fleet", create=True)
        ConfluenceGrantLedger(second).grant("puddle", "replacement-fleet")
        write_text, replace = Path.write_text, os.replace

        def fail_primary_write(path, *args, **kwargs):
            if path == output:
                raise PermissionError("primary publication failed")
            return write_text(path, *args, **kwargs)

        def fail_primary_replace(source, target, *args, **kwargs):
            if Path(target) == output:
                raise PermissionError("primary publication failed")
            return replace(source, target, *args, **kwargs)

        with mock.patch.object(Path, "write_text", fail_primary_write),\
                mock.patch.object(os, "replace", fail_primary_replace):
            with self.assertRaises(PermissionError):
                materialize_bundle(second, consumer="puddle", out=output, version=2)
        self.assertEqual(before[output], output.read_bytes())
        self.assertEqual(before[sidecar], sidecar.read_bytes())

        def fail_sidecar_replace(source, target, *args, **kwargs):
            if Path(target) == sidecar:
                raise PermissionError("sidecar publication failed")
            return replace(source, target, *args, **kwargs)

        with mock.patch.object(os, "replace", fail_sidecar_replace):
            with self.assertRaises(PermissionError):
                materialize_bundle(second, consumer="puddle", out=output, version=2)
        self.assertEqual(second.tenant_id, json.loads(output.read_bytes())["tenant_id"])
        self.assertFalse(sidecar.exists())

    def test_confluence_marks_a_launch_without_sender_testimony_incomplete(self):
        from floati.confluence import ConfluenceGrantLedger, materialize_bundle

        self.controller().accept(self.event(), now=self.now)
        ConfluenceGrantLedger(self.root).grant("puddle", "launch-projection")
        output = materialize_bundle(self.root, consumer="puddle",
                                    out=self.base / "receipts-read.json", version=2)
        projection = json.loads(output.with_name("codex-launches.json").read_text())
        self.assertEqual({
            "schema_version": 0, "kind": "codex_headless_launch_projection",
            "complete": False, "records": [],
        }, projection)

    def test_historical_admission_replay_schema_and_active_exclusion(self):
        from floati.records import validate_record
        path = self.root.tenant_home / self.journal.relative_path
        original_frame = path.read_bytes()
        controller = self.controller()
        controller.accept(self.event(), now=self.now)
        for phase in ("opened", "terminal"):
            if phase == "terminal":
                self.assertEqual("terminal_pending_cleanup",
                                 controller.accept(self.event("terminal"), now=self.now)["state"])
            retained = path.read_bytes()
            self.assertTrue(retained.startswith(original_frame))
            self.assertEqual(self.admitted, self.replay())
            self.refuses("adhoc_dispatch_busy", lambda: self.replay(key="different-valid-key"))
            self.assertEqual(retained, path.read_bytes())
            self.assertEqual(2, len(AdhocJournal(self.root).records()))
        for update in ({"phase": "provider_opened"}, {"provider_coordinate": self.coordinate}):
            row = {**deepcopy(self.admitted), **update}
            with self.subTest(update=update), self.assertRaises(ProtocolRefusal) as caught:
                validate_record(row, self.root.tenant_id, {"adhoc_dispatch_record"}, integrity=False)
            self.assertEqual("adhoc_dispatch_authentication_failed", caught.exception.code)
        second = self.profiles().create("node-a", "codex", self.profile_record["lane_opening_id"],
            "fixture", "coordinator", idempotency_key="same-row-new-profile", now=self.now)
        item = self.work.add("second profile same physical row", "node-a", [],
            adhoc_profile_id=second["id"], now=self.now)
        dispatch = self.dispatch(second, node_id="node-a", node_lease_id=self.lease_record["id"],
            lane_opening_id=second["lane_opening_id"], work_item_id=item["id"],
            authority_subject="borrowed-work")
        envelope = self.envelope(dispatch, name="same-row-new-profile", recipient="node-a")
        retained = path.read_bytes()
        self.refuses("adhoc_dispatch_busy", lambda:
            self.writer.admit(second["id"], envelope["id"], dispatch, actor="node-a",
                acting_session_id="fixture-parent", idempotency_key="same-row-new-key", now=self.now))
        self.assertEqual(retained, path.read_bytes())

    def test_opened_kind_rejects_generic_forged_and_replacement_writers(self):
        from floati import jsonl
        self.controller().accept(self.event(), now=self.now)
        row = self.journal.records()[-1]
        path = self.root.tenant_home / self.journal.relative_path
        retained = path.read_bytes()
        kinds = {row["kind"]}
        for relative in (self.journal.relative_path, Path("scratch/foreign-opened.jsonl")):
            routes = (
                lambda: jsonl.append_record(self.root, relative, row, allowed_kinds=kinds),
                lambda: jsonl.transact(self.root, relative, lambda _rows: (row, row), allowed_kinds=kinds),
                lambda: jsonl.transact_records(self.root, relative, lambda _rows: (row, [row]), allowed_kinds=kinds),
                lambda: jsonl.transact_exact_frame(self.root, relative,
                    lambda _rows, _lines: (row, row), allowed_kinds=kinds),
                lambda: jsonl._append_frame(self.root.tenant_home / relative, fixture_frame(row)),
            )
            for index, route in enumerate(routes):
                with self.subTest(relative=str(relative), route=index):
                    self.refuses("adhoc_dispatch_authentication_failed", route)
                    self.assertEqual(retained, path.read_bytes())
            # A replacement API must not admit these coordinates either.
            selected = [("events.jsonl", self.root.tenant_home / "events.jsonl", "events", None),
                        (str(relative), self.root.tenant_home / relative, relative.parts[-1], None)]
            self.refuses("epoch_selected_member_changed", lambda:
                jsonl._replace_epoch_selected(self.root, selected, fixture_frame(row)))
        self.assertFalse((self.root.tenant_home / "scratch/foreign-opened.jsonl").exists())
        from floati.bus_epoch import epoch_guard, _preflight_selected_members
        # Existing selected writer has real path/epoch ownership here; the new
        # truth kind must still not be injected into an otherwise permitted path.
        all_before = self.tree_bytes()
        with epoch_guard(self.root, exclusive=True):
            selected = _preflight_selected_members(self.root)
            self.refuses("adhoc_dispatch_authentication_failed", lambda:
                jsonl._replace_epoch_selected(self.root, selected, fixture_frame(row)))
        self.assertEqual(all_before, self.tree_bytes())
        for route in (
            lambda: self.journal._capability_for(object()),
            lambda: self.journal._append(object(), self.context["dispatch_id"], "provider_opened", {}),
            lambda: jsonl._transact_adhoc_records(self.root, lambda _rows: (row, row)),
            lambda: self.journal._transact_admission(lambda _rows: (row, row)),
        ):
            self.refuses("adhoc_dispatch_authentication_failed", route)
        private = required_api(self, "floati.adhoc_journal", "AdhocJournal")
        self.assertTrue(hasattr(private, "_transact_provider_opened"), "API ABSENT: pinned opened producer")
        self.refuses("adhoc_dispatch_authentication_failed", lambda:
            self.journal._transact_provider_opened(lambda _rows: (row, row)))
        self.assertEqual(retained, path.read_bytes())

    def test_read_family_never_becomes_another_producers_append_authority(self):
        from floati import jsonl
        variants = (("admission", "opened"), ("opened", "admission"), ("opened", "profile"))
        for index, (route, replacement) in enumerate(variants):
            fixture = nullcontext(self) if index == 0 else fresh_native_fixture(self)
            with self.subTest(route=route, replacement=replacement), fixture as case:
                protocol = case.controller()
                if route == "admission":
                    protocol.accept(case.event(), now=case.now)
                    alien = deepcopy(case.journal.records()[-1])
                    alien["id"] = "adhoc-native-opened-" + uuid7_hex()
                    profile = case.profile()
                    dispatch = case.dispatch(profile)
                    envelope = case.envelope(dispatch, name="other-row-admission")
                    action = lambda: case.writer.admit(profile["id"], envelope["id"], dispatch,
                        actor="temporary", acting_session_id="fixture-parent",
                        idempotency_key="other-row-admission", now=case.now)
                    source_kind = "adhoc_dispatch_record"
                else:
                    alien = deepcopy(case.admitted if replacement == "admission" else case.profile_record)
                    prefix = "adhoc-journal-" if replacement == "admission" else "adhoc-profile-"
                    alien["id"] = prefix + uuid7_hex()
                    action = lambda: protocol.accept(case.event(), now=case.now)
                    source_kind = "adhoc_native_thread_opened_record"
                original_encode = jsonl._encode_record
                seen = []
                def hostile_encoding(record, *args, **kwargs):
                    if record["kind"] == source_kind:
                        seen.append(record["kind"])
                        return fixture_frame(alien)
                    return original_encode(record, *args, **kwargs)
                path = case.root.tenant_home / case.journal.relative_path
                before = path.read_bytes()
                with mock.patch.object(jsonl, "_encode_record", hostile_encoding):
                    case.refuses("adhoc_dispatch_authentication_failed", action)
                self.assertEqual([source_kind], seen, "attack must reach actual producer encoding boundary")
                self.assertEqual(before, path.read_bytes())

    def test_hostile_opened_projection_is_malformed_without_repair_or_write(self):
        self.controller().accept(self.event(), now=self.now)
        lawful = self.journal.records()
        path = self.root.tenant_home / self.journal.relative_path
        original = path.read_bytes()
        for variant in ("missing-admission", "foreign-admission", "duplicate", "conflicting"):
            rows = deepcopy(lawful)
            if variant == "missing-admission":
                rows = rows[1:]
            elif variant == "foreign-admission":
                rows[-1]["admission_id"] = "adhoc-journal-" + uuid7_hex()
            else:
                extra = deepcopy(rows[-1])
                extra["id"] = "adhoc-native-opened-" + uuid7_hex()
                if variant == "conflicting":
                    extra["evidence"]["provider_coordinate"]["session_id"] = "019f6ed3-cc28-7000-8000-000000000002"
                rows.append(extra)
            hostile = b"".join(fixture_frame(row) for row in rows)
            path.write_bytes(hostile)  # Explicit hostile scratch bytes, not a lawful producer.
            try:
                fresh = AdhocJournal(self.root)
                writer = AdhocJournalController(fresh)
                for action in (lambda: fresh.show(self.context["dispatch_id"]),
                               lambda: active_dispatch_references(self.root),
                               lambda: self.replay(writer=writer)):
                    with self.subTest(variant=variant), self.assertRaises(IntegrityFailure) as caught:
                        action()
                    self.assertEqual("adhoc_dispatch_authentication_failed", caught.exception.code)
                    self.assertEqual(hostile, path.read_bytes())
            finally:
                path.write_bytes(original)

    def test_accept_reauthenticates_changed_live_parent_owners(self):
        from floati.runtruth import RunLedger
        from floati.scheduler import RunScheduler
        from tests.test_adhoc_journal import AdhocJournalTests
        variants = (
            ("lease-expired", "adhoc_lease_invalid"),
            ("lease-replaced", "adhoc_lease_invalid"),
            ("authority-revoked", "adhoc_dispatch_authentication_failed"),
            ("provider-slot", "adhoc_binding_changed"),
            ("workspace", "adhoc_binding_changed"),
            ("attempt-terminal", "effect_attempt_terminal"),
            ("chosen-worker", "effect_launch_binding_invalid"),
            ("adapter", "effect_launch_binding_invalid"),
        )
        for index, (variant, code) in enumerate(variants):
            fixture = nullcontext(self) if index == 0 else fresh_native_fixture(self)
            with self.subTest(variant=variant), fixture as case:
                protocol = case.controller()  # Authenticate while the owners are valid.
                observed = case.now
                if variant == "lease-expired":
                    observed += timedelta(hours=2)
                elif variant == "lease-replaced":
                    case.add_lease("node-a")
                elif variant == "authority-revoked":
                    AuthorityGrantStore(case.root).revoke_exact("borrowed-work", "node-a", 1, case.now)
                elif variant == "provider-slot":
                    case.replace_provider_slot_at_same_path(case.profile_record)
                elif variant == "workspace":
                    workspace = Path(case.profile_record["workspace"])
                    workspace.rename(case.base / "retained-original-workspace")
                    workspace.mkdir()
                elif variant == "attempt-terminal":
                    binding = case.context["effect_binding"]
                    RunScheduler(case.effects.run_ledger).terminal_attempt(
                        binding["run_id"], binding["item_id"], binding["attempt_id"],
                        "failed", "permanent", "malformed_evidence", "idempotent", now=case.now)
                else:
                    # Hostile durable binding, derived from actual native run rows.
                    # Fresh project() below is a positive schema/projection control.
                    from floati.admission import AdmissionPlan
                    from floati.records import run_admission_digest
                    rows = deepcopy(case.effects.run_ledger.records())
                    enabled = next(row for row in rows if row["kind"] == "run_spawn_admission_enabled")
                    admitted = next(row for row in rows if row["kind"] == "run_admission_bound")
                    old_plan, old_admission = enabled["base_plan_digest"], admitted["admission_digest"]
                    for row in rows:
                        if variant == "chosen-worker":
                            if row["kind"] == "worker_pool_bound":
                                row["worker_ids"] = ["foreign"]
                            if row["kind"] == "run_admission_bound":
                                row["workers"][0]["node_id"] = "foreign"
                            if row["kind"] == "run_spawn_admission_enabled":
                                row["base_plan"]["workers"][0]["node_id"] = "foreign"
                            if row["kind"] in {"capability_set_bound", "dispatch_decision"}:
                                row["chosen_worker"] = "foreign"
                            if row["kind"] == "dispatch_decision":
                                row["eligible_workers"] = ["foreign"]
                        elif row["kind"] in {"attempt_spawn_policy_bound", "dispatch_decision"}:
                            row["adapter"] = "pi"
                    new_plan = AdmissionPlan.from_canonical(enabled["base_plan"]).digest
                    new_admission = run_admission_digest(admitted["workers"],
                        admitted["max_active_attempts"], admitted["budget_reservations"], admitted["items"])
                    for row in rows:
                        for field in ("plan_digest", "base_plan_digest"):
                            if row.get(field) == old_plan:
                                row[field] = new_plan
                        if row.get("admission_digest") == old_admission:
                            row["admission_digest"] = new_admission
                    run_path = case.root.tenant_home / case.effects.run_ledger.relative_path
                    run_path.write_bytes(b"".join(fixture_frame(row) for row in rows))
                    binding = case.context["effect_binding"]
                    dispatch = RunLedger(case.root).project().run(binding["run_id"])["dispatches"][binding["attempt_id"]]
                    field = "chosen_worker" if variant == "chosen-worker" else "adapter"
                    self.assertEqual("foreign" if field == "chosen_worker" else "pi", dispatch[field])
                journal_path = case.root.tenant_home / case.journal.relative_path
                before = journal_path.read_bytes()
                foreign = case.tree_bytes(Path(case.foreign_opened["workspace"]))
                with AdhocJournalTests.advanced_clock(case, observed):
                    case.refuses(code, lambda: protocol.accept(case.event(), now=observed))
                self.assertEqual(before, journal_path.read_bytes())
                self.assertEqual([case.admitted], case.journal.records())
                self.assertEqual(foreign, case.tree_bytes(Path(case.foreign_opened["workspace"])))

    def test_accept_revalidates_mutated_owner_types_reciprocity_and_roots(self):
        from floati.approvals import ApprovalLedger
        from floati.effects import EffectController, EffectLedger
        from floati.policy import RepositoryPolicy
        from floati.runtruth import RunLedger
        api = required_api(self, "floati.native_thread_protocol", "NativeThreadProtocol")
        variants = (
            ("writer-type", "adhoc_dispatch_authentication_failed"),
            ("writer-reciprocity", "adhoc_dispatch_authentication_failed"),
            ("effect-controller-type", "effect_controller_invalid"),
            ("effect-ledger-type", "effect_controller_invalid"),
            ("run-ledger-type", "effect_controller_invalid"),
            ("approval-ledger-type", "effect_controller_invalid"),
            ("effect-ledger-root", "effect_root_mismatch"),
            ("run-ledger-root", "effect_root_mismatch"),
            ("approval-ledger-root", "effect_root_mismatch"),
        )
        before = (self.root.tenant_home / self.journal.relative_path).read_bytes()
        with fresh_native_fixture(self) as foreign:
            foreign_before = foreign.tree_bytes()
            for variant, code in variants:
                with self.subTest(variant=variant):
                    writer = AdhocJournalController(AdhocJournal(self.root))
                    effects = EffectController(EffectLedger(self.root), RunLedger(self.root),
                        RepositoryPolicy.load(self.base / "FLOATI.toml"), ApprovalLedger(self.root))
                    protocol = api(self.context, adapter_kind="codex", journal_controller=writer,
                        effect_controller=effects, now=self.now)
                    if variant == "writer-type":
                        protocol.journal_controller = object()
                    elif variant == "writer-reciprocity":
                        protocol.journal_controller.journal = foreign.journal
                    elif variant == "effect-controller-type":
                        protocol.effects = object()
                    else:
                        field = {"effect-ledger": "ledger", "run-ledger": "run_ledger",
                                 "approval-ledger": "approvals"}[variant.rsplit("-", 1)[0]]
                        replacement = object() if variant.endswith("-type") else getattr(foreign.effects, field)
                        setattr(protocol.effects, field, replacement)
                    self.refuses(code, lambda: protocol.accept(self.event(), now=self.now))
                    self.assertEqual(before, (self.root.tenant_home / self.journal.relative_path).read_bytes())
                    self.assertEqual([self.admitted], self.journal.records())
                    self.assertEqual(foreign_before, foreign.tree_bytes())

    def test_lease_expiry_while_waiting_for_opened_journal_lock_refuses_append(self):
        from floati import adhoc_profiles, jsonl
        protocol = self.controller()  # Authenticated at the fixture's initial time.
        path = self.root.tenant_home / self.journal.relative_path
        before = path.read_bytes()
        foreign = Path(self.foreign_opened["workspace"])
        foreign_before = self.tree_bytes(foreign)
        observed = self.now
        boundaries = []
        current_time = adhoc_profiles.current_time
        locked_path = jsonl._locked_path
        journal_lock = path.with_name(path.name + ".lock")
        def clock(now=None):
            return observed if now is None else current_time(now)
        @contextmanager
        def expire_after_real_journal_lock(selected, *args, **kwargs):
            nonlocal observed
            with locked_path(selected, *args, **kwargs):
                if Path(selected) == journal_lock and kwargs.get("exclusive"):
                    boundaries.append(str(selected))
                    observed = self.now + timedelta(hours=2)
                yield
        with mock.patch.object(adhoc_profiles, "current_time", side_effect=clock),\
             mock.patch.object(jsonl, "_locked_path", side_effect=expire_after_real_journal_lock):
            self.refuses("adhoc_lease_invalid", lambda: protocol.accept(self.event(), now=None))
        self.assertTrue(boundaries, "test must reach the real exclusive opened-append lock")
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(foreign_before, self.tree_bytes(foreign))

    def test_wire_resume_identity_and_bounded_metadata(self):
        validate = required_api(self, "floati.native_thread_protocol", "validate_native_event")
        coordinates = (
            ("codex", self.coordinate),
            ("pi", {"provider": "pi_local", "session_id": "pi-fixture",
                    "session_file": str(self.base / "pi-session.jsonl")}),
        )
        for adapter, coordinate in coordinates:
            context = deepcopy(self.context)
            context.update(operation="resume", provider_coordinate=deepcopy(coordinate))
            if adapter == "pi":
                context["permission"] = {"mode": "tools_disabled"}
            opened = self.event(coordinate=coordinate)
            with self.subTest(adapter=adapter):
                self.assertEqual(opened, validate(context, opened, adapter_kind=adapter,
                    opened_coordinate=None, terminal_seen=False))
                changed = deepcopy(coordinate)
                changed["session_id"] = ("019f6ed3-cc28-7000-8000-000000000002"
                                         if adapter == "codex" else "another-pi-session")
                self.refuses("native_session_mismatch", lambda:
                    validate(context, self.event(coordinate=changed), adapter_kind=adapter,
                             opened_coordinate=None, terminal_seen=False))
                terminal = self.event("terminal", coordinate=coordinate)
                self.assertEqual(terminal, validate(context, terminal, adapter_kind=adapter,
                    opened_coordinate=coordinate, terminal_seen=False))
                self.refuses("native_session_mismatch", lambda:
                    validate(context, self.event("terminal", coordinate=changed), adapter_kind=adapter,
                             opened_coordinate=coordinate, terminal_seen=False))
        oversized = self.event()
        oversized["binding"]["profile_digest"] = "x" * 65_537
        deep = self.event()
        value = 0
        for _ in range(1000):
            value = [value]
        deep["binding"]["profile_digest"] = value
        for event in (oversized, deep):
            with self.subTest(bound="bytes" if event is oversized else "depth"):
                with mock.patch("json.dumps", wraps=json.dumps) as recursive_encoder:
                    self.refuses("native_protocol_limit", lambda:
                        validate(self.context, event, adapter_kind="codex",
                                 opened_coordinate=None, terminal_seen=False))
                recursive_encoder.assert_not_called()
        failed = self.event("terminal")
        failed.update(outcome="failed", reason_code="native_protocol_limit",
                      worker_failure="protocol_error")
        self.assertEqual(failed, validate(self.context, failed, adapter_kind="codex",
            opened_coordinate=self.coordinate, terminal_seen=False))
        for changes in ({"worker_failure": "process_timeout"}, {"reason_code": "invented_alias"}):
            self.refuses("protocol_error", lambda:
                validate(self.context, {**failed, **changes}, adapter_kind="codex",
                         opened_coordinate=self.coordinate, terminal_seen=False))

    def test_closed_events_reject_wrong_provider_or_foreign_binding(self):
        controller = self.controller()
        for field, value in (("node_lease_id", self.foreign_lease["id"]),
                             ("dispatch_digest", "0" * 64)):
            event = self.event()
            event["binding"][field] = value
            self.refuses("native_session_mismatch", lambda: controller.accept(event, now=self.now))
        event = self.event(coordinate={"provider": "pi_local", "session_id": "pi-one", "session_file": None})
        self.refuses("native_session_mismatch", lambda: controller.accept(event, now=self.now))
        for change in ({"unreviewed": True}, {"provider_coordinate": {**self.coordinate, "session_id": "x" * 513}}):
            event = {**self.event(), **change}
            self.refuses("protocol_error", lambda: controller.accept(event, now=self.now))
        self.assertEqual([self.admitted], self.journal.records())

    def test_terminal_before_open_and_duplicate_terminal_are_refused(self):
        controller = self.controller()
        self.refuses("protocol_error", lambda: controller.accept(self.event("terminal"), now=self.now))
        controller.accept(self.event(), now=self.now)
        self.refuses("protocol_error", lambda: controller.accept(self.event(), now=self.now))
        pending = controller.accept(self.event("terminal"), now=self.now)
        self.assertEqual("terminal_pending_cleanup", pending["state"])
        self.refuses("protocol_error", lambda: controller.accept(self.event("terminal"), now=self.now))
        self.assertEqual("open", self.work.show(self.context["work_item_id"])[0]["state"])

    def test_opened_record_preserves_admission_and_exclusive_owner(self):
        before = (self.root.tenant_home / self.journal.relative_path).read_bytes()
        ack = self.controller().accept(self.event(), now=self.now)
        self.assertEqual("opened_ack", ack["event"])
        after = (self.root.tenant_home / self.journal.relative_path).read_bytes()
        self.assertTrue(after.startswith(before))
        fresh = AdhocJournal(self.root)
        self.assertEqual(self.admitted, fresh.show(self.context["dispatch_id"]))
        rows = fresh.records()
        self.assertEqual(2, len(rows))
        self.assertEqual("adhoc_native_thread_opened_record", rows[-1]["kind"])
        self.assertEqual("provider_opened", rows[-1]["phase"])
        self.assertEqual(ack["opened_record_id"], rows[-1]["id"])
        self.assertEqual("pre_prompt", rows[-1]["evidence"]["identity_phase"])
        self.assertEqual([self.context["dispatch_id"]],
                         [row["dispatch_id"] for row in active_dispatch_references(self.root)])
        for phase in ("provider_opened", "provider_terminal", "process_closed",
                      "work_completed", "result_published", "teardown_completed",
                      "failed", "unknown", "native_thread_opened"):
            self.refuses("adhoc_dispatch_authentication_failed",
                         lambda: self.writer.append(self.context["dispatch_id"], phase, {}, now=self.now))

    def test_caller_objects_cannot_replace_real_parent_owners(self):
        api = required_api(self, "floati.native_thread_protocol", "NativeThreadProtocol")
        with self.assertRaises(ProtocolRefusal) as caught:
            api(self.context, adapter_kind="codex", journal_controller=self.writer,
                effect_controller=object(), now=self.now)
        self.assertEqual("effect_controller_invalid", caught.exception.code)
        with self.assertRaises(ProtocolRefusal) as caught:
            api(self.context, adapter_kind="codex", journal_controller=object(),
                effect_controller=self.effects, now=self.now)
        self.assertEqual("adhoc_dispatch_authentication_failed", caught.exception.code)
        self.assertEqual([self.admitted], self.journal.records())
        for field, value, code in (
            ("dispatch_id", "adhoc-dispatch-" + uuid7_hex(), "adhoc_dispatch_authentication_failed"),
            ("node_lease_id", self.foreign_lease["id"], "adhoc_dispatch_authentication_failed"),
            ("effect_binding", {**self.context["effect_binding"], "attempt_id": "attempt-" + uuid7_hex()},
             "effect_launch_binding_invalid"),
        ):
            with self.subTest(case=field):
                context = deepcopy(self.context)
                context[field] = value
                self.refuses(code, lambda: self.controller(context))
        self.assertEqual([self.admitted], self.journal.records())
        with fresh_native_fixture(self) as foreign, self.subTest(case="foreign-root"):
            self.assertEqual(self.root.tenant_id, foreign.root.tenant_id)
            self.assertNotEqual(self.root.tenant_home, foreign.root.tenant_home)
            before = foreign.tree_bytes()
            with self.assertRaises(ProtocolRefusal) as caught:
                api(self.context, adapter_kind="codex", journal_controller=self.writer,
                    effect_controller=foreign.effects, now=self.now)
            self.assertEqual("effect_root_mismatch", caught.exception.code)
            self.assertEqual(before, foreign.tree_bytes())
            self.assertEqual([self.admitted], self.journal.records())

    def test_claude_terminal_only_wire_validation_never_invents_an_open_ack(self):
        validate = required_api(self, "floati.native_thread_protocol", "validate_native_event")
        coordinate = {"provider": "claude_local", "session_id": "claude-fixture-one",
                      "session_file": None}
        opened = self.event(coordinate=coordinate)
        self.refuses("protocol_error", lambda: validate(self.context, opened,
            adapter_kind="claude", opened_coordinate=None, terminal_seen=False))
        terminal = self.event("terminal", coordinate=coordinate)
        result = validate(self.context, terminal, adapter_kind="claude",
                          opened_coordinate=None, terminal_seen=False)
        self.assertEqual(terminal, result)
        self.assertNotIn("opened_record_id", result)



class NativeProviderIdentityModeTests(unittest.TestCase):
    def test_claude_has_terminal_only_identity_and_no_open_ack(self):
        mode = required_api(self, "floati.native_thread_protocol", "identity_mode")
        self.assertEqual("terminal_only", mode("claude"))
        self.assertEqual("pre_prompt", mode("codex"))
        self.assertEqual("pre_prompt", mode("pi"))
        with self.assertRaises(ProtocolRefusal):
            mode("fixture-provider")


class NativeChannelDeadlineTests(unittest.TestCase):
    def channels(self):
        left, right = socket.socketpair()
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        right.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        channel = BootstrapChannel(left.detach())
        self.addCleanup(channel.close)
        self.addCleanup(right.close)
        return channel, right

    def test_deadline_send_backpressure_and_legacy_default(self):
        from floati.worker_errors import WorkerAdapterFailure
        # Historical small-frame default remains an ordinary working path.
        legacy, peer = self.channels()
        receiver = BootstrapChannel(peer.detach())
        self.addCleanup(receiver.close)
        legacy.send(("legacy", None))
        self.assertEqual(("legacy", None), receiver.recv(timeout=1))
        self.assertIn("deadline", inspect.signature(BootstrapChannel.send).parameters,
                      "API ABSENT, not behavioral RED: deadline-aware send")
        channel, peer = self.channels()
        deadline = time.monotonic() + 0.6
        time.sleep(0.35)  # More consumed budget than the permitted timing slack.
        outcomes = []
        def send():
            try:
                channel.send(("native_thread", {"padding": "x" * 60_000}), deadline=deadline)
            except BaseException as exc:
                outcomes.append(exc)
            else:
                outcomes.append(None)
        thread = threading.Thread(target=send)
        def cleanup():
            peer.close()  # Unblock even a wrongly unbounded implementation.
            if thread.ident is not None:
                thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        thread.start()
        thread.join(timeout=max(0, deadline - time.monotonic()) + 0.15)
        self.assertFalse(thread.is_alive(), "send ignored its original deadline")
        self.assertEqual(1, len(outcomes))
        self.assertIsInstance(outcomes[0], TimeoutError)
        self.assertLess(time.monotonic(), deadline + 0.15)
        self.assertTrue(select.select([peer], [], [], 0)[0], "no partial write reached real socket")
        self.assertGreater(len(peer.recv(4096)), 0)
        for value in (float("nan"), float("inf"), True):
            fresh, other = self.channels()
            with self.subTest(deadline=value), self.assertRaises(WorkerAdapterFailure) as caught:
                fresh.send(("native_thread", None), deadline=value)
            self.assertEqual("effect_worker_isolation_unavailable", caught.exception.code)
            self.assertEqual([], select.select([other], [], [], 0)[0])
        fresh, other = self.channels()
        with self.assertRaises(TimeoutError):
            fresh.send(("native_thread", None), deadline=time.monotonic() - 1)
        self.assertEqual([], select.select([other], [], [], 0)[0])

    def test_partial_eof_and_trickle_receive_do_not_reset_timeout(self):
        from floati.worker_errors import WorkerAdapterFailure
        for raw in (b"\x00\x00", struct.pack(">I", 12) + b"{"):
            channel, peer = self.channels()
            peer.sendall(raw)
            peer.close()
            with self.subTest(raw=raw), self.assertRaises(WorkerAdapterFailure) as caught:
                channel.recv(timeout=0.2)
            # Retain the existing generic BootstrapChannel failure contract.
            self.assertEqual("effect_worker_isolation_unavailable", caught.exception.code)
        channel, peer = self.channels()
        stop = threading.Event()
        def trickle():
            try:
                for byte in struct.pack(">I", 64) + b"{" * 64:
                    if stop.wait(0.04):
                        return
                    peer.sendall(bytes([byte]))
            except OSError:
                return
        thread = threading.Thread(target=trickle)
        def cleanup():
            stop.set()
            peer.close()
            if thread.ident is not None:
                wait_join(thread, "trickle writer exits after stop")
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 0.15
        thread.start()
        with self.assertRaises(TimeoutError):
            channel.recv(timeout=max(0, deadline - time.monotonic()))
        self.assertLess(time.monotonic(), deadline + 0.3)

    def test_unrepresentable_integer_deadlines_are_typed_refusals_before_transport(self):
        from floati.worker_errors import WorkerAdapterFailure
        value = 10 ** 1000
        channel, peer = self.channels()
        with self.assertRaises(WorkerAdapterFailure) as caught:
            channel.send(("native_thread", None), deadline=value)
        self.assertEqual("effect_worker_isolation_unavailable", caught.exception.code)
        self.assertEqual([], select.select([peer], [], [], 0)[0])
        emitter = required_api(self, "floati.native_thread_protocol", "NativeThreadEmitter")
        with self.assertRaises(WorkerAdapterFailure) as caught:
            emitter(literal_native_context(), channel=channel, deadline=value)
        self.assertEqual("protocol_error", caught.exception.code)
        self.assertEqual([], select.select([peer], [], [], 0)[0])
