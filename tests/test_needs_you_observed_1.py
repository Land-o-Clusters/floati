"""NEEDS-YOU-OBSERVED-1: an item enters the needs-you inbox because Floati observed it.

The row (docs/status/ROWS.md on the board branch): a launched thread that
finished, or that waits on a person, becomes an inbox item read from the
orchestrator's own local list, not only because an agent raised it; each item
says its source. Its RED was measured on 2026-11-22: a launched thread
finished and no item existed until a person noticed it about seven minutes
later.

The laws this module holds:
1. Dedupe is on the event, never the object: a thread that completes a second
   turn after a follow-up is a new item, and the first one is superseded.
2. Observed facts are stamped observed, with their coordinates, in the inbox's
   bound records; a thread waiting on its own background task is not waiting
   on a person.
3. The read is read-only toward the orchestrator, takes no credentials, and
   comes only from an explicit validated source configuration; an absent or
   unreadable source is a typed absence naming its cause, never an empty inbox.
4. The orchestrator here is a fixture executable in a temporary directory; no
   case reads a real orchestrator install or its data.

A new module because the inbox module's fixture is a Git checkout, and these
cases need a fixture orchestrator and a source configuration instead.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  Pin HOME before any product import resolves a path.

from floati import fixture_ids
from floati.mcp import run_cli_artifact
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OBSERVER = fixture_ids.builder("a")
FILER = fixture_ids.builder("b")
PROJECT = "fixture-project"
SECRET_NAME = "FIXTURE_ORCHESTRATOR_TOKEN"

# The fixture orchestrator: prints the thread list kept in its own home, exits
# with the code an "exit" file names, and appends what it was given (argv and
# environment names) to a probe file outside that home.
_ORCHESTRATOR = """#!{python} -I
import json, os, sys
with open({probe!r}, "a") as probe:
    probe.write(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ)}}) + "\\n")
home = os.environ.get("HOME", "")
code = 0
if os.path.exists(os.path.join(home, "exit")):
    code = int(open(os.path.join(home, "exit")).read())
