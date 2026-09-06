"""H1: `floati wait --for CONDITION` and its parity with the Codex-only script.

The verb composes the landed Stop waiter (`floati.codex_wait.run_stop_waiter`);
it writes no waiter of its own. Parity is the row's retirement condition, so it
is measured here: both entry points run against the same fixture shape and the
same outcome is asserted -- exit code, the harness decision on stdout, and the
receipt kinds the run left behind.
"""

from __future__ import annotations

from floati import fixture_ids as public_ids

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


NODE = public_ids.builder("floati")
EXHAUSTION_REASON = "(floati: wait deadline exhausted; end this turn to re-arm)"


class WaitFixture:
    """One armed, consenting waiter fixture: bus home, workspace, consent."""

    def __init__(self, base: Path, name: str, *, deadline_seconds: int) -> None:
        self.bus_home = base / name
        self.root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        Registry(self.root).register(NODE, "worker")
        Registry(self.root).register("architect", "architect")
        self.workspace = base / (name + "-workspace")
        self.workspace.mkdir()
        map_path = self.bus_home / "codex-wait" / "workspaces.v0.json"
        map_path.parent.mkdir(exist_ok=True)
        map_path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": name,
                    "mappings": [
                        {"workspace": str(self.workspace), "node_id": NODE}
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger,
            resolve_participant,
        )

        participant = resolve_participant(self.bus_home, self.workspace)
        assert participant is not None
        self.participant = participant
        CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=deadline_seconds,
            idempotency_key=name + "-consent",
        )

    def send(self, key: str) -> dict:
        return EventLog(self.root).send(
            "architect",
            NODE,
            "floati",
            "a" * 40,
            "docs/evidence/ping.md",
            "live ping",
            idempotency_key=key,
        )

    def receipt_kinds(self, prefix: str, allowed: set[str]) -> list[str]:
        try:
            rows = read_records_snapshot(
                self.root,
                public_ids.compose(prefix, public_ids.ledger(NODE)),
                allowed_kinds=allowed,
            )
        except Exception:
            return []
        return [str(row.get("kind")) for row in rows]

    def exit_reasons(self) -> list[str]:
        try:
            rows = read_records_snapshot(
                self.root,
                public_ids.compose(
                    "receipts/wake-waiter-exit/", public_ids.ledger(NODE)
                ),
                allowed_kinds={"wake_waiter_exit_receipt"},
            )
        except Exception:
            return []
        return [str(row.get("reason_code")) for row in rows]


class WaitCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    # -- helpers -----------------------------------------------------------

    def run_verb(
        self,
        fixture: WaitFixture,
        *,
        session_id: str,
        stdin_payload: Optional[dict] = None,
        workspace: Optional[str] = None,
    ) -> tuple[int, str]:
        from floati import cli

        argv = [
            "wait",
            "--for",
            "fresh-work",
            "--root",
            str(fixture.bus_home),
        ]
        if stdin_payload is None:
            argv += [
                "--workspace",
                workspace if workspace is not None else str(fixture.workspace),
                "--session-id",
                session_id,
            ]
        stdout = io.StringIO()
        stdin = io.StringIO(
            "" if stdin_payload is None else json.dumps(stdin_payload)
        )
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stdin", stdin):
            status = cli.main(argv)
        return status, stdout.getvalue()

    def run_script(
        self, fixture: WaitFixture, *, session_id: str
    ) -> tuple[int, str]:
        launcher = Path(__file__).resolve().parents[1] / "scripts" / "floati-codex-wait"
        payload = {"cwd": str(fixture.workspace), "session_id": session_id}
        completed = subprocess.run(
            [sys.executable, str(launcher), "--root", str(fixture.bus_home)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
            check=False,
        )
        return completed.returncode, completed.stdout

    def normalize(self, emitted: str, message_id: Optional[str]) -> str:
        """Replace the one value that cannot agree across two fleet roots."""

        if message_id is None:
            return emitted
        return emitted.replace(message_id, "<message-id>")

    # -- registration ------------------------------------------------------

    def test_wait_is_a_public_registered_verb_with_a_named_condition(self) -> None:
        """RED today: `wait` is not a command, so no path and no condition exist."""

        from floati.cli import _parser
        from floati.command_contract import describe_parser

        rows = describe_parser(_parser())["commands"]
        row = next((r for r in rows if r["path"] == ["wait"]), None)

        self.assertIsNotNone(row, "floati wait is not registered on the parser")
        assert row is not None
        self.assertTrue(row["public"], "floati wait must be an operator surface")
        condition = next(
            argument
            for argument in row["arguments"]
            if "--for" in argument["option_strings"]
        )
        self.assertTrue(condition["required"])
        self.assertEqual(["fresh-work"], condition["choices"])
        self.assertEqual("CONDITION", condition.get("metavar"))

    def test_wait_renders_a_static_help_page_naming_its_condition(self) -> None:
        """Catches the verb landing without the generated operator surfaces."""

        from floati.helptext import help_for

        page = help_for(["wait", "--help"])

        self.assertIsNotNone(page, "floati wait --help renders nothing")
        assert page is not None
        self.assertIn("--for CONDITION", page)
        self.assertIn("fresh-work", page)

    # -- behaviour ---------------------------------------------------------

    def test_wait_emits_the_harness_block_decision_for_fresh_work(self) -> None:
        fixture = WaitFixture(self.base, "verb-fresh", deadline_seconds=2)
        message = fixture.send("verb-fresh-ping")

        status, emitted = self.run_verb(fixture, session_id="seat-verb")

        self.assertEqual(0, status)
        decision = json.loads(emitted)
        self.assertEqual("block", decision["decision"])
        self.assertIn(message["id"], decision["reason"])
        self.assertEqual(
            ["wake_attempt_receipt"],
            fixture.receipt_kinds("receipts/wakes/", {"wake_attempt_receipt"}),
        )

    def test_wait_reads_the_harness_payload_from_standard_input(self) -> None:
        """A harness Stop hook supplies cwd and session id on stdin, not as flags."""

        fixture = WaitFixture(self.base, "verb-stdin", deadline_seconds=2)
        message = fixture.send("verb-stdin-ping")

        status, emitted = self.run_verb(
            fixture,
            session_id="seat-stdin",
            stdin_payload={
                "cwd": str(fixture.workspace),
                "session_id": "seat-stdin",
            },
        )

        self.assertEqual(0, status)
        self.assertIn(message["id"], json.loads(emitted)["reason"])

    def test_wait_without_a_payload_or_workspace_refuses_with_a_remedy(self) -> None:
        """An operator at a terminal must be told what to supply, never hang."""

        from floati import cli

        fixture = WaitFixture(self.base, "verb-refuse", deadline_seconds=2)
        stdout = io.StringIO()
        stdin = io.StringIO("")
        stdin.isatty = lambda: True  # type: ignore[method-assign]
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stdin", stdin):
            status = cli.main(
                ["wait", "--for", "fresh-work", "--root", str(fixture.bus_home)]
            )

        self.assertEqual(20, status)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("refused", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("wait_payload_absent", evidence["code"])
        self.assertIn("--workspace", evidence["remedy"])
        self.assertIn("--session-id", evidence["remedy"])

    def test_wait_never_resolves_its_root_from_the_ambient_environment(self) -> None:
        """The waiter's foreign-root property must survive the CLI wrapper."""

        fixture = WaitFixture(self.base, "verb-fence", deadline_seconds=2)
        fixture.send("verb-fence-ping")
        foreign = self.base / "foreign-bus"
        foreign.mkdir()
        observed: list[Path] = []
        real_open = Path.open

        def recording_open(path: Path, *args: object, **kwargs: object):
            observed.append(Path(path).resolve(strict=False))
            return real_open(path, *args, **kwargs)

        with mock.patch.dict(
            os.environ,
            {
                "AGENT_BUS_ROOT": str(foreign),
                "FLOATI_BUS_ROOT": str(foreign),
                "CODEX_BUS_AGENT": "foreign-node",
            },
            clear=False,
        ), mock.patch.object(Path, "open", recording_open):
            status, emitted = self.run_verb(fixture, session_id="seat-fence")

        self.assertEqual(0, status)
        self.assertIn('"decision": "block"', emitted)
        self.assertTrue(observed)
        for path in observed:
            with self.subTest(path=path):
                self.assertFalse(path == foreign or foreign in path.parents)

    # -- parity ------------------------------------------------------------

    def test_wait_and_the_codex_script_agree_on_a_fresh_work_fixture(self) -> None:
        """Parity is the row's retirement condition: same fixture, same outcome."""

        script_fixture = WaitFixture(self.base, "parity-script", deadline_seconds=2)
        verb_fixture = WaitFixture(self.base, "parity-verb", deadline_seconds=2)
        script_message = script_fixture.send("parity-ping")
        verb_message = verb_fixture.send("parity-ping")

        script_status, script_out = self.run_script(
            script_fixture, session_id="seat-parity"
        )
        verb_status, verb_out = self.run_verb(
            verb_fixture, session_id="seat-parity"
        )

        self.assertEqual(script_status, verb_status)
        self.assertEqual(
            self.normalize(script_out, script_message["id"]),
            self.normalize(verb_out, verb_message["id"]),
        )
        self.assertEqual(
            script_fixture.receipt_kinds(
                "receipts/wakes/", {"wake_attempt_receipt"}
            ),
            verb_fixture.receipt_kinds(
                "receipts/wakes/", {"wake_attempt_receipt"}
            ),
        )
        self.assertEqual(script_fixture.exit_reasons(), verb_fixture.exit_reasons())

    def test_wait_and_the_codex_script_agree_on_an_exhausted_fixture(self) -> None:
        """The quiet fixture: both must exhaust on the same bounded deadline."""

        script_fixture = WaitFixture(self.base, "quiet-script", deadline_seconds=1)
        verb_fixture = WaitFixture(self.base, "quiet-verb", deadline_seconds=1)

        script_status, script_out = self.run_script(
            script_fixture, session_id="seat-quiet"
        )
        verb_status, verb_out = self.run_verb(verb_fixture, session_id="seat-quiet")

        self.assertEqual(script_status, verb_status)
        self.assertIn(EXHAUSTION_REASON, script_out)
        self.assertEqual(script_out, verb_out)
        self.assertEqual(
            script_fixture.receipt_kinds(
                "receipts/codex-wait-exhaustion/",
                {"codex_wait_exhaustion_receipt"},
            ),
            verb_fixture.receipt_kinds(
                "receipts/codex-wait-exhaustion/",
                {"codex_wait_exhaustion_receipt"},
            ),
        )
        self.assertEqual(["exhausted"], script_fixture.exit_reasons())
        self.assertEqual(script_fixture.exit_reasons(), verb_fixture.exit_reasons())


if __name__ == "__main__":
    unittest.main()
