"""Consumed certification effects require complete native joins and key maps."""
from __future__ import annotations

import hashlib
import hmac
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from floati import key_prefix, records, send_prefix
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.message_consumption import MessageConsumption
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import raw_candidate_delta
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture


def inventory(home):
    return {path.relative_to(home).as_posix(): path.read_bytes()
            for path in home.rglob("*") if path.is_file()}


def metadata(home):
    return {path.relative_to(home).as_posix(): (
        stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_uid,
        info.st_ino, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)
        for path in (home, *home.rglob("*"))
        for info in (path.lstat(),)}


class ConsumedPrefixEffectTests(StableSourceFixture, unittest.TestCase):
    def subject(self):
        name = "tests.fixtures.bus_scale_census.bus_scale_consumed_prefix_effects"
        self.assertIsNotNone(importlib.util.find_spec(name),
            "consumed effects require complete read-only KeyPrefix and native join proof")
        return importlib.import_module(name)

    def setUp(self):
        self.proof = self.subject()
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")
        self.events = EventLog(self.root)
        self.cursor = SparseCursor(self.root)
        self.consumption = MessageConsumption(self.root)
        self.relative = self.consumption._relative("recipient")
        self.first = self.acked("first")
        self.capture()
        self.receipt = self.consume(self.first)

    def acked(self, key, worker=None):
        message = self.events.send(
            "sender", "recipient", "floati", "a" * 40,
            "docs/evidence/consumed-prefix.md", "native proof fixture",
            idempotency_key=key, worker_session_id=worker)["message"]
        self.events.present("recipient", worker_session_id=worker)
        self.cursor.ack("recipient", [message["id"]],
            acting_session_id="seen-session", worker_session_id=worker)
        return message

    def consume(self, message, *, worker=None):
        return self.consumption.consume("recipient", message["id"],
            "acting-session" if worker is None else worker)

    def bank(self, relative=None):
        relative = self.relative if relative is None else Path(relative)
        return self.root.tenant_home / (".consumed-prefix-" +
            hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest())

    def capture(self, *, worker=None):
        self.before = inventory(self.root.tenant_home)
        self.prefix_input = self.proof.capture_consumed_prefix_input(
            self.root.tenant_home, self.before, node="recipient",
            worker_session_id=worker)

    def prove(self, *, worker=None, delta=None, prefix_input=None):
        home = self.root.tenant_home
        after = inventory(home)
        physical = metadata(home)
        try:
            # A verifier must never build or heal the bank that it judges.
            with patch.object(key_prefix.KeyPrefix, "__init__",
                    side_effect=AssertionError("proof constructed a writable cache")):
                return self.proof.verify_consumed_prefix_effect(home,
                    self.before, after,
                    raw_candidate_delta(self.before, after) if delta is None else delta,
                    self.prefix_input if prefix_input is None else prefix_input,
                    node="recipient", worker_session_id=worker)
        finally:
            self.assertEqual(after, inventory(home),
                "complete consumed proof changed physical file bytes")
            self.assertEqual(physical, metadata(home),
                "complete consumed proof changed physical metadata")

    def test_cold_native_append_has_exact_typed_source_and_map_proof(self):
        proof = self.prove()
        self.assertEqual("complete_native_consumed_prefix", proof["kind"])
        self.assertTrue(proof["complete_native_join_validated"])
        self.assertEqual(1, proof["consumed_records"])
        self.assertTrue(proof["changed_files"])
        self.assertTrue(all(Path(path).parts[0] == self.bank().name
                            for path in proof["changed_files"]))

    def test_warm_native_append_proves_complete_old_and_new_maps(self):
        second = self.acked("second")
        self.capture()
        self.consume(second)
        proof = self.prove()
        self.assertEqual(2, proof["consumed_records"])
        self.assertTrue(proof["complete_native_join_validated"])

    def test_read_rebuild_without_checkpoint_preserves_native_source(self):
        (self.bank() / "checkpoint.json").unlink()
        self.capture()
        source = self.root.resolve_relative(self.relative).read_bytes()
        self.assertEqual(self.receipt, self.consume(self.first))
        proof = self.prove()
        self.assertEqual(source, self.root.resolve_relative(self.relative).read_bytes())
        self.assertEqual(1, proof["consumed_records"])

    def test_worker_plane_uses_its_exact_consumed_relative_hash(self):
        worker = "worker-session"
        message = self.acked("worker", worker)
        self.capture(worker=worker)
        self.consume(message, worker=worker)
        proof = self.prove(worker=worker)
        name = self.bank(self.consumption._relative("recipient", worker)).name
        self.assertTrue(proof["changed_files"])
        self.assertTrue(all(Path(path).parts[0] == name for path in proof["changed_files"]))
        with self.assertRaises(ValueError):
            self.prove()

    def test_retained_banks_resolve_only_native_recipient_and_worker_planes(self):
        """Nearest: the worker-plane proof above and retained cursor controls."""
        from floati import consumed_prefix, cursor_prefix
        from tests.bus_scale_lifecycle_fixture import load
        observer = load(Path(self.proof.__file__).with_name(
            "bus-scale-expanded-observation-2026-10-15.py"), "retained_consumed_plane_control")
        planes = [None]
        for mode in ("ordinary", "worker", "empty_worker"):
            with self.subTest(mode=mode):
                plane = None if mode == "ordinary" else mode + "-session"
                if plane is not None:
                    message = self.acked(mode, plane)
                    planes.append(plane)
                    if mode == "worker":
                        self.consume(message, worker=plane)
                    else:
                        path = self.root.resolve_relative(self.consumption._relative("recipient", plane))
                        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        path.touch(mode=0o600)
                for known in planes:
                    self.assertIsNotNone(cursor_prefix.warm(self.root, "recipient", worker_session_id=known))
                self.assertIsNotNone(consumed_prefix.warm(self.root, "recipient"))
                raw = inventory(self.root.tenant_home)
                name = self.bank(self.consumption._relative("recipient", plane)).name
                certificates = observer.capture_mail_certificates(self.root.tenant_home, raw)
                self.assertIn(name, certificates)
                tree = observer.tree(self.root.tenant_home)
                self.assertIn(name, observer.retained_mail_trees(self.root.tenant_home,
                    tree, tree, raw, raw, certificates))

    def test_retained_consumed_content_ignores_rebuild_entropy_after_complete_work_proof(self):
        """Two fully proved rebuilds are equal; source/node tampering still refuses."""
        from datetime import datetime, timezone
        from floati import consumed_prefix, cursor_prefix
        from floati.planes import AuthorityGrantStore
        from floati.work import WorkLog
        from tests.bus_scale_lifecycle_fixture import load
        observer = load(Path(self.proof.__file__).with_name(
            "bus-scale-expanded-observation-2026-10-15.py"), "retained_consumed_content_control")
        message = self.acked("completed-work")
        work = WorkLog(self.root)
        now = datetime.now(timezone.utc)
        grant = AuthorityGrantStore(self.root).claim("work-claims", "recipient", 60, 60, now)
        item = work.add("consume dispatch", "recipient", [])
        work.claim(item["id"], "recipient", "work-claims", grant["epoch"], now=now)
        work.complete(item["id"], "recipient", [], consume_ids=(message["id"],),
            session_id="acting-session")
        home = self.root.tenant_home
        projections, generations = [], []
        for attempt in range(2):
            if attempt:
                source = self.root.resolve_relative("work/items.jsonl")
                replacement = source.with_suffix(".clone")
                shutil.copy2(source, replacement)
                replacement.replace(source)
            for bank in list(home.iterdir()):
                if bank.name == ".send-prefix" or bank.name.startswith((".cursor-prefix-", ".consumed-prefix-")):
                    shutil.rmtree(bank)
            self.assertIsNotNone(send_prefix.warm(self.root))
            self.assertIsNotNone(cursor_prefix.warm(self.root, "recipient"))
            self.assertIsNotNone(consumed_prefix.warm(self.root, "recipient"))
            raw = inventory(home)
            self.proof.verify_complete_consumed_bank(home, raw, node="recipient")
            certificate = self.proof._keys._content_certificate(raw, self.bank().name,
                self.relative.as_posix())
            projections.append(certificate)
            generations.append(json.loads(raw[self.bank().name + "/checkpoint.json"])["payload"]["generation"])
        self.assertNotEqual(*generations)
        self.assertEqual(*projections)
        tree = observer.tree(home)
        node = next((self.bank() / "nodes").iterdir())
        node.write_bytes(node.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            self.proof.verify_complete_consumed_bank(home, inventory(home), node="recipient")
        with self.assertRaises(observer.UnresolvedObservation):
            observer.retained_mail_trees(home, tree, observer.tree(home), raw,
                inventory(home), {self.bank().name: certificate})

    def test_validly_resealed_false_map_entry_is_refused_without_repair(self):
        from floati.consumed_prefix import ConsumedPrefix
        prefix = ConsumedPrefix(self.root, self.relative, "recipient")
        prefix.load()
        lookup = send_prefix._key("message", self.receipt["message_id"])
        path = prefix.nodes / prefix.filename(lookup)
        path.write_bytes(send_prefix._bytes(prefix._entry(lookup,
            dict(self.receipt, acting_session_id="forged-session"))))
        with self.assertRaises(ValueError):
            self.prove()

    def test_resealed_mail_folds_and_tree_nodes_cannot_invent_consumption(self):
        self.prove()
        path = self.bank() / "checkpoint.json"
        original = path.read_bytes()
        secret = (self.bank() / "seal.key").read_bytes()
        for field, replacement in (("members", None), ("event_generation", "0" * 32),
                                   ("dependencies", None), ("ack_generation", "0" * 32)):
            with self.subTest(field=field):
                value = json.loads(original)
                if field == "ack_generation":
                    value["payload"]["folds"]["mail"]["dependencies"]["ack"]["generation"] = replacement
                else:
                    value["payload"]["folds"]["mail"][field] = replacement
                value["seal"] = hmac.new(secret, send_prefix._bytes(value["payload"]), hashlib.sha256).hexdigest()
                try:
                    path.write_bytes(send_prefix._bytes(value))
                    with self.assertRaises(ValueError):
                        self.prove()
                finally:
                    path.write_bytes(original)
        digest = json.loads(original)["payload"]["folds"]["mail"]["members"]
        lookup = send_prefix._key("mail-tree-v1", digest)
        target = self.bank() / "nodes" / format(int(lookup, 2), "064x")
        native = target.read_bytes()
        value = json.loads(native)
        value["payload"]["value"]["item"]["ordinal"] += 1
        value["seal"] = hmac.new(secret, send_prefix._bytes(value["payload"]), hashlib.sha256).hexdigest()
        try:
            target.write_bytes(send_prefix._bytes(value))
            with self.assertRaises(ValueError):
                self.prove()
        finally:
            target.write_bytes(native)

    def test_fully_sealed_receipt_without_native_ack_join_is_refused(self):
        # Fixture-only adversary: a self-consistent byte chain, native-shaped
        # receipt and authenticated key map still cannot invent ACK provenance.
        damaged = dict(self.receipt, ack_receipt_id="ack-" + uuid7_hex())
        raw = encode_frame(damaged)
        path = self.root.resolve_relative(self.relative)
        path.write_bytes(raw)
        class ForgedPrefix(key_prefix.KeyPrefix):
            def _fold(self, rows, prior):
                return folds

        prefix = ForgedPrefix(self.root, {"consumption_receipt"},
            relative=self.relative, cache_name=self.bank().name,
            format="consumed-prefix-v2", folds={"mail": 1}, revision={
                "source": send_prefix._revision(), "reader_version": records.READER_VERSION})
        entries = {send_prefix._key(domain, value): damaged for domain, value in (
            ("id", damaged["id"]), ("message", damaged["message_id"]),
            ("key", damaged["idempotency_key"]))}
        event_rows, _known = self.proof._keys._events._transcript(self.root,
            self.root.resolve_relative("events.jsonl").read_bytes())
        envelope = next(dict(row, ordinal=ordinal) for ordinal, row in enumerate(event_rows)
            if row["kind"] == "message_envelope" and row["id"] == damaged["message_id"])
        members = self.proof._keys._mail_add(entries, None, envelope)
        event_payload = json.loads((self.root.tenant_home / ".send-prefix/checkpoint.json").read_bytes())["payload"]
        dependency = self.proof._dependencies(self.root, inventory(self.root.tenant_home),
            "recipient", None, event_payload)
        folds = {"mail": {"members": members, "event_generation": event_payload["generation"],
            "dependencies": dependency}}
        prefix.rebuild_entries([damaged], entries, send_prefix._identity(path), raw)
        with self.assertRaises(ValueError):
            self.prove()

    def test_missing_bucket_foreign_binding_and_symlink_refuse(self):
        bank = self.bank()
        saved = self.base / "saved-bank"
        shutil.copytree(bank, saved)
        for mutation in ("bucket", "binding", "symlink"):
            with self.subTest(mutation=mutation):
                shutil.rmtree(bank)
                shutil.copytree(saved, bank)
                if mutation == "bucket":
                    next((bank / "buckets").iterdir()).unlink()
                elif mutation == "binding":
                    path = bank / "checkpoint.json"
                    value = json.loads(path.read_bytes())
                    value["payload"]["binding"]["root"] = str(self.base / "foreign")
                    value["seal"] = hmac.new((bank / "seal.key").read_bytes(),
                        send_prefix._bytes(value["payload"]), hashlib.sha256).hexdigest()
                    path.write_bytes(send_prefix._bytes(value))
                else:
                    path = next((bank / "nodes").iterdir())
                    foreign = self.base / "foreign-entry"
                    foreign.write_bytes(path.read_bytes())
                    path.unlink()
                    path.symlink_to(foreign)
                with self.assertRaises(ValueError):
                    self.prove()

    def test_changed_source_bytes_refuse_even_with_retained_output_bank(self):
        path = self.root.resolve_relative(self.relative)
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b"acting-session", b"forged-session"))
        with self.assertRaises(ValueError):
            self.prove()

    def test_unexplained_cache_member_and_invented_raw_delta_refuse(self):
        after = inventory(self.root.tenant_home)
        delta = raw_candidate_delta(self.before, after)
        delta[self.bank().name + "/invented"] = {"operation": "append"}
        with self.assertRaises(ValueError):
            self.prove(delta=delta)
        target = self.bank() / "staging/unexplained"
        target.write_bytes(b"unexplained native-effect claim\n")
        target.chmod(0o600)
        with self.assertRaises(ValueError):
            self.prove()

    def test_foreign_input_coordinate_refuses_and_other_literals_are_not_admitted(self):
        wrong = dict(self.prefix_input, root=str(self.base / "foreign-root"))
        with self.assertRaises(ValueError):
            self.prove(prefix_input=wrong)
        foreign = self.root.tenant_home / (self.bank().name + "-foreign")
        foreign.mkdir(mode=0o700)
        target = foreign / "payload"
        target.write_bytes(b"unreviewed prefix literal\n")
        proof = self.prove()
        self.assertNotIn(target.relative_to(self.root.tenant_home).as_posix(),
                         proof["changed_files"])
