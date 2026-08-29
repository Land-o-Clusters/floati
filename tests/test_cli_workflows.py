from __future__ import annotations

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
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
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
        registry.register("alice", "Codex")
        registry.register("bravo", "Codex")
        self.grant = AuthorityGrantStore(self.root).claim(
            "work-claims", "alice", 60, 60, NOW
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
        self.assertEqual(status_nodes, supervised_nodes)
        self.assertEqual({"liveness", "authority", "mutex"},
                         {key for key in status_nodes[0] if key in {"liveness", "authority", "mutex"}})
        self.assertNotIn("status_schema_version", self.artifact(status)["evidence"])
        self.assertNotIn("kind", self.artifact(status)["evidence"])
        self.assertNotIn("root", self.artifact(status)["evidence"])
        self.assertNotIn("tenant_id", self.artifact(status)["evidence"])
        self.assertNotIn("mode", self.artifact(status)["evidence"])
        self.assertEqual("found", self.artifact(status)["evidence"]["installer_shadow"]["outcome"])
        self.assertFalse(os.path.lexists(self.home / ".slipway-snapshots"))
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
        self.assertFalse(os.path.lexists(self.home / ".slipway-snapshots"))
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
                            "areas_to_avoid": [{"path": "slip/graph.py", "region": "all"}],
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
            "alice", "bravo", "slipway", SHA,
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
            "--owner", "alice", "--repo", "slipway", "--sha", SHA,
            "--doc", "docs/evidence/input.md",
        )
        self.assertEqual(0, added_result.returncode, added_result.stderr)
        item_id = self.artifact(added_result)["evidence"]["id"]

        claimed = self.run_cli(
            "work", "claim", "--root", str(self.home), "--id", item_id,
            "--as", "alice", "--authority-subject", "work-claims",
            "--authority-epoch", str(self.grant["epoch"]),
            "--now", "2026-07-31T12:00:01.000Z",
        )
        completed = self.run_cli(
            "work", "complete", "--root", str(self.home), "--id", item_id,
            "--as", "alice", "--repo", "slipway", "--sha", "b" * 40,
            "--doc", "docs/evidence/output.md", "--now", "2026-07-31T12:00:02.000Z",
        )
        shown = self.run_cli("work", "show", "--root", str(self.home), "--id", item_id)

        self.assertEqual(0, claimed.returncode, claimed.stderr)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(0, shown.returncode, shown.stderr)
        item = self.artifact(shown)["evidence"]["items"][0]
        self.assertEqual("completed", item["state"])
        self.assertEqual("alice", item["holder"])
        self.assertEqual("docs/evidence/output.md", item["artifact_bindings"][-1]["doc"])

    def test_work_add_workspace_records_the_exact_derived_live_mapping(self) -> None:
        result = self.run_cli(
            "work", "add", "--root", str(self.home),
            "--title", "create proof file", "--owner", "alice", "--workspace",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        item = self.artifact(result)["evidence"]
        self.assertEqual(
            f"/private/tmp/floati-work/{item['id']}",
            item["workspace"],
        )

    def test_work_add_accepts_repeated_existing_dependency_ids(self) -> None:
        prerequisite = WorkLog(self.root).add("prepare", "alice", [])

        result = self.run_cli(
            "work", "add", "--root", str(self.home),
            "--title", "consume", "--owner", "alice",
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
        AuthorityGrantStore(self.root).claim("work-alice", "alice", 30, 20, current)
        AuthorityGrantStore(self.root).claim("work-bravo", "bravo", 30, 20, current)
        AuthorityGrantStore(self.root).claim("work-charlie", "charlie", 30, 20, current)
        plan = Path(self.temp.name) / "orchestrate.json"
        plan.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "workers": ["alice", "bravo", "charlie"],
                    "items": [
                        {"key": "a", "title": "Create A.txt", "owner": "alice", "needs": []},
                        {"key": "b", "title": "Create B.txt", "owner": "bravo", "needs": []},
                        {"key": "c", "title": "Create C.txt", "owner": "charlie", "needs": []},
                        {"key": "d", "title": "Create D.txt", "owner": "alice", "needs": ["a", "b", "c"]},
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
        workers = ("lane-a", "lane-b", "lane-c")
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
                        {"key": "a", "title": "A", "owner": "lane-a", "needs": []},
                        {"key": "b", "title": "B", "owner": "lane-b", "needs": []},
                        {"key": "c", "title": "C", "owner": "lane-c", "needs": []},
                        {"key": "d", "title": "D", "owner": "lane-a", "needs": ["a", "b", "c"]},
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


if __name__ == "__main__":
    unittest.main()
