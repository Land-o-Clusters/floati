from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.registry import Registry
from floati.root import FloatiRoot


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def invoke(
    *arguments: str, environment: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    completed = subprocess.run(
        [sys.executable, "-m", "floati", *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=None if environment is None else {**os.environ, **environment},
    )
    stream = completed.stdout if completed.returncode == 0 else completed.stderr
    return completed, json.loads(stream)


class InGuardScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fleet"
        self.opened = FloatiRoot.open_direct_home(self.root, create=True)
        Registry(self.opened).register(public_ids.builder("a"), "Codex")

    def test_empty_inbox_names_root_tenant_and_explicit_source(self):
        _completed, artifact = invoke(
            "inbox", "--root", str(self.root), "--as", public_ids.builder("a"), "--peek"
        )
        self.assertEqual(str(self.opened.path), artifact["evidence"]["scope"]["root"])
        self.assertEqual(self.opened.tenant_id, artifact["evidence"]["scope"]["tenant"])
        self.assertEqual("explicit", artifact["evidence"]["scope"]["root_source"])

    def test_unknown_node_is_typed_not_empty(self):
        completed, artifact = invoke(
            "inbox", "--root", str(self.root), "--as", "never-registered", "--peek"
        )
        self.assertEqual(20, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertEqual(1, len(completed.stderr.splitlines()))
        self.assertEqual(
            json.dumps(
                artifact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            completed.stderr,
        )
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(str(self.opened.path), artifact["evidence"]["scope"]["root"])
        self.assertEqual(self.opened.tenant_id, artifact["evidence"]["scope"]["tenant"])
        self.assertEqual("explicit", artifact["evidence"]["scope"]["root_source"])
        self.assertEqual("recipient_unregistered", artifact["evidence"]["code"])

    def test_status_default_and_json_name_validated_explicit_scope(self):
        for arguments in (
            ("status", "--root", str(self.root)),
            ("status", "--root", str(self.root), "--json"),
        ):
            with self.subTest(arguments=arguments):
                _completed, artifact = invoke(*arguments)
                self.assertEqual(
                    str(self.opened.path), artifact["evidence"]["scope"]["root"]
                )
                self.assertEqual(
                    self.opened.tenant_id,
                    artifact["evidence"]["scope"]["tenant"],
                )
                self.assertEqual(
                    "explicit", artifact["evidence"]["scope"]["root_source"]
                )

    def test_status_uses_validated_environment_scope(self):
        environment_root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "environment-fleet", create=True
        )
        _completed, artifact = invoke(
            "status",
            environment={"FLOATI_BUS_ROOT": str(environment_root.path)},
        )

        self.assertEqual(
            str(environment_root.path), artifact["evidence"]["scope"]["root"]
        )
        self.assertEqual(
            environment_root.tenant_id, artifact["evidence"]["scope"]["tenant"]
        )
        self.assertEqual("environment", artifact["evidence"]["scope"]["root_source"])

    def test_status_explicit_root_precedes_conflicting_environment_scope(self):
        environment_root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "conflicting-environment-fleet", create=True
        )
        _completed, artifact = invoke(
            "status",
            "--root",
            str(self.root),
            "--json",
            environment={"FLOATI_BUS_ROOT": str(environment_root.path)},
        )

        self.assertEqual(str(self.opened.path), artifact["evidence"]["scope"]["root"])
        self.assertEqual(self.opened.tenant_id, artifact["evidence"]["scope"]["tenant"])
        self.assertEqual("explicit", artifact["evidence"]["scope"]["root_source"])


if __name__ == "__main__":
    unittest.main()
