"""HOLD-1: a hold is a versioned record doctor and the seat board list with age.

The done-when (ROWS.md HOLD-1, DRAFT-OPUS 2026-10-11): `floati hold start` /
`floati hold release` write a versioned record (node, reason, started_at,
expected_release or none, schema_version) at the authoring boundary; doctor
and the seat board list open holds with their age; a hold past its expected
release is reported OVERDUE by node; release writes a terminal record; and
readers accept every older record shape - a new rule binds what an operator
may WRITE, never what a reader accepts, because a read-time validator turns
one historical record into an outage.

The reader tests below hand-write the record bytes the verb will write, so
they fail on the property at base (doctor and the board say nothing about a
hold that exists) and never on an import error.
"""

from __future__ import annotations

import contextlib
import io
import json
import shlex
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.ids import uuid7_hex
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _cli(argv: list[str]) -> tuple[int, dict]:
    from floati.cli import main

    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            code = main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    try:
        artifact = json.loads(output.getvalue())
    except json.JSONDecodeError:
        artifact = {}
    return code, artifact


class HoldFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.node = public_ids.builder("a")
        Registry(self.root).register(self.node, "zcode")
        self.now = datetime.now(timezone.utc)

    def write_hold(
        self,
        *,
        node: str | None = None,
        reason: str = "hand-held for review",
        started_at: datetime | None = None,
        expected_release: datetime | None = None,
        with_optional_fields: bool = True,
        key: str = "hold-read-1",
        hold_id: str | None = None,
    ) -> str:
        """Append one hold record in the exact shape the verb will write."""
        node = self.node if node is None else node
        started_at = self.now - timedelta(hours=3) if started_at is None else started_at
        hold_id = "hold-" + uuid7_hex() if hold_id is None else hold_id
        row: dict[str, object] = {
            "schema_version": 1,
            "kind": "hold_receipt",
            "id": hold_id,
            "tenant_id": self.root.tenant_id,
            "timestamp": _iso(started_at),
            "node_id": node,
            "reason": reason,
            "started_at": _iso(started_at),
            "idempotency_key": key,
        }
        if with_optional_fields:
            row["expected_release"] = (
                None if expected_release is None else _iso(expected_release)
            )
            row["phase"] = "held"
            row["released_at"] = None
            row["predecessor_receipt_id"] = None
        ledger = self.home / "receipts" / "holds" / f"{node}.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        return hold_id

    def append_line(self, node: str, line: str) -> None:
        ledger = self.home / "receipts" / "holds" / f"{node}.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def doctor_findings(self) -> tuple[list[dict], int]:
        from floati.doctor import Doctor

        artifact, rc = Doctor(
            REPOSITORY_ROOT,
            self.home,
            ref="origin/main",
            no_sandbox=True,
        ).artifact()
        return artifact.get("findings", []), rc

    def board_detail_lines(self, width: int = 100) -> list[str]:
        from floati.tui import model_from_root
        from floati.tui_render import _detail_lines

        model = model_from_root(self.root)
        return _detail_lines(model, width, 0)


