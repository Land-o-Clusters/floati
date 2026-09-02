from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import io
import json
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
        stream = result.stdout if result.returncode == 0 else result.stderr
        return json.loads(stream)

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
        self.assertEqual(
            [
                {key: value for key, value in node.items() if key != "wake_health"}
                for node in status_nodes
            ],
            supervised_nodes,
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
                self.assertEqual("", result.stdout)
                self.assertEqual("arguments_invalid", self.artifact(result)["evidence"]["code"])
        for label, result in (("plan", relative_plan), ("policy", relative_policy)):
            with self.subTest(relative=label):
                self.assertEqual(20, result.returncode)
                self.assertEqual("", result.stdout)
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

        with patch("floati.cli.CodexAppServerAdapter", return_value=_CliOrchestrateAdapter()):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "orchestrate", "--root", str(self.home), "--plan", str(plan),
                        "--adapter", "codex", "--deadline", "5", "--no-animation",
                    ]
                )

        self.assertEqual(0, exit_code, stderr.getvalue())
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
            ["python3", "-m", "floati", *args],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def artifact(result: subprocess.CompletedProcess[str]) -> dict:
        return json.loads(result.stdout if result.returncode == 0 else result.stderr)

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
        self.assertEqual("", stdout.getvalue())
        artifact = json.loads(stderr.getvalue())
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
        self.assertEqual("", stdout.getvalue())
        artifact = json.loads(stderr.getvalue())
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
                self.assertEqual("", stdout.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertEqual(1, len(stderr.getvalue().splitlines()))
                artifact = json.loads(stderr.getvalue())
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
        self.assertEqual("", stdout.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(1, len(stderr.getvalue().splitlines()))
        artifact = json.loads(stderr.getvalue())
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
        self.assertEqual("", stdout.getvalue())
        self.assertNotIn("\x1b[?1049h", stderr.getvalue())
        artifact = json.loads(stderr.getvalue())
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
        with patch("floati.cli.CodexAppServerAdapter", return_value=adapter):
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
        self.assertIn("WORK DAG", deadline_frames)
        self.assertIn("TURN FAILED", degraded_frames)


class SendUnbankedShaFenceTests(unittest.TestCase):
    """RB-1: `send` refuses a `--sha` reachable from no remote ref (Am.1 §2)."""

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

    def run_send(self, sha: str) -> subprocess.CompletedProcess[str]:
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
            cwd=str(self.checkout),
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
        evidence = json.loads(result.stderr)["evidence"]
        self.assertEqual("sha_unbanked", evidence["code"])
        self.assertIn(str(self.checkout), evidence["detail"])
        self.assertIn("refs/remotes", evidence["detail"])

    def test_send_refusal_names_the_remote_ref_set_it_checked(self) -> None:
        self._git("update-ref", "refs/remotes/origin/other", self.parent_sha)
        result = self.run_send(self.local_sha)
        self.assertEqual(20, result.returncode, result.stdout)
        evidence = json.loads(result.stderr)["evidence"]
        self.assertEqual("sha_unbanked", evidence["code"])
        self.assertIn("refs/remotes/origin/other", evidence["detail"])

    def test_send_passes_a_sha_absent_from_the_checkout_to_the_verify_fence(self) -> None:
        """The send-side fence owns the committed-but-unpushed incident only."""
        absent = "b" * 40
        self._git("update-ref", "refs/remotes/origin/main", self.local_sha)
        result = self.run_send(absent)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ok", json.loads(result.stdout)["status"])

    def test_send_still_accepts_a_sha_reachable_from_a_remote_ref(self) -> None:
        self._git("update-ref", "refs/remotes/origin/main", self.local_sha)
        result = self.run_send(self.local_sha)
        self.assertEqual(0, result.returncode, result.stderr)
        evidence = json.loads(result.stdout)["evidence"]
        self.assertEqual("ok", json.loads(result.stdout)["status"])
        self.assertEqual(self.local_sha, evidence["message"]["sha"])


if __name__ == "__main__":
    unittest.main()
