"""The new cut bank needs its own exact raw-domain native-map witness.

Nearest: NativeCursorPrefixEffectTests, whose closed ACK/delivery maps do not
describe event cut ordinals or the SHA-256 midstate. No existing module fits
this new bank's independent population and retained-node insertion proof.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from floati import send_prefix
from floati.events import EventLog
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_hold import WakeHoldController
from tests.bus_scale_test_profile import StableSourceFixture
from tests.temp_roots import REAL_TEMP_ROOT


class CutPrefixEffectTests(StableSourceFixture, unittest.TestCase):
    def test_warmed_cut_append_remains_admissible_to_the_native_lifecycle_instrument(self):
        from tests.bus_scale_lifecycle_fixture import fixture, execute, load, HELPER
        helper = load(HELPER, "cut_prefix_lifecycle_integration_control")
        with fixture("node-prep-clear") as (bank, home, workspace):
            context = helper.prepare("node-prep-clear", home, workspace, bank)
            self.assertIsNotNone(self.product().warm(FloatiRoot.open_direct_home(home)))
            observation = {}
            execute(self, helper, "node-prep-clear", home, workspace, context, observation=observation)
            self.assertEqual("verified_cut_prefix_append",
                observation["normal"]["delta"]["@cache/cut-prefix"]["operation"])
            for fault in ("missing_delta", "extra_node"):
                with self.subTest(fault=fault):
                    delta = dict(observation["delta"])
                    extra = None
                    try:
                        if fault == "missing_delta":
                            delta.pop(next(path for path in delta if path.startswith(self.product().cache_name() + "/")))
                        else:
                            extra = home / self.product().cache_name() / "nodes" / ("f" * 64)
                            extra.write_bytes(b"unexplained cut node\n")
                            extra.chmod(0o600)
                            delta = self.subject().raw_candidate_delta(
                                observation["bounds"]["before_raw_files"], helper.snapshot(home))
                        with self.assertRaises(ValueError):
                            helper.normalize("node-prep-clear", delta, observation["artifact"],
                                context, **observation["bounds"])
                    finally:
                        if extra is not None:
                            extra.unlink()

    def test_expanded_transport_requires_exact_cut_proof_before_admitting_native_tail(self):
        from tests.bus_scale_observation_fixture import ADAPTER, load
        adapter = load(ADAPTER, "cut_prefix_expanded_transport_control")
        name = self.warm()
        before = self.inventory()
        captured = self.subject().capture_cut_prefix_input(self.home, before)
        from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import capture_prefix_input
        send_input = capture_prefix_input(self.home, before)
        self.send("expanded-tail")
        after = self.inventory()
        delta = adapter.native_delta(before, after, home=self.home, cut_input=captured,
            prefix_input=send_input)
        self.assertEqual(self.subject().raw_candidate_delta(before, after), delta)
        extra = self.home / name / "nodes" / ("f" * 64)
        extra.write_bytes(b"unexplained cut node\n")
        extra.chmod(0o600)
        with self.assertRaises(ValueError):
            adapter.native_delta(before, self.inventory(), home=self.home, cut_input=captured,
                prefix_input=send_input)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        for node in ("sender", "recipient"):
            registry.register(node, "Codex")
        self.events = EventLog(self.root)
        self.send("initial")

    def product(self):
        return importlib.import_module("floati.cut_prefix")

    def subject(self):
        return importlib.import_module(
            "tests.fixtures.bus_scale_census.bus_scale_cut_prefix_effects")

    def send(self, key):
        return self.events.send("sender", "recipient", "floati", "a" * 40,
            "note.md", "native cut proof", idempotency_key=key)

    def inventory(self):
        return {path.relative_to(self.home).as_posix(): path.read_bytes()
            for path in self.home.rglob("*") if path.is_file()}

    def warm(self):
        self.assertIsNotNone(self.product().warm(self.root))
        return self.product().cache_name()

    def test_complete_raw_domain_map_and_midstate_are_recomputed_from_native_bytes(self):
        seed = self.inventory()
        name = self.warm()
        raw = self.inventory()
        proof = self.subject().verify_warmed_cut_clone(self.home, seed)
        data = raw["events.jsonl"]
        frames = data.splitlines(keepends=True)
        prefix = self.product().CutPrefix(self.root, read_only=True)
        prefix.load()
        for ordinal in range(1, len(frames) + 1):
            digest = hashlib.sha256(WakeHoldController._EVENT_DOMAIN.encode("ascii")
                + b"\0" + b"".join(frames[:ordinal])).hexdigest()
            self.assertEqual(ordinal, prefix.get("cut", digest))
        self.assertEqual(len(frames), proof["complete_map_entries"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), proof["source_sha256"])
        self.assertEqual(raw, self.inventory(), "the independent proof must be read-only")
        self.assertTrue(all(path.startswith(name + "/") for path in proof["changed_files"]))

    def test_native_tail_append_preserves_every_retained_cut_node(self):
        name = self.warm()
        before = self.inventory()
        captured = self.subject().capture_cut_prefix_input(self.home, before)
        self.send("tail")
        after = self.inventory()
        delta = self.subject().raw_candidate_delta(before, after)
        proof = self.subject().verify_cut_prefix_effect(self.home, before, after, delta, captured)
        retained = {path: value for path, value in before.items()
            if path.startswith(name + "/nodes/")}
        self.assertTrue(retained)
        self.assertTrue(all(after.get(path) == value for path, value in retained.items()))
        self.assertGreater(proof["complete_map_entries"], len(retained))
        target = next(iter(retained))
        original = (self.home / target).read_bytes()
        (self.home / target).write_bytes(original + b" ")
        try:
            forged = self.inventory()
            with self.assertRaises(ValueError):
                self.subject().verify_cut_prefix_effect(self.home, before, forged,
                    self.subject().raw_candidate_delta(before, forged), captured)
        finally:
            (self.home / target).write_bytes(original)

    def test_resealed_false_cut_or_midstate_cannot_pass_the_complete_native_map(self):
        name = self.warm()
        prefix = self.product().CutPrefix(self.root)
        prefix.load()
        checkpoint = self.home / name / "checkpoint.json"
        node = next((self.home / name / "nodes").iterdir())
        for fault in ("ordinal", "state", "tail", "digest", "extra_node"):
            with self.subTest(fault=fault):
                target = node if fault == "ordinal" else checkpoint
                saved = target.read_bytes()
                extra = None
                try:
                    if fault == "ordinal":
                        value = json.loads(saved)
                        value["payload"]["value"] += 1
                    else:
                        value = json.loads(saved)
                        fold = value["payload"]["folds"]["cut"]
                        if fault == "state":
                            fold["state"][0] ^= 1
                        elif fault == "tail":
                            fold["tail"] = "00" + fold["tail"]
                        elif fault == "digest":
                            fold["digest"] = "f" * 64
                        else:
                            extra = self.home / name / "nodes" / ("f" * 64)
                            extra.write_bytes(node.read_bytes())
                    if fault != "extra_node":
                        key = (self.home / name / "seal.key").read_bytes()
                        value["seal"] = hmac.new(key, send_prefix._bytes(value["payload"]),
                            hashlib.sha256).hexdigest()
                        target.write_bytes(send_prefix._bytes(value))
                    with self.assertRaises(ValueError):
                        self.subject().verify_complete_cut_bank(self.home, self.inventory())
                finally:
                    target.write_bytes(saved)
                    if extra is not None:
                        extra.unlink()

    def test_unknown_kind_never_obtains_a_current_cut_certificate(self):
        from floati.send_prefix import CacheMiss
        name = self.warm()
        unknown = {"schema_version": 2, "id": "future-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": "2026-10-29T00:00:00.000Z",
            "kind": "future_event_kind", "payload": {"future": True}}
        with self.events.path.open("ab") as stream:
            stream.write(encode_frame(unknown))
        for prior in ("stale", "absent"):
            with self.subTest(prior=prior):
                if prior == "absent":
                    shutil.rmtree(self.home / name)
                self.assertIsNone(self.product().warm(self.root))
                with self.assertRaises((CacheMiss, FileNotFoundError)):
                    self.product().CutPrefix(self.root, read_only=True).load()

    def test_warm_proof_refuses_changed_noncache_bytes(self):
        seed = self.inventory()
        self.warm()
        (self.home / "unreviewed.txt").write_bytes(b"foreign change\n")
        before = self.inventory()
        with self.assertRaises(ValueError):
            self.subject().verify_warmed_cut_clone(self.home, seed)
        self.assertEqual(before, self.inventory())