sys.stdout.write(open(os.path.join(home, "threads.json")).read())
sys.exit(code)
"""


def thread(ident: str, status: str, run: object = "run-1", *, title: str = "a builder thread",
           background=(), pending: int = 0, settled: bool = False) -> dict:
    return {"threadId": ident, "title": title, "status": status, "latestRunId": run,
            "pendingBackgroundTasks": list(background), "pendingRequestCount": pending,
            "settled": settled, "archived": False}


class Orchestrator:
    """One fixture orchestrator, its home and the source configuration naming both."""

    def __init__(self, base: Path) -> None:
        self.home = base / "orchestrator-home"
        self.home.mkdir()
        self.probe = base / "orchestrator-probe.jsonl"
        self.cli = base / "orchestrator"
        self.cli.write_text(_ORCHESTRATOR.format(python=sys.executable, probe=str(self.probe)))
        self.cli.chmod(stat.S_IRWXU)
        self.config = base / "observed-source.json"
        self.write_config()
        self.threads([])

    def write_config(self, **change: object) -> None:
        value = {"schema_version": 1, "orchestrator": "t3-code", "cli": str(self.cli),
                 "home": str(self.home), "project": PROJECT, **change}
        self.config.write_text(json.dumps(value))

    def threads(self, rows: list) -> None:
        (self.home / "threads.json").write_text(json.dumps({"threads": rows}))

    def calls(self) -> list:
        if not self.probe.exists():
            return []
        return [json.loads(line) for line in self.probe.read_text().splitlines()]


class ObservedCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register(OBSERVER, "Codex")
        registry.register(FILER, "Codex")
        self.orchestrator = Orchestrator(self.base)

    def cli(self, *argv: str):
        return run_cli_artifact(list(argv))

    def observe(self, config: Path = None):
        return self.cli("needs", "observe", "--root", str(self.root.path), "--as", OBSERVER,
                        "--source-config", str(self.orchestrator.config if config is None else config))

    def observed(self) -> dict:
        code, artifact = self.observe()
        self.assertEqual((0, "ok"), (code, artifact.get("status")), json.dumps(artifact, sort_keys=True))
        return artifact["evidence"]

    def show(self, *extra: str) -> dict:
        code, artifact = self.cli("needs", "show", "--root", str(self.root.path), *extra)
        self.assertEqual(0, code, json.dumps(artifact, sort_keys=True))
        return artifact["evidence"]

    def observed_entries(self, *extra: str) -> dict:
        """Each observed entry of the read, keyed by the thread its coordinate names."""
        entries = {}
        for entry in self.show(*extra)["records"]:
            if entry["source"]["type"] == "observed":
                (coordinate,) = entry["record"]["coordinates"]
                entries.setdefault(coordinate["thread_id"], []).append(entry)
        return entries


class ObservedItemTests(ObservedCase):
    def test_a_thread_that_finished_or_waits_on_a_person_is_an_item_nobody_filed(self) -> None:
        from tests.schema_validation import validate_json_schema

        # Each thread's expected item kind, or None when it needs nobody.
        expected = {
            "t-completed": ("completed", {}, "review"),
            "t-interrupted": ("interrupted", {}, "review"),
            "t-failed": ("failed", {}, "review"),
            "t-error": ("error", {}, "review"),
            "t-waiting-on-person": ("waiting", {}, "question"),
            "t-request-pending": ("running", {"pending": 1}, "question"),
            "t-waiting-on-own-task": ("waiting", {"background": ["remote-run"]}, None),
            "t-running": ("running", {}, None),
            "t-settled": ("completed", {"settled": True}, None),
        }
        self.orchestrator.threads([thread(ident, status, "run-" + ident, **extra)
                                   for ident, (status, extra, _kind) in expected.items()])
        raised = run_cli_artifact(["needs", "file", "--root", str(self.root.path), "--as", FILER,
                                   "--kind", "question", "--title", "which base?",
                                   "--idempotency-key", "raised-1"])
        self.assertEqual(0, raised[0], raised)
        self.assertEqual({}, self.observed_entries(), "nothing is observed before the source is read")
        self.observed()
        entries = self.observed_entries()
        for ident, (status, _extra, kind) in expected.items():
            with self.subTest(thread=ident):
                if kind is None:
                    self.assertNotIn(ident, entries)
                    continue
                (entry,) = entries[ident]
                record = entry["record"]
                validate_json_schema(record, REPOSITORY_ROOT / "schemas/v1/needs-observed.schema.json")
                self.assertEqual((kind, "observed", OBSERVER),
                                 (record["item_kind"], record["statement"]["provenance"], record["actor"]))
                (coordinate,) = record["coordinates"]
                self.assertEqual({"provenance": "observed", "orchestrator": "t3-code", "project": PROJECT,
                                  "thread_id": ident, "run_id": "run-" + ident, "status": status},
                                 {key: coordinate[key] for key in ("provenance", "orchestrator", "project",
                                                                   "thread_id", "run_id", "status")})
                self.assertEqual({"type": "observed", "orchestrator": "t3-code", "project": PROJECT,
                                  "observer": OBSERVER}, entry["source"])
                self.assertEqual(("current", "unread"), (entry["freshness"]["state"], entry["read"]["state"]))
        everyone = self.show()["records"]
        self.assertIn({"type": "raised", "by": FILER},
                      [entry["source"] for entry in everyone], "a raised item says its source too")
        attention = self.cli("needs", "admission", "--root", str(self.root.path))[1]["evidence"]
        self.assertEqual(4, attention["unread_reviews"], "finished threads are review items for the person")

    def test_bounding_a_long_title_does_not_hold_the_needs_writer_lock(self) -> None:
        import fcntl
        from floati import needs_observed

        title = "界" * 8192
        self.orchestrator.threads([thread("t-long", "completed", title=title)])
        lock = self.root.path / "receipts" / "needs" / "ledger.jsonl.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        original = needs_observed.statement_title
        lock_available = []

        def format_title(*args):
            with lock.open("a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock_available.append(False)
                else:
                    lock_available.append(True)
            return original(*args)

        with mock.patch.object(needs_observed, "statement_title", side_effect=format_title):
            (item,) = self.observed()["appended"]
        self.assertNotIn(False, lock_available, "title work blocked independent needs writers")
        stored = item["statement"]["title"]
        self.assertTrue(stored.endswith("界"))
        self.assertLessEqual(len(stored.encode("utf-8")), 256)

    def test_dedupe_is_on_the_event_so_a_second_completion_is_a_new_item(self) -> None:
        self.orchestrator.threads([thread("t-1", "completed", "run-1")])
        first = self.observed()["appended"]
        self.assertEqual(1, len(first))
        ledger = self.root.path / "receipts" / "needs" / "ledger.jsonl"
        before = ledger.read_bytes()
        again = self.observed()
        self.assertEqual(([], before), (again["appended"], ledger.read_bytes()),
                         "the same event observed twice is one item")
        # A follow-up turn runs, then completes again: a new event on the same thread.
        self.orchestrator.threads([thread("t-1", "running", "run-2")])
        self.assertEqual([], self.observed()["appended"])
        self.orchestrator.threads([thread("t-1", "completed", "run-2")])
        second = self.observed()["appended"]
        self.assertEqual(1, len(second), "a second completion after a follow-up is never dropped")
        (head,) = self.observed_entries()["t-1"]
        self.assertEqual((second[0]["id"], "run-2"),
                         (head["record"]["id"], head["record"]["coordinates"][0]["run_id"]))
        history = {entry["record"]["id"]: entry for entry in self.observed_entries("--all")["t-1"]}
        old = history[first[0]["id"]]
        self.assertEqual({"head": False, "superseded_by": second[0]["id"]}, old["chain"])
        self.assertEqual("stale", old["freshness"]["state"])
        # The observer is the filer of what it observed, so it can close it.
        code, closed = self.cli("needs", "close", "--root", str(self.root.path), "--as", OBSERVER,
                                "--item", second[0]["id"], "--reason", "looked at it",
                                "--idempotency-key", "close-1")
        self.assertEqual(0, code, closed)
        self.assertEqual({}, self.observed_entries())


class SourceTests(ObservedCase):
    def test_an_absent_or_unreadable_source_is_a_typed_absence_never_an_empty_inbox(self) -> None:
        never = self.show()["observed_sources"]
        self.assertEqual("none_recorded", never["state"], never)
        self.assertTrue(never["cause"])
        self.orchestrator.threads([thread("t-1", "completed")])
        refused = {
            "absent": (self.base / "no-such-config.json", "needs_source_unconfigured"),
            "relative_cli": ({"cli": "orchestrator"}, "needs_source_config_invalid"),
            "home_missing": ({"home": str(self.base / "nowhere")}, "needs_source_config_invalid"),
            "extra_field": ({"token": "x"}, "needs_source_config_invalid"),
            "unknown_orchestrator": ({"orchestrator": "other"}, "needs_source_config_invalid"),
        }
        for label, (change, code) in refused.items():
            with self.subTest(source=label):
                if isinstance(change, Path):
                    exit_code, artifact = self.observe(change)
                else:
                    self.orchestrator.write_config(**change)
                    exit_code, artifact = self.observe()
                self.assertEqual((20, code), (exit_code, artifact["evidence"].get("code")), artifact)
        self.orchestrator.write_config()
        for label, target in (("config_writable_by_others", self.orchestrator.config),
                              ("cli_writable_by_others", self.orchestrator.cli)):
            with self.subTest(source=label):
                mode = target.stat().st_mode
                target.chmod(mode | stat.S_IWOTH)
                exit_code, artifact = self.observe()
                target.chmod(mode)
                self.assertEqual((20, "needs_source_config_invalid"),
                                 (exit_code, artifact["evidence"].get("code")), artifact)
        self.assertEqual([], self.orchestrator.calls(), "a refused configuration never runs the orchestrator")
        unreadable = {
            "exit_nonzero": lambda: (self.orchestrator.home / "exit").write_text("3"),
            "not_json": lambda: (self.orchestrator.home / "threads.json").write_text("not json"),
            "wrong_shape": lambda: (self.orchestrator.home / "threads.json").write_text('{"threads": 3}'),
        }
        for label, break_it in unreadable.items():
            with self.subTest(source=label):
                self.orchestrator.threads([thread("t-1", "completed")])
                (self.orchestrator.home / "exit").unlink(missing_ok=True)
                break_it()
                exit_code, artifact = self.observe()
                self.assertEqual((35, "degraded"), (exit_code, artifact["status"]), artifact)
                evidence = artifact["evidence"]
                self.assertEqual(("unknown", []), (evidence["observation"]["state"], evidence["appended"]))
                self.assertTrue(evidence["observation"]["cause"]["code"])
                (source,) = self.show()["observed_sources"]["sources"]
                self.assertEqual(("unknown", evidence["observation"]["cause"]["code"]),
                                 (source["outcome"], source["cause"]["code"]))
        (self.orchestrator.home / "exit").unlink(missing_ok=True)
        self.orchestrator.threads([thread("t-1", "completed")])
        self.observed()
        (source,) = self.show()["observed_sources"]["sources"]
        self.assertEqual(("read", None), (source["outcome"], source["cause"]))

    def test_a_count_beyond_the_durable_bound_degrades_the_entire_pull(self) -> None:
        maximum = 2**31 - 1
        self.orchestrator.threads([thread("t-bound", "running", pending=maximum)])
        (item,) = self.observed()["appended"]
        self.assertEqual(maximum, item["coordinates"][0]["pending_requests"])

        self.orchestrator.threads([thread("t-valid", "completed"),
                                   thread("t-over", "running", pending=maximum + 1)])
        code, artifact = self.observe()
        self.assertEqual((35, "degraded"), (code, artifact["status"]))
        self.assertEqual("source_output_invalid", artifact["evidence"]["observation"]["cause"]["code"])
        self.assertEqual([], artifact["evidence"]["appended"])
        self.assertEqual({"t-bound"}, set(self.observed_entries()))

    def test_source_trust_is_checked_on_each_opened_file(self) -> None:
        real_fstat = os.fstat

        for target in (self.orchestrator.config, self.orchestrator.cli):
            with self.subTest(file=target.name):
                selected = target.stat()

                def opened_metadata(descriptor):
                    metadata = real_fstat(descriptor)
                    if (metadata.st_dev, metadata.st_ino) == (selected.st_dev, selected.st_ino):
                        fields = list(metadata)
                        fields[0] |= stat.S_IWGRP
                        return os.stat_result(fields)
                    return metadata

                with mock.patch("floati.needs_observed.os.fstat", side_effect=opened_metadata):
                    code, artifact = self.observe()
                self.assertEqual((20, "needs_source_config_invalid"),
                                 (code, artifact["evidence"].get("code")))
                self.assertEqual([], self.orchestrator.calls())

    def test_the_read_writes_nothing_to_the_orchestrator_and_passes_it_no_credentials(self) -> None:
        self.orchestrator.threads([thread("t-1", "completed"), thread("t-2", "waiting")])
        home_before = sorted((path.name, path.read_bytes()) for path in self.orchestrator.home.iterdir())
        with mock.patch.dict(os.environ, {SECRET_NAME: "must-not-reach-the-orchestrator"}),\
                mock.patch("floati.needs_observed.subprocess.Popen", wraps=subprocess.Popen) as popen:
            self.observed()
        (submitted_env,) = [
            launch.kwargs["env"] for launch in popen.call_args_list
            if launch.args[0][0] == str(self.orchestrator.cli)
        ]
        # Inspect the submitted keys before the host or interpreter adds its own.
        self.assertEqual({"HOME", "PATH", "LANG", "LC_ALL"}, set(submitted_env))
        self.assertEqual(home_before,
                         sorted((path.name, path.read_bytes()) for path in self.orchestrator.home.iterdir()))
        (call,) = self.orchestrator.calls()
        self.assertNotIn(SECRET_NAME, call["env"])
        self.assertEqual(["thread", "list", "--project", PROJECT, "--json"], call["argv"])


if __name__ == "__main__":
    unittest.main()
