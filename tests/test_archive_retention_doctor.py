"""Doctor reports archive retention counts from the fleet it opens."""
from __future__ import annotations

import tests  # Pin HOME/XDG before importing product code.

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati.archive_retention import REMOVAL_LEDGER
from floati.bus_epoch import roll_automatic_epoch, validate_epoch_receipt_archive
from floati.doctor import retention_census
from floati.epoch_carry import build_carry_plan
from floati.epoch_rotation import maintenance
from floati.events import EventLog
from floati.framing import decode_frames
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class ArchiveRetentionDoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")
        self.send("one")
        self.send("two")

        def planner(root, old):
            carry = build_carry_plan(root, old)
            return carry.live_payloads, {
                "trigger": "send", "policy": {"max_bytes": 1, "max_age_days": 7,
                    "source": "operator_declared"},
                "observed_bytes": next(len(raw) for name, _plane, raw in old if name == "events.jsonl"),
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None, "due_reasons": ["bytes"],
            }

        roll_automatic_epoch(self.root, actor="sender", planner=planner)
        self.receipt = decode_frames((self.root.path / "events.jsonl").read_bytes())[0]
        self.archive = validate_epoch_receipt_archive(self.root, self.receipt)

    def send(self, key):
        return EventLog(self.root).send("sender", "recipient", "floati", "a" * 40,
            "AGENTS.md", "doctor retention " + key, idempotency_key=key)

    def test_census_counts_one_retained_epoch_and_measures_no_survivor_checkpoint(self):
        maintenance(self.root, actor="sender", trigger="send")
        report = retention_census(self.root)
        checkpoint = self.root.tenant_home / "state/epoch-history/v1/chunks"
        measured = sum(path.stat().st_size for path in checkpoint.rglob("*") if path.is_file()) if checkpoint.exists() else 0
        self.assertFalse(self.archive.is_dir())
        self.assertTrue(self.archive.is_file())
        self.assertEqual(1, report["retained"])
        self.assertEqual(0, report["expired"])
        self.assertEqual(0, report["keep_forever"])
        self.assertEqual(measured, report["survivor_checkpoint_bytes"])

    def test_keep_forever_is_counted_and_expiry_moves_the_epoch(self):
        policy = self.root.tenant_home / "state/archive-policy.json"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({"schema_version": 1, "keep_forever": True}) + "\n", encoding="utf-8")
        maintenance(self.root, actor="sender", trigger="send")
        kept = retention_census(self.root)
        self.assertTrue(self.archive.is_file())
        self.assertEqual(1, kept["keep_forever"])
        self.assertEqual(1, kept["retained"])
        self.assertEqual(0, kept["expired"])
        policy.write_text(json.dumps({"schema_version": 1, "keep_forever": False}) + "\n", encoding="utf-8")
        stamp = datetime.fromisoformat(self.receipt["timestamp"].replace("Z", "+00:00"))

        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return stamp + timedelta(days=30, seconds=1)

        with mock.patch("floati.epoch_rotation.datetime", FutureClock):
            maintenance(self.root, actor="sender", trigger="send")
        gone = retention_census(self.root)
        self.assertFalse(self.archive.exists())
        self.assertTrue((self.root.tenant_home / REMOVAL_LEDGER).is_file())
        self.assertEqual(0, gone["retained"])
        self.assertEqual(1, gone["expired"])
        self.assertEqual(0, gone["keep_forever"])
