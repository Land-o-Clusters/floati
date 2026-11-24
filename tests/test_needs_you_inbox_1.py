"""NEEDS-YOU-INBOX-1: what needs a person is one durable read that never shows a moved subject as current.

The row (docs/status/FLOATI-FIELD-ROWS-2026-11-22.md on the board branch):
every item an agent raises for a person (a review, an approval, a question) is
a durable ledger record with a read state the person can see; an item bound to
a subject (a branch tip, a commit, a file set) goes STALE by itself when that
subject moves, and says what moved; the inbox is one pull read through the CLI
and MCP; nothing in it is ever presented as current after its subject changed.
Read state is the person's act, recorded. Each item is a bound record
(bound-record contract, version 1): the read returns the stored row verbatim
beside its derived freshness and chain, and a correction supersedes the row it
replaces.

No module in tests/ owned a person-facing item ledger, so this one does. Every
case drives the CLI or MCP boundary an agent or a person uses; the person's act
runs at a real controlling terminal.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import tests  # noqa: F401  Pin HOME before any product import resolves a path.

from floati import fixture_ids
from floati.mcp import McpServer, run_cli_artifact
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FILER = fixture_ids.builder("a")
OTHER = fixture_ids.builder("b")
# A hang guard for the interactive child, never a timing assertion: every wait
# below returns on the event (prompt text, EOF) and reports its load on failure.
CHILD_HANG_GUARD_SECONDS = 120.0
_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "PATH": "/usr/bin:/bin", "HOME": "/var/empty"}


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(["/usr/bin/git", "-C", str(repository), *arguments],
                               env=_GIT_ENV, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


class Fleet:
    """One root with two registered seats and one checkout with a feature branch."""

    def __init__(self, base: Path) -> None:
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        self.root = FloatiRoot.open_direct_home(base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register(FILER, "Codex")
        registry.register(OTHER, "Codex")
        self.checkout = base / "checkout"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q")
        for name in ("a.txt", "b.txt"):
            (self.checkout / name).write_text(name + " one\n")
        git(self.checkout, "add", "a.txt", "b.txt")
        git(self.checkout, "commit", "-q", "-m", "first")
        git(self.checkout, "branch", "feature")
        git(self.checkout, "checkout", "-q", "feature")
        self.first = git(self.checkout, "rev-parse", "HEAD")

    def commit(self, name: str, text: str) -> str:
        (self.checkout / name).write_text(text)
        git(self.checkout, "commit", "-q", "-am", "change " + name)
        return git(self.checkout, "rev-parse", "HEAD")

    def cli(self, *argv: str):
        return run_cli_artifact(list(argv))

    def file(self, key: str, *subject: str, node: str = FILER, kind: str = "review",
             title: str = "review this change"):
        return self.cli("needs", "file", "--root", str(self.root.path), "--as", node, "--kind", kind,
                        "--title", title, "--idempotency-key", key, *subject)

    def show(self, *extra: str) -> dict:
        code, artifact = self.cli("needs", "show", "--root", str(self.root.path), *extra)
        if code != 0:
            raise AssertionError("needs show did not succeed: " + json.dumps(artifact, sort_keys=True))
        return artifact["evidence"]

    def item(self, ident: str, *extra: str) -> dict:
        return next(entry for entry in self.show(*extra)["records"] if entry["record"]["id"] == ident)


def moved(freshness: dict) -> list:
    """Every move the read names, across the record's coordinates."""
    return [move for coordinate in freshness["coordinates"] for move in coordinate["moved"]]


def _filed(test: unittest.TestCase, result) -> dict:
    code, artifact = result
    test.assertEqual((0, "ok"), (code, artifact.get("status")), json.dumps(artifact, sort_keys=True))
    return artifact["evidence"]


def child_environment() -> dict:
    return dict(os.environ, PYTHONPATH=str(REPOSITORY_ROOT), PYTHONDONTWRITEBYTECODE="1")


