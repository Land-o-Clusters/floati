"""NET-HOLD-INBOUND-1: a held item is never presented before a receipted human approval.

The row (docs/status/FLOATI-NETWORK-2026-10-16.md section 2): an operator can
hold a node's inbound mail, then approve or reject each item; a held item is
never presented to the agent before approval, and the approval is a receipted
human act (R5.1's shape). No module in tests/ owned inbound presentation
gating, so this one does: it states the property once per presenter.

The presenter population is derived from the code, not listed here: every
function that builds a record of a presentation kind (the delivery-plane
kinds plus the conversation presentation kind) must reach an inbound-hold
gate, itself or through every caller. The behavioural cases then drive each
presentation primitive those presenters share, with one ordinary message
(sent before the hold, so the path is shown to present) and one held message.

The hold records in the behavioural cases are written as the exact bytes the
operator verb writes, so they fail on the property and never on an import.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tests  # noqa: F401  Pin HOME before any product import resolves a path.

from floati import fixture_ids
from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.waiter_consent import arm_waiter_consent
from tests.temp_roots import REAL_TEMP_ROOT
from tests.waiter_timing import scripted_readiness


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOLD_LEDGER = Path("receipts/inbound-holds")
DELIVERY_KINDS = {"delivery_receipt", "delivered_unpresented", "wake_hold_receipt"}
# A hang guard for the interactive child, never a timing assertion: every
# wait below returns on the event (prompt text, EOF) and reports its load.
CHILD_HANG_GUARD_SECONDS = 120.0


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _envelope_digest(message: dict) -> str:
    return hashlib.sha256(encode_frame(message)).hexdigest()


class Scenario:
    """One fresh root: an ordinary message before the hold, a held one inside it."""

    def __init__(self, base: Path, label: str) -> None:
        self.base = base / label
        self.base.mkdir()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node, self.sender = fixture_ids.builder("a"), fixture_ids.builder("b")
        Registry(self.root).register(self.node, "claude")
        Registry(self.root).register(self.sender, "claude")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        arm_waiter_consent(self.root.path, self.workspace, self.node, "claude", 10, 2)
        self.session = "inbound-hold-session"
        start = datetime.now(timezone.utc) - timedelta(seconds=30)
        self.ordinary = self.send("ordinary", start)
        self.hold_id = self.write_hold(start + timedelta(seconds=1))
        self.held = self.send("held", start + timedelta(seconds=2))

    def send(self, key: str, moment: datetime | None = None) -> dict:
        return EventLog(self.root).send(
            self.sender, self.node, "floati", "a" * 40, "docs/CONFLUENCE-v0.md",
            "note " + key, idempotency_key=key,
            now=moment or datetime.now(timezone.utc))["message"]

    def _append(self, row: dict) -> None:
        path = self.root.tenant_home / HOLD_LEDGER / (self.node + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            stream.write(encode_frame(row))

    def write_hold(self, started: datetime) -> str:
        """The exact version-one start row the operator verb appends."""
        ident = "inbound-hold-" + uuid7_hex()
        self._append({
            "schema_version": 1, "id": ident, "tenant_id": self.root.tenant_id,
            "timestamp": _iso(started), "kind": "inbound_hold_receipt",
            "node_id": self.node, "phase": "held", "hold_id": None,
            "reason": "operator review of inbound mail", "started_at": _iso(started),
            "expected_release": None, "released_at": None, "message_id": None,
            "envelope_digest": None, "decided_at": None, "human_act": None,
            "idempotency_key": "inbound-hold-start-" + ident[-12:],
        })
        return ident

    def delivered_ids(self) -> set:
        rows = read_records_snapshot(
            self.root, SparseCursor(self.root)._delivery_relative_path_for(self.node),
            allowed_kinds=set(DELIVERY_KINDS))
        return {item for row in rows for item in row.get("item_ids", ())}

    def hold_rows(self) -> list:
        path = self.root.tenant_home / HOLD_LEDGER / (self.node + ".jsonl")
        return decode_frames(path.read_bytes()) if path.exists() else []


def _relay(scenario: Scenario) -> str:
    from floati.claude_hook_relay import run_relay
    output = io.StringIO()
    run_relay(bus_home=scenario.root.path, node=scenario.node, runtime=REPOSITORY_ROOT,
              hook_payload={"cwd": str(scenario.workspace), "session_id": scenario.session,
                            "hook_event_name": "PostToolUse"},
              stdout=output, clock=lambda: 1000.0)
    return output.getvalue()


def _stop_waiter(scenario: Scenario) -> str:
    from floati.codex_wait import run_stop_waiter
    clock = [0.0]
    output = io.StringIO()
    run_stop_waiter(
        bus_home=scenario.root.path,
        hook_payload={"cwd": str(scenario.workspace), "session_id": scenario.session,
                      "hook_event_name": "Stop"},
        stdout=output, stderr=io.StringIO(), monotonic=lambda: clock[0],
        wait_readable=scripted_readiness(
            lambda seconds: clock.__setitem__(0, clock[0] + seconds), interval=1.0),
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return output.getvalue()


def _wake_evaluation(scenario: Scenario) -> str:
    from floati.wake_hold import WakeHoldController
    decision = WakeHoldController(scenario.root).evaluate(
        scenario.node, idempotency_key="inbound-hold-wake")
    return json.dumps(decision.get("fresh_messages"), sort_keys=True)


def _inbox_cli(scenario: Scenario) -> str:
    import contextlib
    from floati.cli import main
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        try:
            main(["inbox", "--root", str(scenario.root.path), "--as", scenario.node, "--peek"])
        except SystemExit:
            pass
    return output.getvalue()


def _mcp_inbox(scenario: Scenario) -> str:
    from floati.mcp import McpServer
    server = McpServer(str(scenario.root.path), scenario.node, scenario.session)
    return json.dumps(server.call_tool("inbox", {}))


EVENT_PLANE_PRESENTERS = {
    # Public event-plane presenters: inbox peek, drain, compatible reads.
    "inbox-peek": lambda s: json.dumps(EventLog(s.root).present(s.node)[0]),
    "inbox-drain": lambda s: json.dumps(EventLog(s.root).drain(
        s.node, acting_session_id=s.session)[0]),
    "present-compatible": lambda s: json.dumps(EventLog(s.root).present_compatible(s.node)[0]),
    "inbox-cli": _inbox_cli,
    # A pulled read through the launch-bound MCP inbox tool.
    "mcp-inbox": _mcp_inbox,
}
CONVERSATION_AND_WAKE_PRESENTERS = {
    # Conversation-plane presenters: the Claude context hook and the Stop waiter.
    "claude-context-hook": _relay,
    "stop-waiter": _stop_waiter,
    # The wake decision every wake daemon resume prompt is built from.
    "wake-evaluation": _wake_evaluation,
}


def _decision_row(scenario: Scenario, phase: str) -> dict:
    """The exact version-one decision row the operator act appends."""
    now = _iso(datetime.now(timezone.utc))
    return {
        "schema_version": 1, "id": "inbound-hold-" + uuid7_hex(),
        "tenant_id": scenario.root.tenant_id, "timestamp": now,
        "kind": "inbound_hold_receipt", "node_id": scenario.node, "phase": phase,
        "hold_id": scenario.hold_id, "reason": None, "started_at": None,
        "expected_release": None, "released_at": None, "message_id": scenario.held["id"],
        "envelope_digest": _envelope_digest(scenario.held), "decided_at": now,
        "human_act": {"channel": "controlling_terminal", "rendered_digest": "0" * 64},
        "idempotency_key": "decision-" + phase,
    }


def _synthesized_arguments(schema: dict, scenario: Scenario, scratch: Path) -> dict:
    """Fill every required input from its JSON schema; refusals cannot leak either."""
    values = {"recipient": scenario.node, "node": scenario.node, "actor": scenario.node,
              "source": str(REPOSITORY_ROOT), "consumer": "auditor", "id": scenario.held["id"],
              "out": str(scratch / "bundle.json"), "from_dir": str(scratch),
              "snapshot_id": "snapshot-none"}
    arguments = {}
    for name in schema.get("required", ()):
        spec = schema["properties"][name]
        if "enum" in spec:
            arguments[name] = spec["enum"][0]
        elif spec.get("type") == "integer":
            arguments[name] = 1
        elif spec.get("type") == "boolean":
            arguments[name] = False
        else:
            arguments[name] = values.get(name, "value")
    return arguments


class InboundHoldPresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        prior = Path.cwd()
        self.addCleanup(os.chdir, prior)

    def assert_presenters_withhold_and_still_present(self, presenters) -> None:
        for name, present in presenters.items():
            with self.subTest(presenter=name):
                scenario = Scenario(self.base, name)
                os.chdir(scenario.workspace)
                output = present(scenario)
                self.assertIn(scenario.ordinary["id"], output,
                              "the path must be shown live: it presents ordinary mail")
                self.assertNotIn(scenario.held["id"], output)
                self.assertNotIn(scenario.held["id"], scenario.delivered_ids())

    def test_event_plane_presenters_withhold_a_held_item_and_still_present(self) -> None:
        self.assert_presenters_withhold_and_still_present(EVENT_PLANE_PRESENTERS)

    def test_conversation_and_wake_presenters_withhold_a_held_item_and_still_present(self) -> None:
        self.assert_presenters_withhold_and_still_present(CONVERSATION_AND_WAKE_PRESENTERS)

    def test_warm_cursor_publishes_ordinary_mail_with_held_mail_present(self) -> None:
        from unittest.mock import patch
        from floati import cut_prefix, inbox_prefix, send_prefix
        from floati.cursor_presentations import CursorPresentationLedger
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant)

        scenario = Scenario(self.base, "warm-cursor")
        participant = resolve_participant(scenario.root.path, scenario.workspace)
        consent = CodexWaitConsentLedger(scenario.root).require_armed(participant.binding)
        classification = CodexWaitSessionLedger(scenario.root).participate_classified(
            participant.binding, consent, scenario.session)
        send_prefix.warm(scenario.root)
        inbox_prefix.warm(scenario.root)
        cut_prefix.warm(scenario.root)
        plane = CursorPresentationLedger(scenario.root)
        with patch.object(plane, "_load", side_effect=AssertionError("selected prefix was cold")):
            selection = plane._select_waiter(participant.binding, consent, classification,
                                             harness="cursor")
        body = {"followup_message": json.dumps(selection["fresh_messages"])}
        prepared = plane.prepare(selection, body)
        plane.deliver(prepared, selection)
        self.assertEqual({scenario.ordinary["id"]}, scenario.delivered_ids())

    def test_wake_retry_preserves_the_original_inbound_hold_selection(self) -> None:
        from floati.wake_hold import WakeHoldController
        from tests.schema_validation import validate_json_schema

        scenario = Scenario(self.base, "wake-retry")
        EventLog(scenario.root).drain(scenario.node, acting_session_id=scenario.session)
        eligible = scenario.send("approved-later-envelope")
        scenario._append(dict(_decision_row(scenario, "approved"),
            message_id=eligible["id"], envelope_digest=_envelope_digest(eligible),
            idempotency_key="approve-later-envelope"))
        controller = WakeHoldController(scenario.root)
        first = controller.evaluate(scenario.node, idempotency_key="retry-held-prefix")
        self.assertEqual([eligible["id"]], [m["id"] for m in first["fresh_messages"]])
        validate_json_schema(first["receipt"],
            REPOSITORY_ROOT / "schemas/v4/wake-hold-receipt-record.schema.json")
        self.assertEqual(first, controller.evaluate(scenario.node, idempotency_key="retry-held-prefix"))
        scenario._append(_decision_row(scenario, "approved"))
        self.assertEqual(first, controller.evaluate(scenario.node, idempotency_key="retry-held-prefix"))
        later = controller.evaluate(scenario.node, idempotency_key="new-wake")
        self.assertEqual([scenario.held["id"]], [m["id"] for m in later["fresh_messages"]])

    def test_presentation_commit_points_refuse_a_held_item(self) -> None:
        from floati.cursor_presentations import CursorPresentationLedger
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant)

        scenario = Scenario(self.base, "commit-delivery")
        with self.assertRaises(ProtocolRefusal) as caught:
            EventLog(scenario.root)._write_delivery_receipt(scenario.node, [scenario.held])
        self.assertEqual("inbound_hold_unapproved", caught.exception.code)
        self.assertNotIn(scenario.held["id"], scenario.delivered_ids())

        scenario = Scenario(self.base, "commit-prepare")
        participant = resolve_participant(scenario.root.path, scenario.workspace)
        consent = CodexWaitConsentLedger(scenario.root).require_armed(participant.binding)
        classification = CodexWaitSessionLedger(scenario.root).participate_classified(
            participant.binding, consent, scenario.session)
        plane = CursorPresentationLedger(scenario.root)
        selection = plane.select(participant.binding, consent, classification, harness="codex")
        forged = dict(selection, fresh_messages=[scenario.held],
                      _eligible_ids=(scenario.held["id"],))
        before = (scenario.root.tenant_home / plane.relative(scenario.node))
        before_bytes = before.read_bytes() if before.exists() else b""
        with self.assertRaises(ProtocolRefusal) as caught:
            plane.prepare(forged, {"decision": "block", "reason": scenario.held["id"]})
        self.assertEqual("inbound_hold_unapproved", caught.exception.code)
        self.assertEqual(before_bytes, before.read_bytes() if before.exists() else b"")

    def test_presenter_population_is_derived_and_every_member_reaches_the_gate(self) -> None:
        from floati.records import WAKE_HOLD_KINDS
        from floati.cursor_presentations import KINDS as PRESENTATION_RECORD_KINDS

        kinds = set(WAKE_HOLD_KINDS) | set(PRESENTATION_RECORD_KINDS)
        gate_source = REPOSITORY_ROOT / "floati" / "inbound_hold.py"
        gates = set()
        if gate_source.exists():
            for node in ast.parse(gate_source.read_text(encoding="utf-8")).body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == "PRESENTATION_GATES"):
                    gates = set(ast.literal_eval(node.value))
        functions, writers = {}, {}
        sources = {path: path.read_text(encoding="utf-8")
                   for path in sorted((REPOSITORY_ROOT / "floati").rglob("*.py"))}

        def scan(path):
            module = ast.parse(sources[path])
            constants = {
                node.targets[0].id: node.value.value for node in module.body
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}

            def values(expression):
                if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
                    return {expression.value}
                if isinstance(expression, ast.Name) and expression.id in constants:
                    return {constants[expression.id]}
                if isinstance(expression, ast.IfExp):
                    return values(expression.body) | values(expression.orelse)
                return set()

            def outermost():
                for node in module.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield node.name, node
                    elif isinstance(node, ast.ClassDef):
                        for member in node.body:
                            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                yield node.name + "." + member.name, member

            for qualname, function in outermost():
                name = path.relative_to(REPOSITORY_ROOT).as_posix() + "::" + qualname
                calls = {
                    call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
                    for call in ast.walk(function) if isinstance(call, ast.Call)
                    and isinstance(call.func, (ast.Attribute, ast.Name))}
                functions[name] = (function.name, calls)
                built = {
                    kind for literal in ast.walk(function) if isinstance(literal, ast.Dict)
                    for key, value in zip(literal.keys, literal.values)
                    if isinstance(key, ast.Constant) and key.value == "kind"
                    for kind in values(value) if kind in kinds}
                if built:
                    writers[name] = built

        # A kind is named in a module either literally or by a module constant
        # that spells it, so only modules whose text spells a kind can write one.
        scanned = {path for path, text in sources.items() if any(kind in text for kind in kinds)}
        for path in sorted(scanned):
            scan(path)
        self.assertEqual(kinds, set().union(*writers.values()),
                         "every presentation kind must have a derived writer")
        unreached, population = [], set()
        for writer in sorted(writers):
            simple, calls = functions[writer]
            if calls & gates:
                population.add(writer)
                continue
            for path in sorted(set(sources) - scanned):
                if simple in sources[path]:
                    scanned.add(path)
                    scan(path)
            callers = [name for name, (_simple, called) in functions.items()
                       if simple in called and name != writer]
            if not callers or any(not (functions[name][1] & gates) for name in callers):
                unreached.append(writer)
            population.update(callers)
        self.assertFalse(unreached, "presentation writers that reach no inbound hold gate: "
                         + ", ".join(unreached) + "; derived population: "
                         + ", ".join(sorted(population)))


def _child_environment() -> dict:
    return dict(os.environ, PYTHONPATH=str(REPOSITORY_ROOT), PYTHONDONTWRITEBYTECODE="1")


class OperatorTerminal:
    """A real controlling terminal for the person, and a session without one."""

    def at_terminal(self, argv: list, *, cwd: Path, answer=None):
        """Run one operator act at a real controlling terminal and answer its prompt."""
        import pty
        started = time.monotonic()
        pid, descriptor = pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
                os.execve(argv[0], argv, _child_environment())
            finally:
                os._exit(127)
        screen, answered = b"", False
        try:
            while True:
                remaining = CHILD_HANG_GUARD_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    os.kill(pid, 9)
                    self.fail("interactive operator act hung: bound %.0fs elapsed %.1fs load %s"
                              % (CHILD_HANG_GUARD_SECONDS, time.monotonic() - started,
                                 os.getloadavg()))
                ready, _, _ = select.select([descriptor], [], [], remaining)
                if not ready:
                    continue
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                screen += chunk
                match = re.search(rb"code:\s*([A-Z0-9]{8})\b", screen)
                if not answered and match is not None and b"> " in screen[match.end():]:
                    reply = answer(match.group(1).decode()) if callable(answer) else answer
                    os.write(descriptor, (reply or "").encode() + b"\n")
                    answered = True
        finally:
            _pid, status = os.waitpid(pid, 0)
            os.close(descriptor)
        text = screen.decode("utf-8", "replace").replace("\r\n", "\n")
        artifacts = [json.loads(line) for line in text.splitlines()
                     if line.startswith('{"artifact_version"')]
        return os.waitstatus_to_exitcode(status), (artifacts[-1] if artifacts else {}), text

    def without_terminal(self, argv: list, *, cwd: Path, environment=None, stdin=b"yes\n"):
        completed = subprocess.run(argv, cwd=cwd, env=environment or _child_environment(),
                                   input=stdin, capture_output=True, start_new_session=True,
                                   timeout=CHILD_HANG_GUARD_SECONDS)
        try:
            artifact = json.loads(completed.stdout)
        except json.JSONDecodeError:
            artifact = {}
        return completed.returncode, artifact

class InboundHoldHumanActTests(OperatorTerminal, unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.neutral = self.base / "operator"
        self.neutral.mkdir()
        self.scenario = Scenario(self.base, "human")
        self.environment = _child_environment()

    def decision_argv(self, action: str, message: dict, key: str) -> list:
        return [sys.executable, "-m", "floati.inbound_hold", action,
                "--root", str(self.scenario.root.path), "--node", self.scenario.node,
                "--message", message["id"], "--idempotency-key", key]

    def test_a_terminal_approval_presents_exactly_the_shown_item_once(self) -> None:
        held = self.scenario.held
        code, artifact, screen = self.at_terminal(
            self.decision_argv("approve", held, "approve-held-1"), cwd=self.neutral,
            answer=lambda challenge: challenge)
        self.assertEqual((0, "ok"), (code, artifact.get("status")), screen)
        self.assertIn(held["note"], screen, "the person sees the exact item")
        decision = self.scenario.hold_rows()[-1]
        self.assertEqual(("approved", held["id"], _envelope_digest(held)),
                         (decision["phase"], decision["message_id"], decision["envelope_digest"]))
        self.assertEqual("controlling_terminal", decision["human_act"]["channel"])
        messages, _receipt = EventLog(self.scenario.root).present(self.scenario.node)
        self.assertEqual({self.scenario.ordinary["id"], held["id"]},
                         {message["id"] for message in messages})
        self.assertIn(held["id"], self.scenario.delivered_ids())
        replay, again, _screen = self.at_terminal(
            self.decision_argv("approve", held, "approve-held-1"), cwd=self.neutral)
        self.assertEqual((0, decision["id"]), (replay, again.get("evidence", {}).get("id")))
        self.assertEqual(2, len(self.scenario.hold_rows()), "a replay by its key appends nothing")

    def test_a_wrong_answer_and_a_rejection_never_present_the_item(self) -> None:
        held = self.scenario.held
        code, artifact, _screen = self.at_terminal(
            self.decision_argv("approve", held, "approve-wrong"), cwd=self.neutral,
            answer="ZZZZZZZZ")
        self.assertEqual((20, "refused"), (code, artifact.get("status")))
        self.assertEqual(1, len(self.scenario.hold_rows()))
        code, artifact, _screen = self.at_terminal(
            self.decision_argv("reject", held, "reject-held"), cwd=self.neutral,
            answer=lambda challenge: challenge)
        self.assertEqual((0, "rejected"), (code, artifact.get("evidence", {}).get("phase")))
        self.assertNotIn(held["id"], json.dumps(EventLog(self.scenario.root).present(
            self.scenario.node)[0]))
        self.assertNotIn(held["id"], self.scenario.delivered_ids())

    def test_no_non_human_path_can_approve(self) -> None:
        held = self.scenario.held
        argv = self.decision_argv("approve", held, "approve-sweep")
        sweeps = {
            "plain": {},
            "affirmative-environment": {"FLOATI_INBOUND_HOLD_APPROVE": "1", "FLOATI_YES": "1",
                                        "FLOATI_HUMAN": "1", "CI": "1", "TERM": "xterm-256color"},
            "test-seam-environment": {"FLOATI_TEST_SEAM": "1", "FLOATI_APPROVAL": "approved",
                                      "SUDO_USER": "operator"},
        }
        for label, extra in sweeps.items():
            with self.subTest(environment=label):
                code, artifact = self.without_terminal(
                    argv, cwd=self.neutral, environment=dict(self.environment, **extra))
                self.assertEqual((20, "inbound_hold_human_act_unavailable"),
                                 (code, artifact.get("evidence", {}).get("code")))
        for flag in (["--yes"], ["--confirm", "approved"], ["--as", self.scenario.node],
                     ["--approve-all"]):
            with self.subTest(flag=flag[0]):
                code, _artifact = self.without_terminal(argv + flag, cwd=self.neutral)
                self.assertNotEqual(0, code)
        with self.subTest(context="held seat workspace at a terminal"):
            (self.scenario.workspace / "SEAT.json").write_text("{}", encoding="utf-8")
            code, artifact, screen = self.at_terminal(argv, cwd=self.scenario.workspace,
                                                      answer=lambda challenge: challenge)
            self.assertEqual((20, "inbound_hold_decision_seat_context"),
                             (code, artifact.get("evidence", {}).get("code")), screen)
            self.assertNotIn("code:", screen, "a seat context is refused before any prompt")
        self.assertEqual(1, len(self.scenario.hold_rows()))
        self.assertNotIn(held["id"], json.dumps(EventLog(self.scenario.root).present(
            self.scenario.node)[0]))


class InboundHoldDisplayPopulationTests(unittest.TestCase):
    """Every command output is derived from the live parser and must reach a gate."""

    def test_every_command_output_path_reaches_the_display_or_presentation_gate(self) -> None:
        import argparse
        import inspect
        from floati import cli

        gate_source = REPOSITORY_ROOT / "floati" / "inbound_hold.py"
        gates = set()
        if gate_source.exists():
            for node in ast.parse(gate_source.read_text(encoding="utf-8")).body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == "PRESENTATION_GATES"):
                    gates = set(ast.literal_eval(node.value))
        sources = {path: path.read_text(encoding="utf-8")
                   for path in sorted((REPOSITORY_ROOT / "floati").rglob("*.py"))}
        parsed = {}

        definition = re.compile(r"^([ \t]*)(?:async[ \t]+)?def[ \t]+(\w+)[ \t]*\(", re.MULTILINE)
        called_name = re.compile(r"([A-Za-z_]\w*)[ \t]*\(")
        callers = {}
        for text in sources.values():
            found = list(definition.finditer(text))
            for index, match in enumerate(found):
                indent = len(match.group(1).expandtabs())
                end = len(text)
                for later in found[index + 1:]:
                    if len(later.group(1).expandtabs()) <= indent:
                        end = later.start()
                        break
                body = text[match.end():end]
                for name in set(called_name.findall(body)):
                    callers.setdefault(name, set()).add(match.group(2))

        def calls(function):
            return {call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
                    for call in ast.walk(function) if isinstance(call, ast.Call)
                    and isinstance(call.func, (ast.Attribute, ast.Name))}

        def reaching(targets, wanted):
            """Grow, from the gates outward, every function name that calls one."""
            reached, frontier = set(targets), set(targets)
            while frontier and not wanted <= reached:
                frontier = set().union(*(callers.get(name, set()) for name in frontier)) - reached
                reached |= frontier
            return reached

        # Every ordinary handler's artifact is printed by main through _emit;
        # _emit is the artifact boundary every CLI and MCP tool shares.
        cli_module = ast.parse(Path(inspect.getsourcefile(cli)).read_text(encoding="utf-8"))
        boundary = {node.name: node for node in cli_module.body
                    if isinstance(node, ast.FunctionDef) and node.name in {"main", "_emit"}}
        self.assertIn("_emit", calls(boundary["main"]))
        self.assertTrue(calls(boundary["_emit"]) & gates,
                        "the shared artifact boundary must apply the inbound hold display gate")

        direct = set()

        def leaves(parser):
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for child in action.choices.values():
                        handler = child._defaults.get("direct_handler")
                        if handler is not None:
                            direct.add(handler.__name__)
                        leaves(child)

        leaves(cli._parser())
        self.assertTrue(direct, "the live parser declares direct handlers")
        unreached = sorted(direct - reaching(gates | {"_emit"}, direct))
        self.assertFalse(unreached, "command outputs that reach no inbound hold gate: "
                         + ", ".join(unreached))

    def test_terminal_caller_guard_detects_planted_imports(self) -> None:
        from unittest.mock import patch

        variants = (
            ("floati/relay.py", "from .inbound_hold import release_hold\nrelease_hold()\n"),
            ("floati/adapters/relay.py", "from ..inbound_hold import release_hold\nrelease_hold()\n"),
            ("floati/adapters/nested/relay.py",
             "from ... import inbound_hold as hold\nhold.decide_item()\n"),
        )
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            for relative, source in variants:
                with self.subTest(relative=relative):
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(source, encoding="utf-8")
                    try:
                        with patch(f"{__name__}.REPOSITORY_ROOT", root):
                            with self.assertRaises(AssertionError) as caught:
                                self.test_terminal_acts_have_no_automated_entry_point()
                        self.assertIn(relative, str(caught.exception))
                    finally:
                        path.unlink()

    def test_terminal_acts_have_no_automated_entry_point(self) -> None:
        from floati.cli import _parser
        from floati.command_contract import describe_parser, project_mcp_surface

        parser = _parser()
        terminal_acts = {"approve", "reject", "release"}
        forbidden_paths = {("hold", "inbound", act) for act in terminal_acts}
        paths = {tuple(row["path"]) for row in describe_parser(parser)["commands"]}
        self.assertFalse(forbidden_paths & paths)
        for tool in project_mcp_surface(parser)["tools"]:
            self.assertNotIn(tuple(tool["_meta"]["floati"]["commandPath"]), forbidden_paths)

        # The parser, MCP, and the relay entry points also must not route an
        # alias or a subprocess to either terminal writer in the operator module.
        launcher = re.compile(r"(?:-m\s+\bfloati\.inbound_hold\b|['\"]floati\.inbound_hold['\"])")
        operator_names = {"decide_item", "release_hold", "main", "OPERATOR_ENTRY"}
        automated_paths = []
        for directory in ("floati", "hooks", "tools", "scripts"):
            for path in (REPOSITORY_ROOT / directory).rglob("*"):
                if path.suffix not in {".py", ".sh", ""} or not path.is_file():
                    continue
                if path in {REPOSITORY_ROOT / "floati" / "inbound_hold.py",
                            REPOSITORY_ROOT / "floati" / "generated_help.py"}:
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
                if launcher.search(source):
                    automated_paths.append(str(path.relative_to(REPOSITORY_ROOT)))
                elif path.suffix == ".py" and "inbound_hold" in source:
                    nodes = list(ast.walk(ast.parse(source)))
                    module_names = {"floati.inbound_hold"}
                    imports_operator_name = False
                    for node in nodes:
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                if alias.name == "floati.inbound_hold" and alias.asname:
                                    module_names.add(alias.asname)
                                if alias.name == "floati" and alias.asname:
                                    module_names.add(f"{alias.asname}.inbound_hold")
                        if isinstance(node, ast.ImportFrom):
                            module = node.module or ""
                            if node.level:
                                package = path.parent.relative_to(REPOSITORY_ROOT).parts
                                if node.level > len(package):
                                    continue
                                parent = package[:len(package) - node.level + 1]
                                module = ".".join((*parent, *module.split("."))) if module else ".".join(parent)
                            imported = {alias.name for alias in node.names}
                            if module == "floati.inbound_hold":
                                imports_operator_name |= bool(imported & (operator_names | {"*"}))
                            if module == "floati":
                                module_names.update(alias.asname or alias.name for alias in node.names
                                                    if alias.name == "inbound_hold")
                    if imports_operator_name or any(
                        isinstance(node, ast.Attribute) and node.attr in operator_names
                        and ast.unparse(node.value) in module_names
                        for node in nodes
                    ):
                        automated_paths.append(str(path.relative_to(REPOSITORY_ROOT)))
        self.assertEqual([], sorted(automated_paths), "terminal writer reachable outside the operator module")


class InboundHoldReadViewTests(unittest.TestCase):
    """A read view shows a held item as HELD, never its note, whoever reads it."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.scenario = Scenario(self.base, "read")
        prior = Path.cwd()
        self.addCleanup(os.chdir, prior)
        os.chdir(self.base)

    def cli(self, *argv: str) -> dict:
        from floati.mcp import run_cli_artifact
        return run_cli_artifact(list(argv))[1]

    def test_drain_displays_the_approved_body_when_presence_is_declared(self) -> None:
        from floati.fleet_presence import set_presence

        scenario = self.scenario
        scenario._append(_decision_row(scenario, "approved"))
        set_presence(scenario.root, scenario.sender, "AWAY", "overnight")
        artifact = self.cli("inbox", "--root", str(scenario.root.path),
                            "--as", scenario.node, "--session", scenario.session)
        self.assertEqual("ok", artifact["status"], artifact)
        self.assertIn(scenario.held["id"], SparseCursor(scenario.root).acked_ids(scenario.node))
        shown = {row["id"]: row for row in artifact["evidence"]["messages"]}
        self.assertEqual(scenario.held["note"], shown[scenario.held["id"]].get("note"))
        self.assertTrue(shown[scenario.held["id"]]["fleet_presence"])

    def test_cli_shows_held_item_coordinates_without_presenting_or_writing(self) -> None:
        scenario = self.scenario
        ledger = scenario.root.tenant_home / HOLD_LEDGER / (scenario.node + ".jsonl")
        before = ledger.read_bytes()
        delivered_before = scenario.delivered_ids()
        artifact = self.cli("hold", "inbound", "show", "--root", str(scenario.root.path),
                            "--node", scenario.node)
        self.assertEqual("ok", artifact.get("status"), artifact)
        view = artifact["evidence"]
        self.assertEqual([scenario.hold_id], [hold["id"] for hold in view["holds"]])
        self.assertEqual([(scenario.held["id"], _envelope_digest(scenario.held))],
                         [(item["message_id"], item["envelope_digest"])
                          for item in view["awaiting_decision"]])
        self.assertNotIn(scenario.held["note"], json.dumps(artifact))
        self.assertEqual(before, ledger.read_bytes())
        self.assertEqual(delivered_before, scenario.delivered_ids())

    def test_log_shows_a_held_item_as_held_and_never_its_note(self) -> None:
        artifact = self.cli("log", "--root", str(self.scenario.root.path))
        rendered = json.dumps(artifact)
        self.assertIn(self.scenario.ordinary["note"], rendered, "ordinary mail renders normally")
        self.assertNotIn(self.scenario.held["note"], rendered)
        held = next(row for row in artifact["evidence"]["messages"]
                    if row.get("id") == self.scenario.held["id"])
        self.assertEqual(("held", self.scenario.hold_id, _envelope_digest(self.scenario.held),
                          self.scenario.sender, self.scenario.held["timestamp"]),
                         (held["inbound_hold"]["state"], held["inbound_hold"]["hold_id"],
                          held["inbound_hold"]["envelope_digest"], held["sender"], held["timestamp"]))

    def test_every_read_tool_derived_from_the_parser_never_renders_a_held_note(self) -> None:
        from floati.mcp import McpServer
        server = McpServer(str(self.scenario.root.path), self.scenario.node, self.scenario.session)
        tools = [tool for tool in server.list_tools()
                 if tool["_meta"]["floati"]["exposure"] == "read"]
        self.assertIn("log", {tool["name"] for tool in tools})
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                result = json.dumps(server.call_tool(
                    tool["name"], _synthesized_arguments(tool["inputSchema"], self.scenario, self.base)))
                self.assertNotIn(self.scenario.held["note"], result)
                if tool["name"] == "log":
                    self.assertIn(self.scenario.ordinary["note"], result)

    def test_a_confluence_bundle_keeps_raw_bytes_and_its_artifact_marks_held_items(self) -> None:
        root = str(self.scenario.root.path)
        self.cli("confluence", "grant", "--root", root, "--consumer", "auditor",
                 "--idempotency-key", "grant-auditor")
        out = self.base / "bundle.json"
        artifact = self.cli("confluence", "bundle", "--root", root, "--consumer", "auditor",
                            "--out", str(out), "--schema-version", "2")
        self.assertEqual("ok", artifact["status"], artifact)
        self.assertIn(self.scenario.held["note"], out.read_text(encoding="utf-8"),
                      "the audit export keeps the exact ledger bytes")
        self.assertEqual([(self.scenario.held["id"], "held", self.scenario.hold_id)],
                         [(row["message_id"], row["state"], row["hold_id"])
                          for row in artifact["evidence"]["inbound_holds"]])

    def test_a_rejected_item_stays_withheld_shows_its_decision_and_never_blocks_drain(self) -> None:
        decision = _decision_row(self.scenario, "rejected")
        self.scenario._append(decision)
        drained = EventLog(self.scenario.root).empty_inbox(
            self.scenario.node, acting_session_id=self.scenario.session)
        self.assertEqual([self.scenario.ordinary["id"]], drained["acked_ids"])
        self.assertNotIn(self.scenario.held["id"],
                         SparseCursor(self.scenario.root).acked_ids(self.scenario.node),
                         "a rejection is never recorded as an acknowledgment")
        artifact = self.cli("log", "--root", str(self.scenario.root.path))
        self.assertNotIn(self.scenario.held["note"], json.dumps(artifact))
        held = next(row for row in artifact["evidence"]["messages"]
                    if row.get("id") == self.scenario.held["id"])
        self.assertEqual(("rejected", decision["id"]),
                         (held["inbound_hold"]["state"], held["inbound_hold"]["decision_id"]))

    def test_an_envelope_whose_root_the_command_never_opened_is_shown_unprovable(self) -> None:
        from floati.inbound_hold import mask_held_envelopes
        view = {"messages": [self.scenario.ordinary]}
        self.assertEqual(view, mask_held_envelopes(self.scenario.root, view),
                         "ordinary mail from an opened root renders unchanged")
        masked = mask_held_envelopes(lambda tenant: [], view)["messages"][0]
        self.assertEqual("history_uncertain", masked["inbound_hold"]["state"])
        self.assertNotIn("note", masked)

    def test_an_approved_item_renders_normally(self) -> None:
        self.scenario._append(_decision_row(self.scenario, "approved"))
        artifact = self.cli("log", "--root", str(self.scenario.root.path))
        self.assertIn(self.scenario.held, artifact["evidence"]["messages"])

    def test_the_board_lists_an_open_inbound_hold_and_its_waiting_items(self) -> None:
        from floati.tui import model_from_root
        from floati.tui_render import _detail_lines
        model = model_from_root(self.scenario.root)
        lines = [line for index in range(len(model.nodes))
                 for line in _detail_lines(model, 120, index)]
        inbound = [line for line in lines if "INBOUND HOLD" in line]
        self.assertTrue(inbound, lines)
        self.assertIn(self.scenario.hold_id, " | ".join(inbound))
        self.assertIn("1 awaiting", " | ".join(inbound))


