"""Confluence phases 2-3 — the consent surface and the export verb.

Charter: `docs/design/confluence-phase2plus-charter-2026-08-29.md`
(floati half only; phase 4 is DEFERRED and nothing here builds it).

The six charter acceptance pins, RED-first:
1. an ungranted read refuses NAMING THE GRANT ACT;
2. a grant for root A grants nothing for root B;
3. revocation severs (exercise-after-revoke);
4. bundle determinism (two runs over unchanged state, byte-identical);
5. the bundle is stamped with the grant id it was produced under;
6. zero-network at the socket layer.

Phase 2 consent key granularity (M1): one grant = one root = one
consumer identity. Grant records are bus-ledger records, additive kind,
v1 schema — no encoder change, no epoch roll.
"""

from __future__ import annotations

import io
from contextlib import contextmanager
import fcntl
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati import fixture_ids as public_ids
from floati.confluence import ConfluenceGrantLedger, confluence_adopt, confluence_release
from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.banked_sha_fixture import banked_send_repository
from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).parents[1]


@contextmanager
def observe_bundle_ledger_byte_reads(root):
    """Probe real ledger/epoch contention at each actual payload read."""
    observed = []
    ordinary_open = Path.open

    class ObservedLedger:
        def __init__(self, path, handle):
            self.path, self.handle = path, handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *exception):
            return self.handle.__exit__(*exception)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, *arguments):
            lock = self.path.with_name(self.path.name + ".lock")
            with ordinary_open(lock, "rb") as contender:
                try:
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    ledger_released = False
                else:
                    ledger_released = True
                    fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
            epoch = os.open(root.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                try:
                    fcntl.flock(epoch, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    epoch_protected = True
                else:
                    epoch_protected = False
                    fcntl.flock(epoch, fcntl.LOCK_UN)
            finally:
                os.close(epoch)
            observed.append((self.path.relative_to(root.path).as_posix(), ledger_released, epoch_protected))
            return self.handle.read(*arguments)

    def open_observed(path, mode="r", *arguments, **keywords):
        handle = ordinary_open(path, mode, *arguments, **keywords)
        if mode == "rb" and path.suffix == ".jsonl" and root.path in path.parents:
            return ObservedLedger(path, handle)
        return handle

    with mock.patch.object(Path, "open", open_observed):
        yield observed


class ConfluenceSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root_a = FloatiRoot.open_direct_home(
            self.base / "fleet-a", create=True)
        self.root_b = FloatiRoot.open_direct_home(
            self.base / "fleet-b", create=True)
        for root in (self.root_a, self.root_b):
            Registry(root).register(public_ids.builder("a"), "codex")
            Registry(root).register(public_ids.builder("b"), "codex")
        self.seed_mail(self.root_a)
        self.seed_mail(self.root_b)

    def seed_mail(self, root: FloatiRoot) -> None:
        deliveries = root.resolve_relative(
            Path("receipts/deliveries")
            / public_ids.ledger(public_ids.builder("b")))
        deliveries.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "schema_version": 0,
            "id": "delivery-01a05300000070008000000000000000",
            "tenant_id": root.tenant_id,
            "timestamp": "2026-08-29T12:00:00.000Z",
            "kind": "delivery_receipt",
            "recipient": public_ids.builder("b"),
            "item_ids": ["msg-01a05300000070008000000000000000"],
            "presentation_count": 1,
        }
        with deliveries.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt) + "\n")

    def ledger(self, root: FloatiRoot):
        from floati.confluence import ConfluenceGrantLedger, confluence_adopt, confluence_release

        return ConfluenceGrantLedger(root)

    def test_ungranted_read_refuses_naming_the_grant_act(self) -> None:
        """Acceptance pin 1 (VS-5/VS-8): a typed refusal that tells the
        consumer the exact act that would authorize the read."""
        from floati.confluence import materialize_bundle

        with self.assertRaises(ProtocolRefusal) as caught:
            materialize_bundle(
            self.root_a, consumer="puddle", out=self.base / "out.json")
        self.assertEqual("confluence_grant_required", caught.exception.code)
        self.assertIn("confluence grant", caught.exception.detail)

    def test_grant_for_root_a_grants_nothing_for_root_b(self) -> None:
        """Acceptance pin 2: the grant names the EXACT root — never a home
        scan, never a glob; one grant is one root and one consumer."""
        from floati.confluence import materialize_bundle

        self.ledger(self.root_a).grant("puddle", "key-a-1")
        materialize_bundle(
            self.root_a, consumer="puddle", out=self.base / "out.json")
        with self.assertRaises(ProtocolRefusal) as caught:
            materialize_bundle(
            self.root_b, consumer="puddle", out=self.base / "out-b.json")
        self.assertEqual("confluence_grant_required", caught.exception.code)

    def test_revocation_severs_exercise_after_revoke(self) -> None:
        """Acceptance pin 3: a grant that stopped authorizing is the
        feature; the same read that succeeded before must refuse after."""
        from floati.confluence import materialize_bundle

        ledger = self.ledger(self.root_a)
        ledger.grant("puddle", "key-a-1")
        materialize_bundle(
            self.root_a, consumer="puddle", out=self.base / "out.json")
        ledger.revoke("puddle", "key-a-2")
        with self.assertRaises(ProtocolRefusal) as caught:
            materialize_bundle(
            self.root_a, consumer="puddle", out=self.base / "out.json")
        self.assertEqual("confluence_grant_required", caught.exception.code)
        # and a re-grant re-arms the seam: the consent surface is reusable
        ledger.grant("puddle", "key-a-3")
        materialize_bundle(
            self.root_a, consumer="puddle", out=self.base / "out.json")

    def test_bundle_is_byte_deterministic_across_runs(self) -> None:
        """Acceptance pin 4: two materializations over unchanged state are
        byte-identical — snapshot_at is state-derived, never wall-clock."""
        from floati.confluence import materialize_bundle

        self.ledger(self.root_a).grant("puddle", "key-a-1")
        first = materialize_bundle(
            self.root_a, consumer="puddle", out=self.base / "a-1.json")
        second = materialize_bundle(
            self.root_a, consumer="puddle", out=self.base / "a-2.json")
        self.assertEqual(
            first.read_bytes(), second.read_bytes(),
            "the same root state produced two different bundles")

    def test_complete_bundle_reads_release_ledger_lock_but_preserve_epoch_and_bytes(self) -> None:
        from floati.confluence import materialize_bundle

        self.ledger(self.root_a).grant("puddle", "bundle-sealed-read")
        baseline = materialize_bundle(self.root_a, consumer="puddle", out=self.base / "baseline.json").read_bytes()
        with observe_bundle_ledger_byte_reads(self.root_a) as reads:
            output = materialize_bundle(self.root_a, consumer="puddle", out=self.base / "observed.json")
        self.assertEqual(baseline, output.read_bytes(), "the complete bundle testimony must remain byte-identical")
        self.assertEqual({"confluence/grants.jsonl", "receipts/deliveries/builder-b.jsonl"},
                         {path for path, _released, _protected in reads})
        self.assertTrue(all(released for _path, released, _protected in reads),
                        "complete ledger byte reads must release their ledger lock before reading payload: " + repr(reads))
        self.assertTrue(all(protected for _path, _released, protected in reads),
                        "native epoch replacement must remain excluded while snapshot bytes detach: " + repr(reads))

    def test_bundle_is_stamped_with_the_grant_id(self) -> None:
        """Acceptance pin 5: the bundle names the grant it was produced
        under, and the document validates against the v1 bundle schema
        (the documented additive evolution: new version, new fixture)."""
        from tests.schema_validation import validate_json_schema

        from floati.confluence import materialize_bundle

        grant = self.ledger(self.root_a).grant("puddle", "key-a-1")
        bundle_path = materialize_bundle(
            self.root_a, consumer="puddle", out=self.base / "stamp.json")
        document = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertEqual(grant["id"], document["grant_id"])
        self.assertEqual(1, document["schema_version"])
        validate_json_schema(
            document,
            REPOSITORY_ROOT
            / "schemas/v0/receipts-read-bundle-v1.schema.json")

    def test_bundle_materializes_without_any_socket(self) -> None:
        """Acceptance pin 6: zero-network at the socket layer — the seam
        opens no socket while materializing, reading, or writing."""
        from floati.confluence import materialize_bundle

        self.ledger(self.root_a).grant("puddle", "key-a-1")

        def _forbid_socket(*args, **kwargs):
            raise AssertionError("the confluence seam opened a socket")

        with mock.patch.object(socket, "socket", _forbid_socket),\
                mock.patch.object(socket, "create_connection", _forbid_socket):
            bundle_path = materialize_bundle(
                self.root_a, consumer="puddle", out=self.base / "net.json")
        document = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertEqual("receipts_read_bundle", document["kind"])

    def test_bundle_carries_the_v0_read_surface_with_ordinates(self) -> None:
        """The bundle only ever contains the six allowlisted ledger
        families, every entry names its source path and one-based source
        ordinal, and the sequence is total and gap-free."""
        from floati.confluence import materialize_bundle

        self.ledger(self.root_a).grant("puddle", "key-a-1")
        document = json.loads(
            materialize_bundle(
                self.root_a, consumer="puddle",
                out=self.base / "surface.json").read_text(
                encoding="utf-8"))
        entries = document["entries"]
        self.assertEqual(1, len(entries), "the seeded mail is the evidence")
        entry = entries[0]
        self.assertEqual(
            "receipts/deliveries/builder-b.jsonl", entry["source"])
        self.assertEqual(1, entry["source_ordinal"])
        self.assertEqual(1, entry["sequence"])
        self.assertEqual("delivery_receipt", entry["record"]["kind"])
        self.assertEqual(
            [entry["sequence"] for entry in entries],
            list(range(1, len(entries) + 1)))

    def test_v1_fixture_is_committed_and_schema_valid(self) -> None:
        """The v1 bundle has its own frozen fixture, per the v0 law:
        additive changes require a new schema version and new fixtures."""
        from tests.schema_validation import validate_json_schema

        schema_path = (
            REPOSITORY_ROOT / "schemas/v0/receipts-read-bundle-v1.schema.json")
        fixture_path = (
            REPOSITORY_ROOT / "tests/fixtures/confluence/v0/receipts-read-v1.json")
        self.assertTrue(schema_path.is_file(), "v1 bundle schema missing")
        self.assertTrue(fixture_path.is_file(), "v1 bundle fixture missing")
        validate_json_schema(
            json.loads(fixture_path.read_text(encoding="utf-8")), schema_path)


