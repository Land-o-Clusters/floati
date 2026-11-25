from __future__ import annotations

from floati import fixture_ids as public_ids

import argparse
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati.cli import _parser
from floati.command_contract import describe_parser
try:
    from floati.command_contract import project_mcp_surface
except ImportError:
    project_mcp_surface = None
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records
try:
    from floati.mcp import McpServer, run_cli_artifact
except ImportError:
    McpServer = None
    run_cli_artifact = None
from floati.registry import REGISTRY_KINDS, Registry
from floati.work import WorkLog
from floati.root import FloatiRoot
from floati.timings import derive_chart
from tests.temp_roots import REAL_TEMP_ROOT
from tests.banked_sha_fixture import banked_send_repository


SHA = "a" * 40
READ_TOOLS = {
    "checkout-liveness",
    "confluence_bundle",
    "confluence_status",
    "describe",
    "doctor",
    "effects",
    "graph",
    "intake_scan",
    "intake_show",
    "intake_preview",
    "log",
    "needs_admission",
    "needs_show",
    "quiet_show",
    "quiet_status",
    "receipts",
    "status",
    "who",
    "work_handoff_show",
    "dispatch_show",
}
GOVERNED_TOOLS = {
    "ack",
    "ask",
    "consume",
    "confluence_grant",
    "confluence_revoke",
    "inbox",
    "needs_close",
    "needs_file",
    "send",
    "wake_pause",
    "wake_resume",
    "work_claim",
    "work_complete",
}


def leaf_parser(
    parser: argparse.ArgumentParser,
    path: tuple[str, ...],
) -> argparse.ArgumentParser:
    current = parser
    for command in path:
        action = next(
            item
            for item in current._actions
            if isinstance(item, argparse._SubParsersAction)
        )
        current = action.choices[command]
    return current


class McpToolSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        cls.addClassCleanup(temporary.cleanup)
        cls.send_repository, cls.sha = banked_send_repository(Path(temporary.name))

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet",
            create=True,
        )
        self.registry = Registry(self.root)
        self.registry.register(public_ids.builder('a'), "Codex")
        self.registry.register(public_ids.builder('b'), "Codex")

    def server(self, node: str = public_ids.builder('a')):
        self.assertIsNotNone(McpServer, "floati.mcp.McpServer must exist")
        return McpServer(str(self.root.path), node, "session-a")

    def surface(self, parser: argparse.ArgumentParser | None = None) -> dict[str, object]:
        self.assertIsNotNone(
            project_mcp_surface,
            "command_contract.project_mcp_surface must exist",
        )
        return project_mcp_surface(_parser() if parser is None else parser)

    def tool(self, name: str, *, server=None) -> dict[str, object]:
        selected = self.server() if server is None else server
        return next(tool for tool in selected.list_tools() if tool["name"] == name)

    def send_arguments(self, *, note: str, key: str) -> dict[str, object]:
        return {
            "recipient": public_ids.builder('b'),
            "repo": "floati",
            "sha": self.sha,
            "doc": "note.md",
            "note": note,
            "idempotency_key": key,
        }

    def use_send_repository(self) -> None:
        # MCP binds the repository to the server's working directory.
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.send_repository)

    def assert_tool_error(self, result: dict[str, object], code: str) -> dict[str, object]:
        self.assertIs(True, result["isError"])
        artifact = result["structuredContent"]
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(code, artifact["evidence"]["code"])
        self.assertEqual(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            result["content"][0]["text"],
        )
        return artifact

    def test_dispatch_authoring_refuses_invalid_delivery_branches(self) -> None:
        from floati.dispatch_records import DispatchRecords
        from floati.errors import ProtocolRefusal

        brief = Path(self.temporary.name) / "brief.txt"
        brief.write_text("Review the proposal.\n", encoding="utf-8")
        records = DispatchRecords(self.root)
        sender, recipient = public_ids.builder("a"), public_ids.builder("b")
        for index, branch in enumerate(("review..v2", "review/@{1}", "review.lock")):
            with self.subTest(branch=branch):
                with self.assertRaises(ProtocolRefusal) as refusal:
                    records.record(f"review-{index}", sender, "initial", recipient, branch, brief,
                                   external="review request")
                self.assertEqual("dispatch_branch_invalid", refusal.exception.code)
        self.assertEqual([], records.show()["records"])

        authored = records.record("review", sender, "initial", recipient, "review/v2", brief,
                                  external="review request")
        self.assertEqual("review/v2", authored["branch"])

    def test_older_invalid_delivery_branch_can_be_read_and_superseded(self) -> None:
        from floati.dispatch_records import DIRECTORY, DispatchRecords

        brief = Path(self.temporary.name) / "brief.txt"
        brief.write_text("Review the proposal.\n", encoding="utf-8")
        records = DispatchRecords(self.root)
        sender, recipient = public_ids.builder("a"), public_ids.builder("b")
        revision = records.record("review", sender, "initial", recipient, "review", brief,
                                  external="review request")
        historical = dict(revision, branch="review..v2")
        journal = self.root.tenant_home / DIRECTORY / "review.jsonl"
        journal.write_text(json.dumps(historical) + "\n", encoding="utf-8")

        shown = records.show("review")
        self.assertEqual([historical], [entry["record"] for entry in shown["records"]], shown)
        correction = records.record("review", sender, "correction", recipient, "review/v2", brief,
                                    external="review request", supersedes=revision["id"])
        self.assertEqual((2, "review/v2", revision["id"]),
                         (correction["revision"], correction["branch"], correction["supersedes"]))

    def test_tool_set_is_derived_from_parser_exposure_tags(self) -> None:
        """Catches a hand-maintained allowlist drifting from live parser tags."""

        tools = self.server().list_tools()

        self.assertEqual(READ_TOOLS | GOVERNED_TOOLS, {tool["name"] for tool in tools})
        for tool in tools:
            self.assertNotIn("DRAFT", tool["description"])
            self.assertNotEqual(tool["description"], "floati " + tool["name"].replace("_", " "))
        self.assertEqual("approved", self.surface()["copy_state"])

    def test_ask_returns_the_correlated_mcp_answer_receipt(self) -> None:
        from floati.ask import wait_for_question

        self.use_send_repository()
        asker = self.server()
        respondent = self.server(public_ids.builder("b"))
        self.assertIn("ask", {tool["name"] for tool in asker.list_tools()})
        arguments = self.send_arguments(note="Can you review this?", key="mcp-question")

        def answer_then_observe(root, question, wait_seconds, *, stop_waiting=None):
            reply = respondent.call_tool("ask", {
                **arguments, "recipient": public_ids.builder("a"),
                "idempotency_key": "mcp-answer", "reply_to": question["id"],
                "disposition": "answer", "note": "--help",
            })["structuredContent"]
            self.assertEqual("ok", reply["status"], reply)
            return wait_for_question(root, question, wait_seconds, stop_waiting=stop_waiting)

        with patch("floati.ask.wait_for_question", side_effect=answer_then_observe):
            result = asker.call_tool("ask", {**arguments, "wait_seconds": 0})

        self.assertEqual("ok", result["structuredContent"]["status"], result)
        evidence = result["structuredContent"]["evidence"]
        records = {row["id"]: row for row in EventLog(self.root).event_records()}
        self.assertEqual("answered", evidence["outcome"])
        question = records[evidence["question_id"]]
        answer = records[evidence["answer_id"]]
        self.assertEqual(public_ids.builder("a"), question["sender"])
        self.assertEqual(public_ids.builder("b"), answer["sender"])
        self.assertEqual(question["id"], answer["reply_to"])
        self.assertEqual("--help", evidence["answer_note"])
        chart = derive_chart(self.root, command="mcp ask")
        self.assertEqual(2, chart["commands"]["mcp ask"]["count"])

    def test_who_returns_the_bound_roster_and_receipts_the_read(self) -> None:
        result = self.server().call_tool("who", {})["structuredContent"]
        self.assertEqual("ok", result["status"], result)
        rows = {row["node_id"]: row for row in result["evidence"]["roster"]}
        self.assertEqual({public_ids.builder("a"), public_ids.builder("b")}, set(rows))
        self.assertEqual("UNKNOWN", rows[public_ids.builder("b")]["liveness"]["stamp"])
        chart = derive_chart(self.root, command="mcp who")
        self.assertEqual(1, chart["commands"]["mcp who"]["count"])

    def test_unanswered_mcp_question_returns_the_durable_timeout(self) -> None:
        self.use_send_repository()
        arguments = self.send_arguments(note="Can you review this?", key="mcp-timeout")
        result = self.server().call_tool("ask", {**arguments, "wait_seconds": 0})
        artifact = result["structuredContent"]
        self.assertIs(True, result["isError"])
        self.assertEqual("orchestration_deadline", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual(("UNKNOWN", "ask_timeout"), (evidence["outcome"], evidence["code"]))
        rows = {row["id"]: row for row in EventLog(self.root).event_records()}
        self.assertEqual(evidence["question_id"], rows[evidence["timeout_id"]]["reply_to"])

    def test_message_tools_refuse_approval_inputs_and_receipt_the_refusal(self) -> None:
        from floati.approvals import ApprovalLedger

        self.use_send_repository()
        server = self.server()
        for name, extra in (
            ("ask", {"disposition": "approve", "wait_seconds": 0}),
            ("send", {"approval_decision_id": "approve"}),
        ):
            with self.subTest(tool=name):
                arguments = self.send_arguments(note="Approve this.", key="mcp-approval")
                self.assert_tool_error(server.call_tool(name, {**arguments, **extra}),
                                       "arguments_invalid")
                self.assertEqual([], EventLog(self.root).event_records())
                chart = derive_chart(self.root, command=f"mcp {name}")
                self.assertEqual(1.0, chart["commands"][f"mcp {name}"]["refused_share"])

        arguments = self.send_arguments(note="I approve this.", key="mcp-peer-note")
        result = server.call_tool("send", arguments)["structuredContent"]
        self.assertEqual("ok", result["status"], result)
        self.assertEqual(arguments["note"], result["evidence"]["message"]["note"])
        approvals = ApprovalLedger(self.root)
        self.assertEqual([], read_records(self.root, approvals.decision_path,
                                         allowed_kinds={"approval_decision"}))

    def test_handoff_reader_reports_absent_evidence_for_the_bound_root(self) -> None:
        item = WorkLog(self.root).add("Prepare review", public_ids.builder("a"), [])
        server = self.server()
        result = server.call_tool("work_handoff_show", {"item_id": item["id"], "json": True})
        self.assertNotIn("isError", result)
        evidence = result["structuredContent"]["evidence"]
        self.assertEqual(("bound-record", 1, [], "none"),
                         (evidence["contract"], evidence["contract_version"], evidence["records"],
                          evidence["evidence_state"]))
        self.assertNotIn("work_handoff_record", {tool["name"] for tool in server.list_tools()})

    def test_dispatch_mcp_reads_the_roster_without_serving_briefs_or_writes(self) -> None:
        from floati.dispatch_records import DispatchRecords
        from floati.replay_history import project_asked

        sender, recipient = public_ids.builder("a"), public_ids.builder("b")
        envelope = EventLog(self.root).send(sender, recipient, "floati", self.sha,
                                            "README.md", "Review the proposal.")["message"]
        brief = self.root.path.parent / "brief.txt"
        secret = "Local context for the reviewer only.\n"
        brief.write_text(secret, encoding="utf-8")
        revision = DispatchRecords(self.root).record(
            "review", sender, "initial", recipient, "review", brief, envelope_id=envelope["id"],
        )
        server = self.server()
        result = server.call_tool("dispatch_show", {"dispatch_id": "review", "json": True})
        self.assertNotIn("isError", result)
        evidence = result["structuredContent"]["evidence"]
        self.assertEqual(revision, evidence["records"][0]["record"])
        self.assertEqual("observed", revision["carrier"]["provenance"])
        asked = project_asked(revision)
        self.assertEqual((sender, revision["id"], 1), (asked["node"], asked["receipt_id"], asked["revision"]))
        self.assertEqual(asked, evidence["roster"][0]["asked"])
        self.assertNotIn(secret.strip(), json.dumps(result))
        messages = EventLog(self.root).records()
        self.assertEqual([envelope["id"]], [message["id"] for message in messages])
        self.assertNotIn(secret.strip(), json.dumps(messages))
        self.assertEqual({"dispatch_show"}, {t["name"] for t in server.list_tools()
                                              if t["name"].startswith("dispatch_")})

    def test_dispatch_mcp_keeps_known_history_visible_when_a_future_row_adds_fields(self) -> None:
        from floati.dispatch_records import DIRECTORY, DispatchRecords

        brief = Path(self.temporary.name) / "brief.txt"
        brief.write_text("Review the proposal.\n", encoding="utf-8")
        sender, recipient = public_ids.builder("a"), public_ids.builder("b")
        revision = DispatchRecords(self.root).record(
            "review", sender, "initial", recipient, "review", brief, external="review request",
        )
        future_id = "dispatch-revision-" + uuid7_hex()
        future = dict(revision, id=future_id, schema_version=2, revision=2,
                      idempotency_key="follow-up", supersedes=revision["id"], audience="reviewers")
        journal = self.root.tenant_home / DIRECTORY / "review.jsonl"
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(future) + "\n")

        result = self.server().call_tool("dispatch_show", {"dispatch_id": "review", "json": True})
        self.assertNotIn("isError", result)
        evidence = result["structuredContent"]["evidence"]
        self.assertEqual([revision], [entry["record"] for entry in evidence["records"]], evidence)
        self.assertEqual("unknown", evidence["records"][0]["freshness"]["state"])
        self.assertIsNone(evidence["records"][0]["chain"]["head"])
        self.assertEqual(
            [{"dispatch_id": "review", "record_id": future_id, "code": "schema_version_invalid"}],
            evidence["refusals"],
        )

    def test_status_pulls_current_fleet_presence_without_hook_participation(self) -> None:
        outside_reader = self.server(public_ids.builder("b"))
        cli_status = ["status", "--root", str(self.root.path), "--json"]
        for mode in ("UNDECLARED", "AWAY", "PRESENT"):
            with self.subTest(mode=mode):
                if mode != "UNDECLARED":
                    code, declaration = run_cli_artifact([
                        "presence", "set", "--root", str(self.root.path),
                        "--as", public_ids.builder("a"), "--mode", mode,
                        "--idempotency-key", "operator-" + mode.lower(),
                    ])
                    self.assertEqual(0, code, declaration)
                code, cli = run_cli_artifact(cli_status)
                self.assertEqual(0, code, cli)
                result = outside_reader.call_tool("status", {})
                self.assertFalse(result.get("isError", False), result)
                observed = result["structuredContent"]["evidence"]["fleet_presence"]
                self.assertEqual(mode, observed["mode"])
                self.assertEqual(cli["evidence"]["fleet_presence"], observed)
                if mode != "UNDECLARED":
                    self.assertEqual(declaration["evidence"], observed)

    def test_exposure_mutation_changes_both_generated_tool_and_deny_sets(self) -> None:
        """Catches the server ignoring a parser exposure mutation or shadowing deny truth."""

        parser = _parser()
        before = self.surface(parser)
        described = describe_parser(parser)
        expected_denied = {
            tuple(row["path"])
            for row in described["commands"]
            if row["executable"] and row["mcp_exposure"] == "never"
        }
        self.assertEqual(
            expected_denied,
            {tuple(path) for path in before["denied_paths"]},
        )

        leaf_parser(parser, ("worker", "run")).floati_mcp_exposure = "governed"
        after = self.surface(parser)

        self.assertNotIn(("worker", "run"), {tuple(path) for path in after["denied_paths"]})
        self.assertIn("worker_run", {tool["name"] for tool in after["tools"]})

    def test_input_schemas_are_closed_and_derived_from_argparse_actions(self) -> None:
        """Catches bound fields, wrong scalar types, choices, or repeats in MCP schemas."""

        send = self.tool("send")["inputSchema"]
        self.assertFalse({"root", "sender", "actor", "session", "repository"} & set(send["properties"]))
        self.assertIn("recipient", send["properties"])
        self.assertEqual(
            {"recipient", "repo", "sha", "doc", "note", "idempotency_key"},
            set(send["required"]),
        )
        self.assertIs(False, send["additionalProperties"])

        status = self.tool("status")["inputSchema"]
        self.assertNotIn("root", status["properties"])
        self.assertNotIn("json", status["properties"])
        doctor = self.tool("doctor")["inputSchema"]
        self.assertEqual("number", doctor["properties"]["probe_budget"]["type"])
        self.assertEqual("boolean", doctor["properties"]["probe"]["type"])
        receipts = self.tool("receipts")["inputSchema"]
        self.assertEqual(["node"], receipts["required"])
        inbox = self.tool("inbox")["inputSchema"]
        self.assertEqual({}, inbox["properties"])
        self.assertEqual([], inbox["required"])

        parser = _parser()
        leaf_parser(parser, ("worker", "run")).floati_mcp_exposure = "governed"
        leaf_parser(parser, ("work", "add")).floati_mcp_exposure = "governed"
        generated = {tool["name"]: tool for tool in self.surface(parser)["tools"]}
        self.assertEqual(
            ["claude", "codex", "pi"],
            generated["worker_run"]["inputSchema"]["properties"]["adapter"]["enum"],
        )
        self.assertEqual(
            {"type": "array", "items": {"type": "string"}, "default": []},
            generated["work_add"]["inputSchema"]["properties"]["needs"],
        )

    def test_log_exposes_only_its_single_artifact_path(self) -> None:
        """Catches replay presentation frames escaping the MCP artifact boundary."""

        server = self.server()
        schema = self.tool("log", server=server)["inputSchema"]

        self.assertEqual({}, schema["properties"])
        result = server.call_tool("log", {})
        self.assertEqual("log", result["structuredContent"]["command"])
        self.assertEqual("no_result", result["structuredContent"]["status"])
        self.assertNotIn("isError", result)

    def test_bound_identity_fields_are_absent_and_rejected(self) -> None:
        """Catches a tool call switching root, actor, sender, recipient identity, or session."""

        cases = (
            ("send", "root", str(self.root.path / "other")),
            ("send", "sender", public_ids.builder('b')),
            ("work_claim", "actor", public_ids.builder('b')),
            ("ack", "recipient", public_ids.builder('b')),
            ("ack", "session", "session-b"),
            ("wake_pause", "actor", public_ids.builder('b')),
        )
        server = self.server()
        for tool_name, field, value in cases:
            with self.subTest(tool=tool_name, field=field):
                schema = self.tool(tool_name, server=server)["inputSchema"]
                self.assertNotIn(field, schema["properties"])
                self.assert_tool_error(
                    server.call_tool(tool_name, {field: value}),
                    "arguments_invalid",
                )

    def test_deeper_refusals_never_name_cli_flags(self) -> None:
        """MCP-2-F1: a valid-shape call that fails inside the CLI command is
        re-shaped to the tool's input names at the boundary - code kept,
        remedy non-null, no flag survives to the caller."""

        server = self.server()
        work = WorkLog(self.root)
        item = work.add("the capture item", public_ids.builder('a'), [])

        result = server.call_tool("work_claim", {
            "item_id": item["id"],
            "authority_subject": "capture-live",
            "authority_epoch": 1,
        })
        evidence = self.assert_tool_error(result, "authority_missing")["evidence"]
        self.assertNotIn("--", json.dumps(evidence))
        self.assertIsInstance(evidence["remedy"], str)
        self.assertTrue(evidence["remedy"])

        self.use_send_repository()
        provided = self.send_arguments(note="n" * 1100, key="mcp2f1-send")
        evidence = self.assert_tool_error(
            server.call_tool("send", provided), "note_invalid"
        )["evidence"]
        self.assertNotIn("--", json.dumps(evidence))
        self.assertEqual("shorten note to at most 1024 characters", evidence["remedy"])

    def test_mcp_refusals_name_mcp_inputs_never_cli_flags(self) -> None:
        """MCP-2 (public #24): catches CLI vocabulary leaking into tool refusals."""

        server = self.server()
        schema = self.tool("send", server=server)["inputSchema"]

        artifact = self.assert_tool_error(
            server.call_tool("send", {}), "arguments_invalid"
        )
        detail = artifact["evidence"]["detail"]
        self.assertNotIn("--", detail)
        self.assertNotIn("choose from", detail)
        for name in sorted(schema["required"]):
            self.assertIn(name, detail)

        provided = self.send_arguments(note="x", key="k")
        provided["bogus_input"] = 1
        artifact = self.assert_tool_error(
            server.call_tool("send", provided), "arguments_invalid"
        )
        detail = artifact["evidence"]["detail"]
        self.assertNotIn("--", detail)
        self.assertIn("bogus_input", detail)

        artifact = self.assert_tool_error(
            server.call_tool("not_a_tool", {}), "arguments_invalid"
        )
        detail = artifact["evidence"]["detail"]
        self.assertNotIn("choose from", detail)
        self.assertIn("not_a_tool", detail)

    def test_mcp_keyed_governed_tools_refuse_without_a_caller_key(self) -> None:
        """Catches MCP inheriting the CLI's human-only idempotency-key mint."""

        server = self.server()
        send = self.send_arguments(note="missing key", key="discarded")
        del send["idempotency_key"]
        cases = (
            ("send", send),
            ("wake_pause", {}),
            ("wake_resume", {}),
        )
        for tool_name, arguments in cases:
            with self.subTest(tool=tool_name):
                schema = self.tool(tool_name, server=server)["inputSchema"]
                self.assertIn("idempotency_key", schema["required"])
                self.assert_tool_error(
                    server.call_tool(tool_name, arguments),
                    "arguments_invalid",
                )
        self.assertEqual([], EventLog(self.root).records())
        self.assertFalse(
            (self.root.path / "receipts" / "wake-control").exists()
        )

    def test_supplied_wake_key_survives_cli_and_mcp_artifacts(self) -> None:
        """Catches either transport replacing the caller's wake-control key."""

        direct_exit, direct = run_cli_artifact([
            "wake", "pause",
            "--root", str(self.root.path),
            "--as", public_ids.builder('a'),
            "--session", "session-direct",
            "--idempotency-key", "direct-pause-key",
        ])
        server = self.server()
        paused = server.call_tool(
            "wake_pause",
            {"idempotency_key": "mcp-pause-key"},
        )
        resumed = server.call_tool(
            "wake_resume",
            {"idempotency_key": "mcp-resume-key"},
        )

        self.assertEqual(0, direct_exit)
        self.assertEqual(
            "direct-pause-key",
            direct["evidence"]["receipt"]["idempotency_key"],
        )
        self.assertEqual(
            "mcp-pause-key",
            paused["structuredContent"]["evidence"]["receipt"]["idempotency_key"],
        )
        self.assertEqual(
            "mcp-resume-key",
            resumed["structuredContent"]["evidence"]["receipt"]["idempotency_key"],
        )

    def test_human_cli_minted_wake_key_is_echoed_in_the_artifact(self) -> None:
        """Catches human convenience silently minting an unobservable replay key."""

        exit_code, artifact = run_cli_artifact([
            "wake", "pause",
            "--root", str(self.root.path),
            "--as", public_ids.builder('a'),
            "--session", "session-human",
        ])

        self.assertEqual(0, exit_code)
        self.assertTrue(
            artifact["evidence"]["receipt"]["idempotency_key"].startswith(
                "wake-cli-pause-"
            )
        )

    def test_call_routes_through_exact_cli_artifact_and_deduplicates_across_servers(self) -> None:
        """Catches MCP inventing output, identity, or process-local idempotency truth."""

        self.assertIsNotNone(run_cli_artifact, "floati.mcp.run_cli_artifact must exist")
        self.use_send_repository()
        arguments = self.send_arguments(note="n" * 1024, key="mcp-deduplicate")
        direct_exit, direct_artifact = run_cli_artifact([
            "send",
            "--root", str(self.root.path),
            "--from", public_ids.builder('a'),
            "--to", public_ids.builder('b'),
            "--repo", "floati",
            "--sha", self.sha,
            "--doc", "note.md",
            "--note", "n" * 1024,
            "--idempotency-key", "mcp-deduplicate",
        ])
        first = self.server().call_tool("send", arguments)
        second = self.server().call_tool("send", arguments)

        self.assertEqual(0, direct_exit)
        self.assertEqual(direct_artifact, first["structuredContent"])
        self.assertEqual(first, second)
        chart = derive_chart(self.root, command="mcp send")
        self.assertEqual(2, chart["commands"]["mcp send"]["count"])
        self.assertNotIn("isError", first)
        self.assertEqual(
            public_ids.builder('a'),
            direct_artifact["evidence"]["message"]["sender"],
        )
        self.assertEqual(
            "unknown",
            direct_artifact["evidence"]["recipient_readiness"]["state"],
        )
        self.assertEqual("mechanism_evidence_incomplete",
            direct_artifact["evidence"]["recipient_readiness"]["reason"])
        hook = next(fact for fact in direct_artifact["evidence"]["recipient_readiness"]["mechanism_evidence"]
                    if fact["mechanism"] == "stop_hook")
        self.assertEqual("unknown", hook["state"])
        self.assertEqual(1, len(EventLog(self.root).records()))
        # Replaying across native/MCP servers retains one envelope and exactly
        # its frozen companion observation, rather than another readiness poll.
        rows = EventLog(self.root).event_records()
        self.assertEqual(["message_envelope", "send_readiness_observation"],
                         [row["kind"] for row in rows])
        self.assertEqual(rows[0]["id"], rows[1]["envelope_id"])
        self.assertEqual(direct_artifact["evidence"]["recipient_readiness"],
                         rows[1]["readiness"])

    def test_note_cap_refuses_with_exact_cli_artifact_parity(self) -> None:
        """Catches MCP weakening the CLI note cap or translating its refusal."""

        self.assertIsNotNone(run_cli_artifact, "floati.mcp.run_cli_artifact must exist")
        self.use_send_repository()
        note = "n" * 1025
        direct_exit, direct_artifact = run_cli_artifact([
            "send",
            "--root", str(self.root.path),
            "--from", public_ids.builder('a'),
            "--to", public_ids.builder('b'),
            "--repo", "floati",
            "--sha", self.sha,
            "--doc", "note.md",
            "--note", note,
            "--idempotency-key", "mcp-too-long",
        ])
        result = self.server().call_tool(
            "send",
            self.send_arguments(note=note, key="mcp-too-long"),
        )

        self.assertEqual(20, direct_exit)
        served = result["structuredContent"]
        self.assertEqual(direct_artifact["evidence"]["code"], served["evidence"]["code"])
        self.assertEqual(direct_artifact["evidence"]["detail"], served["evidence"]["detail"])
        self.assertIn("--note", direct_artifact["evidence"]["remedy"])
        self.assertNotIn("--", served["evidence"]["remedy"])
        self.assertEqual([], EventLog(self.root).records())

    def test_dict_remedy_with_a_real_action_keeps_that_action(self) -> None:
        """MCP-2-F1 Am.1: RefusalRemedy is Union[str, dict] and REM-1 binds
        dict remedies - the boundary re-speaks a dict remedy field by field;
        only the unnamed placeholder (kind none) becomes the generic remedy."""

        server = self.server()
        reshaped = server._reshape_refusal(
            {
                "artifact_version": 0,
                "command": "send",
                "status": "refused",
                "evidence": {
                    "code": "note_invalid",
                    "detail": "the note exceeds its cap",
                    "remedy": {
                        "kind": "retry",
                        "action": "shorten --note to at most 1024 characters",
                    },
                },
            },
            ("send",),
        )
        self.assertEqual(
            {
                "kind": "retry",
                "action": "shorten note to at most 1024 characters",
            },
            reshaped["evidence"]["remedy"],
        )

        unnamed = server._reshape_refusal(
            {
                "artifact_version": 0,
                "command": "send",
                "status": "refused",
                "evidence": {
                    "code": "authority_missing",
                    "detail": "no grant names this holder",
                    "remedy": {"kind": "none", "why": "no action was named for this refusal"},
                },
            },
            ("send",),
        )
        self.assertEqual(
            "correct the condition named in detail and retry the tool call",
            unnamed["evidence"]["remedy"],
        )

    def test_retirement_is_rechecked_for_listing_and_every_governed_call(self) -> None:
        """Catches process-cached liveness preserving authority after retirement."""

        server = self.server()
        self.assertEqual(
            GOVERNED_TOOLS,
            {tool["name"] for tool in server.list_tools()} - READ_TOOLS,
        )
        self.registry.retire(public_ids.builder('a'))

        self.assertEqual(READ_TOOLS, {tool["name"] for tool in server.list_tools()})
        wake_exit, wake_artifact = run_cli_artifact([
            "wake", "pause",
            "--root", str(self.root.path),
            "--as", public_ids.builder('a'),
            "--session", "session-a",
            "--idempotency-key", "retired-wake",
        ])
        wake_result = server.call_tool(
            "wake_pause",
            {"idempotency_key": "retired-wake"},
        )
        self.assertEqual(20, wake_exit)
        self.assertEqual("unknown_node", wake_artifact["evidence"]["code"])
        self.assert_tool_error(wake_result, "unknown_node")
        self.assertIn("node add", str(wake_result["structuredContent"]["evidence"]["remedy"]))
        self.use_send_repository()
        arguments = self.send_arguments(
            note="after retirement",
            key="retired-send",
        )
        direct_exit, direct_artifact = run_cli_artifact([
            "send",
            "--root", str(self.root.path),
            "--from", public_ids.builder('a'),
            "--to", public_ids.builder('b'),
            "--repo", "floati",
            "--sha", self.sha,
            "--doc", "docs/evidence/mcp-tools.md",
            "--note", "after retirement",
            "--idempotency-key", "retired-send",
        ])
        result = server.call_tool(
            "send",
            arguments,
        )
        self.assertEqual(20, direct_exit)
        self.assertEqual("refused", direct_artifact["status"])
        self.assert_tool_error(result, "unknown_node")
        self.assertIn("node add", str(result["structuredContent"]["evidence"]["remedy"]))
        self.assertEqual([], EventLog(self.root).records())

    def _root_record_bytes(self) -> dict[str, bytes]:
        records: dict[str, bytes] = {}
        for path in self.root.path.rglob("*"):
            if not path.is_file() or path.suffix != ".jsonl":
                continue
            records[path.relative_to(self.root.path).as_posix()] = path.read_bytes()
        return records

    def _listed_tool_arguments(self, tool: dict[str, object]) -> dict[str, object]:
        name = str(tool["name"])
        required = list(tool["inputSchema"].get("required") or [])
        values = {
            "consumer": "reader-a",
            "idempotency_key": f"expo-{name}",
            "out": str(Path(self.temporary.name) / "confluence-bundle.json"),
            "source": str(self.root.path),
            "directory": str(self.temporary.name),
            "path": str(self.temporary.name),
            "snapshot_id": "snap-00000000000000000000000000000000",
            "item_id": "work-00000000000070008000000000000000",
            "operation": "comment",
            "node": public_ids.builder("a"),
        }
        return {field: values[field] for field in required}

    def test_inactive_listed_tools_leave_the_root_record_set_unmoved(self) -> None:
        """Catches a writer remaining on the inactive list_tools surface."""

        self.registry.retire(public_ids.builder("a"))
        server = self.server()
        mutated: list[str] = []
        for tool in server.list_tools():
            before = self._root_record_bytes()
            server.call_tool(str(tool["name"]), self._listed_tool_arguments(tool))
            after = self._root_record_bytes()
            if after != before:
                mutated.append(str(tool["name"]))
        # These reads write invocation timing receipts only.
        self.assertEqual(["who", "doctor"], mutated)
        rows = read_records(
            self.root,
            Path("receipts/timings/doctor.jsonl"),
            allowed_kinds={"timing_receipt"},
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("doctor", rows[-1]["command"])
        self.assertIn(
            rows[-1]["outcome"],
            {"ok", "refused", "degraded", "no_result", "error"},
        )

    def test_inactive_call_tool_refuses_every_tool_absent_from_list_tools(self) -> None:
        """Catches list_tools filtering a name that call_tool still runs."""

        server = self.server()
        catalog = {str(tool["name"]) for tool in server.list_tools()}
        self.registry.retire(public_ids.builder("a"))
        listed = {str(tool["name"]) for tool in server.list_tools()}
        absent = catalog - listed
        self.assertTrue(absent)
        for name in sorted(absent):
            with self.subTest(tool=name):
                artifact = self.assert_tool_error(server.call_tool(name, {}), "unknown_node")
                self.assertIn("node add", str(artifact["evidence"]["remedy"]))

    def test_expired_launch_lease_refuses_writes_without_hiding_tools(self) -> None:
        """Catches a long-running MCP connection keeping expired write authority."""

        expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        append_record(
            self.root,
            self.registry.relative_path,
            {
                "schema_version": 0,
                "id": "lease-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": (expired_at - timedelta(minutes=1)).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "kind": "node_lease",
                "node_id": public_ids.builder('a'),
                "workspace": str(self.root.path / "nodes" / public_ids.builder('a')),
                "expires_at": expired_at.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                "state": "active",
            },
            allowed_kinds=REGISTRY_KINDS,
        )
        server = self.server()

        self.assertTrue({"ask", "send"} <= {tool["name"] for tool in server.list_tools()})
        self.use_send_repository()
        for name, extra in (("send", {}), ("ask", {"wait_seconds": 0})):
            with self.subTest(tool=name):
                args = self.send_arguments(note="after expiry", key=f"expired-{name}")
                result = server.call_tool(name, {**args, **extra})
                artifact = self.assert_tool_error(result, "node_lease_expired")
                self.assertIsInstance(artifact["evidence"]["remedy"], str)
                self.assertTrue(artifact["evidence"]["remedy"])
                self.assertEqual([], EventLog(self.root).event_records())


if __name__ == "__main__":
    unittest.main()
