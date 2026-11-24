"""Scratch-only consumer contracts for the ruled Codex idle window.

These tests exercise emitted installer arguments, native hook/consent writes,
doctor observations, and two independent no-mail Stop invocations. Accelerated
clocks are product logic evidence, never a native harness-duration measurement.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.codex_hook_install import CodexHookInstaller
from floati.codex_hook_trust import observe_codex_waiter_hooks
from floati.codex_wait_contract import (
    CodexWaitConsentLedger, consent_ledger_relative, resolve_participant,
)
from floati.doctor import Doctor, project_codex_wait_window_findings
from floati.errors import ProtocolRefusal
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.test_codex_hook_install import expected_stop_block, operator_pair
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


class WaiterIdleWindowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.source = Path(__file__).resolve().parents[1]
        self.bus_home = self.home / "scratch-fleet"
        self.root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        self.node = public_ids.builder("floati")
        Registry(self.root).register(self.node, "Codex")
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        self.hooks = self.home / ".codex" / "hooks.json"
        self.hooks.parent.mkdir()
        self.hooks.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")
        self.destination = self.hooks.parent / "floati-wake"
        self.config = self.hooks.with_name("config.toml")
        self.config.write_text("# no trust granted\n", encoding="utf-8")

    def install(self):
        return CodexHookInstaller(
            source_root=self.source, bus_home=self.bus_home,
            hooks_path=self.hooks, destination=self.destination,
        ).install(
            self.workspace, self.node, hook_timeout_seconds=86400,
            wait_deadline_seconds=86300,
            acknowledge_stop_hook_change=operator_pair(
                None, expected_stop_block(self.destination, self.bus_home, 86400)),
        )

    def windows(self):
        artifact, _ = Doctor(
            self.source, self.bus_home, ref="HEAD", no_sandbox=True,
            codex_hooks=self.hooks, codex_config=self.config,
        ).artifact()
        validate_json_schema(artifact, self.source / "schemas/v1/doctor-artifact.schema.json")
        return [dict(row["wait_window"], subject=row["subject"])
                for row in artifact["findings"] if row["code"] == "codex_wait_window"]

    def projected_windows(self):
        try:
            trust_rows = observe_codex_waiter_hooks(self.hooks, self.config)
        except ProtocolRefusal:
            trust_rows = []
        findings = project_codex_wait_window_findings(self.root, self.hooks, trust_rows)
        return [dict(row["wait_window"], subject=row["subject"])
                for row in findings if row["code"] == "codex_wait_window"]

    def test_doctor_reports_the_written_hook_and_consent_without_granting_trust(self):
        """Missing or invented timing observations fail against private native writes."""
        receipt = self.install()
        before = self.config.read_bytes(), self.hooks.read_bytes()
        rows = self.windows()
        self.assertEqual(1, len(rows), rows)
        row = rows[0]
        self.assertEqual("matched", row["window_state"])
        self.assertEqual(86400, row["hook_timeout_seconds"])
        self.assertEqual(86400, row["consent_hook_timeout_seconds"])
        self.assertEqual(86300, row["wait_deadline_seconds"])
        self.assertEqual(receipt["consent_receipt_id"], row["consent_receipt_id"])
        self.assertEqual(str(self.workspace), row["subject"])
        self.assertEqual(f"{self.hooks}:stop:0:0", row["hook_timeout_source"])
        self.assertEqual(str(self.bus_home / consent_ledger_relative(self.node)),
                         row["wait_deadline_source"])
        self.assertIsNone(row["harness_ceiling_seconds"])
        self.assertFalse(row["hook_armed"])
        self.assertEqual(before, (self.config.read_bytes(), self.hooks.read_bytes()))

    def test_doctor_exposes_a_shorter_hook_and_absent_consent_as_distinct_observations(self):
        """A policy default must never replace the actual hook or absent receipt."""
        self.install()
        document = json.loads(self.hooks.read_text(encoding="utf-8"))
        document["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 1800
        self.hooks.write_text(json.dumps(document) + "\n", encoding="utf-8")
        rows = self.projected_windows()
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("mismatch", rows[0]["window_state"])
        self.assertEqual(1800, rows[0]["hook_timeout_seconds"])
        self.assertEqual(86300, rows[0]["wait_deadline_seconds"])
        block = document["hooks"]["Stop"][0]
        document["hooks"]["Stop"].append(json.loads(json.dumps(block)))
        self.hooks.write_text(json.dumps(document) + "\n", encoding="utf-8")
        rows = self.projected_windows()
        self.assertEqual("hook_ambiguous", rows[0]["window_state"])
        self.assertIsNone(rows[0]["hook_timeout_seconds"])
        self.assertIsNone(rows[0]["hook_timeout_source"])
        document["hooks"]["Stop"] = [block]
        command = block["hooks"][0]["command"]
        block["hooks"][0]["command"] = command.replace(str(self.bus_home), str(self.home / "other-fleet"))
        self.hooks.write_text(json.dumps(document) + "\n", encoding="utf-8")
        rows = self.projected_windows()
        self.assertEqual("hook_unavailable", rows[0]["window_state"])
        self.assertIsNone(rows[0]["hook_timeout_seconds"])
        block["hooks"][0]["command"] = command
        self.hooks.write_text(json.dumps(document) + "\n", encoding="utf-8")
        before = self.hooks.read_bytes()
        self.hooks.unlink()
        rows = self.projected_windows()
        self.assertEqual("hook_unavailable", rows[0]["window_state"])
        self.assertIsNone(rows[0]["hook_timeout_seconds"])
        self.assertEqual(86300, rows[0]["wait_deadline_seconds"])
        self.hooks.write_bytes(before)
        (self.bus_home / consent_ledger_relative(self.node)).unlink()
        rows = self.projected_windows()
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("consent_unavailable", rows[0]["window_state"])
        self.assertEqual(1800, rows[0]["hook_timeout_seconds"])
        self.assertIsNone(rows[0]["wait_deadline_seconds"])
        self.assertIsNone(rows[0]["consent_receipt_id"])
        external = self.home / "foreign-consent.jsonl"
        external.write_bytes(b"not native consent testimony\n")
        consent_path = self.bus_home / consent_ledger_relative(self.node)
        consent_path.symlink_to(external)
        rows = self.projected_windows()
        self.assertEqual("consent_unavailable", rows[0]["window_state"])
        self.assertEqual("path_not_contained", rows[0]["consent_error_code"])
        self.assertEqual(str(consent_path), rows[0]["wait_deadline_source"])
        self.assertEqual(b"not native consent testimony\n", external.read_bytes())

    def test_seat_default_no_mail_windows_rearm_from_each_invocation_start(self):
        """An old seat default or reuse of an exhausted window breaks this contract."""
        from floati.codex_wait import run_stop_waiter
        from floati.seat_waiter_lifecycle import prepare_waiter

        prepare_waiter(self.root, self.node, self.workspace)
        participant = resolve_participant(self.bus_home, self.workspace)
        self.assertIsNotNone(participant)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        self.assertEqual(86400, consent["hook_timeout_seconds"])
        self.assertEqual(86300, consent["wait_deadline_seconds"])
        clock = [0.0]
        for _ in range(2):
            start = clock[0]
            stdout, stderr = io.StringIO(), io.StringIO()
            status = run_stop_waiter(
                bus_home=self.bus_home,
                hook_payload={"cwd": str(self.workspace), "session_id": "idle-window-test"},
                stdout=stdout, stderr=stderr, monotonic=lambda: clock[0],
                sleep=lambda seconds: clock.__setitem__(0, clock[0] + 86300),
                poll_interval_seconds=1.0,
            )
            self.assertEqual(0, status, stderr.getvalue())
            self.assertEqual(86300, clock[0] - start)
            decision = json.loads(stdout.getvalue())
            self.assertEqual("block", decision["decision"])
            self.assertIn("end this turn to re-arm", decision["reason"])
        rows = read_records_snapshot(
            self.root, Path("receipts/codex-wait-exhaustion") / f"{self.node}.jsonl",
            allowed_kinds={"codex_wait_exhaustion_receipt"},
        )
        self.assertEqual([86300, 86300], [row["waited_seconds"] for row in rows])
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
