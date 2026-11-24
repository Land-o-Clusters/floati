"""Draft native wait delta decoding controls; no phase or CPU qualification.

Adopt under tests/ before running. Append entries expose ordered `frames`,
JSON replacements expose their complete `value`, and create_lock entries keep
their operation. Original raw transport evidence remains with the producer.
"""
from __future__ import annotations

import base64
import copy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from floati.bus_epoch import epoch_guard
from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import (CodexWaitConsentLedger, CodexWaitSessionLedger,
    resolve_participant, write_workspace_map)
from floati.framing import decode_frames, encode_frame
from floati.jsonl import _append_frame
from floati.cursor_presentations import CursorPresentationLedger
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_hold import wake_coordination_guard
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import full_scale_enabled
from tests.waiter_timing import scripted_readiness

ROOT = Path(__file__).resolve().parents[1]
SESSION = "bus-scale-native-census"
HOLDER = "state/codex-wait/scale-worker/holder.json"
BREAKER = "state/codex-wait/scale-worker/breaker.json"
HOLDS = "receipts/stop-holds/scale-worker.jsonl"
EXHAUSTION = "receipts/codex-wait-exhaustion/scale-worker.jsonl"
EXITS = "receipts/wake-waiter-exit/scale-worker.jsonl"
PRESENTATIONS = "receipts/cursor-presentations/scale-worker.jsonl"
WITNESS = "state/cursor-wait/scale-worker/presentation-history.v1.json"
INVOCATION = "state/waiter-invocations/scale-worker/" + hashlib.sha256(SESSION.encode()).hexdigest() + ".json"
OBSERVATION = "state/waiter-observations/scale-worker.json"


def coordinate(path, tail):
    return "/delta/" + path.replace("~", "~0").replace("/", "~1") + "/" + tail


def replace_pointer(value, pointer, replacement):
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    key = int(parts[-1]) if isinstance(value, list) else parts[-1]
    original = value[key]
    value[key] = replacement
    return original


