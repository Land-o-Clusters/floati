"""ACK effects require complete native index and unchanged source proofs."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from floati import cursor_prefix, jsonl
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.records import WAKE_HOLD_KINDS
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_lifecycle_fixture import load
from tests.bus_scale_shared_fixture import RetainedNativeFixture
from tests.bus_scale_test_profile import full_scale_enabled, StableSourceFixture
from tests.bus_scale_filesystem_clock import capture_filesystem_clock


HELPER = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/bus_scale_cursor_prefix_effects.py"


class NativeCursorPrefixEffectTests(StableSourceFixture, unittest.TestCase):
    _warm_fixture = None

    def setUp(self):
        method = getattr(self, self._testMethodName)
        self._reuse_native = (not full_scale_enabled()
            and "native" in method.__code__.co_names
            and self._testMethodName != "test_cold_ack_cache_requires_complete_warmed_delivery_join")
        self.addCleanup(self._finish_fixture)
        retained = type(self)._warm_fixture if self._reuse_native else None
        if retained is not None:
            if retained.restore():
                self.temp, self.home = retained.temporary, retained.home
                self.root = FloatiRoot.open_direct_home(self.home)
                self.log, self.cursor = EventLog(self.root), SparseCursor(self.root)
                for field, value in retained.state.items():
                    setattr(self, field, copy.deepcopy(value))
                self.helper = load(HELPER, "native_cursor_prefix_effect_test")
                return
            type(self)._warm_fixture = None
            retained.close()
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        for node in ("sender", "architect"):
            Registry(self.root).register(node, "codex")
        self.log, self.cursor = EventLog(self.root), SparseCursor(self.root)
        self.messages = [self.log.send("sender", "architect", "floati", "a" * 40,
            "note.md", "native ACK proof", idempotency_key="message-" + str(index)) for index in range(3)]
        _presented, self.delivery_row = self.log.present("architect")
        self.helper = load(HELPER, "native_cursor_prefix_effect_test")
        self.ack = self.cursor._relative_path_for("architect").as_posix()
        self.delivery = self.cursor._delivery_relative_path_for("architect").as_posix()
        self.name = self.helper.cache_name(self.ack)
        self.delivery_cache = self.helper.cache_name(self.delivery)

    def _finish_fixture(self):
        retained = type(self)._warm_fixture
        if retained is not None and retained.home == getattr(self, "home", None):
            if not retained.restore():
                type(self)._warm_fixture = None
                retained.close()
        elif hasattr(self, "temp"):
            self.temp.cleanup()

    def snapshot(self):
        return {path.relative_to(self.home).as_posix(): path.read_bytes()
            for path in self.home.rglob("*") if path.is_file()}

    def test_retained_cursor_certificates_ignore_rebuild_generation_only_after_complete_proof(self):
        observer = load(HELPER.with_name("bus-scale-expanded-observation-2026-10-15.py"),
            "retained_cursor_certificate_control")
        projections, generations = [], []
        for _attempt in range(2):
            for name in (self.name, self.delivery_cache, ".send-prefix"):
                bank = self.home / name
                if bank.exists():
                    shutil.rmtree(bank)
            self.assertIsNotNone(cursor_prefix.warm(self.root, "architect"))
            raw, tree = self.snapshot(), observer.tree(self.home)
            verified = self.helper.verify_complete_cursor_banks(self.home, raw, node="architect")
            self.assertIn("certificates", verified,
                "complete cursor proof needs generation-free native content certificates")
            projections.append(observer.retained_mail_trees(self.home, tree, tree, raw, raw,
                verified["certificates"]))
            generations.append(json.loads(raw[self.name + "/checkpoint.json"])["payload"]["generation"])
        self.assertNotEqual(*generations)
        self.assertEqual(*projections)
        node = next((self.home / self.name / "nodes").iterdir())
        saved = node.read_bytes()
        node.write_bytes(saved + b" ")
        with self.assertRaises(ValueError):
            self.helper.verify_complete_cursor_banks(self.home, self.snapshot(), node="architect")
        with self.assertRaises(observer.UnresolvedObservation):
            observer.retained_mail_trees(self.home, tree, observer.tree(self.home), raw,
                self.snapshot(), verified["certificates"])

    def test_prepared_cursor_input_keeps_valid_banks_and_rebuilds_without_authority(self):
        """Nearest: the exact-input preflight carrier control below."""
        from floati import send_prefix
        observer = load(HELPER.with_name("bus-scale-expanded-observation-2026-10-15.py"),
            "prepared_cursor_input_control")
        from _bus_scale_fixture_proofs import bus_scale_prefix_effects as warm_proof
        shutil.rmtree(self.home / ".send-prefix")
        self.assertIsNotNone(send_prefix.warm(self.root))
        self.assertIsNotNone(cursor_prefix.warm(self.root, "architect"))
        raw, issued = self.snapshot(), []
        warm_proof.verify_warmed_clone(self.home, raw, full_mail=True, cursor_preflights=issued)
        expected = {}
        for label, relative, name in (("delivery", self.delivery, self.delivery_cache),
                                      ("ack", self.ack, self.name)):
            payload = json.loads(raw[name + "/checkpoint.json"])["payload"]
            expected[label] = {key: payload[key] for key in ("identity", "count", "head")}
        before = observer.tree(self.home)

        def prepare(carriers):
            # Before the input-reuse port exists, this is the observer's real
            # unconditional native preparation, not a fabricated test result.
            operation = getattr(observer, "prepare_cursor_input", None)
            if operation is None:
                return cursor_prefix.warm(self.root, "architect")
            return operation(self.home, "architect", cursor_preflights=carriers)

        self.assertEqual(expected, prepare(tuple(issued)))
        self.assertEqual(before, observer.tree(self.home),
            "an already proved current native input was rebuilt during preparation")
        checkpoint = json.loads((self.home / self.name / "checkpoint.json").read_bytes())
        generation = checkpoint["payload"]["generation"]
        self.assertEqual(expected, prepare(({},)), "untrusted data must use ordinary native warming")
        after = json.loads((self.home / self.name / "checkpoint.json").read_bytes())
        self.assertNotEqual(generation, after["payload"]["generation"])
        self.helper.verify_complete_cursor_banks(self.home, self.snapshot(), node="architect")
        with self.assertRaises(ValueError):
            prepare(tuple(issued))

    def test_warm_cursor_certificate_carrier_reuses_only_the_exact_live_prechild_input(self):
        """Nearest: the retained generation-free certificate property above."""
        from contextlib import nullcontext
        from unittest.mock import patch
        from floati import send_prefix
        from tests.bus_scale_io import IOProbe
        observer = load(HELPER.with_name("bus-scale-expanded-observation-2026-10-15.py"),
            "warm_cursor_certificate_carrier_control")
        from _bus_scale_fixture_proofs import bus_scale_prefix_effects as warm_proof

        def issue():
            import shutil
            shutil.rmtree(self.home / ".send-prefix")
            self.assertIsNotNone(send_prefix.warm(self.root))
            self.assertIsNotNone(cursor_prefix.warm(self.root, "architect"))
            raw = self.snapshot()
            issued = []
            warm_proof.verify_warmed_clone(self.home, raw, full_mail=True,
                cursor_preflights=issued)
            self.assertTrue(issued, "complete warm verification must retain its cursor authority")
            expected = observer.capture_mail_certificates(self.home, raw)
            return raw, tuple(issued), expected

        raw, issued, expected = issue()
        probe = IOProbe(self.home.resolve())
        with probe:
            actual = observer.capture_mail_certificates(self.home, raw,
                cursor_preflights=issued)
        probe.assert_complete()
        self.assertEqual(expected, actual,
            "carrying the completed proof must preserve every generation-free certificate")
        self.assertEqual(0, sum(probe.read_bytes_by_path.values()),
            "the exact already-proved pre-child input must not replay source or bank content")
        tree = observer.tree(self.home)
        self.assertEqual(observer.retained_mail_trees(self.home, tree, tree, raw, raw, expected),
            observer.retained_mail_trees(self.home, tree, tree, raw, raw, actual))

        for fault in ("source", "source_identity", "bank", "bank_identity", "namespace",
                      "metadata", "revision", "serialized", "forged"):
            with self.subTest(fault=fault):
                raw, issued, expected = issue()
                target, saved, mode = None, None, None
                candidates = issued
                guard = nullcontext()
                if fault in {"source", "source_identity"}:
                    target = self.home / "events.jsonl"
                    saved = target.read_bytes()
                    if fault == "source":
                        changed = saved.replace(b"native ACK proof", b"native ACK prooF", 1)
                        self.assertNotEqual(saved, changed)
                        target.write_bytes(changed)
                    else:
                        replacement = target.with_name(target.name + ".identity-control")
                        replacement.write_bytes(saved)
                        replacement.chmod(stat.S_IMODE(target.stat().st_mode))
                        replacement.replace(target)
                elif fault in {"bank", "bank_identity", "metadata"}:
                    target = next((self.home / self.name / "nodes").iterdir())
                    saved, mode = target.read_bytes(), stat.S_IMODE(target.stat().st_mode)
                    if fault == "bank":
                        target.write_bytes(saved + b" ")
                    elif fault == "bank_identity":
                        replacement = target.with_name(target.name + ".identity-control")
                        replacement.write_bytes(saved)
                        replacement.chmod(mode)
                        replacement.replace(target)
                    else:
                        target.chmod(0o644)
                elif fault == "namespace":
                    target = self.home / self.name / "nodes" / ("f" * 64)
                    self.assertFalse(target.exists())
                    target.write_bytes(b"unexplained cursor node\n")
                    target.chmod(0o600)
                elif fault == "revision":
                    guard = patch.object(send_prefix, "_revision", return_value="0" * 64)
                elif fault == "serialized":
                    candidates = (json.loads(json.dumps(expected)),)
                else:
                    candidates = ({"kind": "cursor_preflight", "root": str(self.home.resolve()),
                        "certificates": copy.deepcopy(expected)},)
                try:
                    poisoned = self.snapshot()
                    probe = IOProbe(self.home.resolve())
                    refused, result = False, None
                    with guard, probe:
                        try:
                            result = observer.capture_mail_certificates(self.home, poisoned,
                                cursor_preflights=candidates)
                        except (ValueError, observer.UnresolvedObservation):
                            refused = True
                    probe.assert_complete()
                    if fault in {"serialized", "forged", "bank_identity"}:
                        if not refused:
                            self.assertEqual(expected, result)
                            self.assertGreater(sum(probe.read_bytes_by_path.values()), 0,
                                "untrusted data or replaced bank identity needs complete native revalidation")
                    else:
                        self.assertTrue(refused,
                            "changed source, bank, population, metadata or revision gained carried authority")
                    self.assertEqual(poisoned, self.snapshot(),
                        "revalidation/refusal must preserve all supplied native bytes")
                finally:
                    if target is not None:
                        if saved is None:
                            target.unlink()
                        else:
                            if target.read_bytes() != saved:
                                target.write_bytes(saved)
                            if mode is not None:
                                target.chmod(mode)

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

    def acknowledge(self, session, *, reverse=False):
        items = [row["id"] for row in self.messages[:2]]
        if reverse:
            items.reverse()
        return self.cursor.ack("architect", items, acting_session_id=session)

    def native(self, *, cold=False):
        retained = type(self)._warm_fixture if self._reuse_native and not cold else None
        if retained is not None:
            observation = copy.deepcopy(retained.observation)
            # Reproduce the real proof at the retained live inode bindings;
            # copied testimony alone is never admission to reuse a fixture.
            self.assertEqual(observation["after"], self.snapshot())
            proof = self.verify(observation)
            self.assertEqual(observation["proof"], proof)
            return observation
        if not cold:
            self.acknowledge("historical")
        self.assertIsNotNone(cursor_prefix.warm(self.root, "architect"))
        if cold:
            shutil.rmtree(self.home / self.name)
        before = self.snapshot()
        prefix_input = self.helper.capture_cursor_prefix_input(self.home, before)
        before_tree = self.tree()
        begin, filesystem_clock = capture_filesystem_clock()
        row = self.acknowledge("selected")
        end = datetime.now(timezone.utc)
        after, after_tree = self.snapshot(), self.tree()
        proof = self.helper.verify_cursor_prefix_effect(self.home, before, after,
            self.helper.raw_candidate_delta(before, after), prefix_input)
        observation = {"before": before, "after": after, "input": prefix_input, "proof": proof,
            "before_tree": before_tree, "after_tree": after_tree, "begin": begin, "end": end,
            "filesystem_clock": filesystem_clock, "row": row}
        if self._reuse_native and not cold:
            state = {field: getattr(self, field) for field in
                     ("messages", "delivery_row", "ack", "delivery", "name", "delivery_cache")}
            cache_names = {self.name, self.delivery_cache, ".send-prefix", ".registry-prefix"}
            retained = RetainedNativeFixture(self.home, cache_names, observation, state, self.temp)
            type(self)._warm_fixture = retained
            type(self).addClassCleanup(retained.close)
            type(self).addClassCleanup(setattr, type(self), "_warm_fixture", None)
        return observation

    def verify(self, observation, *, delta=None, prefix_input=None):
        after = self.snapshot()
        return self.helper.verify_cursor_prefix_effect(self.home, observation["before"], after,
            self.helper.raw_candidate_delta(observation["before"], after) if delta is None else delta,
            observation["input"] if prefix_input is None else prefix_input)

    def resign(self, name, checkpoint, key=None):
        if key is None:
            key = (self.home / name / "seal.key").read_bytes()
        checkpoint["seal"] = hmac.new(key, self.helper._base._bytes(checkpoint["payload"]), hashlib.sha256).hexdigest()
        (self.home / name / "checkpoint.json").write_bytes(self.helper._base._bytes(checkpoint))

    def test_real_warm_ack_proves_all_maps_and_keeps_complete_raw_effects(self):
        observation = self.native()
        proof = observation["proof"]
        self.assertEqual("verified_cursor_prefix_append", proof["semantic"]["operation"])
        self.assertTrue(proof["semantic"]["complete_delivery_join_and_unchanged_sources_validated"])
        self.assertTrue(proof["semantic"]["input_checkpoint_seal_validated"])
        self.assertEqual(observation["after"], self.snapshot())
        self.assertTrue(any(path.startswith(self.name + "/nodes/") for path in proof["changed_files"]))
        self.helper.project_cursor_prefix_tree(observation["before_tree"], observation["after_tree"],
            proof, observation["begin"], observation["end"], filesystem_clock=observation["filesystem_clock"])

    def test_cold_ack_cache_requires_complete_warmed_delivery_join(self):
        observation = self.native(cold=True)
        proof = observation["proof"]
        self.assertEqual("absent_native_empty", proof["semantic"]["input_cache_state"])
        self.assertEqual({}, proof["cache_before"])
        self.assertIn(self.name + "/seal.key", proof["changed_files"])
        self.helper.project_cursor_prefix_tree(observation["before_tree"], observation["after_tree"],
            proof, observation["begin"], observation["end"], filesystem_clock=observation["filesystem_clock"])

    def test_missing_delivery_cache_is_not_an_auxiliary_path_waiver(self):
        self.native()
        shutil.rmtree(self.home / self.delivery_cache)
        with self.assertRaisesRegex(ValueError, "cache directory|incomplete cache"):
            self.helper.capture_cursor_prefix_input(self.home, self.snapshot())

    def test_absent_ack_cache_cannot_hide_nonempty_ack_history(self):
        self.native()
        shutil.rmtree(self.home / self.name)
        with self.assertRaisesRegex(ValueError, "native empty prior"):
            self.helper.capture_cursor_prefix_input(self.home, self.snapshot())

    def test_extra_content_addressed_ack_node_is_refused(self):
        observation = self.native()
        raw = self.helper._base._bytes({"extra": "valid-content-addressed-node"})
        target = self.home / self.name / "nodes" / hashlib.sha256(raw).hexdigest()
        target.write_bytes(raw)
        target.chmod(0o600)
        after = self.snapshot()
        with self.assertRaisesRegex(ValueError, "namespace population"):
            self.verify(observation)
        self.assertEqual(after, self.snapshot(), "proof rejection is read-only")

    def test_missing_historical_ack_payload_is_not_hidden_by_selected_paths(self):
        observation = self.native()
        prefix = cursor_prefix.AckPrefix(self.root, self.ack)
        prefix.load()
        key = self.helper._base._key("retry", self.helper._retry("historical", self.messages_ids()))
        (prefix.nodes / prefix.filename(key)).unlink()
        with self.assertRaisesRegex(ValueError, "namespace population"):
            self.verify(observation)

    def messages_ids(self):
        return [row["id"] for row in self.messages[:2]]

    def test_exact_retry_map_preserves_last_row_order_and_acting_session(self):
        first = self.acknowledge("historical")
        self.acknowledge("historical", reverse=True)
        self.acknowledge("other-session")
        latest = dict(first, id="ack-" + uuid7_hex())
        jsonl.append_record(self.root, self.ack, latest, allowed_kinds=cursor_prefix.ACK_KINDS)
        cursor_prefix.warm(self.root, "architect")
        before = self.snapshot()
        prefix_input = self.helper.capture_cursor_prefix_input(self.home, before)
        self.acknowledge("selected")
        after = self.snapshot()
        self.helper.verify_cursor_prefix_effect(self.home, before, after,
            self.helper.raw_candidate_delta(before, after), prefix_input)
        rows = self.helper._transcript(self.root, self.ack, cursor_prefix.ACK_KINDS, after[self.ack])
        for fault in ("first", "order", "session"):
            with self.subTest(fault=fault):
                key = self.helper._base._key("retry", self.helper._retry("historical", self.messages_ids()))
                target = self.home / self.name / "nodes" / format(int(key, 2), "064x")
                entry = json.loads(after[target.relative_to(self.home).as_posix()])
                if fault == "first":
                    entry["payload"]["value"] = first
                else:
                    wrong = self.helper._retry("rewritten-session" if fault == "session" else "historical",
                        self.messages_ids() if fault == "session" else list(reversed(self.messages_ids())))
                    entry["payload"]["key"] = self.helper._base._key("retry", wrong)
                entry["seal"] = hmac.new((self.home / self.name / "seal.key").read_bytes(),
                    self.helper._base._bytes(entry["payload"]), hashlib.sha256).hexdigest()
                target.write_bytes(self.helper._base._bytes(entry))
                with self.assertRaisesRegex(ValueError, "complete native map"):
                    forged = self.snapshot()
                    self.helper.verify_cursor_prefix_effect(self.home, before, forged,
                        self.helper.raw_candidate_delta(before, forged), prefix_input)

    def test_resigned_wrong_ledger_or_revision_binding_is_refused(self):
        observation = self.native()
        original = (self.home / self.name / "checkpoint.json").read_bytes()
        for field, value in (("relative", "receipts/acks/foreign.jsonl"), ("revision", "0" * 64)):
            with self.subTest(field=field):
                checkpoint = json.loads(original)
                checkpoint["payload"]["binding"][field] = value
                self.resign(self.name, checkpoint)
                with self.assertRaisesRegex(ValueError, "binding or reader revision"):
                    self.verify(observation)

    def test_resigned_mail_folds_and_tree_nodes_require_complete_native_facts(self):
        self.native()
        self.helper.verify_complete_cursor_banks(self.home, self.snapshot(), node="architect")
        for name in (self.name, self.delivery_cache):
            checkpoint_path = self.home / name / "checkpoint.json"
            original = checkpoint_path.read_bytes()
            faults = [("members", None), ("ack_rows", 999), ("latest_utc", "2000-01-01T00:00:00Z")]
            if name == self.name:
                faults.append(("health_safe", False))
            for field, replacement in faults:
                with self.subTest(bank=name, field=field):
                    checkpoint = json.loads(original)
                    checkpoint["payload"]["folds"]["mail"][field] = replacement
                    try:
                        self.resign(name, checkpoint)
                        if field == "health_safe":
                            self.helper.capture_cursor_prefix_input(self.home, self.snapshot(),
                                auxiliary_unavailable_temporal_certificate=True)
                        with self.assertRaisesRegex(ValueError, "folds differ"):
                            self.helper.verify_complete_cursor_banks(self.home, self.snapshot(), node="architect")
                    finally:
                        checkpoint_path.write_bytes(original)
            digest = json.loads(original)["payload"]["folds"]["mail"]["members"]
            lookup = self.helper._base._key("mail-tree-v1", digest)
            target = self.home / name / "nodes" / format(int(lookup, 2), "064x")
            native = target.read_bytes()
            value = json.loads(native)
            value["payload"]["value"]["count"] += 1
            value["seal"] = hmac.new((self.home / name / "seal.key").read_bytes(),
                self.helper._base._bytes(value["payload"]), hashlib.sha256).hexdigest()
            try:
                target.write_bytes(self.helper._base._bytes(value))
                with self.assertRaisesRegex(ValueError, "complete native map entry"):
                    self.helper.verify_complete_cursor_banks(self.home, self.snapshot(), node="architect")
            finally:
                target.write_bytes(native)

    def test_warm_temporal_certificate_preserves_the_native_backdated_ack_fact(self):
        from datetime import timedelta
        from floati.framing import encode_frame

        self.native()
        rows = self.helper._transcript(self.root, self.ack, cursor_prefix.ACK_KINDS,
            self.snapshot()[self.ack])
        before_delivery = datetime.fromisoformat(self.delivery_row["timestamp"].replace("Z", "+00:00")) - timedelta(seconds=2)
        damaged = dict(rows[0], timestamp=before_delivery.isoformat(
            timespec="milliseconds").replace("+00:00", "Z"))
        (self.home / self.ack).write_bytes(b"".join(encode_frame(row) for row in [damaged, *rows[1:]]))
        cursor_prefix.warm(self.root, "architect")
        self.helper.verify_complete_cursor_banks(self.home, self.snapshot(), node="architect")
        path = self.home / self.name / "checkpoint.json"
        checkpoint = json.loads(path.read_bytes())
        self.assertIs(False, checkpoint["payload"]["folds"]["mail"]["health_safe"])
        checkpoint["payload"]["folds"]["mail"]["health_safe"] = True
        self.resign(self.name, checkpoint)
        with self.assertRaisesRegex(ValueError, "folds differ"):
            self.helper.capture_cursor_prefix_input(self.home, self.snapshot(),
                auxiliary_unavailable_temporal_certificate=True)
        with self.assertRaisesRegex(ValueError, "folds differ"):
            self.helper.verify_complete_cursor_banks(self.home, self.snapshot(), node="architect")

    def test_resigned_ack_delivery_generation_is_not_its_own_source_proof(self):
        observation = self.native()
        checkpoint = json.loads((self.home / self.name / "checkpoint.json").read_bytes())
        checkpoint["payload"]["binding"]["delivery"]["generation"] = "0" * 32
        self.resign(self.name, checkpoint)
        after = self.snapshot()
        with self.assertRaisesRegex(ValueError, "binding or reader revision"):
            self.verify(observation)
        self.assertEqual(after, self.snapshot(), "generation refusal must remain read-only")

    def test_resigned_message_map_cannot_replace_first_ack_with_later_native_ack(self):
        observation = self.native()
        key = self.helper._base._key("message", self.messages[0]["id"])
        target = self.home / self.name / "nodes" / format(int(key, 2), "064x")
        value = json.loads(target.read_bytes())
        self.assertNotEqual(observation["row"], value["payload"]["value"])
        value["payload"]["value"] = observation["row"]
        value["seal"] = hmac.new((self.home / self.name / "seal.key").read_bytes(),
            self.helper._base._bytes(value["payload"]), hashlib.sha256).hexdigest()
        target.write_bytes(self.helper._base._bytes(value))
        after = self.snapshot()
        with self.assertRaisesRegex(ValueError, "complete native map entry"):
            self.verify(observation)
        self.assertEqual(after, self.snapshot(), "first-ACK map refusal must remain read-only")

    def test_resigned_false_consumption_certificate_cannot_replace_complete_true_testimony(self):
        observation = self.native()
        key = self.helper._base._key("consumption", "complete")
        target = self.home / self.name / "nodes" / format(int(key, 2), "064x")
        value = json.loads(target.read_bytes())
        self.assertIs(True, value["payload"]["value"])
        value["payload"]["value"] = False
        value["seal"] = hmac.new((self.home / self.name / "seal.key").read_bytes(),
            self.helper._base._bytes(value["payload"]), hashlib.sha256).hexdigest()
        target.write_bytes(self.helper._base._bytes(value))
        after = self.snapshot()
        with self.assertRaisesRegex(ValueError, "complete native map entry"):
            self.verify(observation)
        self.assertEqual(after, self.snapshot(), "complete-certificate refusal must remain read-only")

    def test_complete_false_certificate_is_proved_but_cannot_be_resealed_as_true(self):
        from floati.wake_hold import WakeHoldController
        held = self.log.send("sender", "architect", "floati", "a" * 40,
            "note.md", "unrelated held ACK", idempotency_key="unrelated-held-ACK")
        WakeHoldController(self.root).evaluate("architect", idempotency_key="proof-held-ACK")
        self.cursor.ack("architect", [held["id"]], acting_session_id="held-seen")
        self.assertIsNotNone(cursor_prefix.warm(self.root, "architect"))
        before = self.snapshot()
        prefix_input = self.helper.capture_cursor_prefix_input(self.home, before)
        self.acknowledge("selected")
        after = self.snapshot()
        self.helper.verify_cursor_prefix_effect(self.home, before, after,
            self.helper.raw_candidate_delta(before, after), prefix_input)
        key = self.helper._base._key("consumption", "complete")
        target = self.home / self.name / "nodes" / format(int(key, 2), "064x")
        value = json.loads(target.read_bytes())
        self.assertIs(False, value["payload"]["value"])
        value["payload"]["value"] = True
        value["seal"] = hmac.new((self.home / self.name / "seal.key").read_bytes(),
            self.helper._base._bytes(value["payload"]), hashlib.sha256).hexdigest()
        target.write_bytes(self.helper._base._bytes(value))
        forged = self.snapshot()
        with self.assertRaisesRegex(ValueError, "complete native map entry"):
            self.helper.verify_cursor_prefix_effect(self.home, before, forged,
                self.helper.raw_candidate_delta(before, forged), prefix_input)
        self.assertEqual(forged, self.snapshot(), "false-certificate refusal must remain read-only")

    def test_checkpoint_authentication_and_retained_secret_are_required(self):
        observation = self.native()
        checkpoint = json.loads((self.home / self.name / "checkpoint.json").read_bytes())
        checkpoint["seal"] = "0" * 64
        (self.home / self.name / "checkpoint.json").write_bytes(self.helper._base._bytes(checkpoint))
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            self.verify(observation)
        key = os.urandom(32)
        (self.home / self.name / "seal.key").write_bytes(key)
        self.resign(self.name, checkpoint, key)
        with self.assertRaisesRegex(ValueError, "retained sealing key"):
            self.verify(observation)

    def test_auxiliary_delivery_cache_write_is_refused_even_with_valid_seal(self):
        observation = self.native()
        checkpoint = json.loads((self.home / self.delivery_cache / "checkpoint.json").read_bytes())
        checkpoint["payload"]["count"] += 1
        self.resign(self.delivery_cache, checkpoint)
        with self.assertRaisesRegex(ValueError, "changed an auxiliary cache"):
            self.verify(observation)

    def test_changed_delivery_source_is_refused(self):
        observation = self.native()
        path = self.home / self.delivery
        path.write_bytes(path.read_bytes() + b"unreviewed\n")
        with self.assertRaisesRegex(ValueError, "changed its event or delivery source"):
            self.verify(observation)

    def test_changed_event_source_is_refused(self):
        observation = self.native()
        path = self.home / "events.jsonl"
        path.write_bytes(path.read_bytes() + b"unreviewed\n")
        with self.assertRaisesRegex(ValueError, "changed its event or delivery source"):
            self.verify(observation)

    def test_complete_delivery_join_rejects_unrequested_historical_wrong_reference(self):
        self.native()
        bad = dict(self.delivery_row, id="delivery-" + uuid7_hex(), item_ids=["msg-" + uuid7_hex()])
        jsonl.append_record(self.root, self.delivery, bad, allowed_kinds=WAKE_HOLD_KINDS)
        with self.assertRaisesRegex(ValueError, "invalid native event or worker plane"):
            self.helper.capture_cursor_prefix_input(self.home, self.snapshot())

    def test_forged_delta_and_staging_or_mode_changes_are_refused(self):
        observation = self.native()
        delta = self.helper.raw_candidate_delta(observation["before"], observation["after"])
        del delta[self.name + "/checkpoint.json"]
        with self.assertRaisesRegex(ValueError, "delta population"):
            self.verify(observation, delta=delta)
        staging = self.home / self.name / "staging" / "residual"
        staging.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "staging population"):
            self.verify(observation)
        staging.rmdir()
        (self.home / self.name / "checkpoint.json").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "not private"):
            self.verify(observation)

    def test_tree_projection_binds_auxiliary_metadata_and_all_ack_files(self):
        observation = self.native()
        for target in (self.name + "/seal.key", self.delivery_cache + "/checkpoint.json"):
            with self.subTest(target=target):
                forged = copy.deepcopy(observation["after_tree"])
                forged["entries"][target]["sha256"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "tree file|auxiliary cache tree"):
                    self.helper.project_cursor_prefix_tree(observation["before_tree"], forged,
                        observation["proof"], observation["begin"], observation["end"],
                        filesystem_clock=observation["filesystem_clock"])

    def delivery_native(self, *, cold=False):
        node = "architect"
        messages = self.messages
        if cold:
            node = "cold-recipient"
            Registry(self.root).register(node, "codex")
            messages = [self.log.send("sender", node, "floati", "a" * 40, "note.md", "cold native delivery",
                idempotency_key="cold-native-delivery")]
        cursor_prefix.warm(self.root, node)
        relative = self.cursor._delivery_relative_path_for(node).as_posix()
        name = self.helper.cache_name(relative)
        if cold:
            shutil.rmtree(self.home / name)
        before = self.snapshot()
        prefix_input = self.helper.capture_delivery_prefix_input(self.home, before, node=node)
        before_tree = self.tree()
        begin, filesystem_clock = capture_filesystem_clock()
        self.log._write_delivery_receipt(node, messages)
        end = datetime.now(timezone.utc)
        after, after_tree = self.snapshot(), self.tree()
        proof = self.helper.verify_delivery_prefix_effect(self.home, before, after,
            self.helper.raw_candidate_delta(before, after), prefix_input, node=node)
        return dict(node=node, relative=relative, name=name, before=before, after=after, prefix_input=prefix_input,
            before_tree=before_tree, after_tree=after_tree, begin=begin, end=end,
            filesystem_clock=filesystem_clock, proof=proof)

    def test_native_warm_delivery_append_proves_id_and_shared_ackable_values(self):
        observation = self.delivery_native()
        self.assertEqual("verified_delivery_prefix_append", observation["proof"]["semantic"]["operation"])
        self.assertEqual(1, sum(path.startswith(observation["name"] + "/nodes/") for path in observation["proof"]["changed_files"]),
            "repeated native delivery must preserve every existing ackable value and insert only its ID")
        self.assertEqual(observation["after"], self.snapshot())
        self.helper.project_delivery_prefix_tree(observation["before_tree"], observation["after_tree"],
            observation["proof"], observation["begin"], observation["end"],
            filesystem_clock=observation["filesystem_clock"])

    def test_native_cold_delivery_append_proves_exact_empty_prior(self):
        observation = self.delivery_native(cold=True)
        self.assertEqual("absent_native_empty", observation["proof"]["semantic"]["input_cache_state"])
        self.helper.project_delivery_prefix_tree(observation["before_tree"], observation["after_tree"],
            observation["proof"], observation["begin"], observation["end"],
            filesystem_clock=observation["filesystem_clock"])

    def test_resigned_delivery_map_cannot_replace_first_real_delivery(self):
        observation = self.delivery_native()
        name = observation["name"]
        rows = self.helper._transcript(self.root, observation["relative"], WAKE_HOLD_KINDS,
            observation["after"][observation["relative"]])
        key = self.helper._base._key("delivered", self.messages[0]["id"])
        target = self.home / name / "nodes" / format(int(key, 2), "064x")
        value = json.loads(target.read_bytes())
        self.assertNotEqual(rows[-1], value["payload"]["value"])
        value["payload"]["value"] = rows[-1]
        value["seal"] = hmac.new((self.home / name / "seal.key").read_bytes(),
            self.helper._base._bytes(value["payload"]), hashlib.sha256).hexdigest()
        target.write_bytes(self.helper._base._bytes(value))
        after = self.snapshot()
        with self.assertRaisesRegex(ValueError, "complete native map entry"):
            self.helper.verify_delivery_prefix_effect(self.home, observation["before"], after,
                self.helper.raw_candidate_delta(observation["before"], after), observation["prefix_input"])
        self.assertEqual(after, self.snapshot(), "first-delivery map refusal must remain read-only")

    def test_delivery_proof_refuses_extra_node_and_resigned_wrong_event_generation(self):
        observation = self.delivery_native()
        name = observation["name"]
        target = self.home / name / "nodes" / ("0" * 64)
        target.write_bytes(b"{}")
        target.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "namespace population"):
            self.helper.verify_delivery_prefix_effect(self.home, observation["before"], self.snapshot(),
                self.helper.raw_candidate_delta(observation["before"], self.snapshot()), observation["prefix_input"])
        target.unlink()
        checkpoint = json.loads((self.home / name / "checkpoint.json").read_bytes())
        checkpoint["payload"]["binding"]["events"]["generation"] = "0" * 32
        self.resign(name, checkpoint)
        with self.assertRaisesRegex(ValueError, "binding or reader revision"):
            self.helper.verify_delivery_prefix_effect(self.home, observation["before"], self.snapshot(),
                self.helper.raw_candidate_delta(observation["before"], self.snapshot()), observation["prefix_input"])

    def test_combined_native_drain_requires_full_delivery_proof_before_ack(self):
        cursor_prefix.warm(self.root, "architect")
        before = self.snapshot()
        ack_input = self.helper.capture_cursor_prefix_input(self.home, before)
        delivery_input = self.helper.capture_delivery_prefix_input(self.home, before)
        before_tree = self.tree()
        begin, filesystem_clock = capture_filesystem_clock()
        messages, delivery, ack = self.log.drain("architect", acting_session_id="combined-native-drain")
        end = datetime.now(timezone.utc)
        self.assertTrue(messages)
        self.assertIsNotNone(delivery)
        self.assertIsNotNone(ack)
        after, after_tree = self.snapshot(), self.tree()
        delta = self.helper.raw_candidate_delta(before, after)
        with self.assertRaisesRegex(ValueError, "changed its event or delivery source"):
            self.helper.verify_cursor_prefix_effect(self.home, before, after, delta, ack_input)
        proof = self.helper.verify_cursor_prefix_effect(self.home, before, after, delta, ack_input,
            delivery_input=delivery_input)
        self.assertIn("combined_delivery_effect", proof)
        self.helper.project_cursor_prefix_tree(before_tree, after_tree, proof, begin, end,
            filesystem_clock=filesystem_clock)
        self.helper.project_delivery_prefix_tree(before_tree, after_tree, proof["combined_delivery_effect"], begin, end,
            filesystem_clock=filesystem_clock)


if __name__ == "__main__":
    unittest.main()
