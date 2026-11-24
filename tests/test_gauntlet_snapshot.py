from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from floati.events import EventLog
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.jsonl import append_record
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.replay import ReplayTimeline
from floati.replay_render import render_replay_plain
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.tui import model_from_root
from floati.tui_render import render_plain_dump
from floati.work import WorkLog

# The dot-prefixed workspace name the pre-rename product wrote, built from
# the fence's own governed token rather than spelled: these fixtures drive a
# refusal (or assert an absence) whose whole mechanism is these exact bytes.
LEGACY_PREFIX = "." + RETIRED_PRODUCT_NAME
from tests.hm3i_gauntlet_fixtures import (
    assert_physical_projection,
    build_success_trace,
    canonical_observation_from_records,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
UUID = "018f7e9b3c117abc8def0123456789ab"
INBOX_NOTE = "snapshot gauntlet"


@dataclass(frozen=True)
class ReaderCase:
    name: str
    root: FloatiRoot
    answer: Callable[[], object]
    snapshot_path: Path


class HostileSnapshotGauntletTests(unittest.TestCase):
    @staticmethod
    def canonical(envelope: dict[str, object]) -> bytes:
        unsigned = dict(envelope)
        unsigned.pop("checksum", None)
        return json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def rewrite(
        cls,
        path: Path,
        mutation: Callable[[dict[str, object]], None],
        *,
        checksum: bool = True,
    ) -> None:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        mutation(envelope)
        if checksum:
            envelope["checksum"] = hashlib.sha256(
                cls.canonical(envelope)
            ).hexdigest()
        path.write_text(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    @staticmethod
    def nonempty_anchor(envelope: dict[str, object]) -> dict[str, object]:
        return next(
            anchor
            for anchor in envelope["sources"]
            if anchor["byte_offset"] > 0
        )

    @staticmethod
    def rewrite_inbox(
        case: ReaderCase,
        mutation: Callable[[dict[str, object]], None],
        *,
        authenticate: bool = True,
    ) -> None:
        def encode(value):
            return json.dumps(value, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True).encode("ascii")

        envelope = json.loads(case.snapshot_path.read_bytes())
        mutation(envelope["payload"])
        if authenticate:
            secret = (case.snapshot_path.parent / "seal.key").read_bytes()
            envelope["seal"] = hmac.new(secret, encode(envelope["payload"]), hashlib.sha256).hexdigest()
        case.snapshot_path.write_bytes(encode(envelope))

    def build_case(self, base: Path, name: str) -> ReaderCase:
        root = FloatiRoot.open_direct_home(base / f"gauntlet-{name}", create=True)
        registry = Registry(root)
        registry.register(public_ids.worker('alpha'), "worker")
        WorkLog(root).add("gauntlet item", public_ids.worker('alpha'), [], now=NOW)
        if name == "inbox":
            registry.register("bob", "worker")
            message = EventLog(root).send(
                public_ids.worker('alpha'),
                "bob",
                "floati",
                "a" * 40,
                "docs/evidence/HM3H-GAUNTLET.md",
                INBOX_NOTE,
                idempotency_key="snapshot-gauntlet",
            )
            answer = lambda: EventLog(root).present("bob")[0]
            prefix = "inbox-"
        elif name == "status":
            answer = lambda: FleetProjection(root).status_artifact(NOW)
            prefix = "status-"
        elif name == "replay":
            append_record(
                root,
                "receipts/denials.jsonl",
                {
                    "schema_version": 0,
                    "id": f"denial-{UUID}",
                    "tenant_id": root.tenant_id,
                    "timestamp": "2026-08-01T12:00:00.000Z",
                    "kind": "denial_receipt",
                    "attempt_id": f"attempt-{UUID}",
                    "claimed_sender": public_ids.worker('alpha'),
                    "claimed_recipient": "missing",
                    "reason_code": "unknown_recipient",
                },
                allowed_kinds={"denial_receipt"},
            )
            answer = lambda: render_replay_plain(
                ReplayTimeline.from_root(root).artifact()
            )
            prefix = "replay-render-"
        elif name == "board":
            answer = lambda: render_plain_dump(model_from_root(root, NOW))
            prefix = "board-"
        else:
            raise AssertionError(f"unknown reader {name}")
        expected = answer()
        if name == "inbox":
            # Inbox deliberately replaced SnapshotStore with this durable
            # recipient prefix. Exercise the artifact the next call trusts.
            self.assertEqual([message], expected)
            snapshot_path = root.tenant_home / ".inbox-prefix" / "checkpoint.json"
            self.assertTrue(snapshot_path.is_file(), "inbox must publish its prefix proof")
            self.assertEqual([], list(
                (root.tenant_home / ".floati-snapshots" / "v0").glob("inbox-*.json")
            ))
        else:
            snapshots = list(
                (root.tenant_home / ".floati-snapshots" / "v0").glob(
                    f"{prefix}*.json"
                )
            )
            self.assertEqual(1, len(snapshots), "Floati snapshot reader must publish one cache")
            snapshot_path = snapshots[0]
        self.assertFalse(os.path.lexists(root.tenant_home / f"{LEGACY_PREFIX}-snapshots"))
        case = ReaderCase(name, root, answer, snapshot_path)
        self.assertEqual(expected, case.answer())
        return case

    def mutate_torn(self, case: ReaderCase) -> None:
        case.snapshot_path.write_bytes(b'{"snapshot_version":0')

    def mutate_stale_boundary(self, case: ReaderCase) -> None:
        if case.name == "inbox":
            self.rewrite_inbox(case, lambda payload: payload.__setitem__("offset", payload["offset"] - 1))
            return

        def mutation(envelope: dict[str, object]) -> None:
            anchor = self.nonempty_anchor(envelope)
            anchor["byte_offset"] -= 1

        self.rewrite(case.snapshot_path, mutation)

    def mutate_past_eof(self, case: ReaderCase) -> None:
        if case.name == "inbox":
            source = case.root.resolve_relative("events.jsonl")
            self.rewrite_inbox(case, lambda payload: payload.__setitem__("offset", source.stat().st_size + 1))
            return

        def mutation(envelope: dict[str, object]) -> None:
            anchor = self.nonempty_anchor(envelope)
            source = case.root.resolve_relative(anchor["path"])
            anchor["byte_offset"] = source.stat().st_size + 1

        self.rewrite(case.snapshot_path, mutation)

    def mutate_ordinal(self, case: ReaderCase) -> None:
        if case.name == "inbox":
            self.rewrite_inbox(case, lambda payload: payload.__setitem__("count", payload["count"] + 1))
            return

        def mutation(envelope: dict[str, object]) -> None:
            anchor = self.nonempty_anchor(envelope)
            anchor["record_ordinal"] += 1

        self.rewrite(case.snapshot_path, mutation)

    def mutate_foreign_root(self, case: ReaderCase) -> None:
        if case.name == "inbox":
            self.rewrite_inbox(case, lambda payload: payload["binding"].__setitem__(
                "root", str(case.root.path) + "-other"))
            return

        self.rewrite(
            case.snapshot_path,
            lambda envelope: envelope.__setitem__(
                "root", str(case.root.path) + "-other"
            ),
        )

    def mutate_version(self, case: ReaderCase) -> None:
        if case.name == "inbox":
            self.rewrite_inbox(case, lambda payload: payload["binding"].__setitem__("format", "inbox-prefix-unknown"))
            return

        self.rewrite(
            case.snapshot_path,
            lambda envelope: envelope.__setitem__("snapshot_version", 1),
        )

    def mutate_payload_bytes(self, case: ReaderCase) -> None:
        if case.name == "inbox":
            # The recipient and message-ID entries wrap the same note differently.
            # Replace its JSON string in every copy, leaving each stored seal intact.
            note = json.dumps(INBOX_NOTE).encode("ascii")
            changed = 0
            for path in (case.snapshot_path.parent / "nodes").iterdir():
                data = path.read_bytes()
                if note in data:
                    path.write_bytes(data.replace(note, b'"hostile cached message"'))
                    changed += 1
            self.assertGreaterEqual(changed, 2, "expected multiple cached copies of the selected note")
            return
        self.rewrite(
            case.snapshot_path,
            lambda envelope: envelope["payload"].__setitem__(
                "hostile_corruption", True
            ),
            checksum=False,
        )

    def mutate_directory_bytes(self, case: ReaderCase) -> None:
        (case.snapshot_path.parent / "directory.bin").write_bytes(b"hostile directory")

    def mutate_bucket_bytes(self, case: ReaderCase) -> None:
        buckets = list((case.snapshot_path.parent / "buckets").iterdir())
        self.assertTrue(buckets, "inbox must cache selected membership")
        for path in buckets:
            path.write_bytes(b"hostile bucket")

    def mutate_seal_bytes(self, case: ReaderCase) -> None:
        path = case.snapshot_path.parent / "seal.key"
        original = path.read_bytes()
        self.assertTrue(original)
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    def test_every_hostile_snapshot_degrades_to_authoritative_answer(self) -> None:
        mutations = (
            self.mutate_torn,
            self.mutate_stale_boundary,
            self.mutate_past_eof,
            self.mutate_ordinal,
            self.mutate_foreign_root,
            self.mutate_version,
            self.mutate_payload_bytes,
        )
        for reader in ("status", "inbox", "replay", "board"):
            reader_mutations = mutations
            if reader == "inbox":
                reader_mutations += (
                    self.mutate_directory_bytes,
                    self.mutate_bucket_bytes,
                    self.mutate_seal_bytes,
                )
            for mutation in reader_mutations:
                with self.subTest(reader=reader, mutation=mutation.__name__):
                    with tempfile.TemporaryDirectory() as directory:
                        case = self.build_case(Path(directory), reader)
                        expected = case.answer()
                        source = case.root.resolve_relative("events.jsonl")
                        original = source.read_bytes() if reader == "inbox" else None
                        mutation(case)

                        actual = case.answer()

                        self.assertEqual(expected, actual)
                        if reader == "inbox":
                            self.assertEqual(original, source.read_bytes())
                            self.assertEqual(expected, case.answer())

    def test_symlinked_snapshot_directory_cannot_escape_tenant_or_fail_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open_direct_home(base / "gauntlet-symlink", create=True)
            Registry(root).register(public_ids.worker('alpha'), "worker")
            WorkLog(root).add("symlink item", public_ids.worker('alpha'), [], now=NOW)
            outside = base / "outside"
            outside.mkdir()
            (root.tenant_home / ".floati-snapshots").symlink_to(
                outside, target_is_directory=True
            )

            artifact = FleetProjection(root).status_artifact(NOW)

            self.assertEqual("fleet_status", artifact["kind"])
            self.assertEqual([], list(outside.iterdir()))
            self.assertFalse(os.path.lexists(root.tenant_home / f"{LEGACY_PREFIX}-snapshots"))

    def test_hm3i_run_projection_reopens_authoritative_frames_without_a_run_snapshot_cache(self) -> None:
        """Run truth is read from its physical ledger, never a newly invented snapshot surface."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "run-truth", create=True)
            trace = build_success_trace(root)
            expected = assert_physical_projection(trace)
            snapshot_root = root.tenant_home / ".floati-snapshots"
            legacy_snapshot_root = root.tenant_home / f"{LEGACY_PREFIX}-snapshots"

            self.assertFalse(snapshot_root.exists())
            self.assertFalse(os.path.lexists(legacy_snapshot_root))
            reopened = FloatiRoot.open_direct_home(root.path, create=False)
            records = RunLedger(reopened).records()

            self.assertEqual(expected, canonical_observation_from_records(trace, records))
            self.assertEqual(
                [record["id"] for record in trace.records],
                [record["id"] for record in records],
            )
            self.assertFalse(snapshot_root.exists())
            self.assertFalse(os.path.lexists(legacy_snapshot_root))


if __name__ == "__main__":
    unittest.main()
