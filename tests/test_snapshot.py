from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from floati.errors import SnapshotRefusal
from floati.jsonl import append_record
from floati.root import FloatiRoot
from floati.snapshot import SnapshotStore, SourceSpec


UUIDS = (
    "018f7e9b3c117abc8def0123456789ab",
    "018f7e9b3c127abc8def0123456789ab",
)


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "snapshot-alpha", create=True
        )
        append_record(
            self.root,
            "events.jsonl",
            self.message(0),
            allowed_kinds={"message_envelope"},
        )

    def message(self, index: int) -> dict[str, object]:
        return {
            "schema_version": 0,
            "id": f"msg-{UUIDS[index]}",
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-01T12:00:00.000Z",
            "kind": "message_envelope",
            "sender": "alice",
            "recipient": "bob",
            "repo": "slipway",
            "sha": "a" * 40,
            "doc": "docs/evidence/HM3H-GAUNTLET.md",
            "note": f"snapshot {index}",
            "idempotency_key": f"snapshot-{index}",
        }

    def store(self, root: FloatiRoot | None = None) -> SnapshotStore:
        selected = self.root if root is None else root
        return SnapshotStore(
            selected,
            reader="status",
            key="default",
            discover_sources=lambda: (
                SourceSpec(Path("events.jsonl"), frozenset({"message_envelope"})),
            ),
        )

    def test_snapshot_path_uses_floati_directory_without_a_legacy_write(self) -> None:
        store = self.store()

        self.assertEqual(
            (".floati-snapshots", "v0"),
            store.path.relative_to(self.root.tenant_home).parts[:2],
        )
        store.refresh({"count": 1})

        self.assertTrue(store.path.is_file())
        self.assertFalse(os.path.lexists(self.root.tenant_home / ".slipway-snapshots"))

    @staticmethod
    def rewrite(path: Path, mutate: object, *, checksum: bool = True) -> None:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        mutate(envelope)
        if checksum:
            unsigned = dict(envelope)
            unsigned.pop("checksum", None)
            canonical = json.dumps(
                unsigned,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            envelope["checksum"] = hashlib.sha256(canonical).hexdigest()
        path.write_text(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def test_valid_anchor_loads_only_validated_tail(self) -> None:
        store = self.store()
        store.refresh({"count": 1})
        second = self.message(1)
        append_record(
            self.root,
            "events.jsonl",
            second,
            allowed_kinds={"message_envelope"},
        )

        loaded = store.load()

        self.assertEqual({"count": 1}, loaded.payload)
        self.assertEqual(
            [second["id"]],
            [row["id"] for row in loaded.tails["events.jsonl"]],
        )

    def test_refresh_refuses_when_source_changes_during_projection(self) -> None:
        store = self.store()
        before_scan = store.capture()
        append_record(
            self.root,
            "events.jsonl",
            self.message(1),
            allowed_kinds={"message_envelope"},
        )

        with self.assertRaises(SnapshotRefusal) as raised:
            store.refresh({"count": 1}, expected=before_scan)

        self.assertEqual("snapshot_source_changed", raised.exception.code)
        self.assertFalse(store.path.exists())

    def test_torn_snapshot_is_typed(self) -> None:
        store = self.store()
        store.refresh({"count": 1})
        store.path.write_bytes(b'{"snapshot_version":0')

        with self.assertRaises(SnapshotRefusal) as raised:
            store.load()

        self.assertEqual("snapshot_parse_invalid", raised.exception.code)

    def test_version_root_offset_ordinal_digest_and_checksum_are_distinct(self) -> None:
        mutations = (
            (
                "snapshot_version_mismatch",
                lambda envelope: envelope.__setitem__("snapshot_version", 1),
                True,
            ),
            (
                "snapshot_identity_mismatch",
                lambda envelope: envelope.__setitem__("root", "/private/tmp/other"),
                True,
            ),
            (
                "snapshot_anchor_past_eof",
                lambda envelope: envelope["sources"][0].__setitem__(
                    "byte_offset", envelope["sources"][0]["byte_offset"] + 1
                ),
                True,
            ),
            (
                "snapshot_anchor_ordinal_mismatch",
                lambda envelope: envelope["sources"][0].__setitem__(
                    "record_ordinal", envelope["sources"][0]["record_ordinal"] + 1
                ),
                True,
            ),
            (
                "snapshot_anchor_digest_mismatch",
                lambda envelope: envelope["sources"][0].__setitem__(
                    "prefix_sha256", "0" * 64
                ),
                True,
            ),
            (
                "snapshot_checksum_mismatch",
                lambda envelope: envelope["payload"].__setitem__("count", 2),
                False,
            ),
        )
        for code, mutation, checksum in mutations:
            with self.subTest(code=code):
                store = self.store()
                store.refresh({"count": 1})
                self.rewrite(store.path, mutation, checksum=checksum)

                with self.assertRaises(SnapshotRefusal) as raised:
                    store.load()

                self.assertEqual(code, raised.exception.code)

    def test_snapshot_copied_from_another_root_is_typed(self) -> None:
        source = self.store()
        source.refresh({"count": 1})
        other = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "snapshot-beta", create=True
        )
        destination = self.store(other)
        destination.path.parent.mkdir(parents=True, exist_ok=True)
        destination.path.write_bytes(source.path.read_bytes())

        with self.assertRaises(SnapshotRefusal) as raised:
            destination.load()

        self.assertEqual("snapshot_identity_mismatch", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
