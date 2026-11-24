"""Complete native receipt-cache effects require byte, map and metadata proof."""
from __future__ import annotations

import copy
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from floati import jsonl, receipt_prefix
from floati.codex_wait_contract import CodexWaitReceiptLedger
from floati.ids import uuid7_hex
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_lifecycle_fixture import load
from tests.bus_scale_test_profile import StableSourceFixture
from tests.bus_scale_filesystem_clock import capture_filesystem_clock


HELPER = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/bus_scale_receipt_prefix_effects.py"
_PROOF = load(HELPER, "receipt_prefix_candidate_delta")


def raw_delta(before, after):
    return _PROOF.raw_candidate_delta(before, after)


class PrefixFilesystemClockTests(unittest.TestCase):
    def test_cache_projections_accept_only_recorded_filesystem_clock_window(self):
        from tests import bus_scale_filesystem_clock as clocks
        cursor = load(HELPER.with_name("bus_scale_cursor_prefix_effects.py"), "prefix_clock_cursor")
        realtime, coarse = 1793230441768300931, 1793230441767141750
        for platform in ("linux", "darwin"):
            with patch.object(clocks.sys, "platform", platform),\
                    patch.object(clocks.time, "time_ns", return_value=realtime),\
                    patch.object(clocks.time, "clock_gettime_ns", return_value=coarse, create=True):
                begin, receipt = clocks.capture_filesystem_clock()
                end = begin + timedelta(seconds=1)
                lower, upper = clocks.filesystem_bounds(begin, end, receipt)
                cases = (
                    (_PROOF.project_receipt_prefix_tree, _PROOF.cache_name("scale-worker"),
                     {"node": "scale-worker"}),
                    (cursor.project_cursor_prefix_tree, cursor.cache_name("receipts/acks/architect.jsonl"),
                     {"ack_relative": "receipts/acks/architect.jsonl"}),
                    (cursor.project_delivery_prefix_tree, cursor.cache_name("receipts/deliveries/architect.jsonl"),
                     {"delivery_relative": "receipts/deliveries/architect.jsonl"}),
                )
                for project, name, prefix_input in cases:
                    target = name + "/seal.key"
                    inventory = {target: {"bytes": 32, "sha256": "a" * 64}}
                    proof = {"before_input": prefix_input, "cache_before": {},
                        "cache_after": inventory, "changed_files": [target], "semantic": {},
                        "auxiliary_unchanged": {"cache_names": []}}
                    for stamp in (lower, upper, lower - 1, upper + 1, True):
                        entries = {path: {"kind": "directory", "mode": 0o700, "mtime_ns": stamp}
                            for path in (name, name + "/nodes", name + "/staging", name + "/buckets")}
                        entries[target] = dict(inventory[target], kind="file", mode=0o600, mtime_ns=stamp)
                        with self.subTest(platform=platform, projector=project.__name__, stamp=stamp):
                            if type(stamp) is int and lower <= stamp <= upper:
                                try:
                                    project({"entries": {}}, {"entries": entries}, proof, begin, end,
                                        filesystem_clock=receipt)
                                except ValueError as error:
                                    self.fail("in-window filesystem mutation was refused: " + str(error))
                            else:
                                with self.assertRaisesRegex(ValueError, "clock"):
                                    project({"entries": {}}, {"entries": entries}, proof, begin, end,
                                        filesystem_clock=receipt)
                    for row in entries.values():
                        row["mtime_ns"] = lower
                    for fault in ("clock", "platform", "begin", "population", "sample"):
                        forged = dict(receipt)
                        if fault == "clock":
                            forged["clock"] = "unreviewed clock"
                        elif fault == "platform":
                            forged["platform"] = "foreign"
                        elif fault == "begin":
                            forged["wall_begin"] = (begin - timedelta(seconds=1)).isoformat()
                        elif fault == "population":
                            forged["extra"] = True
                        else:
                            forged["realtime_ns"] += 1000
                        with self.subTest(platform=platform, projector=project.__name__, receipt=fault):
                            with self.assertRaisesRegex(ValueError, "filesystem clock"):
                                project({"entries": {}}, {"entries": entries}, proof, begin, end,
                                    filesystem_clock=forged)
                    if platform == "linux":
                        with self.subTest(projector=project.__name__, receipt="missing"):
                            with self.assertRaisesRegex(ValueError, "clock"):
                                project({"entries": {}}, {"entries": entries}, proof, begin, end)