class WaitInvocationClockTests(unittest.TestCase):
    def test_terminal_guard_window_rejects_identity_after_both_completion_clocks(self):
        from floati.ids import uuid7_hex
        from tests.bus_scale_wait_effects import _generated
        from tests.test_bus_scale_wait_control_lock import NativeWaitControlLockTests

        real_now = datetime.now
        for real_seconds in (1, 5):
            with self.subTest(real_completion_seconds=real_seconds):
                start = real_now(timezone.utc)

                class CollectedClock(datetime):
                    completed = None

                    @classmethod
                    def now(cls, tz=None):
                        if cls.completed is None:
                            cls.completed = start
                            return start
                        cls.completed = max(start + timedelta(seconds=real_seconds), real_now(tz))
                        return cls.completed

                fixture = NativeWaitControlLockTests()

                def cleanup_fixture(fixture=fixture):
                    try:
                        # This borrowed fixture retains its standalone evidence;
                        # the clock control needs only this runner's receipt.
                        with redirect_stdout(io.StringIO()):
                            fixture.doCleanups()
                    finally:
                        shutil.rmtree(fixture.retained)

                self.addCleanup(cleanup_fixture)
                scripted_completion = []

                def observe_wait(**kwargs):
                    result = run_stop_waiter(**kwargs)
                    scripted_completion.append(datetime.fromtimestamp(kwargs["wall_time"](), timezone.utc))
                    return result

                with patch("tests.test_bus_scale_wait_control_lock.datetime", CollectedClock),\
                        patch("tests.test_bus_scale_wait_control_lock.full_scale_enabled", return_value=False),\
                        patch("tests.test_bus_scale_wait_control_lock.run_stop_waiter", observe_wait):
                    fixture.setUp()
                delta = fixture.producer.native_delta("wait-start", fixture.before, fixture.after,
                    home=fixture.home, receipt_prefix_input=fixture.receipt_input)
                fixture.normalize = importlib.import_module("tests.bus_scale_effects").normalize_durable_delta
                fixture.normalize("wait-start", delta, fixture.artifact, **fixture.context)
                # Milliseconds truncate downward; the next full ms is outside
                # both the actual completion and the observed injected clock.
                last = max(CollectedClock.completed, scripted_completion[0])
                future_ms = int(last.timestamp() * 1000) + 1
                with self.assertRaisesRegex(ValueError, "outside retained invocation"):
                    _generated("stop-hold-" + uuid7_hex(timestamp_ms=future_ms), "stop-hold-",
                        fixture.context["wall_begin"], fixture.context["wall_end"])

    def test_real_uuid_completion_and_scripted_deadline_both_bound_native_wait(self):
        from floati.ids import uuid7_hex
        from tests.bus_scale_wait_effects import _generated

        # Plant both completion-order edges without sleeping. If this host
        # takes even longer, retain that actual completion as the fixture must.
        real_now = datetime.now
        for real_seconds in (1, 5):
            with self.subTest(real_completion_seconds=real_seconds):
                start = datetime.now(timezone.utc).replace(microsecond=0)
                completion = start + timedelta(seconds=real_seconds)

                class CollectedClock(datetime):
                    samples = iter((start, completion))

                    @classmethod
                    def now(cls, tz=None):
                        sample = next(cls.samples)
                        return sample if sample is start else max(sample, real_now(tz))

                class IsolatedWait(BusScaleWaitDurableDeltaTests):
                    _fixtures = {}

                fixture = IsolatedWait()
                self.addCleanup(IsolatedWait.doClassCleanups)
                milliseconds = int(completion.timestamp()) * 1000
                with patch(__name__ + ".datetime", CollectedClock),\
                        patch("floati.ids.time", SimpleNamespace(time_ns=lambda: milliseconds * 1000000)),\
                        patch(__name__ + ".full_scale_enabled", return_value=False):
                    fixture.setUp()
                for case in ("wait-start", "wait-idle"):
                    with self.subTest(case=case):
                        planted = "stop-hold-" + uuid7_hex(timestamp_ms=milliseconds)
                        try:
                            _generated(planted, "stop-hold-", fixture.context["wall_begin"],
                                fixture.context["wall_end"])
                        except ValueError as error:
                            self.fail("real completion UUID %s at %s outside fixture [%s, %s]: %s" % (
                                planted, completion.isoformat(), fixture.context["wall_begin"].isoformat(),
                                fixture.context["wall_end"].isoformat(), error))
                        fixture.normalize(case, fixture.delta, fixture.artifact, **fixture.context)
                # The identity at actual completion is legitimate. An identity
                # beyond both retained clocks must still fail the real decoder.
                future = fixture.context["wall_end"] + timedelta(seconds=1)
                with self.assertRaisesRegex(ValueError, "outside retained invocation"):
                    _generated("stop-hold-" + uuid7_hex(timestamp_ms=int(future.timestamp()) * 1000),
                        "stop-hold-", fixture.context["wall_begin"], fixture.context["wall_end"])


