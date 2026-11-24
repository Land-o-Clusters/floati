"""Native d66 terminal-publication lock birth; no phase/performance claims.

The new node lock is never preseeded. Real native consent, claim and a two
second expiry establish the complete source testimony. The second control
encodes actual byte changes independently so the producer's refusal cannot
mask the decoder's separate closed-path refusal.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from floati.bus_epoch import epoch_guard
from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import (CodexWaitConsentLedger, CodexWaitSessionLedger,
    resolve_participant, write_workspace_map)
from floati.cursor_presentations import CursorPresentationLedger
from floati.jsonl import _append_frame
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_hold import wake_coordination_guard
from tests.bus_scale_effects import normalize_durable_delta
from tests.bus_scale_test_profile import full_scale_enabled
from tests.temp_roots import REAL_TEMP_ROOT, _PLATFORM_TEMP_ROOT
from tests.waiter_timing import scripted_readiness


ROOT = Path(__file__).resolve().parents[1]
NODE, SESSION = "scale-worker", "bus-scale-native-census"
LOCK = "state/wake-control/scale-worker.lock"
INVOCATION = "state/waiter-invocations/scale-worker/" + hashlib.sha256(SESSION.encode()).hexdigest()
EMPTY = hashlib.sha256(b"").hexdigest()
APPENDS = {"receipts/stop-holds/scale-worker.jsonl",
           "receipts/codex-wait-exhaustion/scale-worker.jsonl",
           "receipts/wake-waiter-exit/scale-worker.jsonl",
           "receipts/cursor-presentations/scale-worker.jsonl"}
REPLACES = {"state/codex-wait/scale-worker/holder.json", "state/codex-wait/scale-worker/breaker.json",
            "state/cursor-wait/scale-worker/presentation-history.v1.json",
            "state/wake-daemon/adapters/scale-worker/codex.json", INVOCATION + ".json",
            "state/waiter-observations/scale-worker.json"}
LOCKS = {path + ".lock" for path in APPENDS} | {LOCK, "events.jsonl.lock",
    "receipts/deliveries/scale-worker.jsonl.lock", "receipts/acks/scale-worker.jsonl.lock",
    "receipts/wake-coordination/scale-worker/lane.lock", INVOCATION + ".lock"}
SOURCES = (Path(__file__).resolve(), ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
    ROOT / "tests/bus_scale_effects.py", ROOT / "tests/bus_scale_wait_effects.py",
    ROOT / "tests/bus_scale_durable.py", ROOT / "floati/codex_wait.py",
    ROOT / "floati/codex_wait_contract.py", ROOT / "floati/wake_control.py", ROOT / "floati/jsonl.py")


def load_producer():
    path = ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py"
    spec = importlib.util.spec_from_file_location("native_wait_control_lock_producer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def source_hashes():
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCES}


def transport(before, after, *, home=None, receipt_prefix_input=None):
    """Closed encoding of actual raw bytes, independent of either consumer."""
    from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import cache_name, verify_receipt_prefix_effect
    cache = cache_name()
    result = {}
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        old, new = before.get(path, b""), after.get(path)
        if new is None:
            raise AssertionError("native expiry removed an unreviewed file: " + path)
        if path in APPENDS:
            if not new.startswith(old):
                raise AssertionError("native expiry rewrote an append prefix")
            operation, payload = "append", new[len(old):]
        elif path in REPLACES:
            operation, payload = "replace", new
        elif path.startswith(cache + "/"):
            if home is None or receipt_prefix_input is None:
                raise AssertionError("native cache transport requires complete pre-child proof")
            operation = "replace" if path.endswith(".json") else "append"
            if operation == "append" and not new.startswith(old):
                raise AssertionError("native receipt cache rewrote a retained append prefix")
            payload = new if operation == "replace" else new[len(old):]
        elif path in LOCKS and path not in before and new == b"":
            operation, payload = "create_lock", b""
        else:
            raise AssertionError("native expiry changed an unreviewed transport path: " + path)
        result[path] = {"operation": operation, "before_bytes": len(old), "after_bytes": len(new),
            "before_sha256": hashlib.sha256(old).hexdigest(), "after_sha256": hashlib.sha256(new).hexdigest(),
            "payload_base64": base64.b64encode(payload).decode("ascii")}
    if any(path.startswith(cache + "/") for path in result):
        verify_receipt_prefix_effect(home, before, after, result, receipt_prefix_input)
    return result


class NativeWaitControlLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bus-scale-wait-control-lock-owned-", dir=REAL_TEMP_ROOT)
        self.base = Path(self.temporary.name).resolve()
        self.retained = Path(tempfile.mkdtemp(prefix="bus-scale-native-wait-control-lock-", dir=_PLATFORM_TEMP_ROOT)).resolve()
        self.record = {"test": self.id(), "source_before": source_hashes(), "performance_evidence": False,
                       "native_execution": True, "node": NODE, "session": SESSION, "new_lock_preseeded": False}
        self.addCleanup(self.retain_and_remove)
        self.producer = load_producer()
        self.home, self.workspace = self.base / "scale-fleet", self.base / "workspace"
        self.home.mkdir(mode=0o700)
        self.workspace.mkdir(mode=0o700)
        root = FloatiRoot.open_direct_home(self.home)
        _append_frame(root.resolve_relative("events.jsonl"), b"")
        Registry(root).register(NODE, "zcode")
        write_workspace_map(self.home, self.workspace, NODE)
        participant = resolve_participant(self.home, self.workspace)
        self.assertIsNotNone(participant)
        consent = CodexWaitConsentLedger(root).arm(participant.binding,
            hook_timeout_seconds=3, wait_deadline_seconds=2, idempotency_key="control-lock-consent")
        claim = CodexWaitSessionLedger(root).arm(participant.binding, consent, SESSION,
            idempotency_key="control-lock-claim")
        # Preserve the existing expiry fixture's native source closure. This
        # does not acquire or create the new wake-control terminal guard.
        with epoch_guard(root, exclusive=False), wake_coordination_guard(root, NODE):
            sources = CursorPresentationLedger(root)._sources(root.tenant_home, NODE)
        self.assertEqual([[], [], []], [sources[index] for index in (0, 2, 4)])
        self.assertFalse((self.home / LOCK).exists())
        self.assertFalse((self.home / LOCK).is_symlink())
        self.before = self.producer.raw_files(self.home)
        from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import capture_receipt_prefix_input
        self.receipt_input = capture_receipt_prefix_input(self.home, self.before)
        self.assertNotIn(LOCK, self.before)
        shutil.copytree(self.home, self.retained / "native-before-root")
        payload = {"cwd": str(self.workspace), "session_id": SESSION}
        output, error = io.StringIO(), io.StringIO()
        now = datetime.now(timezone.utc)
        begin = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        timing = {}
        if not full_scale_enabled():
            clock = [0.0]

            def advance(seconds):
                clock[0] += float(seconds)

            wall_origin = now.timestamp()
            timing = {"monotonic": lambda: clock[0], "sleep": advance,
                      "wall_time": lambda: wall_origin + clock[0],
                      "wait_readable": scripted_readiness(advance)}
        code = run_stop_waiter(bus_home=self.home, hook_payload=payload, stdout=output, stderr=error,
                               **timing)
        # UUIDv7 uses precise realtime; the injected deadline uses its own
        # origin plus elapsed. Their maximum bounds both without adding
        # scripted elapsed to time that already passed in the real invocation.
        end = datetime.now(timezone.utc)
        if not full_scale_enabled():
            end = max(end, datetime.fromtimestamp(wall_origin + clock[0], timezone.utc))
        (self.retained / "native-stdout.txt").write_text(output.getvalue(), encoding="utf-8")
        (self.retained / "native-stderr.txt").write_text(error.getvalue(), encoding="utf-8")
        self.artifact = json.loads(output.getvalue())
        self.after = self.producer.raw_files(self.home)
        shutil.copytree(self.home, self.retained / "native-after-root")
        self.record.update(exit_code=code, artifact=self.artifact, hook_payload=payload,
            child_pid=os.getpid(), wall_begin=begin.isoformat(), wall_end=end.isoformat(),
            consent=consent, claim=claim, execution_boundary=(
                "actual in-process native run_stop_waiter; real clock" if full_scale_enabled() else
                "actual in-process native run_stop_waiter; virtual monotonic/wall deadline and scripted native readiness"))
        write_json(self.retained / "before-raw.json", {p: base64.b64encode(v).decode() for p, v in self.before.items()})
        write_json(self.retained / "after-raw.json", {p: base64.b64encode(v).decode() for p, v in self.after.items()})
        self.assertEqual(0, code)
        self.assertEqual({"decision": "block", "reason": "(floati: wait deadline exhausted; end this turn to re-arm)"}, self.artifact)
        if error.getvalue():
            self.assertRegex(error.getvalue(), r"\Afloati waiter change notifications unavailable: [A-Za-z_][A-Za-z0-9_]*; using bounded polling\n\Z")
        metadata = (self.home / LOCK).lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual(os.getuid(), metadata.st_uid)
        self.assertEqual(1, metadata.st_nlink)
        self.assertEqual(b"", self.after[LOCK])
        self.record["actual_new_lock"] = {"path": LOCK, "bytes": metadata.st_size,
            "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid, "nlink": metadata.st_nlink}
        self.context = dict(wall_begin=begin, wall_end=end, child_pid=os.getpid(), home=self.home, workspace=self.workspace,
                            before_raw_files=self.before, receipt_prefix_input=self.receipt_input)
        self.record["receipt_prefix_input"] = self.receipt_input

    def retain_and_remove(self):
        self.record["source_after"] = source_hashes()
        try:
            for path in SOURCES:
                destination = self.retained / "source" / path.relative_to(ROOT)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            shutil.copytree(self.base, self.retained / "fixture")
        finally:
            self.temporary.cleanup()
            self.record["scratch_removed"] = not self.base.exists()
            write_json(self.retained / "result.json", self.record)
            print("native wait control lock evidence: " + str(self.retained), flush=True)

    def test_producer_admits_the_actual_new_empty_terminal_guard_for_both_wait_cases(self):
        expected = {"operation": "create_lock", "before_bytes": 0, "after_bytes": 0,
                    "before_sha256": EMPTY, "after_sha256": EMPTY, "payload_base64": ""}
        for case in ("wait-start", "wait-idle"):
            with self.subTest(case=case):
                try:
                    result = self.producer.native_delta(case, self.before, self.after,
                        home=self.home, receipt_prefix_input=self.receipt_input)
                except RuntimeError as exc:
                    self.fail("completed native expiry's actual empty terminal guard was rejected: " + str(exc))
                self.assertEqual(expected, result[LOCK])

    def test_decoder_proves_complete_native_expiry_with_independently_encoded_lock_birth(self):
        delta = transport(self.before, self.after, home=self.home, receipt_prefix_input=self.receipt_input)
        write_json(self.retained / "independent-native-delta.json", delta)
        saved = copy.deepcopy((delta, self.artifact))
        for case in ("wait-start", "wait-idle"):
            with self.subTest(case=case):
                try:
                    result = normalize_durable_delta(case, delta, self.artifact, **self.context)
                except ValueError as exc:
                    self.fail("complete actual native expiry proof was rejected: " + str(exc))
                self.assertEqual({"artifact", "delta", "normalizations"}, set(result))
                self.assertEqual({"operation": "create_lock"}, result["delta"][LOCK])
                from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import verify_receipt_prefix_effect, SUMMARY
                proof = verify_receipt_prefix_effect(self.home, self.before, self.after, delta, self.receipt_input)
                semantic_paths = set(delta) - set(proof["changed_files"])
                self.assertEqual(semantic_paths | {SUMMARY}, set(result["delta"]))
                self.assertEqual(proof["semantic"], result["delta"][SUMMARY])
                self.assertEqual(self.artifact, result["artifact"])
                self.assertFalse(any(LOCK.replace("/", "~1") in row["pointer"] for row in result["normalizations"]))
        self.assertEqual(saved, (delta, self.artifact))

    def test_decoder_rejects_forged_transport_or_missing_shared_terminal_guard(self):
        delta = transport(self.before, self.after, home=self.home, receipt_prefix_input=self.receipt_input)
        variants = []
        for field, value in (("before_sha256", "f" * 64), ("after_sha256", "f" * 64),
                             ("before_bytes", 1), ("after_bytes", 1), ("payload_base64", "eA=="),
                             ("extra", True)):
            changed = copy.deepcopy(delta)
            changed[LOCK][field] = value
            variants.append((field, changed))
        changed = copy.deepcopy(delta)
        changed["state/wake-control/architect.lock"] = changed.pop(LOCK)
        variants.append(("foreign_node", changed))
        for defect, changed in variants:
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                normalize_durable_delta("wait-start", changed, self.artifact, **self.context)
        path = self.home / LOCK
        native_metadata = path.stat()
        native_bytes = path.read_bytes()
        changed = copy.deepcopy(delta)
        changed[LOCK].update(after_bytes=1, after_sha256=hashlib.sha256(b"x").hexdigest(),
                             payload_base64="eA==")
        try:
            path.write_bytes(b"x")
            write_json(self.retained / "synthetic-nonempty-lock-control.json", {
                "native_execution": False, "performance_evidence": False,
                "kind": "test_mutated_current_lock_with_coherent_transport", "path": LOCK,
                "actual_current_payload_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
                "synthetic_delta": changed, "original_native_mode": stat.S_IMODE(native_metadata.st_mode),
                "original_native_mtime_ns": native_metadata.st_mtime_ns})
            with self.subTest(defect="coherent_nonempty_current_file"), self.assertRaises(ValueError):
                normalize_durable_delta("wait-start", changed, self.artifact, **self.context)
        finally:
            path.write_bytes(native_bytes)
            path.chmod(stat.S_IMODE(native_metadata.st_mode))
            os.utime(path, ns=(native_metadata.st_atime_ns, native_metadata.st_mtime_ns))
        saved = self.base / "saved-native-terminal-guard"
        path.rename(saved)
        try:
            with self.subTest(defect="nonexistent_current_file"), self.assertRaises(ValueError):
                normalize_durable_delta("wait-start", delta, self.artifact, **self.context)
        finally:
            saved.rename(path)
        alias = self.base / "shared-native-terminal-guard"
        os.link(path, alias)
        try:
            with self.subTest(defect="shared_native_file"), self.assertRaises(ValueError):
                normalize_durable_delta("wait-start", delta, self.artifact, **self.context)
        finally:
            alias.unlink()


class WaitControlLockProducerRejectionTests(unittest.TestCase):
    def test_producer_keeps_new_lock_admission_closed_to_wait_case_node_and_empty_birth(self):
        producer = load_producer()
        for case in ("send", "status", "receipt-append"):
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                producer.native_delta(case, {}, {LOCK: b""})
        for before, after in (({}, {"state/wake-control/architect.lock": b""}),
                              ({}, {LOCK: b"nonempty"}), ({LOCK: b"prior"}, {LOCK: b""})):
            with self.subTest(before=before, after=after), self.assertRaises(RuntimeError):
                producer.native_delta("wait-start", before, after)
        self.assertEqual({}, producer.native_delta("wait-start", {LOCK: b""}, {LOCK: b""}))
