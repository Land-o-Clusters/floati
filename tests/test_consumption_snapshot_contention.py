"""Detached consumption publication must stop retrying a changing snapshot."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from floati import jsonl
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.message_consumption import MessageConsumption
from floati.registry import Registry
from floati.root import FloatiRoot


class _RetrySentinel(Exception):
    """Keep the intentionally unbounded pre-fix control from hanging."""


class ConsumptionSnapshotContentionTests(unittest.TestCase):
    def test_repeated_snapshot_changes_refuse_without_an_unbounded_retry_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = FloatiRoot.open_direct_home(Path(temporary) / "fleet", create=True)
            for node in ("sender", "recipient"):
                Registry(root).register(node, "Codex")
            events = EventLog(root)
            messages = [events.send("sender", "recipient", "floati", "a" * 40,
                "docs/evidence/consumption-contention.md", "snapshot contention fixture",
                idempotency_key="contention-" + str(index))["message"]
                for index in range(18)]
            events.drain("recipient", acting_session_id="contention-session")
            initial = MessageConsumption(root).consume(
                "recipient", messages[0]["id"], "contention-session")
            candidate = dict(initial, id="consume-" + uuid7_hex(),
                message_id=messages[-1]["id"], idempotency_key=messages[-1]["id"])
            relative = Path("receipts/consumed/recipient.jsonl")
            path = root.resolve_relative(relative)
            lock_path = path.with_name(path.name + ".lock")
            native_locked = jsonl._locked_path
            changes = []

            @contextmanager
            def locking(selected, *, exclusive, relative=None, **kwargs):
                if selected == lock_path and exclusive:
                    if len(changes) >= 16:
                        raise _RetrySentinel()
                    message = messages[len(changes) + 1]
                    concurrent = dict(initial, id="consume-" + uuid7_hex(),
                        message_id=message["id"], idempotency_key=message["id"])
                    with native_locked(selected, exclusive=True, relative=relative, **kwargs):
                        jsonl._append_frame(path, encode_frame(concurrent))
                    changes.append(concurrent)
                with native_locked(selected, exclusive=exclusive, relative=relative, **kwargs):
                    yield

            with mock.patch.object(jsonl, "_locked_path", locking):
                try:
                    jsonl._transact_consumption_records(root, relative,
                        lambda _prior: (candidate, [candidate]))
                except ProtocolRefusal as refusal:
                    self.assertEqual("consumption_snapshot_contention", refusal.code)
                    self.assertEqual("transient", refusal.remedy["kind"])
                    self.assertEqual("retry_once", refusal.remedy["action"])
                except _RetrySentinel:
                    self.fail("consumption retried 16 changed snapshots without a bounded refusal")
                else:
                    self.fail("consumption published despite a change at every commit check")
            self.assertTrue(changes, "the fixture must actually invalidate a captured snapshot")
            rows = jsonl.read_records_snapshot(root, relative,
                allowed_kinds={"consumption_receipt"})
            self.assertEqual([initial] + changes, rows)
            self.assertNotIn(candidate["id"], {row["id"] for row in rows})
