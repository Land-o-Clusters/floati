"""Ruled receipt and inbox artifacts retain exact fixture cleanup ownership."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.message_consumption import MessageConsumption
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.bus_scale_case_seeds import FixtureSendCache, inventory
from tests.temp_roots import REAL_TEMP_ROOT


class FixtureBusPrefixCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")
        self.owner = FixtureSendCache(self.base, self.root.path)

    def prepare(self):
        log = EventLog(self.root)
        message = log.send("sender", "recipient", "floati", "a" * 40,
            "note.md", "owned fixture message", idempotency_key="cleanup")["message"]
        log.present("recipient")
        SparseCursor(self.root).ack("recipient", [message["id"]], acting_session_id="seen")
        MessageConsumption(self.root).consume("recipient", message["id"], "acted")
        consumed = ".consumed-prefix-" + hashlib.sha256(
            b"receipts/consumed/recipient.jsonl").hexdigest()
        return message, consumed

    def test_cleanup_removes_only_exact_owned_receipt_and_inbox_artifacts(self):
        message, consumed = self.prepare()
        before = inventory(self.root.path)
        receipt = self.owner.remove([message["id"]])
        expected_names = {".send-prefix", ".inbox-prefix", consumed}
        self.assertTrue(expected_names <= set(receipt["cache_relatives"]),
            "fixture cleanup omitted a ruled native artifact")
        removed = set(receipt["cache_relatives"])
        self.assertEqual({path: raw for path, raw in before.items()
            if Path(path).parts[0] not in removed}, inventory(self.root.path))

    def test_foreign_hashed_receipt_bank_is_refused_without_mutation(self):
        message, consumed = self.prepare()
        foreign = self.base / "foreign" / consumed
        foreign.mkdir(parents=True, mode=0o700)
        (foreign / "sentinel").write_bytes(b"foreign bank")
        before = inventory(self.base)
        with self.assertRaises(ValueError) as refused:
            self.owner.remove([message["id"]], cache_path=foreign)
        self.assertEqual("fixture_cache_outside_root", refused.exception.code)
        self.assertEqual(before, inventory(self.base))

    def test_unknown_hash_is_preserved_rather_than_admitted_by_a_pattern(self):
        message, consumed = self.prepare()
        unknown = self.root.path / (".consumed-prefix-" + "0" * 64)
        unknown.mkdir(mode=0o700)
        (unknown / "sentinel").write_bytes(b"unproved coordinate")
        receipt = self.owner.remove([message["id"]])
        self.assertIn(consumed, receipt["cache_relatives"],
            "the exact native consumed coordinate was not admitted")
        self.assertNotIn(unknown.name, receipt["cache_relatives"])
        self.assertEqual(b"unproved coordinate", (unknown / "sentinel").read_bytes())