class FleetPresenceTests(HoldFixture):
    def set_mode(self, mode, key):
        status, artifact = _cli([
            "presence", "set", "--root", str(self.home), "--as", self.node,
            "--mode", mode, "--idempotency-key", key,
        ])
        self.assertEqual(0, status, artifact)
        return artifact["evidence"]

    def test_status_identifies_an_undeclared_fleet(self):
        status, artifact = _cli(["status", "--root", str(self.home), "--json"])
        self.assertEqual(0, status, artifact)
        self.assertEqual("UNDECLARED", artifact["evidence"].get("fleet_presence", {}).get("mode"))

    def inbox_envelope(self, key):
        from floati.events import EventLog

        sent = EventLog(self.root).send(
            self.node, self.node, "floati", "a" * 40, "note.md", "Review the change",
            idempotency_key=key,
        )
        code, inbox = _cli(["inbox", "--root", str(self.home), "--as", self.node, "--peek"])
        self.assertEqual(0, code, inbox)
        return next(row for row in inbox["evidence"]["messages"]
                    if row["id"] == sent["message"]["id"])

    def test_overnight_envelope_projects_the_authored_mode(self):
        # Write the author's declared mode before invoking the older reader.
        since = "2000-01-02T03:00:00.000Z"
        row = {"kind": "fleet_presence_receipt", "schema_version": 1,
               "id": "fleet-presence-" + uuid7_hex(), "tenant_id": self.root.tenant_id,
               "timestamp": since, "since": since, "actor": self.node, "mode": "AWAY",
               "idempotency_key": "overnight", "predecessor_receipt_id": None, "away_since": None}
        path = self.home / "receipts" / "fleet-presence.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertIn("AWAY", self.inbox_envelope("night").get("fleet_presence", ""))

    def test_operator_transition_reaches_the_next_envelope(self):
        from tests.schema_validation import validate_json_schema

        away = self.set_mode("AWAY", "away")
        self.assertEqual(1, away["schema_version"])
        self.assertEqual(self.node, away["actor"])
        self.assertTrue(away["since"])
        self.assertEqual(away, self.set_mode("AWAY", "away"))
        self.assertIn("AWAY", self.inbox_envelope("overnight")["fleet_presence"])
        self.assertTrue(any("AWAY" in line for line in self.board_detail_lines()))

        present = self.set_mode("PRESENT", "present")
        self.assertEqual(away["id"], present["predecessor_receipt_id"])
        self.assertEqual(away["since"], present["away_since"])
        line = self.inbox_envelope("morning")["fleet_presence"]
        self.assertIn("PRESENT", line)
        self.assertIn(self.node, line)
        self.assertIn(away["since"], line)
        argv = shlex.split(line.split("`")[1])
        self.assertEqual("floati", argv[0])
        code, receipts = _cli(argv[1:])
        self.assertEqual(0, code, receipts)
        self.assertEqual(self.node, receipts["evidence"]["node_id"])
        self.assertTrue(receipts["evidence"]["deliveries"])

        code, status = _cli(["status", "--root", str(self.home), "--json"])
        self.assertEqual(0, code, status)
        self.assertEqual(present, status["evidence"]["fleet_presence"])
        validate_json_schema(present, Path("schemas/v1/fleet-presence-receipt.schema.json"))

    def test_doctor_names_the_active_operator_declaration(self):
        from floati.doctor import Doctor
        from tests.schema_validation import validate_json_schema

        away = self.set_mode("AWAY", "away")
        artifact, _ = Doctor(REPOSITORY_ROOT, self.home, ref="origin/main", no_sandbox=True).artifact()
        self.assertEqual(away, artifact["fleet_presence"])
        validate_json_schema(artifact, Path("schemas/v1/doctor-artifact.schema.json"))

    def test_present_envelope_links_both_reads_when_the_needs_you_inbox_is_available(self):
        from types import ModuleType
        from unittest.mock import patch

        self.set_mode("AWAY", "away")
        self.set_mode("PRESENT", "present")

        def show(root):
            self.assertEqual(self.root, root)
            return {"items": []}

        inbox = ModuleType("floati.needs_inbox")
        inbox.show = show
        for installed in (False, True):
            absent = None if installed else ModuleNotFoundError(name=inbox.__name__)
            with self.subTest(installed=installed), patch(
                "floati.fleet_presence.import_module", return_value=inbox, side_effect=absent
            ):
                line = self.inbox_envelope("return-" + str(installed).lower())["fleet_presence"]
                self.assertIn("floati receipts " + self.node, line)
                self.assertEqual(installed, "floati needs show" in line)
                self.assertEqual(1, len(line.splitlines()))