class ConfluenceCliRoundTrip(unittest.TestCase):
    """End-to-end through the real CLI: the seam verbs exist, refuse
    typed, and produce a bundle — the same flow the consumer runs."""

    def test_grant_bundle_revoke_round_trip(self) -> None:
        import subprocess

        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        root = base / "fleet"

        def cli(*arguments: str, expect: int = 0, cwd: Path | None = None) -> dict:
            completed = subprocess.run(
                [sys.executable, "-m", "floati", *arguments],
                cwd=str(REPOSITORY_ROOT if cwd is None else cwd),
                capture_output=True,
                text=True,
                timeout=120,
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(REPOSITORY_ROOT),
                },
            )
            self.assertEqual(
                expect, completed.returncode,
                f"rc={completed.returncode} stderr={completed.stderr[-300:]}")
            self.assertEqual("", completed.stderr)
            return json.loads(completed.stdout)

        cli("init", "--root", str(root))
        cli("register", "--root", str(root), public_ids.builder("a"),
            "--harness", "codex")
        cli("register", "--root", str(root), public_ids.builder("b"),
            "--harness", "codex")
        # SEND-SHA-RESOLVES-1: the send runs from a repository that holds
        # the sha it names, so the round trip exercises the seam, not the
        # absent-sha fence.
        repository, sha = banked_send_repository(base)
        cli("send", "--root", str(root), "--from", public_ids.builder("a"),
            "--to", public_ids.builder("b"),
            "--repo", "smoke", "--sha", sha,
            "--doc", "docs/CONFLUENCE-v0.md", "--note", "confluence round trip",
            cwd=repository)
        cli("inbox", "--root", str(root),
            "--as", public_ids.builder("b"), "--peek")  # pull writes delivery receipts
        cli("confluence", "grant", "--root", str(root),
            "--consumer", "puddle", "--idempotency-key", "cli-1")
        status = cli("confluence", "status", "--root", str(root))
        self.assertEqual(1, len(status["evidence"]["grants"]))
        bundle = cli(
            "confluence", "bundle", "--root", str(root),
            "--consumer", "puddle", "--out", str(base / "bundle.json"))
        self.assertEqual(1, bundle["evidence"]["entries"])
        document = json.loads((base / "bundle.json").read_text())
        self.assertEqual(1, document["schema_version"])
        self.assertTrue(document["grant_id"])
        cli("confluence", "revoke", "--root", str(root),
            "--consumer", "puddle", "--idempotency-key", "cli-2")
        # exercise-after-revoke: the read refuses, typed, exit 20
        refused = cli(
            "confluence", "bundle", "--root", str(root),
            "--consumer", "puddle", "--out", str(base / "late.json"),
            expect=20)
        self.assertEqual(
            "confluence_grant_required", refused["evidence"]["code"])
        self.assertIn("confluence grant", refused["evidence"]["detail"])
        self.assertFalse((base / "late.json").exists(),
                         "a refused read must not write a bundle")



