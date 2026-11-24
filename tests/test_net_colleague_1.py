"""Presented mail names its sender as a peer agent, not the user's instruction.

The frame is pinned by presence and structured fields. The sentence is not pinned.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from floati.cli import _inbox
from floati.cursor_presentations import notification
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import zcode_wake_prompt
from tests.temp_roots import REAL_TEMP_ROOT
from tests.private_artifacts import require_private_artifact

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BRIDGE = REPOSITORY_ROOT / "hooks" / "stop-hook-bridge.py"


def _walk(value):
    if isinstance(value, dict):
        if (
            "sender_node_id" in value
            and "peer_agent" in value
            and "not_the_users_instruction" in value
        ):
            yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def frames_in_text(text):
    decoder = json.JSONDecoder()
    found = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        found.extend(_walk(value))
    return found


def assert_colleague_frame(test, frame, sender):
    """Pin the structured frame. Do not compare the sentence."""

    test.assertIsInstance(frame, dict)
    test.assertEqual(sender, frame["sender_node_id"])
    test.assertIs(True, frame["peer_agent"])
    test.assertIs(True, frame["not_the_users_instruction"])
    test.assertEqual("DRAFT", frame["copy_stamp"])
    stamped = [
        value
        for value in frame.values()
        if isinstance(value, str)
        and value.startswith("DRAFT")
        and sender in value
        and value != sender
    ]
    test.assertTrue(stamped)
    test.assertNotIn("\n", stamped[0])


class ColleagueFrameTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register("sender-a", "Codex")
        registry.register("sender-b", "Codex")
        registry.register("recipient", "Codex")
        self.log = EventLog(self.root)
        self.first = self.log.send(
            "sender-a", "recipient", "floati", "a" * 40,
            "docs/evidence/one.md", "note-from-a", idempotency_key="colleague-a",
        )
        self.second = self.log.send(
            "sender-b", "recipient", "floati", "b" * 40,
            "docs/evidence/two.md", "note-from-b", idempotency_key="colleague-b",
        )

    def test_inbox_presentation_carries_a_frame_and_the_ledger_does_not(self):
        """Catches a presented envelope that still looks like the user's instruction."""

        for envelope in (self.first["message"], self.second["message"]):
            self.assertEqual([], list(_walk(envelope)))
        previous = Path.cwd()
        os.chdir(self.base)
        try:
            status, evidence, _code = _inbox(Namespace(
                root=str(self.root.path), recipient="recipient", peek=True, session=None,
            ))
        finally:
            os.chdir(previous)
        self.assertEqual("ok", status)
        shown = {row["id"]: row for row in evidence["messages"]}
        self.assertEqual({self.first["id"], self.second["id"]}, set(shown))
        for receipt, sender in ((self.first, "sender-a"), (self.second, "sender-b")):
            envelope = receipt["message"]
            presented = shown[envelope["id"]]
            self.assertEqual(envelope, {key: presented[key] for key in envelope})
            frames = list(_walk(presented))
            self.assertTrue(frames)
            assert_colleague_frame(self, frames[0], sender)

    def test_stop_hook_prompt_carries_a_frame_per_message(self):
        require_private_artifact(self, "hooks/stop-hook-bridge.py")
        spec = importlib.util.spec_from_file_location("stop_hook_bridge_colleague", BRIDGE)
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        prompt = module.build_prompt([self.first["message"], self.second["message"]])
        by_sender = {frame["sender_node_id"]: frame for frame in frames_in_text(prompt)}
        self.assertIn("sender-a", by_sender)
        self.assertIn("sender-b", by_sender)
        assert_colleague_frame(self, by_sender["sender-a"], "sender-a")
        assert_colleague_frame(self, by_sender["sender-b"], "sender-b")

    def test_cursor_notification_carries_a_frame_outside_the_native_envelope(self):
        message = dict(self.first["message"])
        body = notification(
            self.root.path, "recipient", self.base, "session-1", [message],
        )
        line = body["followup_message"].split("Exact native envelopes:\n", 1)[1].splitlines()[0]
        self.assertEqual([message], json.loads(line))
        frames = frames_in_text(body["followup_message"])
        self.assertTrue(frames)
        assert_colleague_frame(self, frames[0], "sender-a")

    def test_zcode_wake_prompt_carries_a_frame_on_one_line(self):
        prompt = zcode_wake_prompt((
            {"id": self.first["id"], "note": "note-from-a", "sender": "sender-a"},
            {"id": self.second["id"], "note": "note-from-b", "sender": "sender-b"},
        ))
        self.assertNotIn("\n", prompt)
        by_sender = {frame["sender_node_id"]: frame for frame in frames_in_text(prompt)}
        self.assertIn("sender-a", by_sender)
        self.assertIn("sender-b", by_sender)
        assert_colleague_frame(self, by_sender["sender-a"], "sender-a")
        assert_colleague_frame(self, by_sender["sender-b"], "sender-b")


class CodexColleagueFrameTests(unittest.TestCase):
    def setUp(self):
        from tests.test_codex_publication_surface import CodexPublicationSurfaceTests

        self.surface = CodexPublicationSurfaceTests("runTest")
        self.addCleanup(self.surface.doCleanups)
        self.surface.setUp()

    def test_codex_fresh_reason_carries_a_colleague_frame(self):
        from floati.fleet_presence import set_presence

        set_presence(self.surface.f.f.root, self.surface.f.node, "AWAY", "overnight")
        message = self.surface.f.send("colleague-frame")
        output, errors = self.surface.collect(self.surface.f.launch())
        reason = json.loads(output)["reason"]
        self.assertIn("AWAY", reason.splitlines()[0])
        frames = frames_in_text(reason)
        self.assertTrue(frames, errors)
        assert_colleague_frame(self, frames[0], message["sender"])
        self.assertNotIn("Exact native envelopes", reason)


class WakeDaemonColleagueFrameTests(unittest.TestCase):
    def setUp(self):
        from tests.test_wake_daemon import WakeDaemonGreenTests

        self.case = WakeDaemonGreenTests("runTest")
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def test_wake_reason_carries_a_colleague_frame(self):
        from floati.fleet_presence import set_presence

        set_presence(self.case.root, self.case.coordinate.node_id, "AWAY", "overnight")
        message = self.case.send("colleague-frame")
        self.assertEqual("woke", self.case.daemon().run_cycle(100.0)["state"])
        reason = self.case.adapter.calls[0][1]
        self.assertIn("AWAY", reason)
        frames = frames_in_text(reason)
        self.assertTrue(frames)
        assert_colleague_frame(self, frames[0], message["sender"])
        self.assertNotIn("\n", reason)


class SeatBoardColleagueFrameTests(unittest.TestCase):
    def setUp(self):
        from tests.test_seat_board import SeatBoardTests

        self.case = SeatBoardTests("runTest")
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def test_boarded_messages_carry_a_colleague_frame(self):
        from floati.fleet_presence import set_presence

        set_presence(self.case.root, "architect", "AWAY", "overnight")
        sent = self.case.send("colleague-board")
        result = self.case.board()
        shown = {row["id"]: row for row in result["messages"]}
        envelope = sent["message"]
        self.assertIn(envelope["id"], shown)
        self.assertIn("AWAY", shown[envelope["id"]]["fleet_presence"])
        self.assertEqual(envelope, {key: shown[envelope["id"]][key] for key in envelope})
        self.assertEqual([], list(_walk(envelope)))
        frames = list(_walk(shown[envelope["id"]]))
        self.assertTrue(frames)
        assert_colleague_frame(self, frames[0], sent["sender"])