class OperatorTerminal:
    """A real controlling terminal for the person, and a session without one."""

    def at_terminal(self, argv: list, *, cwd: Path, answer=None):
        """Run one person's act at a real controlling terminal and answer its prompt."""
        import pty
        started = time.monotonic()
        pid, descriptor = pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
                os.execve(argv[0], argv, child_environment())
            finally:
                os._exit(127)
        screen, answered = b"", False
        try:
            while True:
                remaining = CHILD_HANG_GUARD_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    os.kill(pid, 9)
                    self.fail("interactive act hung: bound %.0fs elapsed %.1fs load %s"
                              % (CHILD_HANG_GUARD_SECONDS, time.monotonic() - started, os.getloadavg()))
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
        artifacts = [json.loads(line) for line in text.splitlines() if line.startswith('{"artifact_version"')]
        return os.waitstatus_to_exitcode(status), (artifacts[-1] if artifacts else {}), text

    def without_terminal(self, argv: list, *, cwd: Path):
        completed = subprocess.run(argv, cwd=cwd, env=child_environment(), input=b"yes\n",
                                   capture_output=True, start_new_session=True,
                                   timeout=CHILD_HANG_GUARD_SECONDS)
        try:
            artifact = json.loads(completed.stdout)
        except json.JSONDecodeError:
            artifact = {}
        return completed.returncode, artifact


def floati_argv(*arguments: str) -> list:
    return [sys.executable, "-m", "floati", *arguments]


class FleetCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.fleet = Fleet(self.base)
        self.neutral = self.base / "person"
        self.neutral.mkdir()


class SubjectFreshnessTests(FleetCase):
    def test_a_bound_item_goes_stale_when_its_subject_moves_and_names_what_moved(self) -> None:
        cases = {
            "ref": (("--ref", "feature"), "a.txt"),
            "ref_and_untouched_file": (("--ref", "feature", "--path", "b.txt"), "a.txt"),
            "ref_and_touched_file": (("--ref", "feature", "--path", "b.txt"), "b.txt"),
            "file_set": (("--path", "a.txt"), None),
            "commit": (("--commit", None), "a.txt"),
        }
        for label, (subject, change) in cases.items():
            with self.subTest(subject=label):
                fleet = Fleet(self.base / label)
                subject = tuple(fleet.first if value is None else value for value in subject)
                filed = _filed(self, fleet.file("k-" + label.replace("_", "-"), "--repository",
                                                str(fleet.checkout), *subject))
                before = fleet.item(filed["id"])["freshness"]
                self.assertEqual("current", before["state"], before)
                if change is None:
                    (fleet.checkout / "a.txt").write_text("edited in the working tree\n")
                    after = fleet.item(filed["id"])["freshness"]
                    self.assertEqual("stale", after["state"], after)
                    self.assertEqual(["a.txt"], [m.get("path") for m in moved(after)])
                    (fleet.checkout / "a.txt").unlink()
                    gone = fleet.item(filed["id"])["freshness"]
                    self.assertEqual(("stale", None), (gone["state"], moved(gone)[0]["to"]))
                    continue
                tip = fleet.commit(change, change + " two\n")
                after = fleet.item(filed["id"])["freshness"]
                if label in ("ref_and_untouched_file", "commit"):
                    # The ref moved, but nothing the item is about changed.
                    self.assertEqual("current", after["state"], after)
                    continue
                self.assertEqual("stale", after["state"], after)
                self.assertIn({"what": "ref", "ref": "feature", "from": fleet.first, "to": tip},
                              moved(after))
                if label == "ref_and_touched_file":
                    self.assertIn({"what": "file", "path": "b.txt"}, moved(after))

    def test_every_filed_row_is_a_bound_record_the_schema_accepts(self) -> None:
        from tests.schema_validation import validate_json_schema

        checkout = str(self.fleet.checkout)
        filings = {
            "ref": ("--repository", checkout, "--ref", "feature", "--path", "a.txt"),
            "typed_commit_on_ref": ("--repository", checkout, "--ref", "feature", "--commit", self.fleet.first),
            "commit": ("--repository", checkout, "--commit", self.fleet.first),
            "files": ("--repository", checkout, "--path", "b.txt"),
            "none": (),
        }
        for label, coordinate in filings.items():
            with self.subTest(coordinate=label):
                row = _filed(self, self.fleet.file("schema-" + label.replace("_", "-"), *coordinate))
                validate_json_schema(row, REPOSITORY_ROOT / "schemas/v1/needs-item.schema.json")
                stamps = [entry["provenance"] for entry in row["coordinates"]]
                self.assertEqual({"ref": ["observed"], "typed_commit_on_ref": ["claimed"], "commit": ["claimed"],
                                  "files": ["observed"], "none": []}[label], stamps)
                self.assertEqual("claimed", row["statement"]["provenance"])

    def test_a_recorded_coordinate_is_honoured_or_refused_never_dropped(self) -> None:
        code, refused = self.fleet.file("k-commit-path", "--repository", str(self.fleet.checkout),
                                        "--commit", self.fleet.first, "--path", "a.txt")
        self.assertEqual((20, "needs_coordinate_invalid"), (code, refused["evidence"]["code"]))
        self.assertEqual([], self.fleet.show()["records"])

    def test_an_unreadable_subject_is_unknown_with_its_cause_never_current(self) -> None:
        filed = _filed(self, self.fleet.file("k-gone", "--repository", str(self.fleet.checkout),
                                             "--ref", "feature"))
        shutil.move(str(self.fleet.checkout), str(self.base / "moved-away"))
        freshness = self.fleet.item(filed["id"])["freshness"]
        self.assertEqual("unknown", freshness["state"], freshness)
        self.assertTrue(freshness["coordinates"][0]["cause"])


