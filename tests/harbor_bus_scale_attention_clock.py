"""Native attention equivalence controls, never timing/performance evidence.

Explicit projection times exercise the real 900-second branch without replacing
clocks, evaluators, I/O, or native costs. Admission samples below are schema
inputs, not measured CPU/byte/lock testimony.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.bus_scale_test_profile import banked_send_repository, StableSourceFixture
from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]
WAITING_AT = datetime(2026, 10, 15, 18, 0, 0, 123000, tzinfo=timezone.utc)


def load(filename, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/fixtures/bus_scale_census" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class AttentionClockEquivalenceTests(StableSourceFixture, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-read-controls-attention-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.workspace, self.sha = banked_send_repository(self.base)
        self.workspace.chmod(0o700)
        self.helper = load("bus-scale-routine-reads-2026-10-15.py", "attention_native_reads")

    def fixture(self, case):
        parent = self.base / case
        parent.mkdir(mode=0o700)
        home, bank = parent / "scale-fleet", parent / "bank"
        home.mkdir(mode=0o700)
        bank.mkdir(mode=0o700)
        root = FloatiRoot.open_direct_home(home)
        for node in ("scale-worker", "architect"):
            Registry(root).register(node, "codex")
        message = EventLog(root).send("scale-worker", "architect", "floati", self.sha,
            "note.md", "real pending native attention", idempotency_key="attention-prefix",
            now=WAITING_AT)["message"]
        context = self.helper.prepare(case, home, self.workspace, bank)
        context["canonical_time"] = WAITING_AT.isoformat()
        return root, context, message

    def artifact(self, case, root, seconds):
        observed = WAITING_AT + timedelta(seconds=seconds)
        if case == "supervise":
            from floati.supervisor import Supervisor
            evidence = Supervisor(root).snapshot(observed)
        else:
            from floati.installer_shadow import observe_installer_shadow
            from floati.projection import FleetProjection
            snapshot = FleetProjection(root).snapshot(observed)
            snapshot["installer_shadow"] = observe_installer_shadow(None)
            evidence = {"delta": {"kind": "initial", "snapshot": snapshot}}
        return {"artifact_version": 0, "command": case, "status": "ok", "evidence": evidence}

    def attention(self, case, artifact):
        evidence = artifact["evidence"]
        if case == "watch":
            evidence = evidence["delta"]["snapshot"]
        return next(row["seat_attention"] for row in evidence["nodes"] if row["node_id"] == "architect")

    def normalize(self, case, root, context, artifact, seconds):
        observed = WAITING_AT + timedelta(seconds=seconds)
        self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
            "kind": "supervision_returned" if case == "supervise" else "bounded_watch_returned", "count": 1}},
            self.helper.witness(case, root.path, {}, artifact, context, {}))
        return self.helper.normalize(case, {}, artifact, context, home=root.path,
                                     wall_begin=observed, wall_end=observed)

    def assert_aged_equivalence(self, case):
        root, context, message = self.fixture(case)
        events_before = (root.path / "events.jsonl").read_bytes()
        projected = []
        for seconds in (901, 903):
            artifact = self.artifact(case, root, seconds)
            original = copy.deepcopy(artifact)
            attention = self.attention(case, artifact)
            self.assertEqual("unknown", attention["state"])
            self.assertEqual("overdue_without_hold_start", attention["reason"])
            self.assertEqual(seconds, attention["oldest_waiting_age_seconds"])
            self.assertIsNone(attention["next_check_at"])
            self.assertIn("oldest waiting 15m;", attention["detail"])
            try:
                normalized = self.normalize(case, root, context, artifact, seconds)
            except ValueError as error:
                self.fail("clock-only equivalence must retain genuine overdue attention: " + str(error))
            actual = self.attention(case, normalized["artifact"])
            for key in ("state", "reason", "remedy", "hold_evidence_state", "next_check_at",
                        "last_hold_started_at", "oldest_waiting_at", "undelivered_count"):
                self.assertEqual(attention[key], actual[key], key)
            self.assertTrue(actual["detail"], "clock normalization must not erase native branch prose")
            self.assertEqual(attention["detail"].split("oldest waiting ")[0],
                             actual["detail"].split("oldest waiting ")[0])
            self.assertEqual(attention["detail"].split("; last hold start ")[1],
                             actual["detail"].split("; last hold start ")[1])
            self.assertEqual(original, artifact, "normalization must preserve the complete raw artifact")
            projected.append(normalized["artifact"])
        self.assertEqual(projected[0], projected[1], "continuous aging alone must not defeat equivalence")
        self.assertEqual(events_before, (root.path / "events.jsonl").read_bytes())
        self.assertEqual([message["id"]], [row["id"] for row in EventLog(root).records()])
        self.assertFalse((root.path / "receipts/deliveries/architect.jsonl").exists())
        self.assertFalse((root.path / "receipts/acks/architect.jsonl").exists())

    def test_supervise_keeps_native_overdue_category_while_normalizing_only_aging(self):
        self.assert_aged_equivalence("supervise")

    def test_watch_keeps_native_overdue_category_while_normalizing_only_aging(self):
        self.assert_aged_equivalence("watch")

    def test_native_pending_mail_really_crosses_900_seconds_without_delivery_or_ack(self):
        for case in ("supervise", "watch"):
            with self.subTest(case=case):
                root, context, _message = self.fixture(case)
                for seconds, state, reason, next_null in (
                    (899, "not_overdue", "fresh_waiting_mail", False),
                    (901, "unknown", "overdue_without_hold_start", True),
                    (903, "unknown", "overdue_without_hold_start", True),
                ):
                    artifact = self.artifact(case, root, seconds)
                    attention = self.attention(case, artifact)
                    self.assertEqual((state, reason, next_null),
                        (attention["state"], attention["reason"], attention["next_check_at"] is None))
                    self.assertEqual(seconds, attention["oldest_waiting_age_seconds"])
                    self.assertEqual(1, attention["undelivered_count"])
                    self.helper.witness(case, root.path, {}, artifact, context, {})

    def test_forged_category_nullness_and_non_duration_prose_refuse_native_witnessing(self):
        for case in ("supervise", "watch"):
            root, context, _message = self.fixture(case)
            artifact = self.artifact(case, root, 901)
            for field, value in (("state", "not_overdue"), ("reason", "fresh_waiting_mail"),
                                 ("remedy", ""), ("next_check_at", "2026-10-15T18:15:00.124Z"),
                                 ("detail", "architect has no waiting mail")):
                with self.subTest(case=case, field=field):
                    forged = copy.deepcopy(artifact)
                    self.attention(case, forged)[field] = value
                    with self.assertRaises(ValueError):
                        self.helper.witness(case, root.path, {}, forged, context, {})

    def assert_cross_scale_missing_categories_refused(self, case):
        root, context, _message = self.fixture(case)
        artifacts = [self.artifact(case, root, seconds) for seconds in (899, 901)]
        self.assertEqual(["not_overdue", "unknown"],
                         [self.attention(case, artifact)["state"] for artifact in artifacts])
        from floati.cli import _parser
        from tests import bus_scale_workloads as workloads
        scope = workloads.scope_contract(_parser())
        observations = {}
        # Full otherwise-valid first-pass admission population. These are
        # functional validator inputs, not claimed native measurements for
        # the unrelated commands. Only the target artifacts below are native.
        for other, contract in scope["cases"].items():
            exit_code, status, effect_kind = workloads._NATIVE[other]
            native = {"exit_code": exit_code, "status": status,
                      "effect": {"kind": effect_kind, "count": 1}}
            if other == "doctor":
                native["findings_sha256"] = "d" * 64
            arms = {}
            for scale in (1, 10):
                digest = hashlib.sha256((other + "-admission-seed-" + str(scale)).encode()).hexdigest()
                arms[str(scale) + "x"] = {
                    "records": 1000 * scale, "bytes": 100000 * scale,
                    "seed_sha256": digest, "clone_sha256": digest,
                    "child_collected": True, "raw_sha256": "a" * 64,
                    "native": copy.deepcopy(native),
                    "coverage": {"cpu": True, "bytes": True, "locks": True},
                    "metrics": {"cpu_seconds": 1.0, "bytes_read": 100,
                                "max_ledger_lock_seconds": 0.001},
                }
                if other in {"supervise", "watch"} and other != case:
                    from tests.bus_scale_attention_samples import attention_sample
                    arms[str(scale) + "x"]["attention_proof"] = attention_sample(other, scale)
            observations[other] = {"family": contract["family"], "prefix_exact": True,
                                   "selected_work_equal": True, "arms": arms}
        row = observations[case]
        for scale, artifact in zip((1, 10), artifacts):
            native = self.helper.witness(case, root.path, {}, artifact, context, {})
            raw = encoded(artifact)
            (self.base / (case + "-scale-" + str(scale) + "-native.json")).write_bytes(raw)
            seed_digest = hashlib.sha256(("admission-only-seed-" + str(scale)).encode()).hexdigest()
            row["arms"][str(scale) + "x"] = {
                "records": 1000 * scale, "bytes": 100000 * scale,
                "seed_sha256": seed_digest, "clone_sha256": seed_digest,
                "child_collected": True, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "native": native, "coverage": {"cpu": True, "bytes": True, "locks": True},
                "metrics": {"cpu_seconds": 1.0, "bytes_read": 100,
                            "max_ledger_lock_seconds": 0.001},
            }
        # Validate the current admission shape independently of the missing
        # category gate. The digest binds genuine data; it cannot expose its
        # category to a gate receiving no inspectable testimony.
        workloads._require_scope(scope)
        for other, observation in observations.items():
            for arm in observation["arms"].values():
                workloads._require_arm(other, arm)
        # Reject for the target category testimony, not malformed scope or
        # unrelated population data. This is the real first-pass consumer.
        with self.assertRaisesRegex(ValueError, case + r".*categor",
                                    msg="attention arms without inspectable category testimony must refuse"):
            workloads.require_observations(scope, observations)

    def test_supervise_cross_scale_admission_refuses_missing_inspectable_category_testimony(self):
        self.assert_cross_scale_missing_categories_refused("supervise")

    def test_watch_cross_scale_admission_refuses_missing_inspectable_category_testimony(self):
        self.assert_cross_scale_missing_categories_refused("watch")


if __name__ == "__main__":
    unittest.main()
