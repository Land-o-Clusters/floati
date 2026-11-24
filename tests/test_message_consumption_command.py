"""Explicit ACTED commands keep identity bound and exported readers closed."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import tests  # Freeze HOME before a native command resolves defaults.

from floati.cli import _parser
from floati.command_contract import describe_parser
from floati.confluence import ConfluenceGrantLedger, materialize_bundle
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records_snapshot
from floati.mcp import McpServer, run_cli_artifact
from floati.planes import AuthorityGrantStore
from floati.projection import FleetProjection
from floati.records import WAKE_HOLD_KINDS
from floati.registry import Registry, utc_now
from floati.root import FloatiRoot
from floati.work import WorkLog
from tests.schema_validation import SchemaValidationError, validate_json_schema


REPOSITORY = Path(__file__).resolve().parents[1]


class MessageConsumptionCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="floati-consume-command-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        for node in ("sender", "recipient", "other"):
            registry.register(node, "codex")
        self.events = EventLog(self.root)
        self.cursor = SparseCursor(self.root)

    def _message(self, *, key="dispatch", legacy_export=False, timestamp="2026-10-14T00:00:00.000Z"):
        if legacy_export:
            # The existing closed export contracts support this documented
            # envelope, delivery, and ACK generations. Current unpresented
            # delivery and actor-bound ACK export limits are characterized
            # separately; consumption must not widen either bundle.
            message = {
                "schema_version": 0, "id": "msg-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": timestamp,
                "kind": "message_envelope", "sender": "sender",
                "recipient": "recipient", "repo": "floati", "sha": "a" * 40,
                "doc": "docs/dispatch.md", "note": "bounded dispatch",
                "idempotency_key": key, "attempt_binding": "absent_legacy",
            }
            append_record(self.root, "events.jsonl", message,
                          allowed_kinds={"message_envelope"})
            delivery = {
                "schema_version": 0, "id": "delivery-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id, "timestamp": message["timestamp"],
                "kind": "delivery_receipt", "recipient": "recipient",
                "item_ids": [message["id"]], "presentation_count": 1,
            }
            append_record(self.root, "receipts/deliveries/recipient.jsonl", delivery,
                          allowed_kinds={"delivery_receipt"})
            ack = {
                "schema_version": 0, "id": "ack-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id, "timestamp": message["timestamp"],
                "kind": "ack_receipt", "recipient": "recipient",
                "item_ids": [message["id"]],
            }
            append_record(self.root, "receipts/acks/recipient.jsonl", ack,
                          allowed_kinds={"ack_receipt"})
            return message, ack
        else:
            message = self.events.send(
                "sender", "recipient", "floati", "a" * 40,
                "docs/dispatch.md", "bounded dispatch", idempotency_key=key,
            )
        self.events.present("recipient")
        ack = self.cursor.ack(
            "recipient", [str(message["id"])], acting_session_id="seen-session",
        )
        return message, ack

    def _consume_argv(self, message_id):
        return [
            "consume", str(message_id), "--root", str(self.root.path),
            "--as", "recipient", "--session", "acting-session",
            "--idempotency-key", "explicit-act",
        ]

    def _history(self):
        history = FleetProjection(self.root).receipts("recipient")
        self.assertIn("consumptions", history, "ACTED history is separate from ACK")
        return history["consumptions"]

    def _tool(self, server, name):
        tools = {tool["name"]: tool for tool in server.list_tools()}
        self.assertIn(name, tools, "native consumption must be a governed MCP tool")
        return tools[name]

    def _claimed(self, *, subject="work-claims"):
        now = datetime.now(timezone.utc)
        grant = AuthorityGrantStore(self.root).claim(
            subject, "recipient", 60, 60, now,
        )
        work = WorkLog(self.root)
        item = work.add("act on dispatch", "recipient", [])
        work.claim(str(item["id"]), "recipient", subject, int(grant["epoch"]), now=now)
        return item

    def test_native_consume_authors_and_replays_one_actor_bound_receipt(self):
        message, ack = self._message()
        argv = self._consume_argv(message["id"])
        code, artifact = run_cli_artifact(argv)
        self.assertEqual(0, code, artifact)
        self.assertEqual("ok", artifact["status"])
        receipt, = self._history()
        self.assertEqual(message["id"], receipt["message_id"])
        self.assertEqual(ack["id"], receipt["ack_receipt_id"])
        self.assertEqual("acting-session", receipt["acting_session_id"])
        replay_code, replay = run_cli_artifact(argv)
        self.assertEqual((code, artifact), (replay_code, replay))
        self.assertEqual([receipt], self._history())

    def test_consume_parser_declares_exact_required_authoring_inputs(self):
        rows = {tuple(row["path"]): row for row in describe_parser(_parser())["commands"]}
        self.assertIn(("consume",), rows, "consume command is absent")
        row = rows[("consume",)]
        self.assertEqual("governed", row["mcp_exposure"])
        arguments = {argument["name"]: argument for argument in row["arguments"]}
        self.assertTrue(arguments["message_id"]["positional"])
        self.assertEqual("ID", arguments["message_id"]["metavar"])
        for name in ("root", "recipient", "session", "idempotency_key"):
            self.assertTrue(arguments[name]["required"], name)

    def test_work_complete_declares_repeated_consume_and_session(self):
        row = next(row for row in describe_parser(_parser())["commands"]
                   if row["path"] == ["work", "complete"])
        arguments = {argument["name"]: argument for argument in row["arguments"]}
        self.assertIn("consume_ids", arguments, "completion cannot attest dispatch consumption")
        self.assertEqual(["--consume"], arguments["consume_ids"]["option_strings"])
        self.assertTrue(arguments["consume_ids"]["repeat"])
        self.assertEqual([], arguments["consume_ids"]["default"])
        self.assertIn("session", arguments)
        self.assertFalse(arguments["session"]["required"], "legacy no-envelope completion stays legal")

    def test_work_replay_requires_an_exact_dispatch_link(self):
        message, ack = self._message(legacy_export=True, timestamp=utc_now())
        self._message(key="unrelated-dispatch", legacy_export=True, timestamp=utc_now())
        unpresented, _ = self._message(key="delivery-without-presentation")
        item = self._claimed()
        item_id = item["id"]
        work = WorkLog(self.root)
        argv = ["replay", "--root", str(self.root.path), "--work-item", item_id]
        code, claimed = run_cli_artifact(argv)
        self.assertEqual(0, code, claimed)
        evidence = claimed["evidence"]
        self.assertEqual(item_id, evidence["asked"]["receipt_id"])
        self.assertEqual("work_dispatcher_receipt_missing", evidence["asked"]["actor"]["reason"])
        self.assertEqual("recipient", evidence["claim"]["node"])
        self.assertEqual("work_dispatch_link_missing", evidence["presented"]["reason"])
        self.assertEqual("work_consumption_link_missing", evidence["acted"]["reason"])
        self.assertEqual("work_completion_receipt_missing", evidence["completion"]["reason"])

        completion = work.complete(
            item_id, "recipient", [{"repo": "floati", "sha": "b" * 40, "doc": "docs/result.md"}],
            consume_ids=[message["id"], unpresented["id"]], session_id="acting-session",
        )
        consumption = next(row for row in self._history() if row["message_id"] == message["id"])
        code, completed = run_cli_artifact(argv)
        self.assertEqual(0, code, completed)
        evidence = completed["evidence"]
        self.assertEqual([message["id"]], [row["message_id"] for row in evidence["presented"]["receipts"]])
        self.assertEqual("partial", evidence["presented"]["status"])
        self.assertEqual([{
            "message_id": unpresented["id"], "reason": "work_presentation_receipt_missing",
        }], evidence["presented"]["gaps"])
        acted = next(row for row in evidence["acted"]["receipts"] if row["message_id"] == message["id"])
        self.assertEqual(consumption["id"], acted["receipt_id"])
        self.assertEqual(ack["id"], acted["ack_receipt_id"])
        self.assertEqual(completion["id"], evidence["completion"]["receipt_id"])
        self.assertEqual(completion["id"], evidence["git_change"]["completion"]["receipt_id"])
        self.assertEqual("b" * 40, evidence["git_change"]["completion"]["references"][0]["sha"])
        self.assertEqual("unmeasured", evidence["cost"]["status"])

        relative = "receipts/deliveries/recipient.jsonl"
        deliveries = read_records_snapshot(self.root, relative, allowed_kinds=WAKE_HOLD_KINDS)
        path = self.root.resolve_relative(relative)
        original = path.read_bytes()
        try:
            path.write_bytes(b"".join(
                encode_frame(row) for row in deliveries if message["id"] not in row["item_ids"]
            ))
            code, incomplete = run_cli_artifact(argv)
            self.assertEqual(0, code, incomplete)
            self.assertEqual("partial", incomplete["evidence"]["acted"]["status"])
            self.assertEqual([{
                "message_id": message["id"], "reason": "work_ack_delivery_receipt_missing",
            }], incomplete["evidence"]["acted"]["gaps"])
        finally:
            path.write_bytes(original)

        from floati.bus_epoch import roll_bus_epoch

        AuthorityGrantStore(self.root).grant_exact(
            "bus-epoch-roll", "recipient", 1, datetime.now(timezone.utc),
        )
        roll_bus_epoch(self.root, actor="recipient", idempotency_key="archive-work-replay")
        self.assertEqual((0, completed), run_cli_artifact(argv))

    def test_completion_with_consumption_requires_session_before_mutation(self):
        message, _ = self._message()
        item = self._claimed()
        work_path = self.root.resolve_relative("work/items.jsonl")
        before = work_path.read_bytes()
        code, artifact = run_cli_artifact([
            "work", "complete", "--root", str(self.root.path),
            "--id", str(item["id"]), "--as", "recipient",
            "--consume", str(message["id"]),
        ])
        self.assertEqual(20, code, artifact)
        self.assertEqual("refused", artifact["status"])
        self.assertIn("session", json.dumps(artifact["evidence"]))
        self.assertEqual(before, work_path.read_bytes())
        self.assertFalse(self.root.resolve_relative("receipts/consumed/recipient.jsonl").exists())

    def test_completion_consumption_requires_declared_workspace_coordinate(self):
        foreign_root = FloatiRoot.open_direct_home(self.base / "foreign-fleet", create=True)
        for case, declared_root, declared_node in (
            ("node", self.root, "other"),
            ("root", foreign_root, "recipient"),
        ):
            with self.subTest(mismatch=case):
                workspace = self.base / f"declared-{case}"
                workspace.mkdir()
                (workspace / "SEAT.json").write_text(json.dumps({
                    "schema_version": 1, "tenant_id": declared_root.tenant_id,
                    "root": str(declared_root.path), "node_id": declared_node,
                    "topology": "star", "coordinator": "sender",
                    "coordinator_authority": ["dispatch_bounded_work",
                                              "gate_results_before_merge",
                                              "decide_non_owner_tier_questions"],
                    "owner_tier": ["publishing", "credentials", "key_custody"],
                }) + "\n", encoding="utf-8")
                message, _ = self._message(key=f"dispatch-{case}")
                item = self._claimed(subject=f"work-claims-{case}")
                argv = ["work", "complete", "--root", str(self.root.path),
                        "--id", str(item["id"]), "--as", "recipient"]
                work_path = self.root.resolve_relative("work/items.jsonl")
                before = work_path.read_bytes()
                previous = Path.cwd()
                try:
                    os.chdir(workspace)
                    code, artifact = run_cli_artifact(argv + [
                        "--session", "acting-session", "--consume", str(message["id"]),
                    ])
                    self.assertEqual(20, code, artifact)
                    self.assertEqual("workspace_identity_mismatch", artifact["evidence"]["code"])
                    self.assertEqual(before, work_path.read_bytes())
                    self.assertFalse(self.root.resolve_relative("receipts/consumed/recipient.jsonl").exists())
                    legacy_code, legacy = run_cli_artifact(argv)
                    self.assertEqual(0, legacy_code, legacy)
                finally:
                    os.chdir(previous)

    def test_mcp_consume_binds_recipient_and_acting_session(self):
        message, ack = self._message()
        server = McpServer(str(self.root.path), "recipient", "served-session")
        tool = self._tool(server, "consume")
        schema = tool["inputSchema"]
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual({"message_id", "idempotency_key"}, set(schema["properties"]))
        self.assertEqual({"message_id", "idempotency_key"}, set(schema["required"]))
        result = server.call_tool("consume", {
            "message_id": str(message["id"]), "idempotency_key": "mcp-act",
        })
        self.assertNotIn("isError", result, result)
        receipt, = self._history()
        self.assertEqual("recipient", receipt["recipient"])
        self.assertEqual("served-session", receipt["acting_session_id"])
        self.assertEqual(ack["id"], receipt["ack_receipt_id"])

    def test_mcp_consume_rejects_caller_selected_coordinates_without_mutation(self):
        message, _ = self._message()
        server = McpServer(str(self.root.path), "recipient", "served-session")
        self._tool(server, "consume")
        for name, value in (("root", str(self.base / "foreign")),
                            ("recipient", "other"), ("actor", "other"),
                            ("session", "foreign-session")):
            with self.subTest(field=name):
                result = server.call_tool("consume", {
                    "message_id": str(message["id"]), "idempotency_key": "mcp-act",
                    name: value,
                })
                self.assertIs(True, result["isError"])
                self.assertEqual("arguments_invalid", result["structuredContent"]["evidence"]["code"])
        self.assertFalse(self.root.resolve_relative("receipts/consumed/recipient.jsonl").exists())

    def test_mcp_work_completion_consumption_uses_served_session(self):
        message, _ = self._message()
        item = self._claimed()
        server = McpServer(str(self.root.path), "recipient", "served-session")
        schema = self._tool(server, "work_complete")["inputSchema"]
        self.assertNotIn("session", schema["properties"])
        self.assertIn("consume_ids", schema["properties"])
        self.assertEqual("array", schema["properties"]["consume_ids"]["type"])
        result = server.call_tool("work_complete", {
            "item_id": str(item["id"]), "consume_ids": [str(message["id"])],
        })
        self.assertNotIn("isError", result, result)
        receipt, = self._history()
        self.assertEqual("served-session", receipt["acting_session_id"])
        self.assertEqual("work_complete", receipt["source"])
        self.assertEqual(item["id"], receipt["work_item_id"])

    def test_existing_closed_bundle_versions_omit_consumption_ledger(self):
        message, _ = self._message(legacy_export=True)
        code, artifact = run_cli_artifact(self._consume_argv(message["id"]))
        self.assertEqual(0, code, artifact)
        self.assertEqual(1, len(self._history()))
        ConfluenceGrantLedger(self.root).grant("observer", "read-grant")
        for version, schema_name in ((1, "receipts-read-bundle-v1.schema.json"),
                                     (2, "receipts-read-bundle-v2.schema.json")):
            with self.subTest(version=version):
                output = self.base / f"bundle-{version}.json"
                materialize_bundle(self.root, consumer="observer", out=output, version=version)
                bundle = json.loads(output.read_text(encoding="utf-8"))
                validate_json_schema(bundle, REPOSITORY / "schemas/v0" / schema_name)
                self.assertFalse(any(entry["source"].startswith("receipts/consumed/")
                                     or entry["record"]["kind"] == "consumption_receipt"
                                     for entry in bundle["entries"]))
                self.assertTrue(any(entry["record"]["kind"] == "ack_receipt"
                                    for entry in bundle["entries"]))

    def test_current_native_bundle_incompatibility_precedes_and_survives_consumption(self):
        """Characterize the existing export limit without widening its contract."""
        from floati.roster import project_roster

        message, ack = self._message()
        ConfluenceGrantLedger(self.root).grant("observer", "read-grant")
        # Compare both real projections at one observation time. Consumption
        # must leave every field (including the projected ages) unchanged;
        # host scheduling between exports is not part of that property.
        roster_now = datetime.now(timezone.utc)
        def scheduled_roster(root, now):
            return project_roster(root, roster_now)
        roster_clock = mock.patch("floati.roster.project_roster", side_effect=scheduled_roster)
        roster_clock.start()
        self.addCleanup(roster_clock.stop)
        before = {}
        for version in (1, 2):
            output = self.base / f"native-before-{version}.json"
            materialize_bundle(self.root, consumer="observer", out=output, version=version)
            before[version] = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("ack_receipt", ack["kind"])
        self.assertEqual(1, ack["schema_version"])
        self.assertTrue(any(entry["record"]["kind"] == "delivered_unpresented"
                            for entry in before[2]["entries"]))
        code, artifact = run_cli_artifact(self._consume_argv(message["id"]))
        self.assertEqual(0, code, artifact)
        for version in (1, 2):
            with self.subTest(version=version):
                schema = REPOSITORY / f"schemas/v0/receipts-read-bundle-v{version}.schema.json"
                with self.assertRaises(SchemaValidationError) as existing:
                    validate_json_schema(before[version], schema)
                output = self.base / f"native-after-{version}.json"
                materialize_bundle(self.root, consumer="observer", out=output, version=version)
                after = json.loads(output.read_text(encoding="utf-8"))
                if version == 2:
                    before[version]["fleet_state"].pop("observed_at")
                    after["fleet_state"].pop("observed_at")
                self.assertEqual(before[version], after)
                with self.assertRaises(SchemaValidationError) as unchanged:
                    validate_json_schema(after, schema)
                self.assertEqual(str(existing.exception), str(unchanged.exception))
