"""Native artifact equivalence controls; these do not measure performance.

The missing helper is an assertion RED, before native fixture preparation.
Fixture artifacts come from native Doctor/status producers, without patched
clocks, projections, disk observations, or command outcomes.
"""
from __future__ import annotations

from tests import HOME_PIN_ROOT
from tests.temp_roots import REAL_TEMP_ROOT

import copy
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest

from floati import cli
from floati.codex_wait_contract import (
    CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant,
    consent_ledger_relative, write_workspace_map,
)
from floati.doctor import Doctor
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot

try:
    _subject = importlib.import_module("tests.bus_scale_equivalence")
except ModuleNotFoundError as exc:
    if exc.name != "tests.bus_scale_equivalence":
        raise
    _subject = None

ROOT = Path(__file__).resolve().parents[1]


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _inventory(home):
    return {p.relative_to(home).as_posix(): (p.stat().st_mode, p.read_bytes())
            for p in home.rglob("*") if p.is_file()}


class BusScaleEquivalenceTests(unittest.TestCase):
    _fixtures = None

    def setUp(self):
        self.assertIsNotNone(
            _subject,
            "missing tests.bus_scale_equivalence.normalize_artifact: full native "
            "doctor/status equivalence must validate facts before normalization",
        )
        self.assertTrue(callable(getattr(_subject, "normalize_artifact", None)))
        self.assertEqual(Path(HOME_PIN_ROOT), Path.home())
        if type(self)._fixtures is None:
            type(self)._fixtures = self._prepare()
        self.fixture = type(self)._fixtures

    def _prepare(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="bus-scale-equivalence-", dir=REAL_TEMP_ROOT,
        )
        type(self).addClassCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        workspace = base / "workspace"
        workspace.mkdir(mode=0o700)
        seed = base / "seed" / "scale-fleet"
        seed.parent.mkdir()
        root = FloatiRoot.open_direct_home(seed, create=True)
        Registry(root).register("scale-worker", "codex")
        Registry(root).register("architect", "codex")
        old = datetime.now(timezone.utc) - timedelta(days=2)
        log = EventLog(root)
        first = log.send("scale-worker", "architect", "floati", "a" * 40,
                         "docs/evidence/fixture.md", "first fixed message",
                         idempotency_key="equivalence-first", now=old)["message"]
        log.send("scale-worker", "architect", "floati", "a" * 40,
                 "docs/evidence/fixture.md", "second fixed message",
                 idempotency_key="equivalence-second", now=old)
        log._write_delivery_receipt("architect", [first])
        write_workspace_map(seed, workspace, "scale-worker")
        participant = resolve_participant(seed, workspace)
        consent = CodexWaitConsentLedger(root).arm(
            participant.binding, hook_timeout_seconds=10,
            wait_deadline_seconds=2, idempotency_key="equivalence-consent",
        )
        CodexWaitSessionLedger(root).arm(
            participant.binding, consent, "equivalence-session",
            idempotency_key="equivalence-session",
        )
        arms = {}
        for mode in ("cpu", "diagnostic"):
            home = base / mode / "scale-fleet"
            shutil.copytree(seed, home)
            begin = datetime.now(timezone.utc)
            evidence, code = Doctor(ROOT, home, ref="HEAD", no_sandbox=True).artifact()
            end = datetime.now(timezone.utc)
            self.assertEqual(35, code)
            doctor = {"artifact_version": 0, "command": "doctor",
                      "status": "degraded", "evidence": evidence}
            status_begin = datetime.now(timezone.utc)
            state, payload, status_code = cli._status(SimpleNamespace(
                root=str(home), json=True, consumer=None, destination=None,
            ))
            status_end = datetime.now(timezone.utc)
            self.assertEqual(("ok", 0), (state, status_code))
            status = {"artifact_version": 0, "command": "status",
                      "status": state, "evidence": payload}
            arms[mode] = {"home": home, "doctor": doctor,
                          "doctor_bounds": (begin, end), "status": status,
                          "status_bounds": (status_begin, status_end)}
        wake = self.finding(arms["cpu"]["doctor"], "wake_health", "architect")["wake_health"]
        return {"arms": arms, "workspace": workspace,
                "doctor_time": _stamp(wake["recent_sessions"]["window_end"]),
                "status_time": _stamp(arms["cpu"]["status"]["evidence"]["observed_at"])}

    @staticmethod
    def finding(artifact, code, subject=None):
        rows = [r for r in artifact["evidence"]["findings"]
                if r["code"] == code and (subject is None or r["subject"] == subject)]
        if len(rows) != 1:
            raise AssertionError("native fixture needs one finding: " + code)
        return rows[0]

    def normalize(self, case="doctor", mode="cpu", artifact=None, **overrides):
        arm = self.fixture["arms"][mode]
        begin, end = arm[case + "_bounds"]
        arguments = dict(case=case, artifact=copy.deepcopy(arm[case]) if artifact is None else artifact,
                         home=arm["home"], workspace=self.fixture["workspace"],
                         wall_begin=begin, wall_end=end,
                         canonical_time=self.fixture[case + "_time"])
        arguments.update(overrides)
        result = _subject.normalize_artifact(**arguments)
        self.assertEqual({"artifact", "normalizations"}, set(result))
        self.assertIsInstance(result["normalizations"], list)
        for row in result["normalizations"]:
            self.assertEqual({"pointer", "reason"}, set(row))
            self.assertTrue(isinstance(row["pointer"], str) and row["pointer"].startswith("/"))
            self.assertTrue(isinstance(row["reason"], str) and row["reason"])
        return result

    def reject_doctor(self, change):
        artifact = copy.deepcopy(self.fixture["arms"]["cpu"]["doctor"])
        change(artifact)
        with self.assertRaises(ValueError):
            self.normalize(artifact=artifact)

    def test_native_doctor_cpu_and_diagnostic_artifacts_have_equal_normal_form(self):
        normalized = {mode: self.normalize(mode=mode) for mode in ("cpu", "diagnostic")}
        self.assertEqual(normalized["cpu"]["artifact"], normalized["diagnostic"]["artifact"])
        for mode, result in normalized.items():
            with self.subTest(mode=mode):
                original = self.fixture["arms"][mode]["doctor"]
                finding = self.finding(original, "host_codex_wait_window")
                relative = consent_ledger_relative("scale-worker").as_posix()
                self.assertEqual(str(self.fixture["arms"][mode]["home"] / relative),
                                 finding["wait_window"]["wait_deadline_source"])
                projected = self.finding(result["artifact"], "host_codex_wait_window")
                self.assertEqual("<bus-scale-fixture>/" + relative,
                                 projected["wait_window"]["wait_deadline_source"])
                index = original["evidence"]["findings"].index(finding)
                pointer = "/evidence/findings/" + str(index) + "/wait_window/wait_deadline_source"
                self.assertIn(pointer, {row["pointer"] for row in result["normalizations"]})

    def test_doctor_native_seed_has_a_normalizable_declared_workspace_at_both_scales(self):
        from tests.bus_scale_census import native_seeds
        with tempfile.TemporaryDirectory(prefix="bus-scale-doctor-map-",
                                         dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            workspace = base / "workspace"
            workspace.mkdir(mode=0o700)
            seeds = native_seeds(base, small_count=1, deadline=time.monotonic() + 60,
                                 workspace=workspace)
            for scale, home in seeds.items():
                with self.subTest(scale=scale):
                    begin = datetime.now(timezone.utc)
                    evidence, code = Doctor(ROOT, home, ref="HEAD", no_sandbox=True).artifact()
                    end = datetime.now(timezone.utc)
                    self.assertEqual(35, code)
                    artifact = dict(artifact_version=0, command="doctor",
                                    status="degraded", evidence=evidence)
                    normalized = _subject.normalize_artifact(
                        "doctor", artifact, home, workspace, begin, end, begin)
                    self.assertEqual("doctor", normalized["artifact"]["command"])
                    participant = resolve_participant(home, workspace)
                    self.assertIsNotNone(participant.binding)
                    self.assertFalse((home / consent_ledger_relative("scale-worker")).exists())
                    self.assertFalse((home / "receipts/codex-wait-session/scale-worker.jsonl").exists())

    def test_native_status_cpu_and_diagnostic_artifacts_have_equal_normal_form(self):
        self.assertEqual(self.normalize("status")["artifact"],
                         self.normalize("status", "diagnostic")["artifact"])

    def test_status_clock_and_clone_remedy_compose_one_complete_leaf_audit(self):
        from tests.bus_scale_integration import _roundtrip
        original = self.fixture["arms"]["diagnostic"]["status"]
        result = self.normalize("status", "diagnostic",
            canonical_time=self.fixture["status_time"] - timedelta(seconds=1))
        pointers = [row["pointer"] for row in result["normalizations"]]
        self.assertEqual(len(pointers), len(set(pointers)))
        self.assertIn("/evidence/nodes/0/wake_health/remedy", pointers)
        _roundtrip(original, result["artifact"], result["normalizations"])

    def test_input_artifact_and_native_root_bytes_are_preserved(self):
        arm = self.fixture["arms"]["cpu"]
        for case in ("doctor", "status"):
            artifact = copy.deepcopy(arm[case])
            before = _inventory(arm["home"])
            self.normalize(case, artifact=artifact)
            self.assertEqual(arm[case], artifact)
            self.assertEqual(before, _inventory(arm["home"]))

    def test_changes_are_audited_and_ids_source_and_interpreter_remain_exact(self):
        result = self.normalize()
        self.assertTrue(result["normalizations"])
        original = self.fixture["arms"]["cpu"]["doctor"]
        self.assertEqual(original["evidence"]["source"], result["artifact"]["evidence"]["source"])
        for code in ("launcher_interpreter", "manifest_exact_set"):
            self.assertEqual(self.finding(original, code), self.finding(result["artifact"], code))
        sources = self.finding(original, "seat_attention_unknown", "architect")["seat_attention"]["sources"]
        self.assertEqual(sources, self.finding(result["artifact"], "seat_attention_unknown", "architect")["seat_attention"]["sources"])

    def test_unknown_semantic_fields_are_not_silently_removed(self):
        artifact = copy.deepcopy(self.fixture["arms"]["cpu"]["doctor"])
        artifact["evidence"]["reviewed_extra_fact"] = {"timestamp": "historical fact", "count": 73}
        result = self.normalize(artifact=artifact)
        self.assertEqual(artifact["evidence"]["reviewed_extra_fact"], result["artifact"]["evidence"]["reviewed_extra_fact"])

    def test_wrong_clone_root_is_refused(self):
        self.reject_doctor(lambda a: a["evidence"].__setitem__("root", str(ROOT)))
        home = self.fixture["arms"]["cpu"]["home"]
        for source in (str(home) + "-other/" + consent_ledger_relative("scale-worker").as_posix(),
                       str(home / consent_ledger_relative("architect")),
                       "<bus-scale-fixture>/" + consent_ledger_relative("scale-worker").as_posix()):
            with self.subTest(wait_deadline_source=source):
                self.reject_doctor(lambda a: self.finding(a, "host_codex_wait_window")
                    ["wait_window"].__setitem__("wait_deadline_source", source))
        for field, value in (("consent_receipt_id", "consent-" + "f" * 32),
                             ("wait_deadline_seconds", 1), ("window_state", "matched")):
            with self.subTest(wait_window_field=field):
                self.reject_doctor(lambda a: self.finding(a, "host_codex_wait_window")
                    ["wait_window"].__setitem__(field, value))

    def test_reversed_or_naive_wall_bounds_are_refused(self):
        begin, end = self.fixture["arms"]["cpu"]["doctor_bounds"]
        for overrides in ({"wall_begin": end, "wall_end": begin},
                          {"wall_begin": begin.replace(tzinfo=None)},
                          {"canonical_time": begin.replace(tzinfo=None)}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.normalize(**overrides)

    def test_out_of_window_observation_is_refused(self):
        self.reject_doctor(lambda a: self.finding(a, "wake_health", "architect")["wake_health"].__setitem__("observed_at", "2000-01-01T00:00:00Z"))

    def test_recent_window_must_be_one_native_hour(self):
        self.reject_doctor(lambda a: self.finding(a, "wake_health", "architect")["wake_health"]["recent_sessions"].__setitem__("window_seconds", 1))

    def test_recent_session_membership_is_not_clock_noise(self):
        self.reject_doctor(lambda a: self.finding(a, "wake_health", "architect")["wake_health"]["recent_sessions"]["native_woke"]["sessions"].append({"acting_session_id": "invented"}))

    def test_delivery_count_and_age_must_match_native_history(self):
        for field, value in (("age_minutes", 1), ("node", "scale-worker")):
            with self.subTest(field=field):
                self.reject_doctor(lambda a: self.finding(a, "delivery_health", "architect")["oldest_unread"].__setitem__(field, value))

    def test_delivery_detail_is_reconstructed_not_blanket_stripped(self):
        self.reject_doctor(lambda a: self.finding(a, "delivery_health", "architect").__setitem__("detail", "architect: 999 undelivered, oldest 1m"))

    def test_seat_attention_source_identity_and_reason_are_validated(self):
        self.reject_doctor(lambda a: self.finding(a, "seat_attention_unknown", "architect")["seat_attention"]["sources"][1].__setitem__("record_id", "msg-00000000000070008000000000000000"))

    def test_acknowledgment_count_is_not_age_noise(self):
        self.reject_doctor(lambda a: self.finding(a, "acknowledgment_health", "architect")["acknowledgment"].__setitem__("delivered_unacknowledged_count", 2))

    def test_clock_derived_board_key_is_validated_before_replacement(self):
        self.reject_doctor(lambda a: self.finding(a, "wake_health", "scale-worker")["wake_health"].__setitem__("remedy", "floati seat board --idempotency-key board-notice-invented"))

    def test_disk_free_value_requires_matching_native_prose(self):
        self.reject_doctor(lambda a: self.finding(a, "host_disk_watch")["disk_watch"].__setitem__("free_bytes", 123456789123))

    def test_disk_free_boolean_is_not_a_valid_measurement(self):
        self.reject_doctor(lambda a: self.finding(a, "host_disk_watch")["disk_watch"].__setitem__("free_bytes", True))

    def test_disk_classification_cannot_cross_floor_or_hide_partial_observation(self):
        for field, value in (("exit", 1), ("partial", True), ("build_bytes", 1)):
            with self.subTest(field=field):
                self.reject_doctor(lambda a: self.finding(a, "host_disk_watch")["disk_watch"].__setitem__(field, value))

    def test_ledger_policy_size_and_age_prose_are_validated(self):
        self.reject_doctor(lambda a: self.finding(a, "ledger_roll_policy").__setitem__("detail", "live ledger is 1 bytes (policy 8388608, shipped default); oldest entry 9999.0 days old (policy 30 days)"))

    def test_doctor_severity_and_nonvolatile_detail_are_preserved(self):
        artifact = copy.deepcopy(self.fixture["arms"]["cpu"]["doctor"])
        self.finding(artifact, "manifest_exact_set")["detail"] += " EXTRA SEMANTIC FACT"
        result = self.normalize(artifact=artifact)
        self.assertEqual(self.finding(artifact, "manifest_exact_set"), self.finding(result["artifact"], "manifest_exact_set"))
        self.assertNotEqual(result["artifact"], self.normalize()["artifact"])

    def test_status_wrong_node_counts_are_refused(self):
        artifact = copy.deepcopy(self.fixture["arms"]["cpu"]["status"])
        next(n for n in artifact["evidence"]["nodes"] if n["node_id"] == "architect")["inbox_depth"] += 1
        with self.assertRaises(ValueError):
            self.normalize("status", artifact=artifact)

    def test_status_last_activity_and_ids_are_not_clock_observations(self):
        artifact = copy.deepcopy(self.fixture["arms"]["cpu"]["status"])
        artifact["evidence"]["nodes"][0]["last_activity"] = "2000-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            self.normalize("status", artifact=artifact)

    def test_status_installer_evidence_is_not_erased(self):
        artifact = copy.deepcopy(self.fixture["arms"]["cpu"]["status"])
        artifact["evidence"]["installer_shadow"]["reason"] = "different installer fact"
        result = self.normalize("status", artifact=artifact)
        self.assertEqual(artifact["evidence"]["installer_shadow"], result["artifact"]["evidence"]["installer_shadow"])
        self.assertNotEqual(result["artifact"], self.normalize("status")["artifact"])


if __name__ == "__main__":
    unittest.main()
