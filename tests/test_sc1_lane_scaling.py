from __future__ import annotations

from floati import fixture_ids as public_ids

import importlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog


REPOSITORY_ROOT = Path(__file__).parents[1]


class Sc1LaneScalingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register("architect-a", "architect")
        self.profiles = self.base / "profiles"
        self.profiles.mkdir()
        self.write_profile()

    def module(self):
        try:
            return importlib.import_module("floati.lane_scaling")
        except ModuleNotFoundError:
            self.fail(public_ids.compose('SC-1 ', public_ids.builder('scaling'), ' module is not implemented'))

    def write_profile(
        self,
        *,
        name: str = "sre",
        prompt: str = "Operate as {instance} from {workspace}; report to {architect}.",
    ) -> Path:
        path = self.profiles / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "profile_version": 1,
                    "name": name,
                    "role_template": "sre",
                    "workspace_recipe": "nodes/{instance}",
                    "harness": "Codex",
                    "lifetime": "permanent",
                    "lease_minutes": None,
                    "role_answers": {
                        "repo": "floati",
                        "never_touch": "foreign-project",
                        "reports_to": "{architect}",
                    },
                    "boot_prompt_template": prompt,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def service(self, *, root: FloatiRoot | None = None, fault_injector=None):
        module = self.module()
        profiles = module.load_role_profiles(self.profiles)
        return module.LaneScalingService(
            self.root if root is None else root,
            profiles,
            fault_injector=fault_injector,
        )

    @staticmethod
    def git(cwd: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["/usr/bin/git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        return completed.stdout.strip()

    def test_s1_profile_digit_rule_and_draft_prompt_fence(self) -> None:
        """Catches ambiguous numbered profiles and unstamped copy reaching a seat."""

        module = self.module()
        loaded = module.load_role_profiles(self.profiles)
        self.assertEqual(["sre"], sorted(loaded))

        self.write_profile(name="sre2")
        with self.assertRaises(ProtocolRefusal) as digit:
            module.load_role_profiles(self.profiles)
        self.assertEqual("lane_profile_name_digit_suffix", digit.exception.code)

        (self.profiles / "sre2.json").unlink()
        self.write_profile(prompt="Operate as {instance!r}.")
        with self.assertRaises(ProtocolRefusal) as formatting:
            module.load_role_profiles(self.profiles)
        self.assertEqual("lane_profile_copy_invalid", formatting.exception.code)

        self.write_profile(prompt="DRAFT - Operate as {instance}.")
        draft = module.load_role_profiles(self.profiles)["sre"]
        with self.assertRaises(ProtocolRefusal) as copy:
            module.render_boot_prompt(
                draft,
                instance="sre-1",
                workspace=self.root.path / "nodes" / "sre-1",
                architect="architect-a",
                root=self.root,
            )
        self.assertEqual("lane_profile_copy_draft", copy.exception.code)

    def test_s2_registry_allocates_ordinals_and_names_explicit_collision(self) -> None:
        """Catches process-local ordinal guesses and silent collision retries."""

        service = self.service()
        first = service.spawn(actor="architect-a", profile_name="sre")
        second = service.spawn(actor="architect-a", profile_name="sre")
        self.assertEqual(("sre-1", "sre-2"), (first["node_id"], second["node_id"]))

        with self.assertRaises(ProtocolRefusal) as collision:
            service.spawn(actor="architect-a", profile_name="sre", ordinal=2)
        self.assertEqual("lane_ordinal_collision", collision.exception.code)
        self.assertIn("sre-2", collision.exception.detail)

    def test_s2_concurrent_automatic_spawns_commit_distinct_ordinals(self) -> None:
        """Catches allocation occurring before the registry append lock is held."""

        service = self.service()
        barrier = threading.Barrier(3)
        names: list[str] = []
        failures: list[BaseException] = []

        def spawn() -> None:
            try:
                barrier.wait()
                names.append(service.spawn(actor="architect-a", profile_name="sre")["node_id"])
            except BaseException as exc:  # testimony from both racing callers
                failures.append(exc)

        threads = [threading.Thread(target=spawn) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual([], failures)
        self.assertEqual(["sre-1", "sre-2"], sorted(names))

    def test_s2_spawn_receipt_does_not_shadow_legacy_registry_lifecycle(self) -> None:
        """Catches a receipt row being mistaken for the latest registry entry."""

        spawned = self.service().spawn(actor="architect-a", profile_name="sre")
        retired = Registry(self.root).retire(spawned["node_id"])
        self.assertEqual("retired", retired["state"])

    def test_s3_faults_compensate_workspace_and_receipt_names_failing_step(self) -> None:
        """Catches a partial seat being announced after a provisioning failure."""

        for failing_step in ("workspace", "registry"):
            with self.subTest(failing_step=failing_step):
                root = FloatiRoot.open_direct_home(
                    self.base / f"fleet-{failing_step}", create=True
                )
                Registry(root).register("architect-a", "architect")

                def inject(step: str) -> None:
                    if step == failing_step:
                        raise OSError("injected " + step)

                service = self.service(root=root, fault_injector=inject)
                with self.assertRaises(ProtocolRefusal) as failed:
                    service.spawn(actor="architect-a", profile_name="sre")
                self.assertEqual("lane_spawn_incomplete", failed.exception.code)
                self.assertEqual(("architect-a",), Registry(root).active_node_ids())
                self.assertFalse((root.path / "nodes" / "sre-1").exists())
                receipt = service.receipts()[-1]
                self.assertEqual("spawn_incomplete", receipt["state"])
                self.assertEqual(failing_step, receipt["failing_step"])
                self.assertIn("workspace", receipt["compensated"])

    def test_s4_retire_drains_work_mail_and_requires_l4_reachability(self) -> None:
        """Catches teardown discarding live obligations or unique commits."""

        service = self.service()
        spawned = service.spawn(actor="architect-a", profile_name="sre")
        node = spawned["node_id"]
        item = WorkLog(self.root).add("still owned", node, [])
        with self.assertRaises(ProtocolRefusal) as work:
            service.retire(actor="architect-a", instance=node, drain=True)
        self.assertEqual("lane_teardown_work_outstanding", work.exception.code)
        self.assertIn(str(item["id"]), work.exception.detail)

        clean_root = FloatiRoot.open_direct_home(self.base / "fleet-l4", create=True)
        Registry(clean_root).register("architect-a", "architect")
        clean_service = self.service(root=clean_root)
        clean_node = clean_service.spawn(actor="architect-a", profile_name="sre")["node_id"]
        workspace = clean_root.path / "nodes" / clean_node
        self.git(workspace, "init", "--quiet", "--initial-branch=main")
        self.git(workspace, "config", "user.name", "Scratch")
        self.git(workspace, "config", "user.email", "scratch@example.invalid")
        (workspace / "base.txt").write_text("base\n", encoding="utf-8")
        self.git(workspace, "add", "base.txt")
        self.git(workspace, "commit", "--quiet", "-m", "base")
        self.git(workspace, "checkout", "--quiet", "--detach")
        (workspace / "unique.txt").write_text("unique\n", encoding="utf-8")
        self.git(workspace, "add", "unique.txt")
        self.git(workspace, "commit", "--quiet", "-m", "unique")
        unique = self.git(workspace, "rev-parse", "HEAD")

        with self.assertRaises(ProtocolRefusal) as l4:
            clean_service.retire(actor="architect-a", instance=clean_node, drain=True)
        self.assertEqual("cleanup_unreferenced_commits", l4.exception.code)
        self.assertIn(unique, l4.exception.detail)
        self.assertIn(clean_node, Registry(clean_root).active_node_ids())

    def test_s5_scratch_round_trip_then_symmetric_teardown_receipt(self) -> None:
        """Catches a provisioned sibling that cannot participate or be reversed."""

        service = self.service()
        spawned = service.spawn(actor="architect-a", profile_name="sre")
        node = spawned["node_id"]
        self.assertEqual(f"{node}@{self.root.tenant_id}", spawned["committer_email"])
        self.assertIn(node, spawned["boot_prompt"])

        message = EventLog(self.root).send(
            "architect-a",
            node,
            "floati",
            "a" * 40,
            "docs/evidence/scratch.md",
            "scratch round trip",
            idempotency_key="sc1-round-trip",
        )
        presented, _receipt = EventLog(self.root).present(node)
        self.assertEqual([message["id"]], [item["id"] for item in presented])
        with self.assertRaises(ProtocolRefusal) as mail:
            service.retire(actor="architect-a", instance=node, drain=True)
        self.assertEqual("lane_teardown_mail_unacked", mail.exception.code)
        self.assertIn(str(message["id"]), mail.exception.detail)

        SparseCursor(self.root).ack(
            node,
            [str(message["id"])],
            acting_session_id="scratch-session",
        )
        teardown = service.retire(actor="architect-a", instance=node, drain=True)
        self.assertEqual("complete", teardown["state"])
        self.assertEqual([str(self.root.path / "nodes" / node)], teardown["removed"])
        self.assertIn("registry/entries.jsonl", teardown["retained"])
        self.assertFalse((self.root.path / "nodes" / node).exists())
        self.assertNotIn(node, Registry(self.root).active_node_ids())

    def test_renderer_appends_the_inbox_read_to_a_readless_template(self) -> None:
        """K5 mandatory condition (gate verdict b031de0c), THE PERMAFIX IN
        THE MACHINERY: ZC1-F2 was a seat told to send and never to read,
        and it built on a refusal that had nowhere to land. The renderer —
        the one place every seat's boot prompt is minted — appends the
        canonical inbox read to any template that does not already name
        it, so no profile, existing or future, can mint a one-way
        channel."""
        module = self.module()
        profile = self.write_profile(
            name="readless",
            prompt="Operate as {instance} from {workspace}.",
        )
        loaded = module.load_role_profiles(self.profiles)
        prompt = module.render_boot_prompt(
            loaded["readless"],
            instance="readless-1",
            workspace=self.root.path / "nodes" / "readless-1",
            architect="architect-a",
            root=self.root,
        )
        self.assertIn("inbox --root", prompt)
        self.assertIn(str(self.root.path), prompt)
        self.assertIn("--as readless-1", prompt)

    def test_every_shipped_profile_renders_with_the_inbox_read(self) -> None:
        """The same condition, derived over the SHIPPED profile glob: a
        restamped profile must render with the read (template or renderer
        guarantee); a DRAFT-stamped profile is typed pending-restamp copy —
        its rendered prompt will carry the read the same way once the
        architect restamps it. Survives restamping because it checks the
        rendered prompt, not the stamp."""
        module = self.module()
        profiles = sorted((REPOSITORY_ROOT / "roles" / "profiles").glob("*.json"))
        self.assertTrue(profiles, "no shipped profiles found to check")
        for path in profiles:
            with self.subTest(profile=path.name):
                record = json.loads(path.read_text(encoding="utf-8"))
                loaded = module.load_role_profiles(
                    REPOSITORY_ROOT / "roles" / "profiles")
                profile = loaded[record["name"]]
                try:
                    prompt = module.render_boot_prompt(
                        profile,
                        instance=f"{record['name']}-1",
                        workspace=self.root.path / "nodes" / f"{record['name']}-1",
                        architect="architect-a",
                        root=self.root,
                    )
                except ProtocolRefusal as refused:
                    self.assertEqual(
                        "lane_profile_copy_draft", refused.code,
                        f"{record['name']}: refused for an unexpected reason")
                    self.assertTrue(
                        record["boot_prompt_template"].startswith("DRAFT - "),
                        f"{record['name']}: DRAFT refusal without the stamp")
                else:
                    self.assertIn(
                        "inbox --root", prompt,
                        f"{record['name']}: rendered boot prompt does not "
                        "name the inbox read — it mints a one-way channel")

    def test_inbox_appender_cannot_bypass_the_ascii_gate(self) -> None:
        """K5 gate verdict 29d20f69: the appender sat AFTER the long-standing
        prompt.encode('ascii') gate, so a template naming NEITHER {root} NOR
        {workspace} rendered and RETURNED a non-ASCII prompt under a
        non-ASCII root, while the control (naming {root}) was correctly
        refused. A VALIDATION THAT RUNS BEFORE THE LAST MUTATION VALIDATES
        SOMETHING THAT IS NOT WHAT SHIPS. The probe removes {workspace} and
        {root} from the template so the appended sentence is the ONLY path
        by which the root enters — a probe that cannot reach the code under
        test reports the absence of the defect it could not reach."""
        module = self.module()
        cafe_root = FloatiRoot.open_direct_home(
            self.base / "café" / "fleet", create=True)
        Registry(cafe_root).register("architect-a", "architect")
        self.write_profile(name="readless", prompt="Operate as {instance}.")
        loaded = module.load_role_profiles(self.profiles)
        with self.assertRaises(ProtocolRefusal) as refused:
            module.render_boot_prompt(
                loaded["readless"],
                instance="readless-1",
                workspace=cafe_root.path / "nodes" / "readless-1",
                architect="architect-a",
                root=cafe_root,
            )
        self.assertEqual(
            "lane_profile_copy_invalid", refused.exception.code,
            "a non-ASCII root reached the shipped prompt past the ASCII gate")

    def test_public_two_word_routes_render_restamped_help_and_shipped_profile_spawns(self) -> None:
        """Catches unreachable public verbs and DRAFT copy escaping onto a live help surface."""

        for route in (("node", "spawn"), ("node", "retire")):
            with self.subTest(route=route):
                completed = subprocess.run(
                    [sys.executable, "-m", "floati", *route, "--help"],
                    cwd=REPOSITORY_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual("", completed.stderr)
                for section in ("NAME\n", "SYNOPSIS\n", "DESCRIPTION\n", "EXIT STATUS\n", "EXAMPLES\n"):
                    self.assertIn(section, completed.stdout)
                self.assertNotIn("DRAFT -", completed.stdout)

        spawned = subprocess.run(
            [
                sys.executable,
                "-m",
                "floati",
                "node",
                "spawn",
                "--root",
                str(self.root.path),
                "--as",
                "architect-a",
                "--profile",
                "sre",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, spawned.returncode, spawned.stderr)
        artifact = json.loads(spawned.stdout)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("sre-1", artifact["evidence"]["node_id"])


if __name__ == "__main__":
    unittest.main()
