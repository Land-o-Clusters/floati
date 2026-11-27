from __future__ import annotations

from tests.readiness import READY_SECONDS
from tests.test_cli import LAUNCHER

from floati import fixture_ids as public_ids

import hashlib
import io
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from floati.events import EventLog
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.host_paths import worker_workspace_root
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog

# The dot-prefixed workspace name the pre-rename product wrote, built from
# the fence's own governed token rather than spelled: these fixtures drive a
# refusal (or assert an absence) whose whole mechanism is these exact bytes.
LEGACY_PREFIX = "." + RETIRED_PRODUCT_NAME
from floati.workers import WorkerReceipts


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DARWIN_VARIABLE = "__CF_USER_TEXT_ENCODING"
_DARWIN_SENTINEL = "0x0:0xfeed:0xface"
NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 40


class _CliOrchestrateAdapter:
    name = "codex"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        return item["id"]

    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float
    ) -> list[dict[str, str]]:
        time.sleep(0.05)
        return []


class _CliHangingAdapter(_CliOrchestrateAdapter):
    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float
    ) -> list[dict[str, str]]:
        time.sleep(10)
        return []


class _CliFailingAdapter(_CliOrchestrateAdapter):
    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float
    ) -> list[dict[str, str]]:
        from floati.workers import WorkerAdapterFailure

        raise WorkerAdapterFailure("turn_failed")


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def authoritative_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".floati-snapshots" in relative.parts:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class CliWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "Codex")
        registry.register("bravo", "Codex")
        self.grant = AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )
        self.destination = Path(self.temp.name) / "installed"
        destination_scripts = self.destination / "scripts"
        destination_scripts.mkdir(parents=True)
        (destination_scripts / "floati").write_bytes(b"installed\n")
        self.shadow = Path(self.temp.name) / "shadow"
        self.shadow.mkdir()
        (self.shadow / "floati").write_bytes(b"shadow\n")
        self.shadow_environment = {
            **os.environ,
            "PATH": os.pathsep.join((str(self.shadow), str(destination_scripts))),
        }

    def run_cli(
        self, *args: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "floati", *args], cwd=REPOSITORY_ROOT,
            check=False, capture_output=True, text=True, env=environment,
        )

    @staticmethod
    def artifact(result: subprocess.CompletedProcess[str]) -> dict:
        return json.loads(result.stdout)

    def test_dispatch_rebuilds_roster_and_flags_returns_to_old_briefs(self) -> None:
        from floati.cli import main
        from tests.git_fixture_cache import fixture_git
        from tests.schema_validation import validate_json_schema

        checkout = Path(self.temp.name) / "checkout"
        checkout.mkdir()
        git = [fixture_git("/usr/bin/git"), "-C", str(checkout)]
        subprocess.run([*git, "init", "-q", "-b", "review"], check=True)
        subprocess.run(
            [*git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "commit", "-qm", "Fixture", "--allow-empty"], check=True,
        )
        brief = Path(self.temp.name) / "brief.txt"
        original = "Review the first proposal.\n"
        brief.write_text(original, encoding="utf-8")
        actor = public_ids.worker("alpha")

        def dispatch(command, *args, ok=True, fresh=False):
            argv = ("dispatch", command, "--root", str(self.home), *args)
            if fresh:
                result = self.run_cli(*argv)
                status, output = result.returncode, result.stdout
            else:
                stream = io.StringIO()
                with redirect_stdout(stream):
                    status = main(argv)
                output = stream.getvalue()
            self.assertEqual(0 if ok else 20, status, output)
            return json.loads(output)["evidence"]

        def record(key, *args, dispatch_id="review-one", recipient="bravo", **options):
            return dispatch("record", "--id", dispatch_id, "--as", actor,
                            "--to", recipient, "--branch", "review", "--brief-file", str(brief),
                            "--external", "native task", "--idempotency-key", key, *args, **options)

        first = record("original")["record"]
        validate_json_schema(first, REPOSITORY_ROOT / "schemas/v1/dispatch-revision-record.schema.json")
        self.assertEqual(first, record("original")["record"])
        self.assertEqual("observed", first["brief"]["provenance"])
        self.assertEqual("claimed", first["carrier"]["provenance"])
        brief.write_text("Review the revised proposal.\n", encoding="utf-8")
        self.assertEqual("supersedes_not_head", record("accidental", ok=False)["code"])
        record("correction", "--supersedes", first["id"])
        record("another", dispatch_id="review-two", recipient="native-reviewer")

        def returned(key, *args, by=actor, **options):
            return dispatch(
                "return", "--id", "review-one", "--as", by,
                "--repository", str(checkout), "--ref", "refs/heads/review",
                "--artifact", "result.md", "--statement", "Review completed",
                "--idempotency-key", key, *args, **options,
            )

        self.assertEqual("dispatch_actor_mismatch",
                         returned("child", "--revision", "2", by="bravo", ok=False)["code"])
        self.assertEqual("coordinate_unresolved", returned("future", "--revision", "3", ok=False)["code"])
        stale = returned("old", "--revision", "1")["record"]
        validate_json_schema(stale, REPOSITORY_ROOT / "schemas/v1/dispatch-return-record.schema.json")
        brief.unlink()
        view = dispatch("show", "--json", fresh=True)
        self.assertEqual("bound-record", view["contract"])
        self.assertEqual(
            [("bravo", "review", 2, "returned_stale"), ("native-reviewer", "review", 1, "out")],
            [(r["recipient"], r["branch"], r["revision"], r["state"]) for r in view["roster"]],
        )
        prior = next(entry for entry in view["records"] if entry["record"] == stale)
        self.assertEqual("stale", prior["freshness"]["state"])
        self.assertEqual({"what": "brief", "dispatch_id": "review-one", "from": 1, "to": 2},
                         prior["freshness"]["coordinates"][0]["moved"][0])
        self.assertFalse(next(entry["chain"]["head"] for entry in view["records"] if entry["record"] == first))
        self.assertNotIn(original.strip(), json.dumps(view))
        self.assertEqual(original, dispatch("brief", "--id", "review-one", "--revision", "1",
                                            "--as", actor)["text"])

        returned("current", "--revision", "2")
        self.assertEqual("returned", dispatch("show", "--id", "review-one")["roster"][0]["state"])
        close = dispatch("close", "--id", "review-one", "--as", actor, "--reason", "Review complete",
                         "--idempotency-key", "closed")["record"]
        validate_json_schema(close, REPOSITORY_ROOT / "schemas/v1/dispatch-close-record.schema.json")
        self.assertEqual("closed", dispatch("show", "--id", "review-one")["roster"][0]["state"])

    def test_status_and_supervise_share_three_plane_snapshot(self) -> None:
        status = self.run_cli(
            "status", "--root", str(self.home), "--destination", str(self.destination),
            environment=self.shadow_environment,
        )
        before = tree_digest(self.home)
        supervise = self.run_cli("supervise", "--root", str(self.home))
        after = tree_digest(self.home)

        self.assertEqual(0, status.returncode, status.stderr)
        self.assertEqual(0, supervise.returncode, supervise.stderr)
        status_nodes = self.artifact(status)["evidence"]["nodes"]
        supervised_nodes = self.artifact(supervise)["evidence"]["nodes"]
        self.assertTrue(all("wake_health" in node for node in status_nodes))
        self.assertTrue(all("wake_health" not in node for node in supervised_nodes))

        def shared_node(node: dict) -> dict:
            self.assertIn("seat_attention", node)
            attention = node["seat_attention"]
            observed = datetime.fromisoformat(attention["observed_at"].replace("Z", "+00:00"))
            self.assertIsNotNone(observed.utcoffset())
            age = attention["oldest_waiting_age_seconds"]
            self.assertTrue(age is None or isinstance(age, (int, float)))
            # These CLI commands observe at separate times. Keep the complete
            # stable fact; exact timed parity is covered at one native clock.
            return {
                **{key: value for key, value in node.items() if key != "wake_health"},
                "seat_attention": {
                    key: value for key, value in attention.items()
                    if key not in {"observed_at", "oldest_waiting_age_seconds"}
                },
            }

        self.assertEqual(
            [shared_node(node) for node in status_nodes],
            [shared_node(node) for node in supervised_nodes],
        )
        self.assertEqual({"liveness", "authority", "mutex"},
                         {key for key in status_nodes[0] if key in {"liveness", "authority", "mutex"}})
        self.assertNotIn("status_schema_version", self.artifact(status)["evidence"])
        self.assertNotIn("kind", self.artifact(status)["evidence"])
        self.assertNotIn("root", self.artifact(status)["evidence"])
        self.assertNotIn("tenant_id", self.artifact(status)["evidence"])
        self.assertNotIn("mode", self.artifact(status)["evidence"])
        self.assertEqual("found", self.artifact(status)["evidence"]["installer_shadow"]["outcome"])
        self.assertFalse(os.path.lexists(self.home / f"{LEGACY_PREFIX}-snapshots"))
        self.assertEqual(before, after)

    def test_status_json_is_an_explicit_versioned_read_only_contract(self) -> None:
        before = authoritative_tree_digest(self.home)
        result = self.run_cli(
            "status", "--root", str(self.home), "--destination", str(self.destination), "--json",
            environment=self.shadow_environment,
        )
        after = authoritative_tree_digest(self.home)

        self.assertEqual(0, result.returncode, result.stderr)
        artifact = self.artifact(result)
        self.assertEqual(0, artifact["artifact_version"])
        self.assertEqual("status", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(1, artifact["evidence"]["status_schema_version"])
        self.assertEqual("fleet_status", artifact["evidence"]["kind"])
        self.assertEqual(str(self.home.resolve()), artifact["evidence"]["root"])
        self.assertEqual(self.root.tenant_id, artifact["evidence"]["tenant_id"])
        self.assertEqual("report_only", artifact["evidence"]["mode"])
        from tests.schema_validation import validate_json_schema

        validate_json_schema(
            artifact,
            REPOSITORY_ROOT / "schemas/v1/fleet-status-artifact.schema.json",
        )
        snapshots = list((self.home / ".floati-snapshots" / "v0").glob("status-*.json"))
        self.assertEqual(1, len(snapshots))
        self.assertFalse(os.path.lexists(self.home / f"{LEGACY_PREFIX}-snapshots"))
        self.assertEqual(before, after)

    def test_plan_requires_explicit_absolute_inputs_and_has_zero_effect(self) -> None:
        """Catches a plan explanation that discovers a root, writes evidence, or treats refusal as an execution failure."""
        from tests.test_admission import ITEM_A, VALID_POLICY

        input_directory = Path(self.temp.name).resolve()
        plan = input_directory / "admission-plan.json"
        policy = input_directory / "FLOATI.toml"
        plan.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "workers": [{"node_id": "node-a", "worker_profile": "good"}],
                    "max_active_attempts": 1,
                    "budget_reservations": [{"budget_id": "build", "amount": 1}],
                    "items": [{
                        "item_id": ITEM_A,
                        "contract": {
                            "objective": "admit bounded work",
                            "non_goals": ["no model authority"],
                            "areas_to_avoid": [{"path": "floati/graph.py", "region": "all"}],
                            "input_hashes": {"brief": "a" * 64},
                            "acceptance_checks": {"tests.unit": "python3 -m unittest"},
                            "constraints": {"network": "dark"},
                            "risk_class": "low",
                            "retry_policy": {
                                "max_attempts": 1,
                                "backoff": {
                                    "base_delay_ms": 0,
                                    "cap_delay_ms": 0,
                                    "strategy": "fixed",
                                },
                            },
                            "dependencies": [],
                        },
                        "capability_selector": "review_write",
                        "requires_cancellation": True,
                        "requires_callback": True,
                        "workspace_key": "workspace-a",
                        "concurrency_key": "concurrency-a",
                        "retry_class": "transient",
                        "effect_safety": "idempotent",
                        "merge_gate": None,
                    }],
                    "dependency_edges": [],
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        policy.write_text(VALID_POLICY, encoding="utf-8")
        refused_plan = input_directory / "refused-plan.json"
        refused = json.loads(plan.read_text(encoding="utf-8"))
        refused["budget_reservations"][0]["amount"] = 6
        refused_plan.write_text(json.dumps(refused, separators=(",", ":")), encoding="utf-8")
        operator_plan = input_directory / "operator-plan.json"
        operator = json.loads(plan.read_text(encoding="utf-8"))
        operator["items"][0]["contract"]["risk_class"] = "high"
        operator_plan.write_text(json.dumps(operator, separators=(",", ":")), encoding="utf-8")
        before = tree_digest(input_directory)

        first = self.run_cli(
            "plan", "--root", str(self.home), "--plan", str(plan), "--policy", str(policy), "--explain", "--json"
        )
        second = self.run_cli(
            "plan", "--root", str(self.home), "--plan", str(plan), "--policy", str(policy), "--explain", "--json"
        )
        refused_result = self.run_cli(
            "plan", "--root", str(self.home), "--plan", str(refused_plan), "--policy", str(policy), "--explain", "--json"
        )
        operator_result = self.run_cli(
            "plan", "--root", str(self.home), "--plan", str(operator_plan), "--policy", str(policy), "--explain", "--json"
        )
        missing_plan = self.run_cli("plan", "--root", str(self.home), "--policy", str(policy), "--explain")
        missing_policy = self.run_cli("plan", "--root", str(self.home), "--plan", str(plan), "--explain")
        missing_explain = self.run_cli("plan", "--root", str(self.home), "--plan", str(plan), "--policy", str(policy))
        relative_plan = self.run_cli(
            "plan", "--root", str(self.home), "--plan", "relative-plan.json", "--policy", str(policy), "--explain"
        )
        relative_policy = self.run_cli(
            "plan", "--root", str(self.home), "--plan", str(plan), "--policy", "FLOATI.toml", "--explain"
        )
        after = tree_digest(input_directory)

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual("", first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        artifact = self.artifact(first)
        self.assertEqual("plan", artifact["command"])
        self.assertEqual("admitted", artifact["status"])
        self.assertEqual("plan_admission", artifact["evidence"]["kind"])
        for expected_status, result in (("refused", refused_result), ("needs_operator", operator_result)):
            with self.subTest(status=expected_status):
                self.assertEqual(0, result.returncode, result.stderr)
                result_artifact = self.artifact(result)
                self.assertEqual(expected_status, result_artifact["status"])
                self.assertEqual("plan_admission", result_artifact["evidence"]["kind"])
        for label, result in (
            ("plan", missing_plan),
            ("policy", missing_policy),
            ("explain", missing_explain),
        ):
            with self.subTest(missing=label):
                self.assertEqual(20, result.returncode)
                self.assertEqual("", result.stderr)
                self.assertEqual("arguments_invalid", self.artifact(result)["evidence"]["code"])
        for label, result in (("plan", relative_plan), ("policy", relative_policy)):
            with self.subTest(relative=label):
                self.assertEqual(20, result.returncode)
                self.assertEqual("", result.stderr)
                self.assertEqual("refused", self.artifact(result)["status"])
        self.assertEqual(before, after)

    def test_receipts_command_returns_distinct_node_history(self) -> None:
        event = EventLog(self.root).send(
            public_ids.worker('alpha'), "bravo", "floati", SHA,
            "docs/evidence/checkpoint.md", "notice", idempotency_key="receipt-cli",
        )
        EventLog(self.root).present("bravo")

        result = self.run_cli("receipts", "bravo", "--root", str(self.home))

        self.assertEqual(0, result.returncode, result.stderr)
        history = self.artifact(result)["evidence"]
        self.assertEqual(event["id"], history["deliveries"][0]["item_ids"][0])
        self.assertEqual([], history["acks"])
        self.assertEqual([], history["denials"])

    def test_work_add_claim_complete_show_round_trip(self) -> None:
        added_result = self.run_cli(
            "work", "add", "--root", str(self.home), "--title", "build board",
            "--owner", public_ids.worker('alpha'), "--repo", "floati", "--sha", SHA,
            "--doc", "docs/evidence/input.md",
        )
        self.assertEqual(0, added_result.returncode, added_result.stderr)
        item_id = self.artifact(added_result)["evidence"]["id"]

        duplicate = self.run_cli(
            "work", "add", "--root", str(self.home), "--title", "build board",
            "--owner", public_ids.worker('alpha'), "--repo", "floati", "--sha", SHA,
            "--doc", "docs/evidence/input.md",
        )
        self.assertEqual(20, duplicate.returncode, duplicate.stderr)
        self.assertEqual("work_duplicate_open", self.artifact(duplicate)["evidence"]["code"])

        projected = self.run_cli("work", "show", "--root", str(self.home), "--id", item_id,
                                 "--as", public_ids.worker('alpha'))
        self.assertEqual(0, projected.returncode, projected.stderr)
        self.assertIn("eligibility", self.artifact(projected)["evidence"]["items"][0])

        claimed = self.run_cli(
            "work", "claim", "--root", str(self.home), "--id", item_id,
            "--as", public_ids.worker('alpha'), "--authority-subject", "work-claims",
            "--authority-epoch", str(self.grant["epoch"]),
            "--now", "2026-07-31T12:00:01.000Z",
        )
        completed = self.run_cli(
            "work", "complete", "--root", str(self.home), "--id", item_id,
            "--as", public_ids.worker('alpha'), "--repo", "floati", "--sha", "b" * 40,
            "--doc", "docs/evidence/output.md", "--now", "2026-07-31T12:00:02.000Z",
        )
        shown = self.run_cli("work", "show", "--root", str(self.home), "--id", item_id)

        self.assertEqual(0, claimed.returncode, claimed.stderr)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(0, shown.returncode, shown.stderr)
        item = self.artifact(shown)["evidence"]["items"][0]
        self.assertEqual("completed", item["state"])
        self.assertEqual(public_ids.worker('alpha'), item["holder"])
        self.assertEqual("docs/evidence/output.md", item["artifact_bindings"][-1]["doc"])

    def test_work_handoff_records_run_results_and_rechecks_its_own_checkout(self) -> None:
        from floati.cli import main

        def handoff(*args, expected=0):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(args)
            self.assertEqual(expected, status, stdout.getvalue() or stderr.getvalue())
            return json.loads(stdout.getvalue())["evidence"]

        actor = public_ids.worker("alpha")
        work = WorkLog(self.root)
        item = work.add("Prepare a handoff", actor, [])
        claim = work.claim(item["id"], actor, "work-claims", self.grant["epoch"], now=NOW)
        completion = work.complete(item["id"], actor, [], now=NOW)
        repository = Path(self.temp.name).resolve() / "repo"
        repository.mkdir()

        def git(*args):
            return subprocess.check_output(
                ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(repository), *args],
                text=True, stderr=subprocess.PIPE,
            ).strip()

        git("init")
        git("config", "user.name", "Test Author")
        git("config", "user.email", "author@example.invalid")
        (repository / "test_sample.py").write_text(
            "import os, unittest\n"
            "class Sample(unittest.TestCase):\n"
            "    def test_pass(self): os.write(1, b'9')\n"
            "    def test_fail(self): self.fail()\n",
            encoding="utf-8",
        )
        (repository / "test_empty.py").write_text("# No tests\n", encoding="utf-8")
        note = repository / "result.txt"
        note.write_text("Evidence\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-m", "Record evidence")
        original = git("rev-parse", "HEAD")
        coordinate = ("--root", str(self.home), "--id", item["id"])
        show_args = ("work", "handoff", "show", *coordinate, "--json")
        empty = handoff(*show_args)
        self.assertEqual(("bound-record", 1, [], "none"),
                         (empty["contract"], empty["contract_version"], empty["records"], empty["evidence_state"]))

        record_args = (
            "work", "handoff", "record", *coordinate, "--as", actor,
            "--repository", str(repository), "--unittest", "test_sample",
            "--run", json.dumps([str(Path(sys.executable).resolve()), "-c",
                                 "with open('ran', 'a') as marker: marker.write('run'); raise SystemExit(7)"]),
            "--artifact", "result.txt", "--remaining", "Investigate the failing test",
            "--statement", "212 tests, exit 0",
            "--idempotency-key", "first",
        )
        row = handoff(*record_args)["handoff"]
        from tests.schema_validation import validate_json_schema

        validate_json_schema(row, REPOSITORY_ROOT / "schemas/v1/work-handoff-record.schema.json")
        self.assertEqual({"kind": "ref", "ref": "HEAD", "paths": [], "commit": original,
                          "repository": str(repository), "provenance": "observed"}, row["coordinates"][0])
        self.assertEqual({"claim_id": claim["id"], "completion_id": completion["id"], "provenance": "observed"},
                         row["work_receipts"])
        self.assertEqual([{"text": "Investigate the failing test", "provenance": "claimed"}], row["remaining"])
        self.assertEqual([{"text": "212 tests, exit 0", "provenance": "claimed"}], row["statements"])
        self.assertEqual("observed", row["coordinates"][1]["provenance"])
        self.assertIsNone(row["supersedes"])
        self.assertEqual([(7, None), (1, 2)],
                         [(run["exit_code"], run["test_count"]) for run in row["runs"]])
        self.assertTrue(all(run["provenance"] == "observed" for run in row["runs"]))

        current = handoff(*show_args)["records"][0]
        self.assertEqual("current", current["freshness"]["state"])
        self.assertEqual("recorded", current["evidence_state"])
        self.assertEqual(row, handoff(*record_args)["handoff"])
        self.assertEqual("run", (repository / "ran").read_text())

        partial = handoff(
            "work", "handoff", "record", *coordinate, "--as", actor,
            "--repository", str(repository), "--remaining", "Run checks",
            "--idempotency-key", "second",
        )["handoff"]
        self.assertEqual(row["id"], partial["supersedes"])
        self.assertEqual(partial, handoff(*show_args)["records"][0]["record"])
        self.assertEqual("none", handoff(*show_args)["records"][0]["evidence_state"])
        conflict = handoff(
            "work", "handoff", "record", *coordinate, "--as", actor,
            "--repository", str(repository), "--supersedes", row["id"],
            "--idempotency-key", "late-correction", expected=20,
        )
        self.assertEqual("supersedes_not_head", conflict["code"])
        zero = handoff(
            "work", "handoff", "record", *coordinate, "--as", actor,
            "--repository", str(repository), "--unittest", "test_empty",
            "--idempotency-key", "third",
        )["handoff"]["runs"][0]
        self.assertEqual(("observed", 0, 0), (zero["provenance"], zero["exit_code"], zero["test_count"]))
        handoff(
            "work", "handoff", "record", *coordinate, "--as", actor,
            "--repository", str(repository), "--statement", "212 tests, exit 0",
            "--idempotency-key", "fourth",
        )
        claimed = handoff(*show_args)["records"][0]
        self.assertEqual("claimed", claimed["evidence_state"])
        self.assertEqual([], claimed["record"]["runs"])
        original_handoff = (*show_args, "--handoff-id", row["id"])
        historical = handoff(*original_handoff)["records"][0]
        self.assertEqual(row, historical["record"])
        self.assertEqual({"head": False, "superseded_by": partial["id"]}, historical["chain"])
        note.unlink()
        note.mkdir()
        unreadable = handoff(*original_handoff)["records"][0]["freshness"]
        self.assertEqual("unknown", unreadable["state"])
        self.assertIsNotNone(unreadable["coordinates"][1]["cause"])
        note.rmdir()
        artifact_only = handoff(*original_handoff)["records"][0]["freshness"]
        digest = row["coordinates"][1]["files"][0]["sha256"]
        missing = {"what": "file", "path": "result.txt", "from": digest, "to": None}
        self.assertEqual("stale", artifact_only["state"])
        self.assertEqual([missing], artifact_only["coordinates"][1]["moved"])
        git("add", "-u")
        git("commit", "-m", "Remove evidence file")
        before = tree_digest(self.home)
        stale = handoff(*original_handoff)["records"][0]
        self.assertEqual(row, stale["record"])
        self.assertEqual("stale", stale["freshness"]["state"])
        self.assertEqual([{"what": "ref", "ref": "HEAD", "from": original, "to": git("rev-parse", "HEAD")}],
                         stale["freshness"]["coordinates"][0]["moved"])
        self.assertEqual([missing], stale["freshness"]["coordinates"][1]["moved"])
        repository.rename(repository.with_name("moved-repo"))
        unreadable = handoff(*original_handoff)["records"][0]["freshness"]
        self.assertEqual("unknown", unreadable["state"])
        self.assertTrue(all(result["cause"] for result in unreadable["coordinates"]))
        self.assertEqual(before, tree_digest(self.home))

    def test_work_handoff_observation_leaves_the_journal_available_and_has_a_deadline(self):
        from floati.errors import ProtocolRefusal
        from floati.work_handoff import WorkHandoffs
        from tests.git_fixture_cache import fixture_git

        actor = public_ids.worker("alpha")
        work = WorkLog(self.root)
        item = work.add("Capture independent evidence", actor, [])
        work.claim(item["id"], actor, "work-claims", self.grant["epoch"], now=NOW)
        repository = Path(self.temp.name).resolve() / "observed"
        repository.mkdir()
        git = [fixture_git("/usr/bin/git"), "-C", str(repository)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run(
            [*git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "commit", "-qm", "Fixture", "--allow-empty"],
            check=True,
        )
        handoffs = WorkHandoffs(self.root)
        python = str(Path(sys.executable).resolve())
        common = ["work", "handoff", "record", "--root", str(self.home), "--id", item["id"],
                  "--as", actor, "--repository", str(repository)]

        def record(program, key):
            return handoffs.record(item["id"], actor, str(repository), [[python, "-c", program]],
                                   [], [], [], [], key)

        with self.subTest(property="journal remains available"):
            nested = (
                f"import sys; sys.path.insert(0, {str(REPOSITORY_ROOT)!r}); "
                "from floati.cli import main; "
                f"sys.exit(main({common + ['--idempotency-key', 'nested']!r}))"
            )
            observed = record(nested, "outer")
            self.assertEqual(0, observed["runs"][0]["exit_code"])
            self.assertIsNotNone(observed["supersedes"])

        with self.subTest(property="a stalled command has a bounded observation"):
            # The child has its own watchdog so a regression returns to the test.
            stalled = "import signal; signal.alarm(2); signal.pause()"
            before = handoffs.show(item["id"])
            with patch("floati.work_handoff._RUN_TIMEOUT_SECONDS", 0.1, create=True):
                with self.assertRaises(ProtocolRefusal) as caught:
                    record(stalled, "stalled")
            self.assertEqual("handoff_run_timeout", caught.exception.code)
            self.assertEqual(before["records"], handoffs.show(item["id"])["records"])

    def test_handoff_children_receive_only_the_declared_environment(self):
        from floati.work_handoff import _UNNITEST_PROGRAM, _observe_run

        checkout = Path(self.temp.name).resolve()
        snapshot = checkout / "child-environment.json"
        python = str(Path(sys.executable).resolve())
        capture = "import json, os\nopen('child-environment.json', 'w').write(json.dumps(dict(os.environ)))\n"
        (checkout / "test_capture.py").write_text(
            capture + "import unittest\nclass Capture(unittest.TestCase):\n"
            "    def test_count(self): pass\n",
            encoding="utf-8",
        )

        for instrumented, argv in (
            (False, [python, "-I", "-B", "-c", capture]),
            (True, [python, "-I", "-B", "-c", _UNNITEST_PROGRAM, "test_capture"]),
        ):
            with self.subTest(instrumented=instrumented), patch.dict(
                os.environ, {"GITHUB_TOKEN": "fixture credential", _DARWIN_VARIABLE: _DARWIN_SENTINEL}
            ):
                run = _observe_run(checkout, argv, is_unittest=instrumented)
                self.assertEqual(0, run["exit_code"])
                child_env = json.loads(snapshot.read_text(encoding="utf-8"))
                self.assertFalse("GITHUB_TOKEN" in child_env, "handoff forwarded a parent credential variable")
                # CoreFoundation sets this variable inside every Darwin process
                # that starts from an empty environment. The planted value is
                # never handed down; a different value is the process's own.
                self.assertNotEqual(_DARWIN_SENTINEL, child_env.get(_DARWIN_VARIABLE),
                                    "handoff forwarded a parent variable that the platform also sets")
                declared = {"HOME", "TMPDIR", "PATH", "LANG", "LC_ALL"}
                self.assertTrue(declared <= set(child_env), sorted(child_env))
                self.assertEqual(set(), set(child_env) - declared - (
                    {_DARWIN_VARIABLE} if sys.platform == "darwin" else set()), sorted(child_env))
                self.assertEqual(child_env["HOME"], child_env["TMPDIR"])
                self.assertFalse(Path(child_env["HOME"]).exists(), "handoff scratch was retained")

    def test_work_add_reports_missing_notification_inputs(self) -> None:
        result = self.run_cli(
            "work", "add", "--root", str(self.home), "--title", "unrouted work",
            "--owner", public_ids.worker('alpha'),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        evidence = self.artifact(result)["evidence"]
        self.assertIn("findings", evidence)
        finding = evidence["findings"][0]
        self.assertEqual("work_add_unnotified", finding["code"])
        self.assertEqual({"dispatcher", "artifact"}, set(finding["missing"]))
        self.assertEqual({"--as", "--repo", "--sha", "--doc"}, set(finding["missing_flags"]))
        self.assertIn("--as", finding["remedy"])
        self.assertIn("--repo", finding["remedy"])
        self.assertEqual([], EventLog(self.root).event_records())

    def test_work_complete_reports_missing_dispatcher_and_artifact(self) -> None:
        work = WorkLog(self.root)
        item = work.add("complete without mail", public_ids.worker('alpha'), [])
        work.claim(item["id"], public_ids.worker('alpha'), "work-claims", self.grant["epoch"], now=NOW)
        result = self.run_cli(
            "work", "complete", "--root", str(self.home), "--id", item["id"],
            "--as", public_ids.worker('alpha'), "--now", "2026-07-31T12:00:02Z",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        findings = self.artifact(result)["evidence"].get("findings", [])
        self.assertTrue(findings)
        self.assertEqual("work_complete_unnotified", findings[0]["code"])
        self.assertEqual({"dispatcher", "artifact"}, set(findings[0]["missing"]))

    def test_work_completion_notice_and_ack_link_back_to_work(self) -> None:
        from floati.cursor import SparseCursor
        from floati.message_consumption import MessageConsumption

        work = WorkLog(self.root)
        item = work.add("finish routed work", public_ids.worker('alpha'), [], dispatcher="bravo")
        work.claim(item["id"], public_ids.worker('alpha'), "work-claims", self.grant["epoch"], now=NOW)
        dispatch = EventLog(self.root).send(
            "bravo", public_ids.worker('alpha'), "floati",
            "b8f95f22b737b4c0dc9fd52c91998f57d5bd7a00", "docs/input.md",
            "Complete this work", idempotency_key="completion-consume-control",
        )
        EventLog(self.root).present(public_ids.worker('alpha'))
        dispatch_ack = SparseCursor(self.root).ack(
            public_ids.worker('alpha'), [dispatch["id"]], acting_session_id="test-session",
        )
        result = self.run_cli(
            "work", "complete", "--root", str(self.home), "--id", item["id"],
            "--as", public_ids.worker('alpha'), "--repo", "floati", "--sha",
            "b8f95f22b737b4c0dc9fd52c91998f57d5bd7a00", "--doc", "docs/evidence/output.md",
            "--consume", dispatch["id"], "--session", "test-session",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        envelopes = [row for row in EventLog(self.root).event_records()
                     if row["kind"] == "message_envelope" and row["recipient"] == "bravo"]
        self.assertEqual(1, len(envelopes))
        self.assertIn(item["id"], envelopes[0]["note"])
        EventLog(self.root).present("bravo")
        SparseCursor(self.root).ack("bravo", [envelopes[0]["id"]], acting_session_id="test-session")
        shown = self.run_cli("work", "show", "--root", str(self.home), "--id", item["id"])
        self.assertEqual(0, shown.returncode, shown.stderr)
        notifications = self.artifact(shown)["evidence"]["items"][0]["notifications"]
        completion = [row for row in notifications if row["phase"] == "sent" and row["transition"] == "complete"]
        self.assertEqual(1, len(completion))
        self.assertEqual(envelopes[0]["id"], completion[0]["envelope_id"])
        self.assertEqual(self.artifact(result)["evidence"]["id"], completion[0]["transition_id"])
        consumed = MessageConsumption(self.root).records(public_ids.worker('alpha'))
        self.assertEqual(1, len(consumed))
        self.assertEqual("work_complete", consumed[0]["source"])
        self.assertEqual(item["id"], consumed[0]["work_item_id"])
        self.assertEqual(dispatch["id"], consumed[0]["message_id"])
        self.assertEqual(dispatch_ack["id"], consumed[0]["ack_receipt_id"])

    def test_work_add_notifies_eligible_node_with_caller_artifact(self) -> None:
        banked_sha = "b8f95f22b737b4c0dc9fd52c91998f57d5bd7a00"
        Registry(self.root).register("charlie", "Codex")
        now = datetime.now(timezone.utc)
        grants = AuthorityGrantStore(self.root)
        grants.grant_exact("available-work", "charlie", 1, now)
        grants.grant_exact("available-work", public_ids.worker('alpha'), 2, now)
        grants.grant_exact("dispatcher-work", "bravo", 1, now)
        AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, now
        )
        result = self.run_cli(
            "work", "add", "--root", str(self.home), "--title", "notify work",
            "--owner", public_ids.worker('alpha'), "--as", "bravo",
            "--repo", "floati", "--sha", banked_sha, "--doc", "docs/input.md",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        item_id = self.artifact(result)["evidence"]["id"]
        envelopes = [r for r in EventLog(self.root).event_records() if r["kind"] == "message_envelope"]
        self.assertEqual(2, len(envelopes))
        self.assertCountEqual([public_ids.worker('alpha'), "bravo"], [r["recipient"] for r in envelopes])
        self.assertEqual("bravo", envelopes[0]["sender"])
        self.assertIn(item_id, envelopes[0]["note"])
        self.assertEqual(("floati", banked_sha, "docs/input.md"),
                         (envelopes[0]["repo"], envelopes[0]["sha"], envelopes[0]["doc"]))
        from floati.wake_hold import WakeHoldController
        for envelope in envelopes:
            recipient = envelope["recipient"]
            self.assertEqual("work-add-" + item_id + "-" + recipient, envelope["idempotency_key"])
            self.assertEqual("normal", envelope.get("message_class", "normal"))
            selection = WakeHoldController(self.root).evaluate(recipient, idempotency_key="notification-" + recipient)
            self.assertEqual("fresh_work", selection["state"])
            self.assertEqual([envelope["id"]], [r["id"] for r in selection["fresh_messages"]])
        shown = WorkLog(self.root).show(item_id, actor="charlie")[0]
        self.assertEqual("authority_superseded", shown["eligibility"]["code"])
        self.assertEqual(["intent", "sent", "sent"], [r["phase"] for r in shown["notifications"]])
        duplicate = self.run_cli(
            "work", "add", "--root", str(self.home), "--title", "notify work",
            "--owner", public_ids.worker('alpha'), "--as", "bravo",
            "--repo", "floati", "--sha", banked_sha, "--doc", "docs/input.md",
        )
        self.assertEqual(20, duplicate.returncode)
        self.assertEqual("work_duplicate_open", self.artifact(duplicate)["evidence"]["code"])

    def test_interrupted_work_fanout_replays_each_recipient_once(self) -> None:
        from floati.cli import main

        now = datetime.now(timezone.utc)
        AuthorityGrantStore(self.root).claim("work-claims", public_ids.worker('alpha'), 60, 60, now)
        AuthorityGrantStore(self.root).claim("other-claims", "bravo", 60, 60, now)
        send = EventLog.send
        calls = []

        def interrupted(log, *args, **kwargs):
            calls.append(args[1])
            if len(calls) == 2:
                raise RuntimeError("simulated interruption")
            return send(log, *args, **kwargs)

        with patch.object(EventLog, "send", interrupted), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                main(["work", "add", "--root", str(self.home), "--title", "recover fanout",
                      "--owner", public_ids.worker('alpha'), "--as", "bravo", "--repo", "floati",
                      "--sha", "b8f95f22b737b4c0dc9fd52c91998f57d5bd7a00", "--doc", "docs/input.md"])
        item_id = WorkLog(self.root).show()[0]["id"]
        for _ in range(2):
            result = self.run_cli("work", "notify", "--root", str(self.home), "--id", item_id, "--as", "bravo")
            self.assertEqual(0, result.returncode, result.stderr)
        envelopes = [r for r in EventLog(self.root).event_records() if r["kind"] == "message_envelope"]
        self.assertCountEqual([public_ids.worker('alpha'), "bravo"], [r["recipient"] for r in envelopes])

    def test_work_add_workspace_records_the_exact_derived_live_mapping(self) -> None:
        result = self.run_cli(
            "work", "add", "--root", str(self.home),
            "--title", "create proof file", "--owner", public_ids.worker('alpha'), "--workspace",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        item = self.artifact(result)["evidence"]
        self.assertEqual(
            str(worker_workspace_root() / item["id"]),
            item["workspace"],
        )

    def test_work_add_accepts_repeated_existing_dependency_ids(self) -> None:
        prerequisite = WorkLog(self.root).add("prepare", public_ids.worker('alpha'), [])

        result = self.run_cli(
            "work", "add", "--root", str(self.home),
            "--title", "consume", "--owner", public_ids.worker('alpha'),
            "--needs", prerequisite["id"],
        )

        self.assertEqual(0, result.returncode, result.stderr)
        item = self.artifact(result)["evidence"]
        self.assertEqual([prerequisite["id"]], item["needs"])
        self.assertEqual("blocked", WorkLog(self.root).show(item["id"])[0]["readiness"])

    def test_ownerless_work_refuses_in_a_non_solo_multi_node_root(self) -> None:
        result = self.run_cli(
            "work", "add", "--root", str(self.home), "--title", "ambiguous"
        )

        self.assertEqual(20, result.returncode)
        self.assertEqual("solo_configuration_missing", self.artifact(result)["evidence"]["code"])

    def test_exact_codex_worker_cli_route_completes_through_local_reference_harness(self) -> None:
        from floati.adapters.codex_live import CodexAppServerAdapter
        from floati.cli import main

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "live-fleet"
            root = FloatiRoot.open_direct_home(home, create=True)
            Registry(root).register("worker-a", "Codex")
            current = datetime.now(timezone.utc)
            item = WorkLog(root).add(
                "Create PROOF.txt",
                "worker-a",
                [],
                provision_workspace=True,
                now=current,
            )
            self.addCleanup(shutil.rmtree, Path(str(item["workspace"])), True)
            AuthorityGrantStore(root).claim(
                "worker-live", "worker-a", 30, 20, current
            )
            harness = REPOSITORY_ROOT / "tests" / "fixtures" / "codex-app-server" / "reference_harness.py"
            adapter = CodexAppServerAdapter(
                (str(Path(sys.executable).resolve()), str(harness), "--mode", "complete")
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with patch("floati.cli.CodexAppServerAdapter", return_value=adapter):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "worker", "run", "--root", str(home),
                            "--as", "worker-a", "--adapter", "codex",
                            "--codex-executable", str(Path(sys.executable).resolve()),
                        ]
                    )

            self.assertEqual(0, exit_code, stderr.getvalue())
            artifact = json.loads(stdout.getvalue())
            self.assertEqual("ok", artifact["status"])
            self.assertEqual("complete", artifact["evidence"]["transition"])
            shown = WorkLog(root).show(item["id"])[0]
            self.assertEqual("completed", shown["state"])
            self.assertEqual(
                ["claim", "spawn", "drive", "bind_artifact", "complete"],
                [row["transition"] for row in WorkerReceipts(root).records()],
            )
            self.assertFalse(root.resolve_relative("receipts/deliveries/worker-a.jsonl").exists())
            self.assertFalse(root.resolve_relative("receipts/acks/worker-a.jsonl").exists())

    def test_orchestrate_seeds_streams_and_emits_one_final_artifact(self) -> None:
        from floati import orchestrate, tui_render
        from floati.cli import main

        current = datetime.now(timezone.utc)
        Registry(self.root).register("charlie", "Codex")
        AuthorityGrantStore(self.root).claim(public_ids.compose('work-', public_ids.worker('alpha')), public_ids.worker('alpha'), 30, 20, current)
        AuthorityGrantStore(self.root).claim("work-bravo", "bravo", 30, 20, current)
        AuthorityGrantStore(self.root).claim("work-charlie", "charlie", 30, 20, current)
        plan = Path(self.temp.name) / "orchestrate.json"
        plan.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "workers": [public_ids.worker('alpha'), "bravo", "charlie"],
                    "items": [
                        {"key": "a", "title": "Create A.txt", "owner": public_ids.worker('alpha'), "needs": []},
                        {"key": "b", "title": "Create B.txt", "owner": "bravo", "needs": []},
                        {"key": "c", "title": "Create C.txt", "owner": "charlie", "needs": []},
                        {"key": "d", "title": "Create D.txt", "owner": public_ids.worker('alpha'), "needs": ["a", "b", "c"]},
                    ],
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        parent_pid = os.getpid()
        render = tui_render.render_plain_dump
        frame_rendered = False

        def record_first_frame(model):
            nonlocal frame_rendered
            frame = render(model)
            frame_rendered = True
            return frame

        def clock_after_frame():
            gap = 9.0 if frame_rendered and os.getpid() == parent_pid else 0.0
            return time.monotonic() + gap

        clock = SimpleNamespace(monotonic=clock_after_frame, sleep=time.sleep)
        with patch("floati.cli.CodexAppServerAdapter", return_value=_CliOrchestrateAdapter()),\
                patch.object(orchestrate, "time", clock),\
                patch.object(tui_render, "render_plain_dump", record_first_frame):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "orchestrate", "--root", str(self.home), "--plan", str(plan),
                        "--adapter", "codex", "--deadline", str(READY_SECONDS), "--no-animation",
                    ]
                )

        self.assertEqual(0, exit_code, stdout.getvalue() or stderr.getvalue())
        lines = stdout.getvalue().splitlines()
        self.assertEqual(1, len(lines))
        artifact = json.loads(lines[0])
        self.assertEqual("orchestrate", artifact["command"])
        self.assertEqual("drained", artifact["status"])
        self.assertEqual(4, artifact["evidence"]["item_count"])
        self.assertIn("WORK DAG", stderr.getvalue())


class SoloWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "solo-fleet"

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *args],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def artifact(result: subprocess.CompletedProcess[str]) -> dict:
        return json.loads(result.stdout)

    def test_solo_init_work_and_board_round_trip(self) -> None:
        initialized = self.run_cli("init", "--root", str(self.home), "--solo", "me")
        added = self.run_cli(
            "work", "add", "--root", str(self.home), "--title", "record session"
        )
        item_id = self.artifact(added)["evidence"]["id"] if added.returncode == 0 else "missing"
        claimed = self.run_cli(
            "work", "claim", "--root", str(self.home), "--id", item_id
        )
        completed = self.run_cli(
            "work", "complete", "--root", str(self.home), "--id", item_id
        )
        board = self.run_cli(
            "board", "--root", str(self.home), "--no-animation"
        )

        self.assertEqual(0, initialized.returncode, initialized.stderr)
        self.assertEqual("me", self.artifact(initialized)["evidence"]["solo"]["node_id"])
        self.assertTrue((self.home / "cursors").is_dir())
        self.assertTrue((self.home / "receipts" / "deliveries").is_dir())
        self.assertTrue((self.home / "nodes" / "me").is_dir())
        self.assertEqual(0, added.returncode, added.stderr)
        self.assertEqual("me", self.artifact(added)["evidence"]["owner"])
        self.assertEqual(0, claimed.returncode, claimed.stderr)
        self.assertEqual("me", self.artifact(claimed)["evidence"]["actor"])
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("me", self.artifact(completed)["evidence"]["actor"])
        self.assertEqual(0, board.returncode, board.stderr)
        self.assertIn("DONE", board.stdout)
        self.assertIn("me", board.stdout)

    def test_matching_solo_init_is_idempotent_and_mismatch_refuses(self) -> None:
        from floati.jsonl import read_records

        first = self.run_cli(
            "init", "--root", str(self.home), "--solo", "me", "--harness", "Codex"
        )
        second = self.run_cli(
            "init", "--root", str(self.home), "--solo", "me", "--harness", "Codex"
        )
        mismatch = self.run_cli("init", "--root", str(self.home), "--solo", "other")
        root = FloatiRoot.open_direct_home(self.home)
        grants = read_records(
            root,
            "authority-grants/solo-work.jsonl",
            allowed_kinds={"authority_grant"},
        )

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(1, len(grants))
        self.assertEqual(86400, grants[0]["ttl_seconds"])
        self.assertEqual("Codex", self.artifact(second)["evidence"]["solo"]["harness"])
        self.assertEqual(20, mismatch.returncode)
        self.assertEqual("solo_identity_mismatch", self.artifact(mismatch)["evidence"]["code"])

    def test_fully_flagged_solo_init_bypasses_the_door_and_keeps_legacy_artifact(self) -> None:
        """Catches a complete legacy init being diverted through interactive onboarding."""
        from floati.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "floati.tui_doors.run_solo_door",
                side_effect=AssertionError("complete init must bypass the door"),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "init",
                    "--root",
                    str(self.home),
                    "--solo",
                    "me",
                    "--harness",
                    "Codex",
                ]
            )

        self.assertEqual(0, code, stderr.getvalue())
        self.assertEqual("", stderr.getvalue())
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("me", artifact["evidence"]["solo"]["node_id"])
        self.assertEqual("Codex", artifact["evidence"]["solo"]["harness"])

    def test_no_value_solo_init_routes_door_answers_to_one_stdout_artifact(self) -> None:
        """Catches the ruled no-value --solo shape remaining unreachable from the CLI."""
        from floati.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "floati.tui_doors.run_solo_door",
                return_value=("me", "Codex"),
            ) as door,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(["init", "--root", str(self.home), "--solo"])

        self.assertEqual(0, code, stderr.getvalue())
        self.assertEqual(1, len(stdout.getvalue().splitlines()))
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("me", artifact["evidence"]["solo"]["node_id"])
        self.assertEqual("Codex", artifact["evidence"]["solo"]["harness"])
        door.assert_called_once()

    def test_interactive_solo_commits_the_same_reviewed_plan_bytes_exactly_once(self) -> None:
        """Catches CLI solo rebuilding reviewed values or initializing before final Commit."""
        from floati import solo
        from floati.cli import main

        plan_factory = getattr(solo, "plan_solo_bootstrap", None)
        self.assertIsNotNone(plan_factory, "interactive solo requires an immutable plan seam")
        reviewed_plan = plan_factory("me", "Codex")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "floati.tui_doors.run_solo_door",
                return_value=reviewed_plan,
            ) as door,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(["init", "--root", str(self.home), "--solo"])

        self.assertEqual(0, code, stderr.getvalue())
        door.assert_called_once()
        self.assertEqual(
            reviewed_plan.configuration_bytes,
            (self.home / "solo.json").read_bytes(),
        )
        self.assertEqual(
            1,
            len((self.home / "registry" / "entries.jsonl").read_text().splitlines()),
        )
        self.assertEqual(
            1,
            len(
                (self.home / "authority-grants" / "solo-work.jsonl")
                .read_text()
                .splitlines()
            ),
        )
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("me", artifact["evidence"]["solo"]["node_id"])
        self.assertNotIn("plan", artifact["evidence"]["solo"])

    def test_no_value_solo_init_non_tty_refuses_before_creating_the_root(self) -> None:
        """Catches non-TTY onboarding mutating a root before naming the complete command."""
        from floati.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["init", "--root", str(self.home), "--solo"])

        self.assertEqual(20, code)
        self.assertEqual("", stderr.getvalue())
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("interactive_terminal_required", artifact["evidence"]["code"])
        self.assertEqual(
            "DRAFT - floati init --root ROOT --solo NODE --harness HARNESS",
            artifact["evidence"]["remedy"],
        )
        self.assertFalse(self.home.exists())

    def test_interactive_solo_validates_door_answers_before_creating_the_root(self) -> None:
        """Catches raw door answers bypassing solo lexical validation before root creation."""
        from floati.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "floati.tui_doors.run_solo_door",
                return_value=("NOT VALID", "Codex"),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(["init", "--root", str(self.home), "--solo"])

        self.assertEqual(20, code)
        self.assertEqual("", stderr.getvalue())
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("node_invalid", artifact["evidence"]["code"])
        self.assertFalse(self.home.exists())

    def test_solo_terminal_io_failures_become_one_typed_artifact(self) -> None:
        """Catches setup or cleanup I/O escaping the CLI as an untyped traceback."""
        from floati.cli import main
        from floati.tui_doors import DoorTerminalIOError

        for failure in ("setup", "cleanup"):
            with self.subTest(failure=failure):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch(
                        "floati.tui_doors.run_solo_door",
                        side_effect=DoorTerminalIOError(failure),
                    ),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    code = main(["init", "--root", str(self.home), "--solo"])

                self.assertEqual(20, code)
                self.assertEqual("", stderr.getvalue())
                self.assertEqual(1, len(stdout.getvalue().splitlines()))
                artifact = json.loads(stdout.getvalue())
                self.assertEqual("door_terminal_io_failed", artifact["evidence"]["code"])
                self.assertEqual(
                    "DRAFT - floati init --root ROOT --solo NODE --harness HARNESS",
                    artifact["evidence"]["remedy"],
                )
                self.assertFalse(self.home.exists())

    def test_interactive_solo_ctrl_c_is_one_typed_refusal_before_root_creation(self) -> None:
        """Catches Ctrl-C escaping the init entry point or being emitted on stdout."""
        from floati.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with (
                patch(
                    "floati.tui_doors.run_door_terminal",
                    side_effect=KeyboardInterrupt(),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = main(["init", "--root", str(self.home), "--solo"])
        except KeyboardInterrupt:
            self.fail("Ctrl-C escaped the interactive solo entry point")

        self.assertEqual(20, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(1, len(stdout.getvalue().splitlines()))
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("door_cancelled", artifact["evidence"]["code"])
        self.assertEqual(
            "DRAFT - floati init --root ROOT --solo NODE --harness HARNESS",
            artifact["evidence"]["remedy"],
        )
        self.assertFalse(self.home.exists())

    def test_term_dumb_solo_refuses_before_terminal_setup_or_root_creation(self) -> None:
        """Catches TERM=dumb entering alternate-screen setup for no-value solo."""
        from floati.cli import main

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        stdout = io.StringIO()
        stderr = TTY()
        remedy = "DRAFT - floati init --root ROOT --solo NODE --harness HARNESS"
        with (
            patch.dict(os.environ, {"TERM": "dumb"}, clear=True),
            patch.object(sys, "stdin", TTY()),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(["init", "--root", str(self.home), "--solo"])

        self.assertEqual(20, code)
        self.assertEqual("", stderr.getvalue())
        self.assertNotIn("\x1b[?1049h", stdout.getvalue())
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("interactive_terminal_required", artifact["evidence"]["code"])
        self.assertEqual(remedy, artifact["evidence"]["remedy"])
        self.assertFalse(self.home.exists())

    def test_solo_resolution_refuses_an_expired_grant(self) -> None:
        from floati.errors import ProtocolRefusal
        from floati.solo import initialize_solo, resolve_solo_authority

        root = FloatiRoot.open_direct_home(self.home, create=True)
        initialize_solo(root, "me", "solo", NOW)

        with self.assertRaises(ProtocolRefusal) as caught:
            resolve_solo_authority(root, "me", NOW + timedelta(seconds=86400))

        self.assertEqual("solo_authority_expired", caught.exception.code)

        work = WorkLog(root)
        claimed = work.add("claimed before expiry", "me", [], now=NOW)
        available = work.add("available after expiry", "me", [], now=NOW)
        work.claim(claimed["id"], "me", "solo-work", 1, now=NOW)
        ledger = self.home / "work" / "items.jsonl"
        before = ledger.read_bytes()
        for item, code in ((claimed, "work_already_claimed"),
                           (available, "solo_authority_expired")):
            with self.subTest(code=code):
                result = self.run_cli(
                    "work", "claim", "--root", str(self.home), "--id", item["id"],
                    "--now", "2026-08-01T12:00:00.000Z",
                )
                self.assertEqual(20, result.returncode, result.stderr)
                self.assertEqual(code, self.artifact(result)["evidence"]["code"])
                self.assertEqual(before, ledger.read_bytes())

    def test_malformed_solo_identity_is_integrity_failure_not_configuration_refusal(self) -> None:
        self.home.mkdir()
        (self.home / "solo.json").write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "kind": "solo_configuration",
                    "node_id": "NOT VALID",
                    "harness": "solo",
                    "authority_subject": "solo-work",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.run_cli(
            "work", "add", "--root", str(self.home), "--title", "must fail closed"
        )

        self.assertEqual(33, result.returncode)
        self.assertEqual(
            "solo_configuration_malformed", self.artifact(result)["evidence"]["code"]
        )


class OrchestrationCliOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def prepare(self, name: str) -> tuple[Path, Path]:
        home = self.directory / name
        root = FloatiRoot.open_direct_home(home, create=True)
        current = datetime.now(timezone.utc)
        workers = (public_ids.builder('a'), public_ids.builder('b'), public_ids.builder('c'))
        for node in workers:
            Registry(root).register(node, "Codex")
            AuthorityGrantStore(root).claim(f"work-{node}", node, 30, 20, current)
        plan = self.directory / f"{name}.json"
        plan.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "workers": list(workers),
                    "items": [
                        {"key": "a", "title": "A", "owner": public_ids.builder('a'), "needs": []},
                        {"key": "b", "title": "B", "owner": public_ids.builder('b'), "needs": []},
                        {"key": "c", "title": "C", "owner": public_ids.builder('c'), "needs": []},
                        {"key": "d", "title": "D", "owner": public_ids.builder('a'), "needs": ["a", "b", "c"]},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return home, plan

    def run_case(self, name: str, adapter: object, deadline: str) -> tuple[int, dict, str]:
        from floati.cli import main

        home, plan = self.prepare(name)
        stdout = io.StringIO()
        stderr = io.StringIO()
        parent_pid = os.getpid()
        readings = iter((0.0,))
        degraded = multiprocessing.get_context("fork").Event()
        original_append = WorkerReceipts.append

        def append_and_signal(store: WorkerReceipts, *args: object, **kwargs: object) -> dict:
            record = original_append(store, *args, **kwargs)
            if record["transition"] == "degrade" and record["outcome_code"] == "turn_failed":
                # Signal only after the real terminal receipt has been committed.
                degraded.set()
            return record

        def late_clock() -> float:
            if os.getpid() != parent_pid:
                return time.monotonic()
            if name == "degraded":
                # CLI classification must not race the host's scheduling speed.
                return 0.0
            # Force deadline expiry without waiting for real time to pass.
            return next(readings, float(deadline) + 1.0)

        def wait_for_receipt(seconds: float) -> None:
            if os.getpid() == parent_pid and name == "degraded":
                # A hang bound, not an expected completion time. The product
                # reads the receipt and determines the outcome after this gate.
                self.assertTrue(degraded.wait(30), "no terminal turn_failed receipt")
            else:
                time.sleep(seconds)

        clock = SimpleNamespace(monotonic=late_clock, sleep=wait_for_receipt)
        with patch("floati.cli.CodexAppServerAdapter", return_value=adapter), patch(
            "floati.orchestrate.time", clock
        ), patch(
            "floati.workers.WorkerReceipts.append", append_and_signal
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "orchestrate", "--root", str(home), "--plan", str(plan),
                        "--adapter", "codex", "--deadline", deadline,
                        "--no-animation",
                    ]
                )
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_deadline_and_degradation_have_distinct_cli_return_codes(self) -> None:
        deadline_code, deadline, deadline_frames = self.run_case(
            "deadline", _CliHangingAdapter(), "0.1"
        )
        degraded_code, degraded, degraded_frames = self.run_case(
            "degraded", _CliFailingAdapter(), "2"
        )

        self.assertEqual(34, deadline_code, deadline)
        self.assertEqual("deadline", deadline["status"])
        self.assertEqual(35, degraded_code)
        self.assertEqual("degraded", degraded["status"])
        self.assertTrue(any(
            row["state"] == "degraded" and row["outcome_code"] == "turn_failed"
            for row in degraded["evidence"]["terminal_sessions"]
        ), degraded)
        self.assertIn("WORK DAG", deadline_frames)
        self.assertIn("TURN FAILED", degraded_frames)


class SendUnbankedShaFenceTests(unittest.TestCase):
    """RB-1: `send` refuses a `--sha` reachable from no remote ref (Am.1 §2).

    SEND-SHA-RESOLVES-1 extends the same fence to the checkout itself: a
    well-formed `--sha` that names no commit in the checkout `send` runs
    from is refused (`sha_absent`), and a send from no repository at all
    states, typed, that it could not check (`sha_unchecked`) — so an
    envelope never points a seat at nothing.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fence-fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        Registry(self.root).register("sender-a", "Codex")
        Registry(self.root).register("receiver-b", "Codex")
        self.checkout = Path(self.temp.name) / "checkout"
        self.checkout.mkdir()
        self._git("init", "--quiet", "--initial-branch=main")
        self._git("config", "user.name", "Fence Fixture")
        self._git("config", "user.email", "fence-fixture@example.invalid")
        (self.checkout / "docs").mkdir()
        (self.checkout / "docs" / "note.md").write_text("work\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "base work")
        (self.checkout / "docs" / "note.md").write_text("more work\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "local work")
        self.parent_sha = self._git("rev-parse", "HEAD~1")
        self.local_sha = self._git("rev-parse", "HEAD")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(self.checkout), check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def run_send(self, sha: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, "-m", "floati", "send",
                "--root", str(self.home),
                "--from", "sender-a", "--to", "receiver-b",
                "--repo", "fixture-repo",
                "--sha", sha,
                "--doc", "docs/note.md",
                "--note", "fence fixture",
            ],
            cwd=str(self.checkout if cwd is None else cwd),
            check=False, capture_output=True, text=True,
            env={
                **os.environ,
                "PYTHONPATH": str(REPOSITORY_ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )

    def test_send_refuses_a_sha_reachable_from_no_remote_ref(self) -> None:
        result = self.run_send(self.local_sha)
        self.assertEqual(20, result.returncode, result.stdout)
        evidence = json.loads(result.stdout)["evidence"]
        self.assertEqual("sha_unbanked", evidence["code"])
        self.assertIn(str(self.checkout), evidence["detail"])
        self.assertIn("refs/remotes", evidence["detail"])

    def test_send_refusal_names_the_remote_ref_set_it_checked(self) -> None:
        self._git("update-ref", "refs/remotes/origin/other", self.parent_sha)
        result = self.run_send(self.local_sha)
        self.assertEqual(20, result.returncode, result.stdout)
        evidence = json.loads(result.stdout)["evidence"]
        self.assertEqual("sha_unbanked", evidence["code"])
        self.assertIn("refs/remotes/origin/other", evidence["detail"])

    def test_send_refuses_a_well_formed_sha_absent_from_the_checkout(self) -> None:
        """SEND-SHA-RESOLVES-1: an invented sha names no commit the named
        repository can resolve, so the send is refused (RB-1's old
        pass-through to the verify fence pointed envelopes at nothing)."""
        self._git("update-ref", "refs/remotes/origin/main", self.local_sha)
        absent = "b" * 40
        result = self.run_send(absent)
        self.assertEqual(20, result.returncode, result.stdout)
        evidence = json.loads(result.stdout)["evidence"]
        self.assertEqual("sha_absent", evidence["code"])
        self.assertIn(absent, evidence["detail"])
        self.assertIn(str(self.checkout), evidence["detail"])

    def test_send_states_it_could_not_check_when_no_repository_is_present(self) -> None:
        """SEND-SHA-RESOLVES-1: from a directory that is no Git repository
        the fence cannot check the sha at all, so it refuses with a typed
        code instead of passing the envelope through unchecked."""
        nowhere = self.home.parent / "no-repository"
        nowhere.mkdir()
        result = self.run_send(self.local_sha, cwd=nowhere)
        self.assertEqual(20, result.returncode, result.stdout)
        evidence = json.loads(result.stdout)["evidence"]
        self.assertEqual("sha_unchecked", evidence["code"])
        self.assertIn(str(nowhere), evidence["detail"])

    def test_send_still_accepts_a_sha_reachable_from_a_remote_ref(self) -> None:
        self._git("update-ref", "refs/remotes/origin/main", self.local_sha)
        result = self.run_send(self.local_sha)
        self.assertEqual(0, result.returncode, result.stderr)
        evidence = json.loads(result.stdout)["evidence"]
        self.assertEqual("ok", json.loads(result.stdout)["status"])
        self.assertEqual(self.local_sha, evidence["message"]["sha"])


if __name__ == "__main__":
    unittest.main()
