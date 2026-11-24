"""Status-cache equivalence controls; native fixtures, no measurements.

Draft for the owned census runner. Missing helper is an assertion RED before
fixture creation. Validation must inspect existing cache bytes without repair.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tests import HOME_PIN_ROOT
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture
from floati import cli
from floati.events import EventLog
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot

try:
    _subject = importlib.import_module("tests.bus_scale_effects")
except ModuleNotFoundError as exc:
    if exc.name != "tests.bus_scale_effects":
        raise
    _subject = None


def _inventory(home):
    return {p.relative_to(home).as_posix(): (p.stat().st_mode, p.read_bytes())
            for p in home.rglob("*") if p.is_file()}


class BusScaleStatusCacheTests(StableSourceFixture, unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(_subject, "validate_status_cache", None)),
                        "missing tests.bus_scale_effects.validate_status_cache")
        self.assertEqual(Path(HOME_PIN_ROOT), Path.home())
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-status-cache-",
                                                 dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / "scale-fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        for node in ("scale-worker", "architect"):
            Registry(self.root).register(node, "codex")
        log = EventLog(self.root)
        first = log.send("scale-worker", "architect", "floati", "a" * 40,
                         "docs/evidence/fixture.md", "fixed cache history",
                         idempotency_key="cache-first",
                         now=datetime.now(timezone.utc) - timedelta(days=2))["message"]
        log._write_delivery_receipt("architect", [first])
        state, evidence, code = cli._status(SimpleNamespace(
            root=str(self.home), json=True, consumer=None, destination=None))
        self.assertEqual(("ok", 0), (state, code))
        self.artifact = dict(artifact_version=0, command="status", status=state,
                             evidence=evidence)
        self.path = FleetProjection(self.root)._status_snapshot_store().path
        self.cache = json.loads(self.path.read_bytes())

    def write_cache(self, cache, *, resign=True):
        cache = copy.deepcopy(cache)
        if resign:
            unsigned = {key: value for key, value in cache.items() if key != "checksum"}
            raw = json.dumps(unsigned, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
            cache["checksum"] = hashlib.sha256(raw).hexdigest()
        self.path.write_text(json.dumps(cache, sort_keys=True) + "\n")

    def validate(self, artifact=None):
        return _subject.validate_status_cache(
            self.home, self.artifact if artifact is None else artifact)

    def reject_cache(self, change):
        cache = copy.deepcopy(self.cache)
        change(cache)
        self.write_cache(cache)
        before = _inventory(self.home)
        with self.assertRaises(ValueError):
            self.validate()
        self.assertEqual(before, _inventory(self.home), "validator repaired native state")

    def test_native_cache_returns_the_entire_semantic_payload(self):
        self.assertEqual(0, self.cache["payload"]["artifact"]["status_schema_version"])
        self.assertEqual(1, self.artifact["evidence"]["status_schema_version"])
        self.assertIn("scope", self.artifact["evidence"])
        self.assertIn("installer_shadow", self.artifact["evidence"])
        self.assertNotIn("scope", self.cache["payload"]["artifact"])
        self.assertNotIn("installer_shadow", self.cache["payload"]["artifact"])
        self.assertEqual(self.cache["payload"], self.validate())
        forged = copy.deepcopy(self.artifact)
        forged["evidence"]["fleet_presence"] = {"mode": "UNKNOWN", "reason": "forged"}
        with self.assertRaises(ValueError):
            self.validate(forged)

    def test_checksum_is_checked_without_a_native_rebuild(self):
        cache = copy.deepcopy(self.cache)
        cache["checksum"] = "0" * 64
        self.write_cache(cache, resign=False)
        before = _inventory(self.home)
        with self.assertRaises(ValueError):
            self.validate()
        self.assertEqual(before, _inventory(self.home))

    def test_resigned_progress_generation_cannot_be_missing_boolean_or_unknown(self):
        for defect in ("missing", True, 0, 2):
            with self.subTest(defect=defect):
                def change(cache):
                    if defect == "missing":
                        del cache["payload"]["message_progress_observation_version"]
                    else:
                        cache["payload"]["message_progress_observation_version"] = defect
                self.reject_cache(change)

    def test_resigned_wrong_cache_identity_is_rejected(self):
        for field, value in (("root", str(self.home.parent)), ("tenant_id", "foreign"),
                             ("reader", "graph"), ("key", "another-key"),
                             ("snapshot_version", 1)):
            with self.subTest(field=field):
                self.reject_cache(lambda cache: cache.__setitem__(field, value))

    def test_resigned_changed_source_anchor_is_rejected(self):
        anchor = next(row for row in self.cache["sources"] if row["path"] in {
            "events.jsonl", ".send-prefix/checkpoint.json"})
        selected_path = anchor["path"]
        changes = (("path", "foreign.jsonl"), ("byte_offset", anchor["byte_offset"] + 1),
                   ("record_ordinal", anchor["record_ordinal"] + 1),
                   ("prefix_sha256", "0" * 64),
                   ("id_fingerprints", "0" * len(anchor["id_fingerprints"])))
        for field, value in changes:
            with self.subTest(field=field):
                self.reject_cache(lambda cache: next(row for row in cache["sources"]
                    if row["path"] == selected_path).__setitem__(field, value))

    def test_missing_authoritative_source_is_rejected(self):
        self.reject_cache(lambda cache: cache["sources"].pop())

    def test_valid_partial_anchor_cannot_hide_uncovered_cold_seed_history(self):
        def change(cache):
            anchor = next(row for row in cache["sources"] if row["path"] in {
                "events.jsonl", ".send-prefix/checkpoint.json"})
            anchor.update(byte_offset=0, record_ordinal=0,
                          prefix_sha256=hashlib.sha256(b"").hexdigest(), id_fingerprints="")
        self.reject_cache(change)

    def test_resigned_payload_cannot_disagree_with_stdout(self):
        self.reject_cache(lambda cache: cache["payload"]["artifact"]["receipt_counts"]
                          .__setitem__("delivery", 999))

    def test_resigned_work_states_are_validated_against_native_sources(self):
        self.reject_cache(lambda cache: cache["payload"]["work_states"]
                          .__setitem__("invented-work", {"state": "done"}))

    def test_built_at_and_time_sensitive_are_native_facts(self):
        for field, value in (("built_at", "2000-01-01T00:00:00.000Z"),
                             ("time_sensitive", False)):
            with self.subTest(field=field):
                self.reject_cache(lambda cache: cache["payload"].__setitem__(field, value))

    def test_cached_schema_one_is_not_the_native_schema_zero_payload(self):
        self.reject_cache(lambda cache: cache["payload"]["artifact"]
                          .__setitem__("status_schema_version", 1))

    def test_stdout_must_have_exact_native_schema_one_and_identity(self):
        for field, value in (("status_schema_version", 0), ("status_schema_version", 2),
                             ("root", str(self.home.parent)), ("tenant_id", "foreign")):
            artifact = copy.deepcopy(self.artifact)
            artifact["evidence"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.validate(artifact)

    def test_unknown_semantic_artifact_fields_are_preserved_when_both_agree(self):
        extra = {"timestamp": "historical fact", "count": 73}
        cache, artifact = copy.deepcopy(self.cache), copy.deepcopy(self.artifact)
        cache["payload"]["artifact"]["reviewed_extra_fact"] = extra
        artifact["evidence"]["reviewed_extra_fact"] = copy.deepcopy(extra)
        self.write_cache(cache)
        result = self.validate(artifact)
        self.assertEqual(cache["payload"], result)
        self.assertEqual(extra, result["artifact"]["reviewed_extra_fact"])

    def test_unknown_cache_fact_cannot_be_dropped_to_match_stdout(self):
        self.reject_cache(lambda cache: cache["payload"]["artifact"]
                          .__setitem__("reviewed_extra_fact", {"count": 73}))

    def test_unknown_envelope_field_is_a_native_schema_refusal(self):
        self.reject_cache(lambda cache: cache.__setitem__("unrecognized", True))

    def test_validation_preserves_input_artifact_and_all_native_root_bytes(self):
        artifact, before = copy.deepcopy(self.artifact), _inventory(self.home)
        self.validate(artifact)
        self.assertEqual(self.artifact, artifact)
        self.assertEqual(before, _inventory(self.home))

    def test_missing_cache_is_unavailable_and_not_rebuilt(self):
        self.path.unlink()
        before = _inventory(self.home)
        with self.assertRaises(ValueError):
            self.validate()
        self.assertEqual(before, _inventory(self.home))


if __name__ == "__main__":
    unittest.main()
