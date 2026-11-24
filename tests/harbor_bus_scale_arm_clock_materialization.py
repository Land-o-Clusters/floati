"""Native fixture materialization controls; no census/performance claim."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from floati.framing import decode_frames, encode_frame
from floati.errors import IntegrityFailure
from floati.events import EventLog
from floati.presence import PresenceService
from floati.root import FloatiRoot
from floati.supervisor import Supervisor
from tests.bus_scale_test_profile import banked_send_repository
from tests.bus_scale_arm_clock import materialize_attention_pair, publish_attention_context
from tests.bus_scale_case_seeds import inventory, make_case_seed_pair
from tests.bus_scale_test_profile import seed_counts, StableSourceFixture
from tests import bus_scale_case_seeds
from tests.bus_scale_seeds import _native_prelude
from tests.temp_roots import REAL_TEMP_ROOT
from tests.harbor_bus_scale_arm_clock import EarlierNativeMailPreparation, ENTRY, load
from tests.harbor_bus_scale_attention_proof import AttentionSeedPreparation


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


class AttentionSeedMaterializationTests(StableSourceFixture, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-clock-materialization-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.workspace, sha = banked_send_repository(self.base)
        self.workspace.chmod(0o700)
        seeds = self.base / "seeds"
        seeds.mkdir(mode=0o700)
        helper = load(ENTRY, "materialization_native_entry").routine_module("supervise")
        self.preparation = EarlierNativeMailPreparation(helper, sha)
        self.pair = make_case_seed_pair(seeds, "registry", "supervise", self.preparation, self.workspace,
                                        counts=seed_counts())
        self.bank = self.base / "arm-bank"
        self.bank.mkdir(mode=0o700)

    def test_native_materialization_preserves_pending_identity_and_exact_inverse_bytes(self):
        before = {scale: inventory(self.pair[scale]) for scale in ("1", "10")}
        context = copy.deepcopy(self.pair["context"])
        begin = datetime.now(timezone.utc)
        materialized = materialize_attention_pair(self.pair, "supervise", self.bank)
        end = datetime.now(timezone.utc)
        binding = materialized["clock_materialization"]
        self.assertEqual({"path", "sha256"}, set(binding))
        receipt_path = Path(binding["path"])
        self.assertEqual(self.bank / "seed-clock.json", receipt_path)
        self.assertEqual(hashlib.sha256(receipt_path.read_bytes()).hexdigest(), binding["sha256"])
        receipt = json.loads(receipt_path.read_bytes())
        anchor = datetime.fromisoformat(receipt["real_anchor"])
        self.assertLessEqual(begin, anchor)
        self.assertLessEqual(anchor, end)
        self.assertIs(type(receipt["offset_ms"]), int)
        self.assertGreater(receipt["offset_ms"], 900000)
        self.assertEqual(context, self.pair["context"])
        for scale, count in zip(("1", "10"), seed_counts()):
            self.assertEqual(before[scale], inventory(self.pair[scale]))
            self.assertNotEqual(Path(self.pair[scale]), materialized[scale])
            self.assertNotIn(self.bank, materialized[scale].parents)
            actual = inventory(materialized[scale])
            self.assertEqual({k: v for k, v in before[scale].items() if k != "events.jsonl"},
                             {k: v for k, v in actual.items() if k != "events.jsonl"})
            proof = receipt["scales"][scale]
            self.assertEqual(count, proof["primary_records"])
            self.assertEqual(proof["template_inventory"], proof["inverse_inventory"])
            self.assertEqual({"records": True, "event_cross_validation": True, "cursor_cross_validation": True},
                             proof["native_validators"])
            original_rows = decode_frames(before[scale]["events.jsonl"])
            rows = decode_frames(actual["events.jsonl"])
            for row, original in zip(rows, original_rows):
                restored = copy.deepcopy(row)
                for leaf in proof["changed_leaves"]:
                    if leaf["record_id"] != row["id"]:
                        continue
                    target = restored
                    parts = leaf["pointer"].strip("/").split("/")
                    for part in parts[:-1]:
                        target = target[part]
                    self.assertEqual(leaf["after"], target[parts[-1]])
                    old = datetime.fromisoformat(leaf["before"].replace("Z", "+00:00"))
                    new = datetime.fromisoformat(leaf["after"].replace("Z", "+00:00"))
                    self.assertEqual(timedelta(milliseconds=receipt["offset_ms"]), new - old)
                    target[parts[-1]] = leaf["before"]
                self.assertEqual(original, restored)
            self.assertEqual(before[scale]["events.jsonl"], b"".join(encode_frame(row) for row in original_rows))
            native = Supervisor(FloatiRoot.open_direct_home(materialized[scale], create=False)).snapshot(end)
            architect = next(row for row in native["nodes"] if row["node_id"] == "architect")
            self.assertEqual(("not_overdue", "fresh_waiting_mail", 1, "", ""),
                tuple(architect["seat_attention"][key] for key in ("state", "reason", "undelivered_count", "detail", "remedy")))
            self.assertEqual(self.preparation.message["id"], rows[0]["id"])
            self.assertEqual("native arm-clock selected mail", rows[0]["note"])
        with self.assertRaises(ValueError):
            materialize_attention_pair(self.pair, "supervise", self.bank)

    def test_forged_readiness_clock_refuses_before_detached_publication(self):
        source = self.pair["1"] / "events.jsonl"
        rows = decode_frames(source.read_bytes())
        observation = next(row for row in rows if row["kind"] == "send_readiness_observation")
        observation["readiness"]["observed_at"] = "1970-01-01T00:00:00.000Z"
        source.write_bytes(b"".join(encode_frame(row) for row in rows))
        poisoned = inventory(self.pair["1"])
        with self.assertRaises((IntegrityFailure, ValueError)) as raised:
            materialize_attention_pair(self.pair, "supervise", self.bank)
        self.assertIn("readiness", str(raised.exception).lower())
        self.assertEqual(poisoned, inventory(self.pair["1"]))
        self.assertEqual([], list(self.bank.iterdir()))
        self.assertEqual([], list(Path(self.pair["preparation_bank"]).parent.glob("arm-clock-*")))

    def test_positive_presence_profile_refuses_without_translating_expiry(self):
        root = FloatiRoot.open_direct_home(self.pair["1"], create=False)
        PresenceService(root).report_self("architect", ttl_seconds=60, now=datetime.now(timezone.utc))
        before = inventory(self.pair["1"])
        with self.assertRaisesRegex(ValueError, "unsupported positive sidecar"):
            materialize_attention_pair(self.pair, "supervise", self.bank)
        self.assertEqual(before, inventory(self.pair["1"]))
        self.assertEqual([], list(self.bank.iterdir()))

    def test_native_hint_profile_rejects_bytes_permissions_and_unbound_recipients(self):
        home = self.pair["1"]
        hint = home / "state/wake-hints/architect/generation"
        self.assertEqual(b"", hint.read_bytes(), "native preparation must retain its real empty send hint")
        mode = hint.stat().st_mode & 0o777
        for poison in ("bytes", "mode", "unbound"):
            with self.subTest(poison=poison):
                unbound = home / "state/wake-hints/unbound/generation"
                if poison == "bytes":
                    hint.write_bytes(b"unreviewed state")
                elif poison == "mode":
                    hint.chmod(0o640)
                else:
                    unbound.parent.mkdir(mode=0o700)
                    unbound.write_bytes(b"")
                    unbound.chmod(0o600)
                before = inventory(home)
                try:
                    with self.assertRaises(ValueError):
                        materialize_attention_pair(self.pair, "supervise", self.bank)
                    self.assertEqual(before, inventory(home))
                    self.assertEqual([], list(self.bank.iterdir()))
                finally:
                    hint.write_bytes(b"")
                    hint.chmod(mode)
                    if unbound.exists():
                        unbound.unlink()
                        unbound.parent.rmdir()

    def test_context_publication_accepts_only_known_clock_changes_and_preserves_mode(self):
        original = self.pair["context"]
        materialized = copy.deepcopy(original)
        materialized["canonical_time"] = (datetime.fromisoformat(original["canonical_time"].replace("Z", "+00:00"))
                                          + timedelta(seconds=960)).isoformat()
        path = Path(self.pair["prepared_home"]).parent / "case-context.json"
        path.write_bytes(canonical(original))
        path.chmod(0o600)
        publish_attention_context(path, original, materialized)
        self.assertEqual(canonical(materialized), path.read_bytes())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        metadata = path.stat()
        publish_attention_context(path, original, materialized)
        self.assertEqual(metadata.st_mtime_ns, path.stat().st_mtime_ns)
        unknown = copy.deepcopy(materialized)
        unknown["canonical_time"] = "1970-01-01T00:00:00+00:00"
        path.write_bytes(canonical(unknown))
        with self.assertRaisesRegex(ValueError, "known arm context"):
            publish_attention_context(path, original, materialized)
        self.assertEqual(canonical(unknown), path.read_bytes())

    def test_context_alias_shared_file_and_nonclock_poisoning_refuse_unchanged(self):
        original = self.pair["context"]
        desired = copy.deepcopy(original)
        desired["canonical_time"] = datetime.now(timezone.utc).isoformat()
        path = Path(self.pair["prepared_home"]).parent / "case-context.json"
        target = self.base / "foreign-context.json"
        target.write_bytes(canonical(original))
        path.symlink_to(target)
        try:
            with self.assertRaises(ValueError):
                publish_attention_context(path, original, desired)
            self.assertTrue(path.is_symlink())
            self.assertEqual(canonical(original), target.read_bytes())
        finally:
            path.unlink()
        os.link(target, path)
        try:
            with self.assertRaises(ValueError):
                publish_attention_context(path, original, desired)
            self.assertEqual(2, path.stat().st_nlink)
            self.assertEqual(canonical(original), path.read_bytes())
        finally:
            path.unlink()
        path.write_bytes(canonical(original))
        wrong = copy.deepcopy(desired)
        wrong["workspace"] = str(self.base)
        with self.assertRaisesRegex(ValueError, "non-clock"):
            publish_attention_context(path, original, wrong)
        self.assertEqual(canonical(original), path.read_bytes())


class AttentionFixtureCacheCleanupTests(StableSourceFixture, unittest.TestCase):
    """Removing disposable cache must never broaden an immutable seed fence."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-cache-cleanup-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.workspace, self.sha = banked_send_repository(self.base)
        self.workspace.chmod(0o700)
        self.scratch = self.base / "fixture"
        self.scratch.mkdir(mode=0o700)
        self.root = _native_prelude(self.scratch / "scale-fleet")
        self.bank = self.scratch / "native-preparation"
        self.bank.mkdir(mode=0o700)

    def cleanup_owner(self):
        factory = getattr(bus_scale_case_seeds, "FixtureSendCache", None)
        self.assertTrue(callable(factory), "fixture cache removal needs an explicit confined ownership step")
        return factory(self.scratch, self.root.path)

    def send(self):
        return EventLog(self.root).send("scale-worker", "architect", "floati", self.sha,
            "note.md", "native fixture cleanup control", idempotency_key="fixture-cache-control")["message"]

    def assert_refused(self, code, operation):
        before = inventory(self.base)
        with self.assertRaises(ValueError) as caught:
            operation()
        self.assertEqual(code, getattr(caught.exception, "code", None), "cleanup refusal must be typed")
        self.assertEqual(before, inventory(self.base), "refused cleanup changed fixture or foreign bytes")

    def assert_preparation_cleanup(self, case, *, earlier=False):
        helper = load(ENTRY, "fixture_cleanup_native_" + case).routine_module(case)
        preparation = EarlierNativeMailPreparation(helper, self.sha) if earlier else AttentionSeedPreparation(helper)
        context = preparation.prepare(case, self.root.path, self.workspace, self.bank)
        self.assertFalse((self.root.path / ".send-prefix").exists(),
            "immutable attention seeds must not retain any send-cache sidecars")
        messages = EventLog(self.root).records()
        self.assertEqual(1, len(messages), "cleanup must preserve the fixture's sole native selected message")
        self.assertEqual("architect", messages[0]["recipient"])
        receipt = preparation.cache_cleanup_receipt
        self.assertEqual(("fixture_send_cache_cleanup", "ok", "removed"),
                         (receipt["step"], receipt["status"], receipt["outcome"]))
        self.assertEqual(".send-prefix", receipt["cache_relative"])
        self.assertEqual([messages[0]["id"]], receipt["own_send_ids"])
        self.assertTrue(receipt["same_device"])
        self.assertTrue(receipt["no_symlinks"])
        self.assertGreater(receipt["removed_files"], 1, "the cache includes more than checkpoint.json")
        self.assertEqual(str(self.root.path), context["seed_home"])
        if earlier:
            self.assertEqual(preparation.message["timestamp"], context["canonical_time"])

    def test_supervise_preparation_removes_entire_own_cache_before_seed_capture(self):
        self.assert_preparation_cleanup("supervise")

    def test_watch_preparation_removes_entire_own_cache_before_seed_capture(self):
        self.assert_preparation_cleanup("watch")

    def test_earlier_mail_preparation_removes_entire_own_cache_before_seed_capture(self):
        self.assert_preparation_cleanup("supervise", earlier=True)

    def test_raw_attention_seed_pair_removes_own_cache_before_immutable_capture(self):
        for case in ("supervise", "watch"):
            with self.subTest(case=case):
                seeds = self.scratch / case
                seeds.mkdir(mode=0o700)
                helper = load(ENTRY, "raw_fixture_cleanup_" + case).routine_module(case)
                pair = make_case_seed_pair(seeds, "registry", case, helper, self.workspace, counts=seed_counts())
                native_events = (pair["prepared_home"] / "events.jsonl").read_bytes()
                self.assertTrue(native_events, "the seed must retain its genuine native selected send")
                for coordinate in ("prepared_home", "1", "10"):
                    home = pair[coordinate]
                    self.assertFalse((home / ".send-prefix").exists(),
                        "raw-helper attention seeds must not capture send-cache sidecars: " + coordinate)
                    self.assertEqual(native_events, (home / "events.jsonl").read_bytes(),
                        "cache removal must preserve native event testimony at every immutable coordinate")

    def test_native_cache_cleanup_preserves_all_noncache_bytes(self):
        owner = self.cleanup_owner()
        message = self.send()
        cache = self.root.path / ".send-prefix"
        self.assertEqual(self.scratch.stat().st_dev, cache.stat().st_dev)
        before = inventory(self.root.path)
        self.assertTrue(any(name.startswith(".send-prefix/nodes/") for name in before))
        receipt = owner.remove([message["id"]])
        self.assertEqual({name: raw for name, raw in before.items()
                          if Path(name).parts[0] not in {".send-prefix", ".registry-prefix"}},
                         inventory(self.root.path))
        self.assertFalse(cache.exists())
        self.assertEqual("fixture_send_cache_cleanup", receipt["step"])
        self.assertEqual("ok", receipt["status"])
        self.assertEqual([message["id"]], receipt["own_send_ids"])
        self.assertTrue(receipt["same_device"])
        self.assertTrue(receipt["no_symlinks"])

    def test_foreign_cache_outside_fixture_root_refuses_without_mutation(self):
        owner = self.cleanup_owner()
        message = self.send()
        foreign = self.base / "foreign" / ".send-prefix"
        foreign.mkdir(parents=True)
        (foreign / "checkpoint.json").write_bytes(b"foreign cache must survive\n")
        self.assert_refused("fixture_cache_outside_root",
                            lambda: owner.remove([message["id"]], cache_path=foreign))

    def test_preexisting_cache_has_no_fixture_send_ownership(self):
        self.send()
        self.assert_refused("fixture_cache_preexisting", self.cleanup_owner)

    def test_cache_without_native_sends_has_no_removal_authority(self):
        owner = self.cleanup_owner()
        cache = self.root.path / ".send-prefix"
        cache.mkdir(mode=0o700)
        (cache / "checkpoint.json").write_bytes(b"not authored by native send\n")
        self.assert_refused("fixture_cache_own_sends_missing", lambda: owner.remove([]))

    def test_symlink_cache_refuses_without_touching_foreign_target(self):
        owner = self.cleanup_owner()
        message = self.send()
        cache = self.root.path / ".send-prefix"
        foreign = self.base / "foreign-cache"
        cache.rename(foreign)
        cache.symlink_to(foreign, target_is_directory=True)
        before = inventory(foreign)
        events = (self.root.path / "events.jsonl").read_bytes()
        with self.assertRaises(ValueError) as caught:
            owner.remove([message["id"]])
        self.assertEqual("fixture_cache_symlink", getattr(caught.exception, "code", None))
        self.assertTrue(cache.is_symlink())
        self.assertEqual(before, inventory(foreign))
        self.assertEqual(events, (self.root.path / "events.jsonl").read_bytes())

    def test_symlink_inside_cache_refuses_before_partial_removal(self):
        owner = self.cleanup_owner()
        message = self.send()
        cache = self.root.path / ".send-prefix"
        before = inventory(cache)
        sentinel = self.base / "foreign-sentinel"
        sentinel.write_bytes(b"foreign bytes must survive\n")
        link = cache / "staging" / "foreign"
        link.symlink_to(sentinel)
        with self.assertRaises(ValueError) as caught:
            owner.remove([message["id"]])
        self.assertEqual("fixture_cache_symlink", getattr(caught.exception, "code", None))
        self.assertTrue(link.is_symlink())
        self.assertEqual(b"foreign bytes must survive\n", sentinel.read_bytes())
        link.unlink()
        self.assertEqual(before, inventory(cache), "validation must finish before any cache entry is removed")


if __name__ == "__main__":
    unittest.main()