class NativeReceiptPrefixEffectTests(StableSourceFixture, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        Registry(self.root).register("scale-worker", "codex")
        self.helper = load(HELPER, "native_receipt_prefix_effect_test")
        self.name = self.helper.cache_name()

    def record(self, key):
        return CodexWaitReceiptLedger(self.root).record_exhaustion(node_id="scale-worker",
            session_digest=hashlib.sha256(b"native-cache-proof").hexdigest(),
            waited_seconds=2, idempotency_key=key)

    def snapshot(self):
        return {path.relative_to(self.home).as_posix(): path.read_bytes()
            for path in self.home.rglob("*") if path.is_file()}

    def tree(self):
        entries = {}
        for path in self.home.rglob("*"):
            info = path.lstat()
            row = {"kind": "directory" if path.is_dir() else "file",
                "mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns}
            if path.is_file():
                raw = path.read_bytes()
                row.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            entries[path.relative_to(self.home).as_posix()] = row
        return {"entries": entries}

    def native(self, *, cold=False):
        if not cold:
            self.record("historical")
        before = self.snapshot()
        prefix_input = self.helper.capture_receipt_prefix_input(self.home, before)
        before_tree = self.tree()
        begin, filesystem_clock = capture_filesystem_clock()
        row = self.record("selected")
        end = datetime.now(timezone.utc)
        after, after_tree = self.snapshot(), self.tree()
        proof = self.helper.verify_receipt_prefix_effect(self.home, before, after,
            raw_delta(before, after), prefix_input)
        return {"before": before, "after": after, "input": prefix_input, "proof": proof,
            "before_tree": before_tree, "after_tree": after_tree, "begin": begin, "end": end,
            "filesystem_clock": filesystem_clock, "row": row}

    def verify(self, observation, *, after=None, delta=None, prefix_input=None):
        after = self.snapshot() if after is None else after
        return self.helper.verify_receipt_prefix_effect(self.home, observation["before"], after,
            raw_delta(observation["before"], after) if delta is None else delta,
            observation["input"] if prefix_input is None else prefix_input)

    def resign(self, checkpoint, key):
        checkpoint["seal"] = hmac.new(key, self.helper._bytes(checkpoint["payload"]), hashlib.sha256).hexdigest()
        target = self.home / self.name / "checkpoint.json"
        target.write_bytes(self.helper._bytes(checkpoint))

    def test_real_warm_append_keeps_raw_effects_and_proves_complete_maps(self):
        observation = self.native()
        delta = raw_delta(observation["before"], observation["after"])
        proof = observation["proof"]
        self.assertEqual("verified_receipt_prefix_append", proof["semantic"]["operation"])
        self.assertEqual("complete_warmed_prefix", proof["semantic"]["input_cache_state"])
        self.assertTrue(proof["semantic"]["input_checkpoint_seal_validated"])
        self.assertEqual(observation["after"], self.snapshot(), "proof must be read-only")
        self.assertEqual(delta, raw_delta(observation["before"], observation["after"]))
        self.assertTrue(any(path.startswith(self.name + "/nodes/") for path in proof["changed_files"]))
        self.helper.project_receipt_prefix_tree(observation["before_tree"], observation["after_tree"],
            proof, observation["begin"], observation["end"], filesystem_clock=observation["filesystem_clock"])

    def test_real_cold_first_receipt_proves_exact_absent_empty_prior(self):
        observation = self.native(cold=True)
        proof = observation["proof"]
        self.assertEqual("absent_native_empty", proof["semantic"]["input_cache_state"])
        self.assertEqual({}, proof["cache_before"])
        self.assertFalse(proof["semantic"]["input_checkpoint_seal_validated"])
        self.assertTrue(proof["semantic"]["output_checkpoint_seal_validated"])
        self.assertIn(self.name + "/seal.key", proof["changed_files"])
        self.helper.project_receipt_prefix_tree(observation["before_tree"], observation["after_tree"],
            proof, observation["begin"], observation["end"], filesystem_clock=observation["filesystem_clock"])

    def test_absent_cache_with_nonempty_history_cannot_claim_cold_admission(self):
        self.record("historical")
        shutil.rmtree(self.home / self.name)
        with self.assertRaisesRegex(ValueError, "native empty prior"):
            self.helper.capture_receipt_prefix_input(self.home, self.snapshot())

    def test_extra_valid_content_addressed_node_is_not_a_path_waiver(self):
        observation = self.native()
        raw = self.helper._bytes({"unreviewed": "complete-extra-node"})
        target = self.home / self.name / "nodes" / hashlib.sha256(raw).hexdigest()
        target.write_bytes(raw)
        target.chmod(0o600)
        forged = self.snapshot()
        with self.assertRaisesRegex(ValueError, "namespace population"):
            self.verify(observation)
        self.assertEqual(forged, self.snapshot(), "rejection must not repair extra nodes")

    def test_removed_historical_node_is_not_hidden_by_valid_new_selected_paths(self):
        observation = self.native()
        prefix = receipt_prefix.ReceiptPrefix(self.root, "scale-worker")
        prefix.load()
        (prefix.nodes / prefix.filename(self.helper._key("receipt", "historical"))).unlink()
        with self.assertRaisesRegex(ValueError, "namespace population"):
            self.verify(observation)

    def test_resigned_wrong_revision_and_ledger_binding_are_refused(self):
        observation = self.native()
        checkpoint = self.home / self.name / "checkpoint.json"
        original = checkpoint.read_bytes()
        key = (self.home / self.name / "seal.key").read_bytes()
        for field, value in (("revision", "0" * 64), ("relative", "receipts/codex-wait-exhaustion/foreign.jsonl")):
            with self.subTest(field=field):
                forged = json.loads(original)
                forged["payload"]["binding"][field] = value
                self.resign(forged, key)
                with self.assertRaisesRegex(ValueError, "binding or reader revision"):
                    self.verify(observation)
        checkpoint.write_bytes(original)

    def test_resigned_incomplete_logical_map_is_refused(self):
        observation = self.native()
        checkpoint = json.loads((self.home / self.name / "checkpoint.json").read_bytes())
        key = self.helper._key("receipt", "historical")
        (self.home / self.name / "nodes" / format(int(key, 2), "064x")).unlink()
        self.resign(checkpoint, (self.home / self.name / "seal.key").read_bytes())
        with self.assertRaisesRegex(ValueError, "complete native map namespace"):
            self.verify(observation)

    def test_both_input_and_output_checkpoint_authentication_are_required(self):
        observation = self.native()
        target = self.name + "/checkpoint.json"
        for side in ("input", "output"):
            with self.subTest(side=side):
                before, after = copy.deepcopy(observation["before"]), copy.deepcopy(observation["after"])
                values = before if side == "input" else after
                checkpoint = json.loads(values[target])
                checkpoint["seal"] = "0" * 64
                values[target] = self.helper._bytes(checkpoint)
                prefix_input = copy.deepcopy(observation["input"])
                if side == "input":
                    prefix_input["checkpoint_sha256"] = hashlib.sha256(before[target]).hexdigest()
                with self.assertRaisesRegex(ValueError, "authentication failed"):
                    self.helper.verify_receipt_prefix_effect(self.home, before, after,
                        raw_delta(before, after), prefix_input)

    def test_historical_duplicate_key_map_must_retain_its_first_native_row(self):
        first = self.record("historical")
        duplicate = dict(first, id="codex-wait-exhaustion-" + uuid7_hex(), waited_seconds=3)
        jsonl.append_record(self.root, receipt_prefix.relative("scale-worker"), duplicate,
            allowed_kinds=receipt_prefix.KINDS)
        receipt_prefix.warm(self.root, "scale-worker")
        before = self.snapshot()
        prefix_input = self.helper.capture_receipt_prefix_input(self.home, before)
        self.record("selected")
        after = self.snapshot()
        self.helper.verify_receipt_prefix_effect(self.home, before, after, raw_delta(before, after), prefix_input)
        relative = self.helper.ledger_relative()
        rows = self.helper._transcript(self.root, relative, after[relative])
        key = self.helper._key("receipt", "historical")
        target = self.home / self.name / "nodes" / format(int(key, 2), "064x")
        entry = json.loads(target.read_bytes())
        entry["payload"]["value"] = duplicate
        entry["seal"] = hmac.new((self.home / self.name / "seal.key").read_bytes(),
            self.helper._bytes(entry["payload"]), hashlib.sha256).hexdigest()
        target.write_bytes(self.helper._bytes(entry))
        forged = self.snapshot()
        with self.assertRaisesRegex(ValueError, "complete native map"):
            self.helper.verify_receipt_prefix_effect(self.home, before, forged,
                raw_delta(before, forged), prefix_input)

    def test_changed_retained_secret_is_refused_even_with_a_valid_new_seal(self):
        observation = self.native()
        checkpoint = json.loads((self.home / self.name / "checkpoint.json").read_bytes())
        key = os.urandom(32)
        (self.home / self.name / "seal.key").write_bytes(key)
        self.resign(checkpoint, key)
        target = self.name + "/seal.key"
        after = self.snapshot()
        old = observation["before"][target]
        delta = raw_delta(observation["before"], {**after, target: old})
        delta[target] = {"operation": "replace", "before_bytes": len(old), "after_bytes": len(key),
            "before_sha256": hashlib.sha256(old).hexdigest(), "after_sha256": hashlib.sha256(key).hexdigest(),
            "payload_base64": base64.b64encode(key).decode()}
        with self.assertRaisesRegex(ValueError, "retained sealing key"):
            self.verify(observation, after=after, delta=delta)

    def test_forged_delta_payload_or_population_is_refused(self):
        observation = self.native()
        delta = raw_delta(observation["before"], observation["after"])
        target = self.name + "/checkpoint.json"
        for fault in ("bytes", "population"):
            with self.subTest(fault=fault):
                forged = copy.deepcopy(delta)
                if fault == "bytes":
                    forged[target]["payload_base64"] = "e30="
                else:
                    del forged[target]
                with self.assertRaisesRegex(ValueError, "cache delta"):
                    self.verify(observation, delta=forged)

    def test_pre_child_checkpoint_hash_and_private_metadata_are_bound(self):
        observation = self.native()
        for fault in ("hash", "metadata"):
            with self.subTest(fault=fault):
                forged = copy.deepcopy(observation["input"])
                if fault == "hash":
                    forged["checkpoint_sha256"] = "0" * 64
                else:
                    forged["cache_metadata"][self.name + "/seal.key"]["mode"] = 0o644
                with self.assertRaisesRegex(ValueError, "pre-child checkpoint|retained cache metadata"):
                    self.verify(observation, prefix_input=forged)

    def test_staging_directory_and_public_cache_file_are_refused(self):
        observation = self.native()
        staging = self.home / self.name / "staging" / "unpublished"
        staging.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "staging population"):
            self.verify(observation)
        staging.rmdir()
        (self.home / self.name / "checkpoint.json").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "not private"):
            self.verify(observation)

    def test_tree_projection_cannot_drop_or_rewrite_a_retained_raw_cache_leaf(self):
        observation = self.native()
        for fault in ("population", "digest"):
            with self.subTest(fault=fault):
                forged = copy.deepcopy(observation["after_tree"])
                target = self.name + "/seal.key"
                if fault == "population":
                    del forged["entries"][target]
                else:
                    forged["entries"][target]["sha256"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "cache tree file"):
                    self.helper.project_receipt_prefix_tree(observation["before_tree"], forged,
                        observation["proof"], observation["begin"], observation["end"],
                        filesystem_clock=observation["filesystem_clock"])

    def test_exact_membership_directory_and_every_bucket_are_required(self):
        observation = self.native()
        directory = self.home / self.name / "directory.bin"
        original_directory = directory.read_bytes()
        directory.write_bytes(bytes(len(original_directory)))
        with self.assertRaisesRegex(ValueError, "membership directory"):
            self.verify(observation)
        directory.write_bytes(original_directory)
        bucket = next((self.home / self.name / "buckets").iterdir())
        original = bucket.read_bytes()
        bucket.write_bytes(bytes(len(original)))
        with self.assertRaisesRegex(ValueError, "membership bucket"):
            self.verify(observation)

    def test_validly_resigned_foreign_generation_entry_is_refused(self):
        observation = self.native()
        key = self.helper._key("receipt", "historical")
        target = self.home / self.name / "nodes" / format(int(key, 2), "064x")
        entry = json.loads(target.read_bytes())
        entry["payload"]["generation"] = "0" * 32
        entry["seal"] = hmac.new((self.home / self.name / "seal.key").read_bytes(),
            self.helper._bytes(entry["payload"]), hashlib.sha256).hexdigest()
        target.write_bytes(self.helper._bytes(entry))
        with self.assertRaisesRegex(ValueError, "generation or domain/key"):
            self.verify(observation)

    def test_complete_proof_rejects_noncanonical_or_oversized_compressed_buckets(self):
        import zlib
        observation = self.native()
        target = next((self.home / self.name / "buckets").iterdir())
        original = target.read_bytes()
        logical = self.helper._storage_decode(original)
        self.assertLess(len(original), len(logical))
        for forged in (zlib.compress(logical, 1), original + b"trailing", zlib.compress(bytes(8193), 9)):
            with self.subTest(bytes=len(forged)):
                self.assertNotEqual(original, forged)
                target.write_bytes(forged)
                with self.assertRaisesRegex(ValueError, "membership bucket|compressed bucket"):
                    self.verify(observation)


if __name__ == "__main__":
    unittest.main()