class ConfluencePhase4Tests(unittest.TestCase):
    """Phase 4, authorized by dispatch: the managed-mode adoption seam
    rides the confluence trust gate (consumer grant) AND the L1 authority
    class (the manager's exact active authority lease) — a new operator
    endpoint, not a read bolted onto reads. Plus the dispatch order to
    gate the status --json surface."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(
            self.base / "fleet", create=True)
        Registry(self.root).register(public_ids.builder("a"), "codex")
        Registry(self.root).register("manager-a", "codex")
        self.ledger = ConfluenceGrantLedger(self.root)

    def test_status_json_with_ungranted_consumer_refuses(self) -> None:
        from floati.cli import _status

        args = mock.Mock(
            root=str(self.root.path), json=True, consumer="puddle",
            destination=None)
        with self.assertRaises(ProtocolRefusal) as caught:
            _status(args)
        self.assertEqual("confluence_grant_required", caught.exception.code)

    def test_status_json_with_granted_consumer_passes(self) -> None:
        from floati.cli import _status

        self.ledger.grant("puddle", "k1")
        args = mock.Mock(
            root=str(self.root.path), json=True, consumer="puddle",
            destination=None)
        status, _artifact, _code = _status(args)
        self.assertEqual("ok", status)

    def test_adopt_requires_the_consumer_grant(self) -> None:
        from floati.confluence import confluence_adopt

        with self.assertRaises(ProtocolRefusal) as caught:
            confluence_adopt(
                self.root, consumer="puddle", session="sess-1",
                manager="manager-a", authority_subject="subj-1",
                authority_epoch=1,
                authority_expires_at="2026-08-30T23:59:00.000Z")
        self.assertEqual("confluence_grant_required", caught.exception.code)

    def test_adopt_requires_the_exact_active_authority_lease(self) -> None:
        """The L1 trust class: the consumer grant alone is NOT enough —
        the manager must bind its own exact active authority lease."""
        from floati.confluence import confluence_adopt

        self.ledger.grant("puddle", "k1")
        with self.assertRaises(ProtocolRefusal) as caught:
            confluence_adopt(
                self.root, consumer="puddle", session="sess-1",
                manager="manager-a", authority_subject="subj-1",
                authority_epoch=7,
                authority_expires_at="2026-08-30T23:59:00.000Z")
        self.assertEqual("managed_lease_mismatch", caught.exception.code)

    def test_adopt_and_release_round_trip_is_schema_valid(self) -> None:
        from datetime import datetime, timezone

        from floati.planes import AuthorityGrantStore
        from tests.schema_validation import validate_json_schema

        self.ledger.grant("puddle", "k1")
        lease = AuthorityGrantStore(self.root).claim(
            "subj-1", "manager-a", 3600, 120,
            datetime.now(timezone.utc))
        adoption = confluence_adopt(
            self.root, consumer="puddle", session="sess-1",
            manager="manager-a", authority_subject="subj-1",
            authority_epoch=lease["epoch"],
            authority_expires_at=lease["expires_at"])
        self.assertEqual("MANAGED", adoption["mode"])
        validate_json_schema(
            adoption,
            REPOSITORY_ROOT / "schemas/v0/session-adoption-record.schema.json")
        released = confluence_release(
            self.root, consumer="puddle", session="sess-1",
            manager="manager-a", authority_epoch=lease["epoch"])
        validate_json_schema(
            released,
            REPOSITORY_ROOT / "schemas/v0/session-release-record.schema.json")
        with self.assertRaises(ProtocolRefusal):
            confluence_release(
                self.root, consumer="puddle", session="sess-1",
                manager="manager-a", authority_epoch=lease["epoch"])


if __name__ == "__main__":
    unittest.main()
