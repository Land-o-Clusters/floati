"""FQ-9 / P5: the daemon's own receipts roll like LEDGER-1 (b).

The 09-10 incident measured two lifecycle receipt files at 23.8 MB each,
doubled since 09-07, while every daemon cycle re-read the same file in
full to answer one question: is consent active. The deciding question,
constructed before any fix: when ``receipts/wake-daemon/<node>.jsonl``
rolls (threshold, archive-whole, fresh file), does every reader still see
the truth it saw before - and does ``require_active`` read the consent
record without scanning a single lifecycle row?

Layout under test (the spec, fixed by the row):
  receipts/wake-daemon/<node>.jsonl            lifecycle receipts (rolled)
  receipts/wake-daemon/<node>.consent.jsonl    consent receipts (O(1) read)
  receipts/wake-daemon/<node>.roll.json        one roll marker/receipt
  receipts/wake-daemon/archive-<date>/         archive-whole destination
"""

from __future__ import annotations

from floati import fixture_ids as public_ids

import contextlib
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from tests import test_wd_2
from tests.temp_roots import REAL_TEMP_ROOT


def _policy_file(root: FloatiRoot, payload: dict) -> None:
    policy_dir = root.path / "state"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "ledger-policy.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


class _FqNineFixture(unittest.TestCase):
    """The roll fixtures, carried by every FQ-9 class that needs a root.

    Am.2 split this out of ``FqNineRollTests`` (a move, no behaviour
    change) so the amendment's own class can reuse the mixed-root seed,
    the hand-rolled layout and the path helpers without re-running the
    delivery's eleven tests under a second name. Same idiom as
    ``test_wd_2._ServeFixture``, which this file already subclasses.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        Registry(self.root).register(public_ids.builder("a"), "Cursor")
        self.node = public_ids.builder("a")
        self.legacy_relative = Path("receipts/wake-daemon") / f"{self.node}.jsonl"
        self.plane_relative = Path("receipts/wake-daemon") / f"{self.node}.consent.jsonl"
        self.marker_relative = Path("receipts/wake-daemon") / f"{self.node}.roll.json"

    def coordinate(self):
        from floati.wake_daemon_contract import DaemonCoordinate

        return DaemonCoordinate(self.root, self.node, "cursor")

    def consent_kwargs(self, *, epoch: int = 1, key: str = "fq9-consent") -> dict:
        return {
            "adapter_version": "1",
            "adapter_digest": "a" * 64,
            "min_poll_seconds": 1,
            "max_poll_seconds": 30,
            "max_backoff_seconds": 120,
            "activation_epoch": epoch,
            "idempotency_key": key,
        }

    def seed_mixed_root(self, *, lifecycle_rows: int = 3) -> dict:
        """A legacy root exactly as the 09-10 estate wrote it: one mixed
        file. The split code never produces one - consent goes straight
        to its plane - so the mixed file is rebuilt by hand from rows the
        shipped writers produced."""

        from floati.wake_daemon_contract import (
            DaemonConsentLedger,
            DaemonLifecycleLedger,
        )

        coordinate = self.coordinate()
        consent = DaemonConsentLedger(self.root).consent(
            coordinate, **self.consent_kwargs()
        )
        lifecycle = DaemonLifecycleLedger(self.root)
        for index in range(lifecycle_rows):
            lifecycle.record(
                coordinate,
                daemon_instance_id="fq9-daemon",
                activation_epoch=1,
                event="started" if index == 0 else "idle",
                state="running" if index == 0 else "idle",
                reason_code=None,
                adapter_digest="a" * 64,
                plist_digest=None,
                session_digest=None,
                predecessor_receipt_id=None,
                idempotency_key=f"fq9-lc-{index}",
            )
        plane_bytes = self._plane_path().read_bytes()
        legacy_bytes = self._legacy_path().read_bytes()
        self._legacy_path().write_bytes(plane_bytes + legacy_bytes)
        self._plane_path().unlink()
        return {"coordinate": coordinate, "consent": consent}

    def _legacy_path(self) -> Path:
        return self.root.resolve_relative(self.legacy_relative)

    def _plane_path(self) -> Path:
        return self.root.resolve_relative(self.plane_relative)

    def _marker_path(self) -> Path:
        return self.root.resolve_relative(self.marker_relative)

    def _archive_files(self) -> list[Path]:
        receipts = self.root.resolve_relative(Path("receipts/wake-daemon"))
        if not receipts.is_dir():
            return []
        return sorted(receipts.glob("archive-*/*.jsonl"))

    def _hand_roll(self) -> dict:
        """Build the post-roll layout by hand: the layout is the spec.

        Archives the whole legacy file under archive-2026-09-10/, carries
        the consent rows into the plane, leaves the fresh lifecycle file
        absent (its natural state until the next append).
        """

        raw = self._legacy_path().read_bytes()
        from floati.jsonl import read_records_snapshot
        from floati.wake_daemon_contract import DAEMON_KINDS

        rows = read_records_snapshot(
            self.root, self.legacy_relative, allowed_kinds=DAEMON_KINDS
        )
        consent_rows = [
            row for row in rows if row["kind"] == "wake_daemon_consent_receipt"
        ]
        archive_dir = self.root.resolve_relative(
            Path("receipts/wake-daemon/archive-2026-09-10")
        )
        archive_dir.mkdir(parents=True)
        archived_name = f"{self.node}.20260910T000000Z.jsonl"
        (archive_dir / archived_name).write_bytes(raw)
        self._legacy_path().unlink()
        plane_lines = "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in consent_rows
        )
        self._plane_path().write_text(plane_lines, encoding="utf-8")
        return {
            "archived_relative": Path("receipts/wake-daemon")
            / "archive-2026-09-10"
            / archived_name,
            "consent_rows": consent_rows,
            "archived_bytes": raw,
        }

    @contextlib.contextmanager
    def _read_instrument(self):
        """Record (relative path, row kinds) for every contract-plane read."""

        from floati import wake_daemon_contract

        original = wake_daemon_contract.read_records_snapshot
        reads: list[tuple[Path, tuple[str, ...]]] = []

        def instrument(authority, relative, *, allowed_kinds=None, **kwargs):
            rows = original(
                authority, relative, allowed_kinds=allowed_kinds, **kwargs
            )
            reads.append(
                (
                    Path(str(relative)),
                    tuple(str(row.get("kind")) for row in rows),
                )
            )
            return rows

        wake_daemon_contract.read_records_snapshot = instrument
        try:
            yield reads
        finally:
            wake_daemon_contract.read_records_snapshot = original


class FqNineRollTests(_FqNineFixture):
    def test_01_consent_readable_from_rolled_root_without_lifecycle_scan(self) -> None:
        """Deciding question: on a rolled root require_active answers from
        the consent record and never scans a lifecycle row."""

        seeded = self.seed_mixed_root()
        rolled = self._hand_roll()
        coordinate = seeded["coordinate"]

        from floati.wake_daemon_contract import DaemonConsentLedger

        with self._read_instrument() as reads:
            active = DaemonConsentLedger(self.root).require_active(coordinate)
        self.assertEqual(seeded["consent"]["id"], active["id"])
        lifecycle_relative = self.legacy_relative
        for relative, kinds in reads:
            self.assertNotIn("wake_daemon_lifecycle_receipt", kinds)
            self.assertNotEqual(lifecycle_relative, relative)

    def _migrate_setup(self, coordinate) -> None:
        """Drive the layout migration once, so the estate under test is
        the split one: plane holds consent, the lifecycle file is fresh."""

        from floati.wake_daemon_contract import DaemonLifecycleLedger

        DaemonLifecycleLedger(self.root).record(
            coordinate,
            daemon_instance_id="fq9-daemon",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="fq9-lc-migrate-setup",
        )

    def test_02_record_rolls_at_operator_byte_threshold(self) -> None:
        """One lifecycle record past the declared threshold rolls the file:
        archive-whole byte-identical, fresh file, committed marker. The
        mixed seed is migrated first, so this is the pure threshold roll
        on a split root. Control: the shipped default never rolls a small
        file."""

        seeded = self.seed_mixed_root()
        self._migrate_setup(seeded["coordinate"])
        self.assertTrue(self._plane_path().exists())
        _policy_file(
            self.root,
            {"schema_version": 0, "max_bytes": 1, "max_age_days": 30},
        )
        archives_before = self._archive_files()
        legacy_before = self._legacy_path().read_bytes()
        rows_before = legacy_before.count(b"\n")

        from floati.wake_daemon_contract import (
            DAEMON_KINDS,
            DaemonConsentLedger,
            DaemonLifecycleLedger,
        )
        from floati.jsonl import read_records_snapshot

        appended = DaemonLifecycleLedger(self.root).record(
            seeded["coordinate"],
            daemon_instance_id="fq9-daemon",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="fq9-lc-trigger",
        )

        archived = self._archive_files()
        new_archived = [path for path in archived if path not in archives_before]
        self.assertEqual(1, len(new_archived))
        self.assertEqual(legacy_before, new_archived[0].read_bytes())
        fresh = read_records_snapshot(
            self.root, self.legacy_relative, allowed_kinds=DAEMON_KINDS
        )
        self.assertEqual([appended["id"]], [row["id"] for row in fresh])
        marker = json.loads(self._marker_path().read_text(encoding="utf-8"))
        self.assertEqual("committed", marker["state"])
        self.assertEqual("threshold_bytes", marker["reason"])
        self.assertEqual(self.node, marker["node_id"])
        self.assertEqual(rows_before, marker["archived_records"])
        self.assertEqual(0, marker["consent_rows_carried"])
        self.assertEqual(
            hashlib.sha256(new_archived[0].read_bytes()).hexdigest(),
            marker["archived_sha256"],
        )
        self.assertEqual("operator_declared", marker["policy"]["source"])
        active = DaemonConsentLedger(self.root).require_active(seeded["coordinate"])
        self.assertEqual(seeded["consent"]["id"], active["id"])

    def test_02_control_default_policy_rolls_nothing(self) -> None:
        seeded = self.seed_mixed_root()
        self._migrate_setup(seeded["coordinate"])
        archives_before = self._archive_files()
        from floati.wake_daemon_contract import DaemonLifecycleLedger

        DaemonLifecycleLedger(self.root).record(
            seeded["coordinate"],
            daemon_instance_id="fq9-daemon",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="fq9-lc-control",
        )
        self.assertEqual(len(archives_before), len(self._archive_files()))

    def test_03_age_threshold_rolls_a_small_idle_file(self) -> None:
        """Age rolls too: an idle daemon's receipts still roll after
        max_age_days. Control: a fresh file never rolls on age."""

        seeded = self.seed_mixed_root()
        self._migrate_setup(seeded["coordinate"])
        _policy_file(
            self.root,
            {"schema_version": 0, "max_bytes": 1 << 30, "max_age_days": 1},
        )
        archives_before = self._archive_files()
        stale = datetime.now(timezone.utc) - timedelta(days=3)
        os.utime(self._legacy_path(), (stale.timestamp(), stale.timestamp()))

        from floati.wake_daemon_contract import DaemonLifecycleLedger

        DaemonLifecycleLedger(self.root).record(
            seeded["coordinate"],
            daemon_instance_id="fq9-daemon",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="fq9-lc-age",
        )
        self.assertEqual(len(archives_before) + 1, len(self._archive_files()))
        marker = json.loads(self._marker_path().read_text(encoding="utf-8"))
        self.assertEqual("committed", marker["state"])
        self.assertEqual("threshold_age", marker["reason"])

    def test_03_control_fresh_file_rolls_never_on_age(self) -> None:
        seeded = self.seed_mixed_root()
        self._migrate_setup(seeded["coordinate"])
        _policy_file(
            self.root,
            {"schema_version": 0, "max_bytes": 1 << 30, "max_age_days": 1},
        )
        archives_before = self._archive_files()
        from floati.wake_daemon_contract import DaemonLifecycleLedger

        DaemonLifecycleLedger(self.root).record(
            seeded["coordinate"],
            daemon_instance_id="fq9-daemon",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="fq9-lc-age-control",
        )
        self.assertEqual(len(archives_before), len(self._archive_files()))

    def test_04_layout_migration_fires_on_first_record_of_a_mixed_root(
        self) -> None:
        """Every root shipped before the split holds one mixed file. Its
        first lifecycle record migrates the layout regardless of threshold;
        the second record rolls nothing. Control: require_active answers
        from the mixed file before the migration."""

        seeded = self.seed_mixed_root()
        from floati.wake_daemon_contract import (
            DaemonConsentLedger,
            DaemonLifecycleLedger,
        )

        before = DaemonConsentLedger(self.root).require_active(seeded["coordinate"])
        self.assertEqual(seeded["consent"]["id"], before["id"])

        DaemonLifecycleLedger(self.root).record(
            seeded["coordinate"],
            daemon_instance_id="fq9-daemon",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="fq9-lc-migrate",
        )
        archived = self._archive_files()
        self.assertEqual(1, len(archived))
        marker = json.loads(self._marker_path().read_text(encoding="utf-8"))
        self.assertEqual("committed", marker["state"])
        self.assertEqual("layout_migration", marker["reason"])
        self.assertEqual(1, marker["consent_rows_carried"])
        self.assertTrue(self._plane_path().exists())
        active = DaemonConsentLedger(self.root).require_active(seeded["coordinate"])
        self.assertEqual(seeded["consent"]["id"], active["id"])

        DaemonLifecycleLedger(self.root).record(
            seeded["coordinate"],
            daemon_instance_id="fq9-daemon",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="fq9-lc-after-migration",
        )
        self.assertEqual(1, len(self._archive_files()))

    def test_05_epoch_chain_enforced_across_roll(self) -> None:
        """The consent plane alone carries the epoch chain after a roll:
        a stale epoch is refused, a fresh epoch lands in the plane, and
        the lifecycle file never gains a consent row."""

        seeded = self.seed_mixed_root()
        self._hand_roll()
        coordinate = seeded["coordinate"]

        from floati.wake_daemon_contract import DaemonConsentLedger

        ledger = DaemonConsentLedger(self.root)
        with self.assertRaisesRegex(ProtocolRefusal, "epoch"):
            ledger.consent(
                coordinate, **self.consent_kwargs(epoch=1, key="fq9-stale")
            )
        advanced = ledger.consent(
            coordinate, **self.consent_kwargs(epoch=2, key="fq9-advance")
        )
        self.assertEqual(
            seeded["consent"]["id"], advanced["predecessor_receipt_id"]
        )
        plane_text = self._plane_path().read_text(encoding="utf-8")
        self.assertIn(advanced["id"], plane_text)
        if self._legacy_path().exists():
            self.assertNotIn(advanced["id"], self._legacy_path().read_text())
        active = ledger.require_active(coordinate)
        self.assertEqual(2, active["activation_epoch"])

    def test_06_revoke_across_roll_carries_its_predecessor(self) -> None:
        """Revoked consent is carried too: post-roll revoke chains onto the
        carried active row and require_active refuses, exactly as before."""

        seeded = self.seed_mixed_root()
        self._hand_roll()
        coordinate = seeded["coordinate"]

        from floati.wake_daemon_contract import DaemonConsentLedger

        revoked = DaemonConsentLedger(self.root).revoke(
            coordinate, idempotency_key="fq9-revoke"
        )
        self.assertEqual(
            seeded["consent"]["id"], revoked["predecessor_receipt_id"]
        )
        self.assertIn(revoked["id"], self._plane_path().read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ProtocolRefusal, "consent_absent"):
            DaemonConsentLedger(self.root).require_active(coordinate)

    def test_07_unit_file_readers_on_a_rolled_root(self) -> None:
        """Fixture-proof for the launchd and systemd readers: on a rolled
        root _last_lifecycle sees the lifecycle plane only - None on a
        fresh file, the appended row after the next record, never a
        consent row."""

        seeded = self.seed_mixed_root()
        self._hand_roll()
        coordinate = seeded["coordinate"]
        launcher = self.base / "installed" / "scripts" / "floati"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        launcher.chmod(0o700)

        from floati.wake_daemon_contract import DaemonLifecycleLedger
        from floati.wake_daemon_launchd import LaunchAgentManager
        from floati.wake_daemon_systemd import SystemdUserUnitManager

        launchd = LaunchAgentManager(
            coordinate,
            installed_launcher=launcher,
            launch_agents_directory=self.base / "Library" / "LaunchAgents",
            uid=501,
            runner=lambda argv: None,
        )
        systemd = SystemdUserUnitManager(
            coordinate,
            installed_launcher=launcher,
            user_units_directory=self.base / "config" / "systemd" / "user",
            runner=lambda argv: None,
            systemctl_locator=lambda: "/usr/bin/systemctl",
        )
        self.assertIsNone(launchd._last_lifecycle())
        self.assertIsNone(systemd._last_lifecycle())

        appended = DaemonLifecycleLedger(self.root).record(
            coordinate,
            daemon_instance_id="fq9-daemon",
            activation_epoch=1,
            event="started",
            state="running",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="fq9-lc-post-roll",
        )
        for manager in (launchd, systemd):
            prior = manager._last_lifecycle()
            self.assertIsNotNone(prior)
            self.assertEqual(appended["id"], prior["id"])

    def test_08_prepared_marker_self_heals(self) -> None:
        """A crash between carrying consent and moving the file leaves a
        prepared marker; the next lifecycle record completes the roll and
        commits the marker. require_active answers throughout."""

        seeded = self.seed_mixed_root()
        from floati.jsonl import read_records_snapshot
        from floati.wake_daemon_contract import (
            DAEMON_KINDS,
            DaemonConsentLedger,
            DaemonLifecycleLedger,
        )

        rows = read_records_snapshot(
            self.root, self.legacy_relative, allowed_kinds=DAEMON_KINDS
        )
        consent_rows = [
            row for row in rows if row["kind"] == "wake_daemon_consent_receipt"
        ]
        self._plane_path().write_text(
            "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in consent_rows
            ),
            encoding="utf-8",
        )
        marker = {
            "schema_version": 0,
            "node_id": self.node,
            "state": "prepared",
            "reason": "layout_migration",
            "archive_dir": "receipts/wake-daemon/archive-2026-09-10",
            "archived_name": f"{self.node}.20260910T000000Z.jsonl",
        }
        self._marker_path().parent.mkdir(parents=True, exist_ok=True)
        self._marker_path().write_text(
            json.dumps(marker, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        # The crash window: plane carried, legacy file untouched, marker
        # prepared, and no archive yet - the move is the missing step.

        active = DaemonConsentLedger(self.root).require_active(seeded["coordinate"])
        self.assertEqual(seeded["consent"]["id"], active["id"])

        DaemonLifecycleLedger(self.root).record(
            seeded["coordinate"],
            daemon_instance_id="fq9-daemon",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="fq9-lc-heal",
        )
        healed = json.loads(self._marker_path().read_text(encoding="utf-8"))
        self.assertEqual("committed", healed["state"])
        self.assertEqual(1, len(self._archive_files()))
        # The append that drove the heal starts the fresh lifecycle file.
        fresh = read_records_snapshot(
            self.root, self.legacy_relative, allowed_kinds=DAEMON_KINDS
        )
        self.assertEqual(["fq9-lc-heal"], [row["idempotency_key"] for row in fresh])
        active = DaemonConsentLedger(self.root).require_active(seeded["coordinate"])
        self.assertEqual(seeded["consent"]["id"], active["id"])

    def test_09_unrolled_mixed_root_still_reads_mixed_file(self) -> None:
        """Compat control: an unrolled root answers exactly as today, from
        the mixed file, with no archive and no marker."""

        seeded = self.seed_mixed_root()
        from floati.wake_daemon_contract import DaemonConsentLedger

        active = DaemonConsentLedger(self.root).require_active(
            seeded["coordinate"]
        )
        self.assertEqual(seeded["consent"]["id"], active["id"])
        self.assertEqual([], self._archive_files())
        self.assertFalse(self._marker_path().exists())
        self.assertFalse(self._plane_path().exists())


class RolledRootTerminalReceiptTests(test_wd_2._ServeFixture):
    """FQ-9 Am.1, the architect's 02:09Z addition to the same amendment:
    on a rolled root, a serve() terminal receipt lands in the fresh
    lifecycle file and is readable by consent.require_active and by
    doctor's unit-file views.

    Constructed from the WD-4 composition red (tests/test_wd_2.py
    ConsentRevokedMidServeRetiresTests
    .test_a_consent_revoked_mid_serve_writes_one_receipt_and_returns):
    the WD-2 terminal read in serve() reads only the mixed ledger path,
    so on a root the FQ-9 split has rolled, the consent chain is
    invisible to it and the retirement receipt is lost silently - the
    P4 promise broken by P5.
    """

    def test_rolled_root_serve_terminal_receipt_lands_in_the_fresh_file(
        self,
    ) -> None:
        _policy_file(
            self.root, {"schema_version": 0, "max_bytes": 1, "max_age_days": 30}
        )

        def hook(_state: dict) -> None:
            if _state["cycles"] == 1:
                self.consent.revoke(
                    self.coordinate, idempotency_key="fq9-am1-revoke-mid-serve"
                )

        self.serve_bounded(self.daemon(), hook)

        retired = test_wd_2._retire_receipts(
            self.root, self.node, "wake_daemon_consent_revoked"
        )
        self.assertEqual(
            1,
            len(retired),
            f"expected exactly one retire receipt, saw {retired}",
        )
        marker = json.loads(
            (self.root.path / "receipts/wake-daemon" / f"{self.node}.roll.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual("committed", marker["state"])
        archived = self.root.path / marker["archived_to"]
        self.assertTrue(archived.is_file())
        self.assertNotIn(retired[0]["id"], archived.read_text(encoding="utf-8"))
        fresh = (
            self.root.path / "receipts/wake-daemon" / f"{self.node}.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn(retired[0]["id"], fresh)
        from floati.wake_daemon_contract import DaemonConsentLedger

        with self.assertRaisesRegex(ProtocolRefusal, "consent_absent"):
            DaemonConsentLedger(self.root).require_active(self.coordinate)
        launcher = self.base / "installed" / "scripts" / "floati"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        launcher.chmod(0o700)
        from floati.wake_daemon_launchd import LaunchAgentManager
        from floati.wake_daemon_systemd import SystemdUserUnitManager

        launchd = LaunchAgentManager(
            self.coordinate,
            installed_launcher=launcher,
            launch_agents_directory=self.base / "Library" / "LaunchAgents",
            uid=501,
            runner=lambda argv: None,
        )
        systemd = SystemdUserUnitManager(
            self.coordinate,
            installed_launcher=launcher,
            user_units_directory=self.base / "config" / "systemd" / "user",
            runner=lambda argv: None,
            systemctl_locator=lambda: "/usr/bin/systemctl",
        )
        for manager in (launchd, systemd):
            last = manager._last_lifecycle()
            self.assertIsNotNone(last)
            self.assertEqual(retired[0]["id"], last["id"])


class FqNineAmendmentTwoTests(_FqNineFixture):
    """FQ-9 Am.2: the two findings of the clean-room gate of Am.1.

    F1 (the crash window). ``_roll`` writes the ``prepared`` marker, then
    carries consent, then moves the file. The heal that completes an
    interrupted roll performed the move and never carried, so a crash in
    that window came back with every row safe in the archive, the marker
    ``committed``, and NO consent plane - ``require_active`` refusing
    ``wake_daemon_consent_absent`` permanently for a node whose consent
    was never revoked. The property these tests carry: after a heal the
    consent plane holds every consent row that was in the pre-roll file,
    exactly once, WHATEVER POINT THE CRASH HAPPENED AT - which is why
    there are three of them, one per reachable crash point, and why the
    half-carried one is the deciding case.

    F2 (the state F1 leaves behind). ``ensure_rolled`` reads "consent
    plane absent" as "layout not migrated", and the carry created no
    plane when there was nothing to carry, so a root with a lifecycle
    file and no consent row re-rolled on every single append.

    The fixtures are ``_FqNineFixture``'s (subclassed, not copied): the
    mixed-root seed, the hand-rolled layout and the archive/plane/marker
    path helpers.
    """

    def _daemon_kinds(self):
        from floati.wake_daemon_contract import DAEMON_KINDS

        return DAEMON_KINDS

    def _plane_rows(self) -> list:
        from floati.jsonl import read_records_snapshot

        return read_records_snapshot(
            self.root, self.plane_relative, allowed_kinds=self._daemon_kinds()
        )

    def _legacy_rows(self) -> list:
        from floati.jsonl import read_records_snapshot

        return read_records_snapshot(
            self.root, self.legacy_relative, allowed_kinds=self._daemon_kinds()
        )

    def _archived_name(self) -> str:
        return f"{self.node}.20260910T000000Z.jsonl"

    def _plant_prepared_marker(self, *, reason: str = "layout_migration") -> dict:
        """The on-disk state ``_roll`` leaves at its marker write: the
        marker is ``prepared`` and names an archive that does not exist
        yet. Nothing else on disk is touched."""

        marker = {
            "schema_version": 0,
            "node_id": self.node,
            "state": "prepared",
            "reason": reason,
            "archive_dir": "receipts/wake-daemon/archive-2026-09-10",
            "archived_name": self._archived_name(),
        }
        self._marker_path().parent.mkdir(parents=True, exist_ok=True)
        self._marker_path().write_text(
            json.dumps(marker, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        return marker

    def _write_plane(self, rows: list) -> None:
        self._plane_path().parent.mkdir(parents=True, exist_ok=True)
        self._plane_path().write_text(
            "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )

    def seed_mixed_root_two_consents(self, *, lifecycle_rows: int = 2) -> dict:
        """The mixed seed with TWO consent rows, so a half-carried plane
        is constructible: the idempotence case needs one row already
        carried and one still missing."""

        from floati.wake_daemon_contract import (
            DaemonConsentLedger,
            DaemonLifecycleLedger,
        )

        coordinate = self.coordinate()
        ledger = DaemonConsentLedger(self.root)
        first = ledger.consent(
            coordinate, **self.consent_kwargs(epoch=1, key="am2-consent-1")
        )
        second = ledger.consent(
            coordinate, **self.consent_kwargs(epoch=2, key="am2-consent-2")
        )
        lifecycle = DaemonLifecycleLedger(self.root)
        for index in range(lifecycle_rows):
            lifecycle.record(
                coordinate,
                daemon_instance_id="fq9-daemon",
                activation_epoch=2,
                event="started" if index == 0 else "idle",
                state="running" if index == 0 else "idle",
                reason_code=None,
                adapter_digest="a" * 64,
                plist_digest=None,
                session_digest=None,
                predecessor_receipt_id=None,
                idempotency_key=f"am2-lc-{index}",
            )
        plane_bytes = self._plane_path().read_bytes()
        legacy_bytes = self._legacy_path().read_bytes()
        self._legacy_path().write_bytes(plane_bytes + legacy_bytes)
        self._plane_path().unlink()
        return {
            "coordinate": coordinate,
            "consents": [first, second],
        }

    def test_am2_f1_heal_carries_consent_when_the_crash_preceded_the_carry(
        self) -> None:
        """F1, crash point C1 - the gate's failing case. The marker is
        written, the process dies, the carry never ran. The heal owes the
        plane, not only the move: a node whose consent was never revoked
        may not come back refusing consent_absent."""

        seeded = self.seed_mixed_root()
        self._plant_prepared_marker()
        self.assertFalse(self._plane_path().exists())
        self.assertTrue(self._legacy_path().exists())
        self.assertEqual([], self._archive_files())

        from floati.wake_daemon_contract import DaemonConsentLedger
        from floati.wake_daemon_roll import ensure_rolled

        healed = ensure_rolled(self.root, self.node)
        self.assertEqual("committed", healed["state"])
        self.assertEqual(1, len(self._archive_files()))
        self.assertTrue(
            self._plane_path().exists(),
            "the heal completed the move and left no consent plane",
        )
        self.assertEqual(
            [seeded["consent"]["id"]],
            [row["id"] for row in self._plane_rows()],
            "the heal must carry every pre-roll consent row into the plane",
        )
        active = DaemonConsentLedger(self.root).require_active(
            seeded["coordinate"]
        )
        self.assertEqual(seeded["consent"]["id"], active["id"])

    def test_am2_f1_heal_carry_is_idempotent_over_a_half_carried_plane(
        self) -> None:
        """F1's deciding property. A crash can also land INSIDE the carry,
        leaving some rows in the plane. The heal must complete the carry
        and must not duplicate what is already there - this is the test
        that decides between carrying in the heal and reordering the
        roll, because only an idempotent carry survives every point."""

        seeded = self.seed_mixed_root_two_consents()
        first, second = seeded["consents"]
        self._write_plane([first])
        self._plant_prepared_marker()
        self.assertEqual([], self._archive_files())

        from floati.wake_daemon_contract import DaemonConsentLedger
        from floati.wake_daemon_roll import ensure_rolled

        ensure_rolled(self.root, self.node)
        self.assertEqual(
            [first["id"], second["id"]],
            [row["id"] for row in self._plane_rows()],
            "the heal must finish a half carry exactly once per row",
        )
        active = DaemonConsentLedger(self.root).require_active(
            seeded["coordinate"]
        )
        self.assertEqual(second["id"], active["id"])

    def test_am2_f1_heal_carries_from_the_archive_after_the_move(self) -> None:
        """F1, the crash point the heal itself creates once it carries:
        the move landed, the carry did not. The rows are in the archive
        and nowhere else, and the plane is still owed - so the heal reads
        its source of truth from the destination, which is byte-identical
        to the pre-roll file at every crash point."""

        seeded = self.seed_mixed_root()
        marker = self._plant_prepared_marker()
        archive_dir = self.root.resolve_relative(Path(marker["archive_dir"]))
        archive_dir.mkdir(parents=True)
        (archive_dir / marker["archived_name"]).write_bytes(
            self._legacy_path().read_bytes()
        )
        self._legacy_path().unlink()
        self.assertFalse(self._plane_path().exists())

        from floati.wake_daemon_contract import DaemonConsentLedger
        from floati.wake_daemon_roll import ensure_rolled

        ensure_rolled(self.root, self.node)
        self.assertEqual(
            [seeded["consent"]["id"]],
            [row["id"] for row in self._plane_rows()],
            "a heal that only writes the marker leaves the plane owed",
        )
        active = DaemonConsentLedger(self.root).require_active(
            seeded["coordinate"]
        )
        self.assertEqual(seeded["consent"]["id"], active["id"])

    def test_am2_f2_plane_less_split_root_rolls_once_not_on_every_append(
        self) -> None:
        """F2. A root with a lifecycle file and no consent row re-rolled
        on EVERY append, because the carry created no plane when there
        was nothing to carry and 'plane absent' is read as 'not yet
        migrated'. Five records under a roomy threshold: the layout
        migration fires once and then the condition clears."""

        from floati.wake_daemon_contract import DaemonLifecycleLedger

        _policy_file(
            self.root,
            {
                "schema_version": 0,
                "max_bytes": 8 * 1024 * 1024,
                "max_age_days": 30,
            },
        )
        coordinate = self.coordinate()
        lifecycle = DaemonLifecycleLedger(self.root)
        observed = []
        for index in range(5):
            lifecycle.record(
                coordinate,
                daemon_instance_id="fq9-daemon",
                activation_epoch=1,
                event="started" if index == 0 else "idle",
                state="running" if index == 0 else "idle",
                reason_code=None,
                adapter_digest="a" * 64,
                plist_digest=None,
                session_digest=None,
                predecessor_receipt_id=None,
                idempotency_key=f"am2-f2-lc-{index}",
            )
            observed.append(len(self._archive_files()))
        self.assertEqual(
            [0, 1, 1, 1, 1],
            observed,
            "a plane-less split root rolled again on every append: "
            f"archives after each record were {observed}",
        )
        self.assertTrue(
            self._plane_path().exists(),
            "the migration must record that this root is split",
        )
        self.assertEqual([], self._plane_rows())
        archived_rows = 0
        for archive in self._archive_files():
            archived_rows += archive.read_bytes().count(b"\n")
        self.assertEqual(
            5,
            archived_rows + len(self._legacy_rows()),
            "no lifecycle row may be lost across the one migration",
        )

    def test_am2_f2_control_mixed_legacy_root_still_migrates_once(self) -> None:
        """Q5's guarantee, re-measured against the F2 fix: a genuinely
        mixed legacy root still splits on its first append, carries its
        consent row, loses nothing, and then stops rolling."""

        seeded = self.seed_mixed_root(lifecycle_rows=3)
        before = self._legacy_rows()
        self.assertEqual(4, len(before))

        from floati.wake_daemon_contract import (
            DaemonConsentLedger,
            DaemonLifecycleLedger,
        )

        lifecycle = DaemonLifecycleLedger(self.root)
        for index in range(3):
            lifecycle.record(
                seeded["coordinate"],
                daemon_instance_id="fq9-daemon",
                activation_epoch=1,
                event="idle",
                state="idle",
                reason_code=None,
                adapter_digest="a" * 64,
                plist_digest=None,
                session_digest=None,
                predecessor_receipt_id=None,
                idempotency_key=f"am2-f2-control-{index}",
            )
        self.assertEqual(1, len(self._archive_files()))
        marker = json.loads(self._marker_path().read_text(encoding="utf-8"))
        self.assertEqual("layout_migration", marker["reason"])
        self.assertEqual(1, marker["consent_rows_carried"])
        archived = self._archive_files()[0].read_bytes().count(b"\n")
        self.assertEqual(len(before), archived)
        self.assertEqual(
            [seeded["consent"]["id"]],
            [row["id"] for row in self._plane_rows()],
        )
        active = DaemonConsentLedger(self.root).require_active(
            seeded["coordinate"]
        )
        self.assertEqual(seeded["consent"]["id"], active["id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