class PullReadTests(FleetCase):
    def test_one_pull_read_lists_every_seats_items_through_cli_and_mcp(self) -> None:
        mine = _filed(self, self.fleet.file("k-cli", "--repository", str(self.fleet.checkout),
                                            "--ref", "feature"))
        server = McpServer(str(self.fleet.root.path), OTHER, "session-b")
        catalog = {tool["name"]: tool["_meta"]["floati"]["exposure"] for tool in server.list_tools()}
        self.assertEqual("read", catalog.get("needs_show"))
        self.assertEqual("governed", catalog.get("needs_file"))
        for person_only in ("needs_read", "needs_budget", "needs_release"):
            self.assertNotIn(person_only, catalog)
        served = server.call_tool("needs_file", {"item_kind": "question", "title": "which base?",
                                                 "idempotency_key": "k-mcp"})
        self.assertFalse(served.get("isError"), served)
        theirs = served["structuredContent"]["evidence"]
        for read in (self.fleet.show(),
                     server.call_tool("needs_show", {})["structuredContent"]["evidence"]):
            self.assertEqual(("bound-record", 1), (read["contract"], read["contract_version"]))
            actors = {entry["record"]["id"]: entry["record"]["actor"] for entry in read["records"]}
            self.assertEqual({mine["id"]: FILER, theirs["id"]: OTHER}, actors)
            unbound = next(entry for entry in read["records"] if entry["record"]["id"] == theirs["id"])
            self.assertEqual("unbound", unbound["freshness"]["state"])
            self.assertEqual(theirs, unbound["record"])

    def test_filing_replays_by_key_corrections_supersede_and_closing_is_the_filers_act(self) -> None:
        first = _filed(self, self.fleet.file("k-one"))
        self.assertEqual(first["id"], _filed(self, self.fleet.file("k-one"))["id"])
        code, conflict = self.fleet.file("k-one", title="a different request")
        self.assertEqual((20, "idempotency_conflict"), (code, conflict["evidence"]["code"]))
        correction = _filed(self, self.fleet.file("k-two", "--supersedes", first["id"],
                                                  title="review this corrected change"))
        self.assertEqual([correction["id"]], [entry["record"]["id"] for entry in self.fleet.show()["records"]])
        old = self.fleet.item(first["id"], "--all")
        self.assertEqual({"head": False, "superseded_by": correction["id"]}, old["chain"])
        self.assertEqual("unread", self.fleet.item(correction["id"])["read"]["state"])
        code, again = self.fleet.file("k-three", "--supersedes", first["id"])
        self.assertEqual((20, "supersedes_not_head"), (code, again["evidence"]["code"]))
        first = correction
        close = ("needs", "close", "--root", str(self.fleet.root.path), "--item", first["id"],
                 "--reason", "merged", "--idempotency-key", "close-one")
        code, other = self.fleet.cli(*close[:4], "--as", OTHER, *close[4:])
        self.assertEqual((20, "needs_not_filer"), (code, other["evidence"]["code"]))
        self.assertIsNone(self.fleet.item(first["id"])["closed"])
        _filed(self, self.fleet.cli(*close[:4], "--as", FILER, *close[4:]))
        self.assertEqual([], self.fleet.show()["records"])
        self.assertEqual(FILER, self.fleet.item(first["id"], "--all")["closed"]["by"])