class BusScaleWaitDurableDeltaTests(unittest.TestCase):
    _fixtures = {}

    def setUp(self):
        try:
            subject = importlib.import_module("tests.bus_scale_effects")
        except ModuleNotFoundError as error:
            if error.name != "tests.bus_scale_effects":
                raise
            subject = None
        self.normalize = getattr(subject, "normalize_durable_delta", None)
        self.assertTrue(callable(self.normalize), "missing normalize_durable_delta wait decoder")
        key = full_scale_enabled()
        fixture = type(self)._fixtures.get(key)
        if fixture is not None:
            for field, value in fixture.items():
                setattr(self, field, copy.deepcopy(value) if field in {
                    "before", "receipt_input", "artifact", "delta", "context"} else value)
            return
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-wait-decoder-")
        type(self).addClassCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home, self.workspace = self.base / "scale-fleet", self.base / "workspace"
        self.home.mkdir(mode=0o700)
        self.workspace.mkdir(mode=0o700)
        root = FloatiRoot.open_direct_home(self.home)
        # A zero-frame native ledger exists before the waiter creates its lock;
        # the producer requires every new ledger lock to have its ledger peer.
        _append_frame(root.resolve_relative("events.jsonl"), b"")
        # Zcode uses this native waiter and StopHoldLedger but does not discover
        # an optional installed Codex adapter executable on the test host.
        Registry(root).register("scale-worker", "zcode")
        write_workspace_map(self.home, self.workspace, "scale-worker")
        participant = resolve_participant(self.home, self.workspace)
        self.assertIsNotNone(participant)
        consent = CodexWaitConsentLedger(root).arm(participant.binding,
            hook_timeout_seconds=3, wait_deadline_seconds=2, idempotency_key="decoder-consent")
        CodexWaitSessionLedger(root).arm(participant.binding, consent, SESSION, idempotency_key="decoder-session")
        # Read the waiter's complete current-epoch source closure natively.
        # Unlike read_records on absent ledgers, this creates each source lock
        # without inventing delivery/ACK frames or presentation history.
        with epoch_guard(root, exclusive=False), wake_coordination_guard(root, "scale-worker"):
            sources = CursorPresentationLedger(root)._sources(root.tenant_home, "scale-worker")
        self.assertEqual([[], [], []], [sources[index] for index in (0, 2, 4)])
        specification = importlib.util.spec_from_file_location("wait_decoder_native_producer",
            ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py")
        producer = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(producer)
        self.producer = producer
        self.before = producer.raw_files(self.home)
        from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import capture_receipt_prefix_input
        self.receipt_input = capture_receipt_prefix_input(self.home, self.before)
        stdout, stderr = io.StringIO(), io.StringIO()
        now = datetime.now(timezone.utc)
        begin = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        elapsed = [0.0]

        def advance(seconds):
            elapsed[0] += seconds

        timing = {} if full_scale_enabled() else {
            "monotonic": lambda: elapsed[0], "sleep": advance,
            "wall_time": lambda: begin.timestamp() + elapsed[0],
            "wait_readable": scripted_readiness(advance),
        }
        code = run_stop_waiter(bus_home=self.home,
            hook_payload={"cwd": str(self.workspace), "session_id": SESSION}, stdout=stdout, stderr=stderr,
            **timing)
        # UUIDv7 uses real time.time_ns() (floati.ids.uuid7_hex), while the
        # bounded waiter injects wall_time for its deadline and breaker. Keep
        # both collected completions: real work can outlast virtual expiry.
        end = datetime.now(timezone.utc)
        if not full_scale_enabled():
            end = max(end, datetime.fromtimestamp(begin.timestamp() + elapsed[0], timezone.utc))
        self.assertEqual(0, code)
        if stderr.getvalue():
            self.assertRegex(stderr.getvalue(), r"\Afloati waiter change notifications unavailable: [A-Za-z_][A-Za-z0-9_]*; using bounded polling\n\Z")
        self.artifact = json.loads(stdout.getvalue())
        self.assertEqual({"decision": "block", "reason": "(floati: wait deadline exhausted; end this turn to re-arm)"}, self.artifact)
        # Natural expiry may pass through _integrity_exit's lock-deadline path;
        # an exhausted receipt/reason is legitimate and not integrity_failure.
        self.delta = producer.native_delta("wait-start", self.before, producer.raw_files(self.home),
            home=self.home, receipt_prefix_input=self.receipt_input)
        self.context = dict(wall_begin=begin, wall_end=end, child_pid=os.getpid(), home=self.home, workspace=self.workspace,
                            before_raw_files=self.before, receipt_prefix_input=self.receipt_input)
        type(self)._fixtures[key] = {
            field: copy.deepcopy(getattr(self, field)) if field in {
                "before", "receipt_input", "artifact", "delta", "context"} else getattr(self, field)
            for field in ("base", "home", "workspace", "producer", "before", "receipt_input",
                          "artifact", "delta", "context")}
        type(self).addClassCleanup(type(self)._fixtures.pop, key, None)

    def decoded(self, delta, path):
        raw = base64.b64decode(delta[path]["payload_base64"], validate=True)
        return decode_frames(raw) if delta[path]["operation"] == "append" else json.loads(raw)

    def rewrite(self, delta, path, value):
        row = delta[path]
        payload = (b"".join(encode_frame(frame) for frame in value) if row["operation"] == "append"
                   else (json.dumps(value, sort_keys=True) + "\n").encode())
        old = self.before.get(path, b"") if row["operation"] == "append" else b""
        row.update(payload_base64=base64.b64encode(payload).decode(), after_bytes=len(old) + len(payload),
            after_sha256=hashlib.sha256(old + payload).hexdigest())

    def semantic(self):
        from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import verify_receipt_prefix_effect, SUMMARY
        proof = verify_receipt_prefix_effect(self.home, self.before,
            self.producer.raw_files(self.home), self.delta, self.receipt_input)
        # Independently prove every cache byte before replacing just that
        # complete insertion delta with its stable native semantic fact.
        delta = {path: row for path, row in self.delta.items() if path not in proof["changed_files"]}
        result = {"artifact": copy.deepcopy(self.artifact), "delta": {
            path: {"operation": row["operation"], **({"frames": self.decoded(self.delta, path)}
                if row["operation"] == "append" else {"value": self.decoded(self.delta, path)}
                if row["operation"] == "replace" else {})} for path, row in delta.items()}}
        result["delta"][SUMMARY] = proof["semantic"]
        return result

    def test_native_expiry_complete_map_and_audit_decode_for_both_wait_case_names(self):
        original = self.semantic()
        saved = copy.deepcopy((self.delta, self.artifact))
        required = {HOLDER, BREAKER, HOLDS, EXHAUSTION, EXITS, PRESENTATIONS, WITNESS}
        self.assertTrue(required <= set(self.delta))
        self.assertEqual([], self.decoded(self.delta, PRESENTATIONS))
        self.assertEqual(1, len(self.decoded(self.delta, BREAKER)["hits"]))
        holds = self.decoded(self.delta, HOLDS)
        self.assertEqual(["start", "exit"], [row["phase"] for row in holds])
        exhaustion, exit_row = self.decoded(self.delta, EXHAUSTION)[0], self.decoded(self.delta, EXITS)[0]
        self.assertEqual((exhaustion["node_id"], exhaustion["session_digest"], exhaustion["waited_seconds"]),
            (exit_row["node_id"], exit_row["session_digest"], exit_row["waited_seconds"]))
        self.assertEqual("consent_reopened", exhaustion["outcome"])
        self.assertEqual("exhausted", exit_row["reason_code"])
        self.assertGreaterEqual(exhaustion["waited_seconds"], 2)
        self.assertLessEqual(exhaustion["waited_seconds"], int((self.context["wall_end"] - self.context["wall_begin"]).total_seconds()) + 1)
        self.assertGreater(holds[0]["deadline_seconds"], 0)
        self.assertLessEqual(holds[0]["deadline_seconds"], 2)
        self.assertEqual(holds[0]["deadline_seconds"], holds[1]["deadline_seconds"])
        allowed = {coordinate(HOLDER, "value/" + key) for key in ("pid", "process_start_epoch", "timestamp")}
        allowed.add(coordinate(BREAKER, "value/hits/0"))
        allowed.add(coordinate(WITNESS, "value/genesis_id"))
        for path in (EXHAUSTION, EXITS):
            allowed.update(coordinate(path, "frames/0/" + key) for key in ("id", "timestamp", "idempotency_key", "waited_seconds"))
        for index in (0, 1):
            allowed.update(coordinate(HOLDS, "frames/%d/%s" % (index, key))
                for key in ("id", "invocation_id", "timestamp", "started_at", "workspace", "deadline_seconds",
                            "holder_pid", "process_start_epoch"))
        allowed.update(coordinate(INVOCATION, "value/" + key) for key in (
            "pid", "process_start_epoch", "parent_pid", "parent_process_start_epoch",
            "token", "invocation_id", "workspace"))
        self.assertIn(self.decoded(self.delta, OBSERVATION)["mode"], {"blocking", "polling"})
        allowed.update(coordinate(OBSERVATION, "value/" + key)
                       for key in ("observed_at", "valid_until"))
        ancestry = self.decoded(self.delta, INVOCATION)["host_ancestry"]
        for index in range(len(ancestry)):
            allowed.update(coordinate(INVOCATION, "value/host_ancestry/" + str(index) + "/" + key)
                for key in ("pid", "process_start_epoch", "parent_pid")
                if key != "parent_pid" or index < len(ancestry) - 1)
        for case in ("wait-start", "wait-idle"):
            with self.subTest(case=case):
                result = self.normalize(case, self.delta, self.artifact, **self.context)
                self.assertEqual({"artifact", "delta", "normalizations"}, set(result))
                normalized = {"artifact": copy.deepcopy(result["artifact"]), "delta": copy.deepcopy(result["delta"])}
                audits = result["normalizations"]
                self.assertEqual(allowed, {row["pointer"] for row in audits})
                self.assertEqual(len(allowed), len(audits))
                for path in (EXHAUSTION, EXITS):
                    self.assertEqual(2, result["delta"][path]["frames"][0]["waited_seconds"])
                for row in result["delta"][HOLDS]["frames"]:
                    self.assertEqual(2, row["deadline_seconds"])
                # Both source-proven expiry paths are legal: _integrity_exit
                # shares the startup key; ordinary evaluation can use a fresh
                # exhaustion key. Preserve that relationship and suffix class.
                normalized_exhaustion = result["delta"][EXHAUSTION]["frames"][0]["idempotency_key"]
                normalized_exit = result["delta"][EXITS]["frames"][0]["idempotency_key"]
                self.assertTrue(normalized_exhaustion.endswith("-exhaustion"))
                self.assertTrue(normalized_exit.endswith("-exit-exhausted"))
                self.assertEqual(exhaustion["idempotency_key"][:-len("-exhaustion")] == exit_row["idempotency_key"][:-len("-exit-exhausted")],
                    normalized_exhaustion[:-len("-exhaustion")] == normalized_exit[:-len("-exit-exhausted")])
                for row in audits:
                    self.assertEqual({"pointer", "reason"}, set(row))
                    self.assertIsInstance(row["reason"], str)
                    self.assertTrue(row["reason"].strip())
                    # Lookup against a copy obtains exact original full testimony.
                    old = replace_pointer(copy.deepcopy(original), row["pointer"], None)
                    value = replace_pointer(normalized, row["pointer"], old)
                    if not row["pointer"].endswith(("/waited_seconds", "/deadline_seconds")):
                        self.assertNotEqual(old, value)
                    else:
                        self.assertIn("consent", row["reason"].lower())
                self.assertEqual(original, normalized)
        self.assertEqual(saved, (self.delta, self.artifact))

    def test_holder_must_bind_own_pid_and_exact_native_session_before_normalization(self):
        for field, value in (("pid", os.getpid() + 1), ("session_id", "foreign-decoder-session")):
            changed = copy.deepcopy(self.delta)
            holder = self.decoded(changed, HOLDER)
            holder[field] = value
            self.rewrite(changed, HOLDER, holder)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.normalize("wait-start", changed, self.artifact, **self.context)

    def test_resealed_invocation_state_keeps_exact_terminal_session_and_hold_binding(self):
        path = self.home / INVOCATION
        original = path.read_bytes()
        for field, value in (("phase", "running"), ("session_id", "foreign-session"),
                             ("invocation_id", "stop-hold-" + "0" * 32),
                             ("parent_pid", os.getpid()), ("future_testimony", {})):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.delta)
                state = json.loads(original)
                state[field] = value
                payload = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
                changed[INVOCATION].update(payload_base64=base64.b64encode(payload).decode(),
                    after_bytes=len(payload), after_sha256=hashlib.sha256(payload).hexdigest())
                path.write_bytes(payload)
                try:
                    with self.assertRaises(ValueError):
                        self.normalize("wait-start", changed, self.artifact, **self.context)
                finally:
                    path.write_bytes(original)

    def test_stop_hold_exit_must_preserve_the_original_immutable_native_budget(self):
        changed = copy.deepcopy(self.delta)
        holds = self.decoded(changed, HOLDS)
        holds[1]["deadline_seconds"] = holds[0]["deadline_seconds"] / 2
        self.rewrite(changed, HOLDS, holds)
        with self.assertRaises(ValueError):
            self.normalize("wait-start", changed, self.artifact, **self.context)

    def test_exhaustion_and_exit_correspondence_is_semantic_not_just_receipt_count(self):
        for field, value in (("session_digest", "f" * 64), ("reason_code", "paused"), ("waited_seconds", 1)):
            changed = copy.deepcopy(self.delta)
            exits = self.decoded(changed, EXITS)
            exits[0][field] = value
            self.rewrite(changed, EXITS, exits)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.normalize("wait-start", changed, self.artifact, **self.context)

    def test_empty_presentation_and_single_breaker_hit_cannot_be_dropped_or_collapsed(self):
        for defect in ("presentation_frame", "breaker_hit"):
            changed = copy.deepcopy(self.delta)
            if defect == "presentation_frame":
                self.rewrite(changed, PRESENTATIONS, [{"kind": "unreviewed_presentation", "timestamp": "literal"}])
            else:
                breaker = self.decoded(changed, BREAKER)
                breaker["hits"].append(breaker["hits"][0])
                self.rewrite(changed, BREAKER, breaker)
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.normalize("wait-idle", changed, self.artifact, **self.context)

    def test_correlated_durations_still_require_native_consent_and_wall_bounds(self):
        for defect in ("hold_above_consent", "too_short_expiry", "beyond_wall"):
            changed = copy.deepcopy(self.delta)
            if defect == "hold_above_consent":
                holds = self.decoded(changed, HOLDS)
                for row in holds:
                    row["deadline_seconds"] = 3
                self.rewrite(changed, HOLDS, holds)
            else:
                duration = 0 if defect == "too_short_expiry" else 1000
                for path in (EXHAUSTION, EXITS):
                    rows = self.decoded(changed, path)
                    rows[0]["waited_seconds"] = duration
                    self.rewrite(changed, path, rows)
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.normalize("wait-start", changed, self.artifact, **self.context)

    def test_unknown_holder_field_is_retained_or_refused_not_recursively_scrubbed(self):
        changed = copy.deepcopy(self.delta)
        holder = self.decoded(changed, HOLDER)
        extra = {"pid": 123, "timestamp": "literal", "workspace": str(self.workspace)}
        holder["future_testimony"] = extra
        self.rewrite(changed, HOLDER, holder)
        original = copy.deepcopy((changed, self.artifact))
        try:
            result = self.normalize("wait-start", changed, self.artifact, **self.context)
        except ValueError:
            self.assertEqual(original, (changed, self.artifact))
            return
        self.assertEqual(extra, result["delta"][HOLDER]["value"]["future_testimony"])
        self.assertFalse(any("future_testimony" in row["pointer"] for row in result["normalizations"]))
        self.assertEqual(original, (changed, self.artifact))


if __name__ == "__main__":
    unittest.main()
