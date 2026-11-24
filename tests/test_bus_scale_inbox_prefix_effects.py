"""Recipient cache admission requires the complete exact native event map."""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

from floati import send_prefix
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import raw_candidate_delta
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture


def inventory(home):
    return {path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*") if path.is_file()}


class InboxPrefixEffectTests(StableSourceFixture, unittest.TestCase):
    def setUp(self):
        name = "tests.fixtures.bus_scale_census.bus_scale_inbox_prefix_effects"
        self.assertIsNotNone(importlib.util.find_spec(name),
            "inbox effects require complete independent native map proof")
        self.proof = importlib.import_module(name)
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")
        self.log = EventLog(self.root)
        self.message = self.log.send("sender", "recipient", "floati", "a" * 40,
            "note.md", "native prefix proof", idempotency_key="proof")["message"]
        self.before = inventory(self.root.path)
        self.captured = self.proof.capture_inbox_prefix_input(self.root.path, self.before)
        self.log.present("recipient")

    def prove(self, *, captured=None):
        after = inventory(self.root.path)
        try:
            return self.proof.verify_inbox_prefix_effect(self.root.path,
                self.before, after, raw_candidate_delta(self.before, after),
                self.captured if captured is None else captured)
        finally:
            self.assertEqual(after, inventory(self.root.path), "proof changed native bytes")

    def test_complete_native_bank_is_proved_without_cache_construction(self):
        from unittest.mock import patch
        with patch("floati.key_prefix.KeyPrefix.__init__", side_effect=AssertionError("proof constructed cache")):
            proof = self.prove()
        self.assertEqual("complete_native_inbox_prefix", proof["kind"])
        self.assertTrue(proof["changed_files"])
        self.assertTrue(all(path.startswith(".inbox-prefix/") for path in proof["changed_files"]))

    def test_validly_resealed_false_recipient_member_refuses(self):
        from floati.inbox_prefix import InboxPrefix, recipient_key
        prefix = InboxPrefix(self.root)
        prefix.load()
        lookup = send_prefix._key("recipient", recipient_key("recipient", 0))
        path = prefix.nodes / prefix.filename(lookup)
        path.write_bytes(send_prefix._bytes(prefix._entry(lookup, dict(self.message, note="false"))))
        with self.assertRaises(ValueError):
            self.prove()

    def test_missing_member_and_unexplained_file_each_refuse(self):
        from floati.inbox_prefix import InboxPrefix
        prefix = InboxPrefix(self.root)
        member = next(prefix.nodes.iterdir())
        raw = member.read_bytes()
        member.unlink()
        with self.assertRaises(ValueError):
            self.prove()
        member.write_bytes(raw)
        member.chmod(0o600)
        (prefix.staging / "unexplained").write_bytes(b"unproved")
        with self.assertRaises(ValueError):
            self.prove()

    def test_changed_authoritative_source_and_foreign_input_refuse(self):
        captured = dict(self.captured, root=str(self.base / "foreign"))
        with self.assertRaises(ValueError):
            self.prove(captured=captured)
        self.log.send("sender", "recipient", "floati", "a" * 40,
            "note.md", "later source", idempotency_key="later")
        with self.assertRaises(ValueError):
            self.prove()

    def test_other_literal_stays_outside_this_proof(self):
        unruled = self.root.path / ".inbox-prefix-other"
        unruled.mkdir(mode=0o700)
        (unruled / "sentinel").write_bytes(b"unproved")
        self.assertNotIn(".inbox-prefix-other/sentinel", self.prove()["changed_files"])

    def native_append(self, *, overwrite_directory=False):
        from contextlib import nullcontext
        from unittest.mock import patch
        from floati.key_prefix import KeyPrefix
        from floati.inbox_prefix import CACHE_NAME

        home = self.root.path
        before = inventory(home)
        captured = self.proof.capture_inbox_prefix_append_input(home, before)
        directory = home / CACHE_NAME / "directory.bin"
        original = KeyPrefix._atomic_at

        def publish(prefix, bank, name, data, **options):
            if bank == "home" and name == "directory.bin":
                (prefix.home / name).write_bytes(data)
            else:
                original(prefix, bank, name, data, **options)

        with directory.open("rb") as retained:
            prior_identity = os.fstat(retained.fileno())
            prior_bytes = retained.read()
            mutation = patch.object(KeyPrefix, "_atomic_at", publish) if overwrite_directory else nullcontext()
            with mutation:
                message = self.log.send("sender", "recipient", "floati", "a" * 40,
                    "note.md", "native appended inbox member", idempotency_key="append-proof")["message"]
            if not overwrite_directory:
                retained.seek(0)
                self.assertEqual(prior_bytes, retained.read(), "native append changed an already open directory")
                current = directory.stat()
                self.assertNotEqual((prior_identity.st_dev, prior_identity.st_ino),
                    (current.st_dev, current.st_ino), "native publication did not replace its derived directory")
        return before, captured, message

    def prove_append(self, before, captured):
        after = inventory(self.root.path)
        try:
            return self.proof.verify_inbox_prefix_append_effect(self.root.path,
                before, after, raw_candidate_delta(before, after), captured)
        finally:
            self.assertEqual(after, inventory(self.root.path), "append proof changed native bytes")

    def test_native_append_accepts_atomic_directory_replacement_and_preserves_open_reader(self):
        from unittest.mock import patch
        before, captured, _message = self.native_append()
        with patch("floati.key_prefix.KeyPrefix.__init__", side_effect=AssertionError("proof constructed cache")):
            proof = self.prove_append(before, captured)
        self.assertEqual("verified_inbox_prefix_append", proof["semantic"]["operation"])
        self.assertIn(".inbox-prefix/directory.bin", proof["changed_files"])
        self.assertEqual(2, proof["semantic"]["appended_records"])

    def test_observer_requires_declared_verified_atomic_inbox_replacement(self):
        before, captured, _message = self.native_append()
        path = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py"
        spec = importlib.util.spec_from_file_location("inbox_append_observer_control", path)
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        after = inventory(self.root.path)
        # Exercise this declared transport alone; complete lifecycle tests own
        # their separate send-index and native report-pair proof.
        before, after = ({path: raw for path, raw in snapshot.items()
            if path == "events.jsonl" or path.startswith(".inbox-prefix/")}
            for snapshot in (before, after))
        with self.assertRaises(observer.UnresolvedObservation):
            observer.native_delta(before, after, home=self.root.path)
        delta = observer.native_delta(before, after, home=self.root.path,
            inbox_append_input=captured)
        self.assertEqual("replace", delta[".inbox-prefix/directory.bin"]["operation"])
        self.assertEqual("append", delta["events.jsonl"]["operation"])
        stale = dict(after)
        stale.update({path: raw for path, raw in before.items() if path.startswith(".inbox-prefix/")})
        for path in set(after) - set(before):
            if path.startswith(".inbox-prefix/"):
                stale.pop(path)
        with self.assertRaises(ValueError):
            observer.native_delta(before, stale, home=self.root.path, inbox_append_input=captured)

    def test_native_append_refuses_in_place_directory_overwrite(self):
        before, captured, _message = self.native_append(overwrite_directory=True)
        with self.assertRaises(ValueError):
            self.prove_append(before, captured)

    def test_declared_immutable_directory_refuses_in_place_native_rewrite(self):
        from unittest.mock import patch
        from floati.key_prefix import KeyPrefix

        original = KeyPrefix.storage_declaration

        def retained_directory(**coordinates):
            declaration = original(**coordinates)
            declaration["index"]["files"]["directory.bin"] = "retained_immutable"
            return declaration

        with patch.object(KeyPrefix, "storage_declaration", staticmethod(retained_directory)):
            before, captured, _message = self.native_append(overwrite_directory=True)
            with self.assertRaisesRegex(ValueError, "retained immutable"):
                self.prove_append(before, captured)

    def test_new_native_index_members_require_writer_role(self):
        import copy
        from unittest.mock import patch
        from floati.key_prefix import KeyPrefix

        before, captured, _message = self.native_append()
        after = inventory(self.root.path)
        fresh = min(path for path in set(after) - set(before)
            if path.startswith(".inbox-prefix/nodes/"))
        declaration = copy.deepcopy(captured["storage_declaration"])
        declaration["index"]["files"][Path(fresh).relative_to(".inbox-prefix").as_posix()] = "unknown_writer_role"
        captured = dict(captured, storage_declaration=declaration)
        with patch.object(KeyPrefix, "storage_declaration",
                staticmethod(lambda **coordinates: copy.deepcopy(declaration))):
            with self.assertRaisesRegex(ValueError, "undeclared native index file"):
                self.prove_append(before, captured)

    def test_observer_follows_writer_storage_declaration_after_cache_relocation(self):
        """A derived index's policy follows its writer, not its old pathname."""
        from unittest.mock import patch
        from floati import inbox_prefix

        home = self.root.path
        relocated = ".relocated-native-index"
        (home / inbox_prefix.CACHE_NAME).rename(home / relocated)
        path = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py"
        spec = importlib.util.spec_from_file_location("writer_declared_index_observer", path)
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        with patch.object(inbox_prefix, "CACHE_NAME", relocated):
            before, captured, _message = self.native_append()
            after = inventory(home)
            before, after = ({name: raw for name, raw in snapshot.items()
                if name == "events.jsonl" or name.startswith(relocated + "/")}
                for snapshot in (before, after))
            delta = observer.native_delta(before, after, home=home,
                inbox_append_input=captured)
            self.assertEqual("replace", delta[relocated + "/directory.bin"]["operation"])
            self.assertEqual("append", delta["events.jsonl"]["operation"])
            rewritten = dict(after, **{"events.jsonl": b"rewritten retained source"})
            with self.assertRaisesRegex(observer.UnresolvedObservation, "retained prefix"):
                observer.native_delta(before, rewritten, home=home,
                    inbox_append_input=captured)

    def test_teardown_preflight_admits_only_exact_inbox_creation(self):
        from unittest.mock import patch
        from floati.inbox_prefix import warm
        path = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py"
        spec = importlib.util.spec_from_file_location("inbox_preflight_control", path)
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        for fault in (None, "bytes", "metadata"):
            with self.subTest(fault=fault):
                home = self.base / ("preflight-" + (fault or "native"))
                root = FloatiRoot.open_direct_home(home, create=True)
                for node in ("sender", "recipient"):
                    Registry(root).register(node, "Codex")
                EventLog(root).send("sender", "recipient", "floati", "a" * 40,
                    "note.md", "native preflight", idempotency_key="preflight")
                sentinel = home / "sentinel.json"
                sentinel.write_bytes(b"retained")
                self.assertFalse((home / ".inbox-prefix").exists())

                def warmed(candidate):
                    result = warm(candidate)
                    if fault == "bytes":
                        sentinel.write_bytes(b"forged")
                    elif fault == "metadata":
                        info = sentinel.stat()
                        os.utime(sentinel, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
                    return result

                with patch("floati.inbox_prefix.warm", warmed):
                    if fault is None:
                        before = observer.tree(home)["entries"]["."]
                        receipt = observer.prepare_teardown_inbox(home)
                        self.assertTrue((home / ".inbox-prefix").is_dir())
                        if receipt is None:
                            # Creating the cache in the same coarse clock tick
                            # need not change the root's mtime at all.
                            self.assertEqual(before, observer.tree(home)["entries"]["."])
                        else:
                            self.assertEqual(str(home), receipt["root"])
                            self.assertEqual(before, receipt["root_before"])
                            self.assertNotEqual(receipt["root_before"], receipt["root_after"])
                    else:
                        with self.assertRaises(observer.UnresolvedObservation):
                            observer.prepare_teardown_inbox(home)

    def test_native_append_refuses_rewritten_source_and_unproved_cache_members(self):
        from floati.inbox_prefix import InboxPrefix
        before, captured, message = self.native_append()
        prefix = InboxPrefix(self.root)
        prefix.load()
        lookup = send_prefix._key("message", message["id"])
        node = prefix.nodes / prefix.filename(lookup)
        source = self.root.path / "events.jsonl"
        extra = prefix.home / "unexplained.bin"
        faults = (
            (node, send_prefix._bytes(prefix._entry(lookup, dict(message, note="false")))),
            (extra, b"unproved"),
            (source, source.read_bytes().replace(b"native prefix proof", b"forged prefix proof", 1)),
        )
        for path, hostile in faults:
            with self.subTest(coordinate=path.relative_to(self.root.path).as_posix()):
                original = path.read_bytes() if path.exists() else None
                path.write_bytes(hostile)
                path.chmod(0o600)
                try:
                    with self.assertRaises(ValueError):
                        self.prove_append(before, captured)
                finally:
                    if original is None:
                        path.unlink()
                    else:
                        path.write_bytes(original)