class ReadStateTests(OperatorTerminal, FleetCase):
    def read_argv(self, item: str, key: str) -> list:
        return floati_argv("needs", "read", "--root", str(self.fleet.root.path), "--item", item,
                           "--idempotency-key", key)

    def test_no_seat_or_session_without_a_terminal_can_mark_an_item_read(self) -> None:
        filed = _filed(self, self.fleet.file("k-read", "--repository", str(self.fleet.checkout),
                                             "--ref", "feature"))
        code, artifact = self.without_terminal(self.read_argv(filed["id"], "read-1"), cwd=self.neutral)
        self.assertEqual((20, "needs_human_act_unavailable"), (code, artifact.get("evidence", {}).get("code")))
        seat = self.base / "seat"
        seat.mkdir()
        (seat / "SEAT.json").write_text("{}")
        code, artifact, _screen = self.at_terminal(self.read_argv(filed["id"], "read-2"), cwd=seat,
                                                   answer=lambda shown: shown)
        self.assertEqual((20, "needs_human_act_seat_context"), (code, artifact.get("evidence", {}).get("code")))
        self.assertEqual("unread", self.fleet.item(filed["id"])["read"]["state"])

    def test_a_persons_read_is_recorded_and_a_later_move_is_not_hidden_by_it(self) -> None:
        filed = _filed(self, self.fleet.file("k-read", "--repository", str(self.fleet.checkout),
                                             "--ref", "feature"))
        unread = self.fleet.item(filed["id"])
        code, refused, _screen = self.at_terminal(self.read_argv(filed["id"], "read-1"), cwd=self.neutral,
                                                  answer="WRONG")
        self.assertEqual((20, "needs_human_act_unconfirmed"), (code, refused.get("evidence", {}).get("code")))
        self.assertEqual(unread["read"], self.fleet.item(filed["id"])["read"])
        code, recorded, screen = self.at_terminal(self.read_argv(filed["id"], "read-1"), cwd=self.neutral,
                                                  answer=lambda shown: shown)
        self.assertEqual(0, code, screen)
        self.assertEqual("controlling_terminal", recorded["evidence"]["human_act"]["channel"])
        read = self.fleet.item(filed["id"])
        self.assertEqual(("unread", "read"), (unread["read"]["state"], read["read"]["state"]))
        self.assertEqual(recorded["evidence"]["id"], read["read"]["receipt_id"])
        self.fleet.commit("a.txt", "moved after it was read\n")
        moved = self.fleet.item(filed["id"])
        self.assertEqual(("stale", "read_before_move"),
                         (moved["freshness"]["state"], moved["read"]["state"]))


class HeldMailTests(FleetCase):
    def test_held_inbound_mail_is_listed_from_its_records_and_never_written(self) -> None:
        ledger = self.fleet.root.path / "receipts" / "inbound-holds" / (OTHER + ".jsonl")
        ledger.parent.mkdir(parents=True)
        row = {"schema_version": 1, "id": "inbound-hold-" + "0" * 12 + "7000" + "8" + "0" * 15,
               "tenant_id": self.fleet.root.tenant_id, "timestamp": "2026-11-22T00:00:00.000Z",
               "kind": "inbound_hold_receipt", "node_id": OTHER, "phase": "held", "hold_id": None,
               "reason": "review inbound mail", "started_at": "2026-11-22T00:00:00.000Z",
               "expected_release": None, "released_at": None, "message_id": None,
               "envelope_digest": None, "decided_at": None, "human_act": None,
               "idempotency_key": "hold-1"}
        ledger.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        before = sorted((path.name, path.read_bytes()) for path in ledger.parent.iterdir())
        held = self.fleet.show()["held_mail"]
        try:
            import floati.inbound_hold  # noqa: F401
            expected = "read"
        except ImportError:
            expected = "unknown"
        self.assertEqual(expected, held["state"], held)
        if expected == "unknown":
            self.assertTrue(held["cause"])
        self.assertEqual(before, sorted((path.name, path.read_bytes()) for path in ledger.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