class HoldAuthoringTests(HoldFixture):
    """The authoring boundary: `floati hold start` / `floati hold release`."""

    def test_hold_start_writes_a_versioned_record_and_replays_by_key(self) -> None:
        argv = [
            "hold", "start",
            "--root", str(self.home),
            "--as", self.node,
            "--reason", "held for review",
            "--idempotency-key", "hold-red-1",
        ]
        code, artifact = _cli(argv)
        self.assertEqual(0, code, artifact)
        self.assertEqual("ok", artifact["status"], artifact)
        receipt = artifact["evidence"]
        self.assertEqual("hold_receipt", receipt["kind"])
        self.assertEqual(1, receipt["schema_version"])
        self.assertEqual(self.node, receipt["node_id"])
        self.assertEqual("held for review", receipt["reason"])
        self.assertIsNone(receipt["expected_release"])
        self.assertEqual("held", receipt["phase"])
        self.assertTrue(receipt["started_at"])
        ledger = self.home / "receipts" / "holds" / f"{self.node}.jsonl"
        self.assertTrue(ledger.is_file())

        replay_code, replay = _cli(argv)
        self.assertEqual(0, replay_code, replay)
        self.assertEqual(receipt["id"], replay["evidence"]["id"])

        conflict = list(argv)
        conflict[conflict.index("--reason") + 1] = "a different reason"
        code, artifact = _cli(conflict)
        self.assertEqual(20, code, artifact)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("hold_key_conflict", artifact["evidence"]["code"])

    def test_hold_start_refuses_an_unregistered_node(self) -> None:
        code, artifact = _cli([
            "hold", "start",
            "--root", str(self.home),
            "--as", "stranger-node",
            "--reason", "held",
            "--idempotency-key", "hold-red-2",
        ])
        self.assertEqual(20, code, artifact)
        self.assertEqual("hold_node_unknown", artifact["evidence"]["code"])

    def test_hold_start_refuses_an_unbounded_reason(self) -> None:
        code, artifact = _cli([
            "hold", "start",
            "--root", str(self.home),
            "--as", self.node,
            "--reason", "   ",
            "--idempotency-key", "hold-red-3",
        ])
        self.assertEqual(20, code, artifact)
        self.assertEqual("hold_reason_invalid", artifact["evidence"]["code"])

    def test_hold_start_refuses_a_malformed_expected_release(self) -> None:
        code, artifact = _cli([
            "hold", "start",
            "--root", str(self.home),
            "--as", self.node,
            "--reason", "held",
            "--expected-release", "not-a-timestamp",
            "--idempotency-key", "hold-red-4",
        ])
        self.assertEqual(20, code, artifact)
        self.assertEqual("hold_expected_release_invalid", artifact["evidence"]["code"])

    def test_hold_release_writes_a_terminal_record_and_replays(self) -> None:
        code, started = _cli([
            "hold", "start",
            "--root", str(self.home),
            "--as", self.node,
            "--reason", "held for review",
            "--idempotency-key", "hold-red-5",
        ])
        self.assertEqual(0, code, started)
        hold_id = started["evidence"]["id"]

        code, released = _cli([
            "hold", "release",
            "--root", str(self.home),
            "--as", self.node,
            "--hold", hold_id,
            "--idempotency-key", "hold-release-5",
        ])
        self.assertEqual(0, code, released)
        self.assertEqual("ok", released["status"], released)
        receipt = released["evidence"]
        self.assertEqual("hold_receipt", receipt["kind"])
        self.assertEqual("released", receipt["phase"])
        self.assertEqual(hold_id, receipt["predecessor_receipt_id"])
        self.assertTrue(receipt["released_at"])
        rows = read_records_snapshot(
            self.root,
            Path("receipts/holds") / f"{self.node}.jsonl",
            allowed_kinds={"hold_receipt"},
        )
        self.assertEqual(2, len(rows), rows)

        again = [
            "hold", "release",
            "--root", str(self.home),
            "--as", self.node,
            "--hold", hold_id,
            "--idempotency-key", "hold-release-5b",
        ]
        code, artifact = _cli(again)
        self.assertEqual(20, code, artifact)
        self.assertEqual("hold_already_released", artifact["evidence"]["code"])

    def test_hold_release_names_the_open_hold_and_refuses_ambiguity(self) -> None:
        code, started = _cli([
            "hold", "start",
            "--root", str(self.home),
            "--as", self.node,
            "--reason", "the only open hold",
            "--idempotency-key", "hold-red-6",
        ])
        self.assertEqual(0, code, started)

        code, released = _cli([
            "hold", "release",
            "--root", str(self.home),
            "--as", self.node,
            "--idempotency-key", "hold-release-6",
        ])
        self.assertEqual(0, code, released)
        self.assertEqual(started["evidence"]["id"], released["evidence"]["predecessor_receipt_id"])

    def test_hold_release_refuses_when_two_holds_are_open(self) -> None:
        for key in ("hold-red-7a", "hold-red-7b"):
            code, _ = _cli([
                "hold", "start",
                "--root", str(self.home),
                "--as", self.node,
                "--reason", "parallel hold " + key,
                "--idempotency-key", key,
            ])
            self.assertEqual(0, code)

        code, artifact = _cli([
            "hold", "release",
            "--root", str(self.home),
            "--as", self.node,
            "--idempotency-key", "hold-release-7",
        ])
        self.assertEqual(20, code, artifact)
        self.assertEqual("hold_ambiguous", artifact["evidence"]["code"])
        remedy = artifact["evidence"].get("remedy") or artifact["evidence"].get("remediation") or ""
        argv = shlex.split(remedy)
        self.assertEqual(["floati", "hold", "release"], argv[:3], remedy)

    def test_hold_release_refuses_with_no_open_hold(self) -> None:
        code, artifact = _cli([
            "hold", "release",
            "--root", str(self.home),
            "--as", self.node,
            "--idempotency-key", "hold-release-8",
        ])
        self.assertEqual(20, code, artifact)
        self.assertEqual("hold_none_open", artifact["evidence"]["code"])


