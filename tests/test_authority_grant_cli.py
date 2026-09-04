from __future__ import annotations

import io
import json
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from floati.cli import _parser, main
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records
from floati.registry import REGISTRY_KINDS, Registry
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from floati.work import WorkLog
from tests.temp_roots import REAL_TEMP_ROOT


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


class AuthorityGrantCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.registry = Registry(self.root)
        for node in ("architect-a", "worker-a", "builder-a"):
            self.registry.register(node, "Codex")
        self.assign_role("architect-a", "architect")
        self.assign_role("builder-a", "builder")

    def assign_role(self, node: str, role: str) -> None:
        templates = load_shipped_role_templates(Path("roles/shipped"))
        template = templates[role]
        answers = {question.key: "fixture" for question in template.questions}
        append_record(
            self.root,
            self.registry.relative_path,
            {
                "schema_version": 0,
                "id": "registry-role-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-08-28T12:00:00.000Z",
                "kind": "registry_role_record",
                "node_id": node,
                "template_role": role,
                "template_version": template.template_version,
                "template_sha256": template.digest,
                "answers": answers,
                "state": "active",
                "predecessor_role_record_id": None,
            },
            allowed_kinds=REGISTRY_KINDS,
        )

    def run_cli(self, *arguments: str) -> tuple[int, dict]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(list(arguments))
        self.assertEqual("", stderr.getvalue())
        return status, json.loads(stdout.getvalue())

    def grant(self, *, actor: str = "architect-a", epoch: int = 1) -> tuple[int, dict]:
        return self.run_cli(
            "grant",
            "--root",
            str(self.root.path),
            "--as",
            actor,
            "--holder",
            "worker-a",
            "--subject",
            "work-claims",
            "--epoch",
            str(epoch),
        )

    def revoke(self, *, actor: str = "architect-a", epoch: int = 1) -> tuple[int, dict]:
        return self.run_cli(
            "grant",
            "revoke",
            "--root",
            str(self.root.path),
            "--as",
            actor,
            "--holder",
            "worker-a",
            "--subject",
            "work-claims",
            "--epoch",
            str(epoch),
        )

    def authority_rows(self) -> list[dict]:
        return read_records(
            self.root,
            "authority-grants/work-claims.jsonl",
            allowed_kinds={"authority_grant"},
        )

    def test_manual_fleet_grant_drains_work_and_missing_refusal_names_coordinate(self) -> None:
        work = WorkLog(self.root)
        item = work.add("manual work", "worker-a", [], now=NOW)
        with self.assertRaises(ProtocolRefusal) as missing:
            work.claim(item["id"], "worker-a", "work-claims", 1, now=NOW)
        self.assertEqual("authority_missing", missing.exception.code)
        for value in ("worker-a", "work-claims", "epoch=1"):
            self.assertIn(value, missing.exception.detail)

        status, granted = self.grant()
        self.assertEqual(0, status)
        record = granted["evidence"]["record"]
        self.assertEqual("authority_grant", record["kind"])
        self.assertEqual("worker-a", record["holder"])
        self.assertEqual("work-claims", record["subject_id"])
        self.assertEqual(1, record["epoch"])

        claimed = work.claim(item["id"], "worker-a", "work-claims", 1)
        completed = work.complete(item["id"], "worker-a", [])
        self.assertEqual("claim", claimed["action"])
        self.assertEqual("complete", completed["action"])

    def test_revoke_is_same_shape_idempotent_and_claim_names_revoking_record(self) -> None:
        self.assertEqual(0, self.grant()[0])
        first_status, first = self.revoke()
        replay_status, replay = self.revoke()
        self.assertEqual(0, first_status)
        self.assertEqual(0, replay_status)
        self.assertEqual(first["evidence"]["record"], replay["evidence"]["record"])

        work = WorkLog(self.root)
        item = work.add("revoked work", "worker-a", [])
        with self.assertRaises(ProtocolRefusal) as inactive:
            work.claim(item["id"], "worker-a", "work-claims", 1)
        self.assertEqual("authority_inactive", inactive.exception.code)
        self.assertIn(first["evidence"]["record"]["id"], inactive.exception.detail)

    def test_epoch_supersession_invalidates_old_coordinate(self) -> None:
        first = self.grant()[1]["evidence"]["record"]
        second = self.grant(epoch=2)[1]["evidence"]["record"]
        replay = self.grant(epoch=2)[1]["evidence"]["record"]
        self.assertEqual(second, replay)
        self.assertEqual(1, first["epoch"])
        self.assertEqual(2, second["epoch"])

        item = WorkLog(self.root).add("new epoch", "worker-a", [])
        with self.assertRaises(ProtocolRefusal) as stale:
            WorkLog(self.root).claim(item["id"], "worker-a", "work-claims", 1)
        self.assertEqual("authority_epoch_mismatch", stale.exception.code)
        self.assertIn("epoch=1", stale.exception.detail)
        WorkLog(self.root).claim(item["id"], "worker-a", "work-claims", 2)

    def test_non_architect_and_absent_architect_role_share_typed_remedy(self) -> None:
        for actor in ("builder-a", "worker-a"):
            with self.subTest(actor=actor):
                status, artifact = self.grant(actor=actor)
                self.assertEqual(20, status)
                self.assertEqual("grant_requires_architect", artifact["evidence"]["code"])
                self.assertIn("node role --template architect", artifact["evidence"]["detail"])
        self.assertEqual([], self.authority_rows())

        absent_root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "no-architect", create=True
        )
        Registry(absent_root).register("worker-only", "Codex")
        status, artifact = self.run_cli(
            "grant", "--root", str(absent_root.path), "--as", "worker-only",
            "--holder", "worker-only", "--subject", "work-claims", "--epoch", "1",
        )
        self.assertEqual(20, status)
        self.assertEqual("grant_requires_architect", artifact["evidence"]["code"])
        self.assertIn("node role --template architect", artifact["evidence"]["detail"])

    def test_non_architect_cannot_revoke_an_existing_grant(self) -> None:
        self.assertEqual(0, self.grant()[0])
        status, artifact = self.revoke(actor="builder-a")
        self.assertEqual(20, status)
        self.assertEqual("grant_requires_architect", artifact["evidence"]["code"])
        self.assertEqual(1, len(self.authority_rows()))
        self.assertEqual("active", self.authority_rows()[0]["state"])

    def test_glob_coordinates_refuse_without_append(self) -> None:
        cases = (("--holder", "*"), ("--subject", "work-*"))
        for flag, value in cases:
            arguments = [
                "grant", "--root", str(self.root.path), "--as", "architect-a",
                "--holder", "worker-a", "--subject", "work-claims", "--epoch", "1",
            ]
            arguments[arguments.index(flag) + 1] = value
            with self.subTest(flag=flag):
                status, artifact = self.run_cli(*arguments)
                self.assertEqual(20, status)
                self.assertEqual("refused", artifact["status"])
        self.assertEqual([], self.authority_rows())

    def test_agents_manual_fleet_examples_round_trip_live_parser(self) -> None:
        document = Path("AGENTS.md").read_text(encoding="utf-8")
        section = document.split("**Manual non-solo work authority:**", 1)[1]
        section = section.split("- **Health check:**", 1)[0]
        examples = [
            line.strip()
            for line in section.splitlines()
            if line.strip().startswith("floati ")
        ]
        self.assertEqual(8, len(examples))
        parser = _parser()
        parsed = [parser.parse_args(shlex.split(line)[1:]) for line in examples]
        grants = [item for item in parsed if item.command == "grant"]
        self.assertEqual([None, "revoke"], [item.grant_command for item in grants])


if __name__ == "__main__":
    unittest.main()
