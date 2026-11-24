"""Only the two ruled, fixture-owned cache literals may leave a seed."""
from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from floati.events import EventLog
from floati.registry import Registry
from tests.banked_sha_fixture import banked_send_repository
from tests.bus_scale_case_seeds import FixtureSendCache, inventory
from tests.bus_scale_seeds import _native_prelude
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture
from tests.bus_scale_test_profile import stable_fixture_sources


class FixtureRegistryCacheTests(StableSourceFixture, unittest.TestCase):
    @stable_fixture_sources()
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="bus-scale-census-owned-registry-cleanup-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.workspace, self.sha = banked_send_repository(self.base)
        self.scratch = self.base / "fixture"
        self.scratch.mkdir(mode=0o700)
        self.root = _native_prelude(self.scratch / "scale-fleet")

    def owner(self):
        return FixtureSendCache(self.scratch, self.root.path)

    def send(self, key="registry-cache-cleanup"):
        return EventLog(self.root).send("scale-worker", "architect", "floati", self.sha,
            "note.md", "native fixture cache ownership", idempotency_key=key)["message"]

    def registry_bank(self):
        with Registry(self.root).read_once(node_id="scale-worker") as view:
            view.require_active("scale-worker")
        bank = self.root.path / ".registry-prefix"
        self.assertTrue(bank.is_dir(),
            "native selected registry reads must create their disposable prefix")
        return bank

    def assert_refused_unchanged(self, code, operation):
        before = inventory(self.base)
        with self.assertRaises(ValueError) as caught:
            operation()
        self.assertEqual(code, getattr(caught.exception, "code", None))
        self.assertEqual(before, inventory(self.base),
            "cleanup refusal must precede all fixture and foreign mutation")

    def test_native_cleanup_removes_both_literals_and_preserves_every_other_byte(self):
        owner = self.owner()
        message = self.send()
        registry = self.registry_bank()
        unruled = self.root.path / ".future-prefix"
        unruled.mkdir(mode=0o700)
        (unruled / "checkpoint.json").write_bytes(b"not a ruled cache literal\n")
        before = inventory(self.root.path)
        self.assertTrue((self.root.path / ".send-prefix").is_dir())
        self.assertEqual(self.scratch.stat().st_dev, registry.stat().st_dev)
        receipt = owner.remove([message["id"]])
        retained = {name: raw for name, raw in before.items()
            if Path(name).parts[0] not in {".send-prefix", ".registry-prefix"}}
        self.assertEqual(retained, inventory(self.root.path))
        self.assertFalse((self.root.path / ".send-prefix").exists())
        self.assertFalse(registry.exists(), "the registry cache must be gone before immutable capture")
        self.assertEqual("ok", receipt["status"])
        self.assertEqual([message["id"]], receipt["own_send_ids"])
        self.assertTrue(receipt["same_device"])
        self.assertTrue(receipt["no_symlinks"])

    def test_foreign_registry_literal_refuses_without_touching_either_root(self):
        owner = self.owner()
        message = self.send()
        foreign = self.base / "foreign" / ".registry-prefix"
        foreign.mkdir(parents=True, mode=0o700)
        (foreign / "checkpoint.json").write_bytes(b"foreign registry cache\n")
        self.assert_refused_unchanged("fixture_cache_outside_root",
            lambda: owner.remove([message["id"]], cache_path=foreign))

    def test_preexisting_registry_literal_never_acquires_fixture_send_ownership(self):
        self.registry_bank()
        self.assert_refused_unchanged("fixture_cache_preexisting", self.owner)

    def test_registry_cache_without_new_native_sends_has_no_removal_authority(self):
        owner = self.owner()
        self.registry_bank()
        self.assert_refused_unchanged("fixture_cache_own_sends_missing",
            lambda: owner.remove([]))

    def test_registry_only_recreation_is_removed_by_the_original_fixture_owner(self):
        owner = self.owner()
        message = self.send()
        self.registry_bank()
        owner.remove([message["id"]])
        self.assertFalse((self.root.path / ".send-prefix").exists())
        registry = self.registry_bank()
        before = inventory(self.root.path)
        owner.remove([message["id"]])
        self.assertEqual({name: raw for name, raw in before.items()
            if Path(name).parts[0] != ".registry-prefix"}, inventory(self.root.path))
        self.assertFalse(registry.exists(),
            "an outer owner must handle registry-only recreation after inner cleanup")

    def test_registry_symlink_is_rejected_before_the_send_cache_is_removed(self):
        owner = self.owner()
        message = self.send()
        registry = self.registry_bank()
        foreign = self.base / "foreign-registry"
        registry.rename(foreign)
        registry.symlink_to(foreign, target_is_directory=True)
        send_before = inventory(self.root.path / ".send-prefix")
        foreign_before = inventory(foreign)
        events_before = (self.root.path / "events.jsonl").read_bytes()
        with self.assertRaises(ValueError) as caught:
            owner.remove([message["id"]])
        self.assertEqual("fixture_cache_symlink", getattr(caught.exception, "code", None))
        self.assertTrue(registry.is_symlink())
        self.assertEqual(send_before, inventory(self.root.path / ".send-prefix"))
        self.assertEqual(foreign_before, inventory(foreign))
        self.assertEqual(events_before, (self.root.path / "events.jsonl").read_bytes())

    def test_both_cache_subtrees_are_validated_before_either_is_removed(self):
        owner = self.owner()
        message = self.send()
        registry = self.registry_bank()
        send_before = inventory(self.root.path / ".send-prefix")
        registry_before = inventory(registry)
        foreign = self.base / "foreign-sentinel"
        foreign.write_bytes(b"foreign bytes must survive\n")
        link = registry / "staging" / "foreign"
        link.symlink_to(foreign)
        with self.assertRaises(ValueError) as caught:
            owner.remove([message["id"]])
        self.assertEqual("fixture_cache_symlink", getattr(caught.exception, "code", None))
        self.assertTrue(link.is_symlink())
        self.assertEqual(b"foreign bytes must survive\n", foreign.read_bytes())
        link.unlink()
        self.assertEqual(send_before, inventory(self.root.path / ".send-prefix"))
        self.assertEqual(registry_before, inventory(registry))

    def test_registry_bank_on_another_device_refuses_before_any_removal(self):
        owner = self.owner()
        message = self.send()
        registry = self.registry_bank()
        native_lstat = Path.lstat

        def cross_device(path, *args, **kwargs):
            metadata = native_lstat(path, *args, **kwargs)
            if path != registry:
                return metadata
            return SimpleNamespace(st_dev=metadata.st_dev + 1, st_ino=metadata.st_ino,
                st_mode=metadata.st_mode, st_uid=metadata.st_uid, st_nlink=metadata.st_nlink)

        before = inventory(self.base)
        with patch.object(Path, "lstat", cross_device):
            with self.assertRaises(ValueError) as caught:
                owner.remove([message["id"]])
        self.assertEqual("fixture_cache_device_mismatch", getattr(caught.exception, "code", None))
        self.assertEqual(before, inventory(self.base))