class HoldDoctorReaderTests(HoldFixture):
    """Doctor lists open holds with their age; OVERDUE is per node."""

    def test_doctor_lists_an_open_hold_with_age_by_node(self) -> None:
        self.write_hold(reason="waiting on the owner")
        findings, _rc = self.doctor_findings()
        rows = [
            row for row in findings
            if row["code"] == "hold_open" and row["subject"] == self.node
        ]
        self.assertEqual(1, len(rows), findings)
        self.assertIn("waiting on the owner", rows[0]["detail"])
        self.assertIn("old", rows[0]["detail"])

    def test_doctor_reports_a_hold_past_its_expected_release_overdue(self) -> None:
        self.write_hold(
            reason="waiting on the owner",
            expected_release=self.now - timedelta(days=1),
        )
        findings, _rc = self.doctor_findings()
        rows = [
            row for row in findings
            if row["code"] == "hold_overdue" and row["subject"] == self.node
        ]
        self.assertEqual(1, len(rows), findings)
        self.assertEqual("warning", rows[0]["severity"])
        self.assertIn("OVERDUE", rows[0]["detail"])
        remediation = rows[0].get("remediation") or ""
        argv = shlex.split(remediation)
        self.assertEqual(["floati", "hold", "release"], argv[:3], remediation)
        self.assertIn("--hold", argv, remediation)

    def test_doctor_says_nothing_when_no_hold_exists(self) -> None:
        findings, _rc = self.doctor_findings()
        self.assertEqual(
            [], [row for row in findings if row["code"].startswith("hold_")],
            findings,
        )


class HoldLegacyReaderTests(HoldFixture):
    """Readers accept every older record shape and never refuse the read."""

    def test_doctor_accepts_an_older_shape_beside_a_current_one(self) -> None:
        legacy_id = self.write_hold(
            reason="a legacy hold",
            with_optional_fields=False,
            key="hold-legacy-1",
        )
        current_id = self.write_hold(
            reason="a current hold",
            expected_release=self.now + timedelta(days=1),
            key="hold-read-2",
        )
        self.assertNotEqual(legacy_id, current_id)
        findings, rc = self.doctor_findings()
        self.assertNotEqual(33, rc, findings)
        listed = [row["detail"] for row in findings
                  if row["code"] in ("hold_open", "hold_overdue")]
        self.assertIn("a legacy hold", json.dumps(listed), listed)
        self.assertIn("a current hold", json.dumps(listed), listed)

    def test_doctor_counts_a_corrupt_line_instead_of_refusing_the_read(self) -> None:
        self.write_hold(reason="a healthy hold")
        self.append_line(self.node, '{"kind": "hold_receipt", "id": "hold-0123')
        findings, rc = self.doctor_findings()
        self.assertNotEqual(33, rc, findings)
        listed = [
            row for row in findings
            if row["code"] in ("hold_open", "hold_overdue")
            and "a healthy hold" in row["detail"]
        ]
        self.assertEqual(1, len(listed), findings)
        partial = [row for row in findings if row["code"] == "hold_ledger_partial"]
        self.assertEqual(1, len(partial), findings)
        self.assertIn("1", partial[0]["detail"])