class InboundHoldRecordTests(OperatorTerminal, unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def operator(self, *argv: str) -> tuple:
        completed = subprocess.run(
            [sys.executable, "-m", "floati.inbound_hold", *argv],
            env=dict(os.environ, PYTHONPATH=str(REPOSITORY_ROOT), PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, start_new_session=True, timeout=CHILD_HANG_GUARD_SECONDS)
        try:
            return completed.returncode, json.loads(completed.stdout)
        except json.JSONDecodeError:
            return completed.returncode, {"stderr": completed.stderr.decode("utf-8", "replace")}

    def test_cli_starts_an_inbound_hold_and_replays_its_receipt(self) -> None:
        from floati.mcp import run_cli_artifact

        root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        node = fixture_ids.builder("a")
        Registry(root).register(node, "claude")
        common = ["--root", str(root.path), "--node", node]
        arguments = ["start", *common, "--reason", "review inbound mail",
                     "--idempotency-key", "inbound-cli-1",
                     "--expected-release", "2035-01-01T00:00:00Z"]
        code, artifact = run_cli_artifact(["hold", "inbound", *arguments])
        self.assertEqual((0, "ok"), (code, artifact.get("status")), artifact)
        receipt = artifact["evidence"]
        self.assertEqual(("inbound_hold_receipt", "held", node, "2035-01-01T00:00:00Z"),
                         (receipt["kind"], receipt["phase"], receipt["node_id"],
                          receipt["expected_release"]))
        code, replay = self.operator(*arguments)
        self.assertEqual((0, receipt), (code, replay["evidence"]))

    def test_cli_preserves_the_operator_refusal_codes(self) -> None:
        from floati.mcp import run_cli_artifact

        root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        node = fixture_ids.builder("a")
        Registry(root).register(node, "claude")
        common = ["--root", str(root.path), "--node", node]
        cases = [
            (["start", *common, "--reason", "review", "--idempotency-key", "review-1",
              "--expected-release", "tomorrow"], "inbound_hold_expected_release_invalid"),
            (["show", "--root", "", "--node", node], "cannot_speak"),
        ]
        for arguments, reason in cases:
            with self.subTest(action=arguments[0], reason=reason):
                operator_code, operator = self.operator(*arguments)
                cli_code, cli = run_cli_artifact(["hold", "inbound", *arguments])
                self.assertEqual((20, reason), (operator_code, operator["evidence"]["code"]))
                self.assertEqual((operator_code, operator["status"], operator["evidence"]),
                                 (cli_code, cli["status"], cli["evidence"]))

    def test_start_and_release_are_versioned_receipts_and_mail_inside_stays_held(self) -> None:
        root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        node, sender = fixture_ids.builder("a"), fixture_ids.builder("b")
        Registry(root).register(node, "claude")
        Registry(root).register(sender, "claude")
        common = ["--root", str(root.path), "--node", node]
        code, started = self.operator("start", *common, "--reason", "review inbound mail",
                                      "--idempotency-key", "inbound-start-1")
        self.assertEqual((0, "ok"), (code, started.get("status")), started)
        hold = started["evidence"]
        self.assertEqual((1, "inbound_hold_receipt", "held", node),
                         (hold["schema_version"], hold["kind"], hold["phase"], hold["node_id"]))
        replay = self.operator("start", *common, "--reason", "review inbound mail",
                               "--idempotency-key", "inbound-start-1")[1]
        self.assertEqual(hold["id"], replay["evidence"]["id"])
        inside = EventLog(root).send(sender, node, "floati", "a" * 40, "docs/x.md",
                                     "sent inside the hold", idempotency_key="inside")["message"]
        release = [sys.executable, "-m", "floati.inbound_hold", "release", *common,
                   "--idempotency-key", "inbound-release-1"]
        code, refused = self.without_terminal(release, cwd=self.base)
        self.assertEqual((20, "inbound_hold_human_act_unavailable"),
                         (code, refused.get("evidence", {}).get("code")),
                         "the held node can never lift its own hold")
        code, released, screen = self.at_terminal(release, cwd=self.base,
                                                  answer=lambda challenge: challenge)
        self.assertEqual((0, "released", hold["id"], "controlling_terminal"),
                         (code, released["evidence"]["phase"], released["evidence"]["hold_id"],
                          released["evidence"]["human_act"]["channel"]), screen)
        after = EventLog(root).send(sender, node, "floati", "a" * 40, "docs/x.md",
                                    "sent after release", idempotency_key="after")["message"]
        presented = {m["id"] for m in EventLog(root).present(node)[0]}
        self.assertEqual({after["id"]}, presented,
                         "release never presents an item that was held")
        code, shown = self.operator("show", *common)
        self.assertEqual(0, code)
        self.assertEqual([inside["id"]], [item["message_id"]
                                          for item in shown["evidence"]["awaiting_decision"]])

    def test_a_row_the_writer_would_refuse_withholds_all_mail_to_the_node(self) -> None:
        scenario = Scenario(self.base, "forged")
        scenario._append({
            "schema_version": 1, "id": "inbound-hold-" + uuid7_hex(),
            "tenant_id": scenario.root.tenant_id, "timestamp": _iso(datetime.now(timezone.utc)),
            "kind": "inbound_hold_receipt", "node_id": scenario.node, "phase": "approved",
            "hold_id": scenario.hold_id, "reason": None, "started_at": None,
            "expected_release": None, "released_at": None, "message_id": scenario.held["id"],
            "envelope_digest": _envelope_digest(scenario.held),
            "decided_at": _iso(datetime.now(timezone.utc)), "human_act": None,
            "idempotency_key": "forged-approval"})
        self.assertEqual([], EventLog(scenario.root).present(scenario.node)[0],
                         "an unprovable decision withholds every item, ordinary mail included")
        from floati.inbound_hold import project_inbound_hold_findings
        self.assertIn("inbound_hold_ledger_partial",
                      {row["code"] for row in project_inbound_hold_findings(scenario.root)})

    def test_doctor_names_an_open_inbound_hold_and_its_waiting_items(self) -> None:
        scenario = Scenario(self.base, "doctor")
        from floati.inbound_hold import project_inbound_hold_findings
        findings = project_inbound_hold_findings(scenario.root)
        self.assertEqual({("inbound_hold_open", scenario.node)},
                         {(row["code"], row["subject"]) for row in findings})
        self.assertIn(scenario.held["id"], findings[0]["detail"])


if __name__ == "__main__":
    unittest.main()
