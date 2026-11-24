"""Complete registry cache effects use native KeyPrefix proof, never a path waiver."""
from __future__ import annotations

import importlib
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from floati.registry import Registry
from floati.registry_prefix import RegistryPrefix
from floati.root import FloatiRoot
from floati.send_prefix import _bytes, _key
from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import raw_candidate_delta
from tests.bus_scale_test_profile import StableSourceFixture


def inventory(home):
    return {path.relative_to(home).as_posix(): path.read_bytes()
            for path in home.rglob("*") if path.is_file()}


class RegistryPrefixEffectTests(StableSourceFixture, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.registry = Registry(self.root)
        self.row = self.registry.register("builder", "Codex")
        self.before = inventory(self.root.tenant_home)
        self.prefix_input = self.subject().capture_registry_prefix_input(self.root.tenant_home, self.before)
        with self.registry.read_once(node_id="builder") as view:
            view.require_active("builder")

    def subject(self):
        name = "tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects"
        self.assertIsNotNone(importlib.util.find_spec(name),
            "registry effects need the existing complete KeyPrefix proof admission")
        return importlib.import_module(name)

    def prove(self):
        after = inventory(self.root.tenant_home)
        try:
            return self.subject().verify_registry_prefix_effect(self.root.tenant_home,
                self.before, after, raw_candidate_delta(self.before, after), self.prefix_input)
        finally:
            self.assertEqual(after, inventory(self.root.tenant_home),
                "complete cache proof must be physically read-only")

    def test_complete_native_bank_has_a_typed_proof_and_no_authoritative_change(self):
        proof = self.prove()
        self.assertEqual("complete_native_registry_prefix", proof["kind"])
        self.assertEqual(1, proof["registry_records"])
        self.assertTrue(proof["changed_files"])
        self.assertTrue(all(Path(path).parts[0] == ".registry-prefix" for path in proof["changed_files"]))

    def test_complete_proof_covers_the_active_catalog_and_timestamp_fold(self):
        prefix = RegistryPrefix(self.root)
        prefix.load()
        self.assertEqual({"max_timestamp": "chart-time-text-v1"},
            prefix.binding.get("folds"), "the chart fold must declare its sealed contract")
        self.assertEqual({"max_timestamp": self.row["timestamp"]},
            prefix.payload.get("folds"))
        self.assertEqual(["builder"], prefix.get("catalog", "active"))
        self.assertEqual([["builder", "Codex", None, None]], prefix.get("roster", "active"))
        self.assertEqual(3, self.prove()["complete_map_entries"],
            "the complete proof must include the catalog as well as registrations")

    def test_validly_resealed_false_timestamp_fold_is_rejected_from_native_history(self):
        path = self.root.tenant_home / ".registry-prefix/checkpoint.json"
        value = json.loads(path.read_bytes())
        self.assertIn("folds", value["payload"], "the historical maximum belongs in the checkpoint")
        value["payload"]["folds"]["max_timestamp"] = "2099-01-01T00:00:00.000Z"
        value["seal"] = hmac.new((path.parent / "seal.key").read_bytes(),
            _bytes(value["payload"]), hashlib.sha256).hexdigest()
        path.write_bytes(_bytes(value))
        with self.assertRaisesRegex(ValueError, "fold"):
            self.prove()

    def test_validly_resealed_false_active_catalog_is_rejected_from_native_history(self):
        prefix = RegistryPrefix(self.root)
        prefix.load()
        self.assertEqual(["builder"], prefix.get("catalog", "active"),
            "the active catalog must be in the complete sealed map")
        for domain in ("catalog", "roster"):
            with self.subTest(domain=domain):
                lookup = _key(domain, "active")
                target = prefix.nodes / prefix.filename(lookup)
                original = target.read_bytes()
                try:
                    target.write_bytes(_bytes(prefix._entry(lookup, [])))
                    with self.assertRaisesRegex(ValueError, "complete native map entry"):
                        self.prove()
                finally:
                    target.write_bytes(original)

    def test_validly_resealed_false_latest_row_is_rejected_by_native_map_comparison(self):
        from floati.ids import uuid7_hex
        from floati.jsonl import transact_records
        from floati.registry import REGISTRY_KINDS

        first_role = {
            "schema_version": 0, "id": "registry-role-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": self.row["timestamp"],
            "kind": "registry_role_record", "node_id": "builder",
            "template_role": "builder", "template_version": 2,
            "template_sha256": "a" * 64,
            "answers": {"repo": "floati", "never_touch": "shared-core", "reports_to": "architect"},
            "state": "active", "predecessor_role_record_id": None,
        }
        latest_role = dict(first_role, id="registry-role-" + uuid7_hex(),
            template_sha256="b" * 64, predecessor_role_record_id=first_role["id"])
        transact_records(self.root, "registry/entries.jsonl",
            lambda _prior: (None, [first_role, latest_role]), allowed_kinds=REGISTRY_KINDS)
        self.before = inventory(self.root.tenant_home)
        self.prefix_input = self.subject().capture_registry_prefix_input(self.root.tenant_home, self.before)
        prefix = RegistryPrefix(self.root)
        prefix.load()
        self.assertEqual(latest_role, prefix.get("role", "builder"))
        self.assertEqual(4, self.prove()["complete_map_entries"])
        for domain, false_row in (("registration", dict(self.row, state="retired")),
                                  ("role", first_role)):
            with self.subTest(domain=domain):
                lookup = _key(domain, "builder")
                target = prefix.nodes / prefix.filename(lookup)
                original = target.read_bytes()
                try:
                    target.write_bytes(_bytes(prefix._entry(lookup, false_row)))
                    with self.assertRaises(ValueError):
                        self.prove()
                finally:
                    target.write_bytes(original)

    def test_missing_positive_is_refused_without_repair(self):
        prefix = RegistryPrefix(self.root)
        (prefix.nodes / prefix.filename(_key("registration", "builder"))).unlink()
        with self.assertRaises(ValueError):
            self.prove()

    def test_extra_unreachable_file_is_not_admitted(self):
        target = self.root.tenant_home / ".registry-prefix/staging/unexplained"
        target.write_bytes(b"unexplained cache payload\n")
        target.chmod(0o600)
        with self.assertRaises(ValueError):
            self.prove()

    def test_other_prefix_literal_is_left_for_the_original_native_effect_fence(self):
        target = self.root.tenant_home / ".registry-prefix-foreign"
        target.mkdir(mode=0o700)
        (target / "payload").write_bytes(b"foreign literal\n")
        proof = self.prove()
        self.assertNotIn(".registry-prefix-foreign/payload", proof["changed_files"])

    def test_warm_clone_admission_proves_its_complete_bank_and_all_noncache_bytes(self):
        from floati.registry_prefix import warm
        import shutil
        seed = inventory(self.root.tenant_home)
        clone = self.base / "clone" / self.root.tenant_home.name
        shutil.copytree(self.root.tenant_home, clone)
        self.assertIsNotNone(warm(FloatiRoot.open_direct_home(clone)))
        before = inventory(clone)
        proof = self.subject().verify_warmed_registry_clone(clone, seed)
        self.assertEqual("complete_native_registry_warm_clone", proof["kind"])
        self.assertEqual(before, inventory(clone))
        (clone / "unexplained").write_bytes(b"unexplained noncache bytes\n")
        after = inventory(clone)
        with self.assertRaises(ValueError):
            self.subject().verify_warmed_registry_clone(clone, seed)
        self.assertEqual(after, inventory(clone))

    def test_changed_registry_source_is_not_a_read_rebuild_effect(self):
        self.registry.register("other", "Codex")
        with self.assertRaises(ValueError):
            self.prove()

    def test_retained_native_registry_banks_compare_only_after_complete_proof(self):
        import os
        import shutil
        from floati.registry_prefix import warm
        from tests.temp_roots import REAL_TEMP_ROOT
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        path = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py"
        spec = importlib.util.spec_from_file_location("retained_registry_tree_control", path)
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        projections, directories = [], []
        for label in ("one", "two"):
            home = base / label / "fleet"
            shutil.copytree(self.root.tenant_home, home)
            self.assertIsNotNone(warm(FloatiRoot.open_direct_home(home)))
            raw = inventory(home)
            tree = observer.tree(home)
            captured = self.subject().capture_registry_prefix_input(home, raw)
            projections.append(observer.retained_registry_tree(home, tree, tree, raw, raw, captured))
            directories.append(raw[".registry-prefix/directory.bin"])
        self.assertNotEqual(directories[0], directories[1])
        self.assertEqual(projections[0], projections[1])
        for fault in ("bytes", "metadata"):
            with self.subTest(fault=fault):
                target = home / ".registry-prefix/directory.bin"
                saved, info = target.read_bytes(), target.stat()
                if fault == "bytes":
                    target.write_bytes(bytes([saved[0] ^ 1]) + saved[1:])
                else:
                    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
                try:
                    with self.assertRaises(observer.UnresolvedObservation):
                        observer.retained_registry_tree(home, tree, observer.tree(home), raw, inventory(home), captured)
                finally:
                    target.write_bytes(saved)
                    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))

    def test_foreign_binding_missing_bucket_and_symlink_each_refuse_without_repair(self):
        import shutil
        bank = self.root.tenant_home / ".registry-prefix"
        saved = self.base / "saved-bank"
        shutil.copytree(bank, saved)
        for mutation in ("foreign_binding", "missing_bucket", "symlink"):
            with self.subTest(mutation=mutation):
                shutil.rmtree(bank)
                shutil.copytree(saved, bank)
                if mutation == "foreign_binding":
                    path = bank / "checkpoint.json"
                    value = json.loads(path.read_bytes())
                    value["payload"]["binding"]["root"] = str(self.base / "foreign-root")
                    value["seal"] = hmac.new((bank / "seal.key").read_bytes(),
                        _bytes(value["payload"]), hashlib.sha256).hexdigest()
                    path.write_bytes(_bytes(value))
                elif mutation == "missing_bucket":
                    next((bank / "buckets").iterdir()).unlink()
                else:
                    target = next((bank / "nodes").iterdir())
                    foreign = self.base / "foreign-entry"
                    foreign.write_bytes(target.read_bytes())
                    target.unlink()
                    target.symlink_to(foreign)
                with self.assertRaises(ValueError):
                    self.prove()
