"""Draft RED: independent waiter qualification and complete cache audit.

Parent adopts this under tests/ only after the active source window closes.
Pure extraction seams in census-bus-scale-2026-10-09.py:
  qualify_wait_start(primary_row, idle_row, details_by_scale) -> witness dict
  normalize_status_cache(cache, stdout_artifact, normalized_artifact)
      -> {'cache': complete cache, 'normalizations': pointer/reason list}
The latter receives a cache already proved by validate_status_cache, and the
unaltered normalize_artifact result. Neither seam launches or measures work.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from floati import cli
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.bus_scale_equivalence import normalize_artifact
from tests.bus_scale_effects import validate_status_cache
from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]  # After parent adoption under tests/.


def subject():
    spec = importlib.util.spec_from_file_location("bus_scale_integration_controls",
        ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pointer(value, coordinate, *, replacement=None, replace=False):
    if type(coordinate) is not str or not coordinate.startswith("/"):
        raise AssertionError("audit coordinate is not a JSON pointer")
    parts = [p.replace("~1", "/").replace("~0", "~") for p in coordinate.split("/")[1:]]
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    key = int(parts[-1]) if isinstance(value, list) else parts[-1]
    old = value[key]
    if replace:
        value[key] = replacement
    return old


class BusScaleWaitStartQualificationTests(unittest.TestCase):
    def setUp(self):
        self.qualify = getattr(subject(), "qualify_wait_start", None)
        self.assertTrue(callable(self.qualify), "missing producer qualify_wait_start extraction")
        self.primary = {"family": "registry", "selected_work_equal": True, "arms": {}}
        self.sibling = {"family": "registry", "selected_work_equal": True, "arms": {}}
        self.details = {}
        for scale, digest in (("1x", "a" * 64), ("10x", "b" * 64)):
            common = dict(seed_sha256=digest, clone_sha256=digest, records=1 if scale == "1x" else 10,
                bytes=100 if scale == "1x" else 1000, child_collected=True,
                coverage={"cpu": True, "bytes": True, "locks": True})
            self.primary["arms"][scale] = dict(copy.deepcopy(common), native={"exit_code": 0,
                "status": "ok", "effect": {"kind": "natural_deadline_exhausted", "count": 1}})
            self.sibling["arms"][scale] = dict(copy.deepcopy(common), native={"exit_code": 0,
                "status": "ok", "effect": {"kind": "unchanged_idle_tick", "count": 1}})
            self.details[scale] = {}
            for mode in ("cpu", "diagnostic"):
                detail = dict(mode=mode, case="wait-idle", native_exit_code=0,
                    startup={"endpoint_ns": 2_000_000_000, "self_cpu_ns": 50_000_000,
                        "collected_child_cpu_seconds": .001, "authorized_reconciliation": True,
                        "endpoint_sample_after_ns": 2_000_001_000, "reads": {},
                        "boundary": "process birth through first settled clean poll after completed authorized caught_up reconciliation"},
                    idle={"begin_ns": 2_000_000_000, "end_ns": 3_000_010_000,
                        "cpu_begin_ns": 50_000_000, "cpu_end_ns": 51_000_000,
                        "native_sleep_seconds": 1.0})
                if mode == "diagnostic":
                    detail["startup"].update(bytes_read=128, reads={"registry/entries.jsonl": 128},
                        ledger_hold_seconds=0, ledger_intervals=[], crossing_ledger_intervals=[],
                        fixture_bytes_boundary="probe installation through endpoint; source/interpreter imports excluded")
                    detail["idle"].update(bytes_read=0, ledger_hold_seconds=0)
                    detail["coverage"] = {"bytes": True, "locks": True}
                self.details[scale][mode] = detail

    def call(self, primary=None, sibling=None, details=None):
        return self.qualify(self.primary if primary is None else primary,
            self.sibling if sibling is None else sibling,
            self.details if details is None else details)

    def test_whole_deadline_primary_is_qualified_by_separate_same_seed_children(self):
        saved = copy.deepcopy((self.primary, self.sibling, self.details))
        witness = self.call()
        self.assertIs(witness.get("independent_phase_qualified"), True)
        # Whole-command CPU stays unobserved: primary has no idle/startup hook.
        self.assertNotIn("startup", self.primary["arms"]["1x"])
        self.assertEqual(saved, (self.primary, self.sibling, self.details))

    def test_deadline_receipts_alone_cannot_qualify_startup(self):
        for missing in (None, {}, {"family": "registry", "selected_work_equal": False, "arms": {}}):
            with self.subTest(sibling=missing), self.assertRaises((ValueError, RuntimeError)):
                self.qualify(self.primary, missing, self.details)

    def test_independent_phase_must_bind_same_family_and_each_exact_seed(self):
        for defect in ("family", "seed", "records", "bytes", "clone"):
            sibling = copy.deepcopy(self.sibling)
            if defect == "family":
                sibling["family"] = "events"
            else:
                field = {"seed": "seed_sha256", "records": "records", "bytes": "bytes", "clone": "clone_sha256"}[defect]
                sibling["arms"]["10x"][field] = "c" * 64 if field.endswith("sha256") else 123
            with self.subTest(defect=defect), self.assertRaises((ValueError, RuntimeError)):
                self.call(sibling=sibling)

    def test_either_independent_mode_missing_or_incomplete_blocks_qualification(self):
        for scale in ("1x", "10x"):
            for mode in ("cpu", "diagnostic"):
                for defect in ("missing", "truthy-placeholder", "wrong-case", "wrong-mode", "exit"):
                    details = copy.deepcopy(self.details)
                    if defect == "missing":
                        del details[scale][mode]
                    elif defect == "truthy-placeholder":
                        details[scale][mode] = {"startup": {"claimed": True}, "idle": {"end_ns": 1}}
                    else:
                        field, value = {"wrong-case": ("case", "wait-start"),
                            "wrong-mode": ("mode", "foreign"), "exit": ("native_exit_code", 31)}[defect]
                        details[scale][mode][field] = value
                    with self.subTest(scale=scale, mode=mode, defect=defect), self.assertRaises((ValueError, RuntimeError)):
                        self.call(details=details)

    def test_holder_only_or_truncated_idle_interval_is_not_completed_reconciliation(self):
        changes = (
            ("startup", "authorized_reconciliation", False),
            ("startup", "endpoint_ns", 1_999_999_999),
            ("idle", "end_ns", 2_100_000_000),
            ("idle", "native_sleep_seconds", .1),
            ("idle", "cpu_end_ns", 49_999_999),
        )
        for part, field, value in changes:
            details = copy.deepcopy(self.details)
            details["1x"]["cpu"][part][field] = value
            with self.subTest(field=field), self.assertRaises((ValueError, RuntimeError)):
                self.call(details=details)

    def test_missing_collection_or_metric_coverage_cannot_be_replaced_by_boolean_claim(self):
        for arm_name in ("primary", "sibling"):
            for defect in ("uncollected", "coverage", "outcome"):
                primary, sibling = copy.deepcopy(self.primary), copy.deepcopy(self.sibling)
                arm = (primary if arm_name == "primary" else sibling)["arms"]["10x"]
                if defect == "uncollected":
                    arm["child_collected"] = False
                elif defect == "coverage":
                    arm["coverage"]["locks"] = False
                else:
                    arm["native"]["effect"]["kind"] = "holder_started"
                with self.subTest(arm=arm_name, defect=defect), self.assertRaises((ValueError, RuntimeError)):
                    self.call(primary, sibling)


class BusScaleCacheAuditIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.normalize = getattr(subject(), "normalize_status_cache", None)
        self.assertTrue(callable(self.normalize), "missing producer normalize_status_cache extraction")
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-cache-audit-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home, self.workspace = self.base / "scale-fleet", self.base / "workspace"
        self.workspace.mkdir(mode=0o700)
        root = FloatiRoot.open_direct_home(self.home, create=True)
        for node in ("architect", "scale-worker"):
            Registry(root).register(node, "codex")
        self.begin = datetime.now(timezone.utc)
        state, evidence, code = cli._status(SimpleNamespace(root=str(self.home), json=True,
            consumer=None, destination=None))
        self.end = datetime.now(timezone.utc)
        self.assertEqual(("ok", 0), (state, code))
        self.artifact = dict(artifact_version=0, command="status", status=state, evidence=evidence)
        self.path = FleetProjection(root)._status_snapshot_store().path
        self.cache = json.loads(self.path.read_bytes())
        validate_status_cache(self.home, self.artifact)
        own = datetime.fromisoformat(evidence["nodes"][0]["wake_health"]["recent_sessions"]["window_end"].replace("Z", "+00:00"))
        self.normalized = normalize_artifact("status", self.artifact, self.home, self.workspace,
            self.begin, self.end, own - timedelta(seconds=1))

    def call(self, cache=None, artifact=None, normalized=None):
        return self.normalize(self.cache if cache is None else cache,
            self.artifact if artifact is None else artifact,
            self.normalized if normalized is None else normalized)

    def test_complete_cache_roundtrip_covers_every_actual_changed_leaf(self):
        saved = copy.deepcopy((self.cache, self.artifact, self.normalized))
        disk = self.path.read_bytes(), self.path.stat().st_mode
        result = self.call()
        self.assertEqual({"cache", "normalizations"}, set(result))
        restored = copy.deepcopy(result["cache"])
        audit = result["normalizations"]
        self.assertTrue(audit)
        self.assertEqual(len(audit), len({row["pointer"] for row in audit}))
        for row in audit:
            self.assertEqual({"pointer", "reason"}, set(row))
            self.assertTrue(row["reason"].strip())
            old, new = pointer(self.cache, row["pointer"]), pointer(restored, row["pointer"])
            self.assertNotEqual(old, new)
            pointer(restored, row["pointer"], replacement=old, replace=True)
        self.assertEqual(self.cache, restored, "unlisted semantic cache change")
        self.assertEqual(saved, (self.cache, self.artifact, self.normalized))
        self.assertEqual(disk, (self.path.read_bytes(), self.path.stat().st_mode))

    def test_cache_retains_schema_zero_all_anchors_and_payload_extensions(self):
        cache, artifact, normalized = copy.deepcopy((self.cache, self.artifact, self.normalized))
        extra = {"literal/time": "2000-01-01T00:00:00Z", "~id": "msg-" + "f" * 32,
                 "root": str(self.home)}
        # Unknown extension is identical in both proven semantic inputs and is
        # deliberately outside the native normalizer's selected coordinates.
        for evidence in (cache["payload"]["artifact"], artifact["evidence"], normalized["artifact"]["evidence"]):
            evidence["future_testimony"] = copy.deepcopy(extra)
        unsigned = {key: value for key, value in cache.items() if key != "checksum"}
        cache["checksum"] = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False,
            allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result = self.call(cache, artifact, normalized)
        self.assertEqual(extra, result["cache"]["payload"]["artifact"]["future_testimony"])
        self.assertEqual(0, result["cache"]["payload"]["artifact"]["status_schema_version"])
        self.assertNotIn("scope", result["cache"]["payload"]["artifact"])
        self.assertNotIn("installer_shadow", result["cache"]["payload"]["artifact"])
        self.assertEqual(cache["sources"], result["cache"]["sources"])
        for field in ("work_states", "time_sensitive"):
            self.assertEqual(cache["payload"][field], result["cache"]["payload"][field])
        self.assertFalse(any("future_testimony" in row["pointer"] for row in result["normalizations"]))

    def test_missing_or_duplicate_stdout_audit_is_not_silently_transplanted(self):
        for defect in ("missing", "duplicate", "nonexistent", "unchanged"):
            normalized = copy.deepcopy(self.normalized)
            if defect == "missing":
                normalized["normalizations"].pop()
            elif defect == "duplicate":
                normalized["normalizations"].append(copy.deepcopy(normalized["normalizations"][0]))
            else:
                normalized["normalizations"].append({"pointer": "/evidence/absent" if defect == "nonexistent"
                    else "/evidence/tenant_id", "reason": "controlled invalid audit"})
            with self.subTest(defect=defect), self.assertRaises((ValueError, RuntimeError)):
                self.call(normalized=normalized)

    def test_unaudited_stable_extension_change_is_rejected(self):
        normalized = copy.deepcopy(self.normalized)
        normalized["artifact"]["evidence"]["new_unlisted_testimony"] = True
        with self.assertRaises((ValueError, RuntimeError)):
            self.call(normalized=normalized)

    def test_mismatched_original_cache_to_stdout_cannot_be_overwritten_by_normal_form(self):
        cache = copy.deepcopy(self.cache)
        cache["payload"]["artifact"]["tenant_id"] = "foreign"
        with self.assertRaises((ValueError, RuntimeError)):
            self.call(cache=cache)

    def test_input_audit_of_stdout_only_scope_is_excluded_from_cache_pointers(self):
        result = self.call()
        self.assertTrue(any(row["pointer"] == "/evidence/scope/root"
                            for row in self.normalized["normalizations"]))
        self.assertFalse(any("/scope" in row["pointer"] or "/installer_shadow" in row["pointer"]
                             for row in result["normalizations"]))
        self.assertIn("/root", {row["pointer"] for row in result["normalizations"]})
        self.assertIn("/checksum", {row["pointer"] for row in result["normalizations"]})
        self.assertIn("/payload/built_at", {row["pointer"] for row in result["normalizations"]})


if __name__ == "__main__":
    unittest.main()
