from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati.cli import main
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot


NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(minutes_before: int) -> str:
    return (
        (NOW - timedelta(minutes=minutes_before))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class AckHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="floati-ack-hygiene-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.registry = Registry(self.root)
        self.registry.register("sender", "codex")
        self.registry.register("recipient", "codex")
        self.log = EventLog(self.root)

    def _send(self, minutes_before: int, note: str) -> dict[str, object]:
        with mock.patch("floati.events.utc_now", return_value=_stamp(minutes_before)):
            return self.log.send(
                "sender", "recipient", "floati", "a" * 40, "docs/x.md", note
            )

    def test_ack_cli_accepts_one_to_one_thousand_repeatable_ids(self) -> None:
        """A-ack2: one receipt acknowledges the exact delivered batch."""

        first = self._send(12, "first")
        second = self._send(11, "second")
        self.log.present("recipient")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            status = main(
                [
                    "ack",
                    "--root",
                    str(self.home),
                    "--as",
                    "recipient",
                    "--id",
                    str(first["id"]),
                    "--id",
                    str(second["id"]),
                    "--session",
                    "session-batch",
                ]
            )

        self.assertEqual(0, status)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(
            [first["id"], second["id"]], artifact["evidence"]["item_ids"]
        )

    def test_inbox_acks_exact_returned_batch_by_default_under_acting_session(self) -> None:
        """A-ack1: one guarded drain writes distinct delivery and ack receipts."""

        first = self._send(12, "first")
        second = self._send(11, "second")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            status = main(
                [
                    "inbox",
                    "--root",
                    str(self.home),
                    "--as",
                    "recipient",
                    "--session",
                    "session-drain",
                ]
            )

        self.assertEqual(0, status)
        artifact = json.loads(stdout.getvalue())
        expected = [first["id"], second["id"]]
        self.assertEqual(expected, [row["id"] for row in artifact["evidence"]["messages"]])
        self.assertEqual(expected, artifact["evidence"]["receipt"]["item_ids"])
        self.assertEqual(expected, artifact["evidence"]["acknowledgment"]["item_ids"])
        self.assertEqual(expected, sorted(SparseCursor(self.root).acked_ids("recipient")))

    def test_inbox_peek_is_explicit_and_does_not_ack_presented_mail(self) -> None:
        """A-ack1: managed/manual process-before-ack reads use the named exception."""

        message = self._send(4, "peek")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            status = main(
                ["inbox", "--root", str(self.home), "--as", "recipient", "--peek"]
            )

        self.assertEqual(0, status)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual(message["id"], artifact["evidence"]["messages"][0]["id"])
        self.assertIsNone(artifact["evidence"]["acknowledgment"])
        self.assertEqual(frozenset(), SparseCursor(self.root).acked_ids("recipient"))

    def test_inbox_without_session_or_peek_refuses_instead_of_accidental_peek(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["inbox", "--root", str(self.home), "--as", "recipient"])

        self.assertEqual(20, status)
        self.assertEqual("", stderr.getvalue())
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("inbox_session_required", artifact["evidence"]["code"])

    def test_sent_projects_each_sender_envelope_from_receipts_only(self) -> None:
        """A-ack3: sender status is a read, never an ack-notification envelope."""

        from floati.sent import SentProjection

        first = self._send(30, "first")
        second = self._send(25, "second")
        with mock.patch("floati.events.utc_now", return_value=_stamp(20)):
            self.log.present("recipient")
        SparseCursor(self.root).ack(
            "recipient",
            [str(first["id"])],
            acting_session_id="session-sent",
            now=NOW - timedelta(minutes=5),
        )
        before = self.log.records()

        items = SentProjection(self.root).items("sender", now=NOW)

        self.assertEqual(before, self.log.records())
        self.assertEqual([first["id"], second["id"]], [row["message_id"] for row in items])
        acknowledged, delivered = items
        self.assertEqual("acknowledged", acknowledged["state"])
        self.assertEqual(_stamp(20), acknowledged["delivered_at"])
        self.assertEqual(_stamp(5), acknowledged["acknowledged_at"])
        self.assertEqual(600, acknowledged["delivery_latency_seconds"])
        self.assertEqual(900, acknowledged["acknowledgment_latency_seconds"])
        self.assertEqual("delivered_unacknowledged", delivered["state"])
        self.assertEqual(300, delivered["delivery_latency_seconds"])
        self.assertIsNone(delivered["acknowledged_at"])
        self.assertEqual(1200, delivered["attention_age_seconds"])

    def test_sent_cli_emits_the_receipt_projection_without_mcp_exposure(self) -> None:
        message = self._send(3, "cli")
        stdout = io.StringIO()

        with mock.patch("floati.sent._utc_now", return_value=NOW), redirect_stdout(stdout):
            status = main(
                ["sent", "--root", str(self.home), "--as", "sender"]
            )

        self.assertEqual(0, status)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("sent", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(message["id"], artifact["evidence"]["items"][0]["message_id"])

        from floati.cli import _parser

        command_action = next(
            action for action in _parser()._actions if action.dest == "command"
        )
        self.assertEqual("never", command_action.choices["sent"].floati_mcp_exposure)


if __name__ == "__main__":
    unittest.main()