class HoldBoardReaderTests(HoldFixture):
    """The seat board lists open holds with their age; OVERDUE flagged."""

    def test_board_lists_an_open_hold_with_age(self) -> None:
        self.write_hold(reason="waiting on the owner")
        lines = self.board_detail_lines()
        hold_lines = [line for line in lines if "HOLD" in line]
        self.assertTrue(hold_lines, lines)
        self.assertIn("waiting on the owner", " | ".join(hold_lines))

    def test_board_flags_a_hold_past_its_expected_release_overdue(self) -> None:
        self.write_hold(
            reason="waiting on the owner",
            expected_release=self.now - timedelta(days=1),
        )
        lines = self.board_detail_lines()
        hold_lines = [line for line in lines if "HOLD" in line]
        self.assertTrue(hold_lines, lines)
        self.assertTrue(any("OVERDUE" in line for line in hold_lines), hold_lines)

    def test_board_says_nothing_when_no_hold_exists(self) -> None:
        lines = self.board_detail_lines()
        self.assertEqual([], [line for line in lines if "HOLD" in line])


class HoldLifecycleControls(HoldFixture):
    def test_ambiguous_legacy_terminal_facts_do_not_hide_open_holds(self):
        hold_id = self.write_hold(reason='still open older hold')
        ledger = self.home / 'receipts/holds' / (self.node + '.jsonl')
        row = json.loads(ledger.read_text())
        row['released_at'] = ''  # an older writer's empty optional value
        ledger.write_text(json.dumps(row) + '\n')
        invalid_release = dict(row, id='hold-' + uuid7_hex(), phase='released',
                               predecessor_receipt_id=hold_id, released_at='unknown clock')
        self.append_line(self.node, json.dumps(invalid_release))
        before = ledger.read_bytes()
        findings, _ = self.doctor_findings()
        self.assertTrue(any(r['code'] == 'hold_open' and 'still open older hold' in r['detail'] for r in findings))
        self.assertTrue(any(r['code'] == 'hold_ledger_partial' for r in findings))
        self.assertIn('still open older hold', ' | '.join(self.board_detail_lines(width=500)))
        self.assertEqual(before, ledger.read_bytes())

    def test_release_canonicalizes_readable_legacy_timestamps_only_in_new_record(self):
        hold_id = self.write_hold()
        ledger = self.home / 'receipts/holds' / (self.node + '.jsonl')
        row = json.loads(ledger.read_text())
        row.update(schema_version=0, started_at='2026-10-10T12:30:00.123456+00:00',
                   expected_release='2026-10-12T12:30:00+00:00')
        ledger.write_text(json.dumps(row) + '\n')
        before = ledger.read_bytes()
        code, artifact = _cli(['hold', 'release', '--root', str(self.home), '--as', self.node,
                               '--hold', hold_id, '--idempotency-key', 'legacy-clock-release'])
        self.assertEqual(0, code, artifact)
        self.assertEqual('2026-10-10T12:30:00.123Z', artifact['evidence']['started_at'])
        self.assertEqual('2026-10-12T12:30:00.000Z', artifact['evidence']['expected_release'])
        self.assertTrue(ledger.read_bytes().startswith(before))

    def test_unreadable_hold_path_isolated_from_healthy_node_board(self):
        other = public_ids.builder('b')
        Registry(self.root).register(other, 'zcode')
        self.write_hold(node=other, reason='healthy sibling hold')
        broken = self.home / 'receipts/holds' / (self.node + '.jsonl')
        broken.mkdir()
        from floati.tui import model_from_root
        from floati.tui_render import _detail_lines
        model = model_from_root(self.root, self.now)
        views = {node['node_id']: ' | '.join(_detail_lines(model, 500, i))
                 for i, node in enumerate(model.nodes)}
        self.assertIn('history', views[self.node])
        self.assertIn('healthy sibling hold', views[other])
        broken.rmdir()
        # A selected hold directory escaping the root is refused as a read,
        # while the rest of doctor remains usable.
        import shutil
        shutil.rmtree(broken.parent)
        broken.parent.symlink_to(self.home.parent, target_is_directory=True)
        findings, _ = self.doctor_findings()
        self.assertTrue(any(r['code'] == 'hold_ledger_partial' for r in findings))

    def test_concurrent_releases_append_only_one_terminal_record(self):
        from concurrent.futures import ThreadPoolExecutor
        from floati.errors import ProtocolRefusal
        from floati.holds import HoldLedger
        started = self.start('race-start')
        ledger = HoldLedger(self.root)

        def release(key):
            try:
                return ledger.release(self.node, key, started['id'])['phase']
            except ProtocolRefusal as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(release, ['race-a', 'race-b']))
        self.assertEqual(['hold_already_released', 'released'], sorted(outcomes))
        rows = (self.home / 'receipts/holds' / (self.node + '.jsonl')).read_text().splitlines()
        self.assertEqual(2, len(rows))

    def test_board_age_and_overdue_are_separate_per_node(self):
        from floati.tui import model_from_root
        from floati.tui_render import _detail_lines
        other = public_ids.builder('b')
        Registry(self.root).register(other, 'zcode')
        self.write_hold(started_at=self.now - timedelta(hours=3), expected_release=self.now - timedelta(minutes=1))
        self.write_hold(node=other, reason='other node hold', started_at=self.now - timedelta(hours=5),
                        expected_release=self.now + timedelta(hours=1), key='other-node')
        model = model_from_root(self.root, self.now)
        views = {node['node_id']: ' | '.join(_detail_lines(model, 500, i))
                 for i, node in enumerate(model.nodes)}
        self.assertIn('10800s old', views[self.node])
        self.assertIn('OVERDUE', views[self.node])
        self.assertIn('18000s old', views[other])
        self.assertNotIn('OVERDUE', views[other])
        self.assertNotIn('other node hold', views[self.node])

    def start(self, key, reason='review pending', **options):
        argv = ['hold', 'start', '--root', str(self.home), '--as', self.node,
                '--reason', reason, '--idempotency-key', key]
        for flag, value in options.items():
            argv.extend(['--' + flag.replace('_', '-'), value])
        code, artifact = _cli(argv)
        self.assertEqual(0, code, artifact)
        return artifact['evidence']

    def test_release_one_of_two_then_last_and_replay_without_duplicate(self):
        first = self.start('lifecycle-a', 'first review')
        second = self.start('lifecycle-b', 'second review')
        argv = ['hold', 'release', '--root', str(self.home), '--as', self.node,
                '--hold', first['id'], '--idempotency-key', 'release-a']
        code, released = _cli(argv)
        self.assertEqual(0, code, released)
        ledger = self.home / 'receipts/holds' / (self.node + '.jsonl')
        after_release = ledger.read_bytes()
        code, replay = _cli(argv)
        self.assertEqual(0, code, replay)
        self.assertEqual(released['evidence']['id'], replay['evidence']['id'])
        self.assertEqual(after_release, ledger.read_bytes())
        lines = ' | '.join(self.board_detail_lines(width=300))
        self.assertIn('second review', lines)
        self.assertNotIn('first review', lines)
        changed = list(argv)
        changed[changed.index('--hold') + 1] = second['id']
        code, conflict = _cli(changed)
        self.assertEqual(20, code, conflict)
        self.assertEqual('hold_key_conflict', conflict['evidence']['code'])
        self.assertEqual(after_release, ledger.read_bytes())
        changed[-1] = 'release-b'
        self.assertEqual(0, _cli(changed)[0])
        self.assertFalse(any('HOLD' in line for line in self.board_detail_lines()))
        findings, _ = self.doctor_findings()
        self.assertFalse(any(row['code'] == 'hold_open' for row in findings))

    def test_cross_node_release_refuses_without_mutation(self):
        other = public_ids.builder('b')
        Registry(self.root).register(other, 'zcode')
        hold_id = self.write_hold(node=other)
        before = {p.name: p.read_bytes() for p in (self.home / 'receipts/holds').glob('*.jsonl')}
        code, artifact = _cli(['hold', 'release', '--root', str(self.home), '--as', self.node,
                               '--hold', hold_id, '--idempotency-key', 'cross-node'])
        self.assertEqual(20, code, artifact)
        self.assertEqual('hold_unknown', artifact['evidence']['code'])
        after = {p.name: p.read_bytes() for p in (self.home / 'receipts/holds').glob('*.jsonl')}
        self.assertEqual(before, after)

    def test_older_shapes_corrupt_middle_and_unknown_age_stay_readable(self):
        hold_id = self.write_hold(reason='older optional fields', with_optional_fields=False)
        ledger = self.home / 'receipts/holds' / (self.node + '.jsonl')
        row = json.loads(ledger.read_text())
        row.pop('schema_version')
        row['legacy_extra'] = 'retained history'
        ledger.write_text(json.dumps(row) + '\n')
        self.append_line(self.node, '{broken middle')
        row = dict(row, id='hold-' + uuid7_hex(), reason='unknown age hold', started_at='old clock')
        self.append_line(self.node, json.dumps(row))
        self.write_hold(reason='healthy after corruption', key='after-corruption')
        before = ledger.read_bytes()
        findings, rc = self.doctor_findings()
        self.assertNotEqual(33, rc)
        details = json.dumps([r for r in findings if r['code'].startswith('hold_')])
        for reason in ('older optional fields', 'unknown age hold', 'healthy after corruption'):
            self.assertIn(reason, details)
        self.assertIn('unknown', details.lower())
        self.assertTrue(any(r['code'] == 'hold_ledger_partial' for r in findings))
        board = ' | '.join(self.board_detail_lines(width=500))
        self.assertIn('healthy after corruption', board)
        self.assertEqual(before, ledger.read_bytes())
        self.assertFalse(ledger.with_name(ledger.name + '.lock').exists())

    def test_legacy_hold_can_be_released_without_rewriting_it(self):
        hold_id = self.write_hold(with_optional_fields=False)
        ledger = self.home / 'receipts/holds' / (self.node + '.jsonl')
        row = json.loads(ledger.read_text())
        row['schema_version'] = 0
        row['extra_old_field'] = True
        ledger.write_text(json.dumps(row) + '\n')
        before = ledger.read_bytes()
        code, artifact = _cli(['hold', 'release', '--root', str(self.home), '--as', self.node,
                               '--hold', hold_id, '--idempotency-key', 'legacy-release'])
        self.assertEqual(0, code, artifact)
        self.assertTrue(ledger.read_bytes().startswith(before))
        self.assertFalse(any('HOLD' in line for line in self.board_detail_lines()))

    def test_authoring_rejects_oversize_and_terminal_control_reasons_without_append(self):
        for reason in ('a' * 1025, 'review\x1b[2J', 'review\nforged line'):
            code, artifact = _cli(['hold', 'start', '--root', str(self.home), '--as', self.node,
                                   '--reason', reason, '--idempotency-key', 'invalid-reason'])
            self.assertEqual(20, code, artifact)
            self.assertEqual('hold_reason_invalid', artifact['evidence']['code'])
        self.assertFalse((self.home / 'receipts/holds' / (self.node + '.jsonl')).exists())

    def test_warm_board_reprojects_age_and_expected_release_without_ledger_write(self):
        from floati.tui import model_from_root, _seat_attention_timeout
        from floati.tui_render import _detail_lines
        model_from_root(self.root, self.now)  # establish a cache before authoring
        self.start('clock-hold', expected_release=_iso(self.now + timedelta(minutes=10)))
        first = model_from_root(self.root, self.now)
        before = ' | '.join(_detail_lines(first, 500, 0))
        self.assertIn('HOLD', before)
        self.assertNotIn('OVERDUE', before)
        timeout = _seat_attention_timeout(first, self.now)
        self.assertIsNotNone(timeout, 'held boards must wake to refresh age/deadline')
        later = model_from_root(self.root, self.now + timedelta(hours=2))
        after = ' | '.join(_detail_lines(later, 500, 0))
        self.assertIn('OVERDUE', after)
        self.assertIn('old', after)
        self.assertNotEqual(before, after)


if __name__ == "__main__":  # pragma: no cover - parity with the suite's modules
    unittest.main()
