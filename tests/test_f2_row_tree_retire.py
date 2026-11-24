"""F2-ROW-TREE-RETIRE-1: offer git worktree remove for finished row trees.

Am.7 / msg-01a12cf2455c7ce282c23d45be376070: a landed, clean, fully
pushed, unclaimed row worktree is offered (never --force). Dirty,
unpushed, claimed, not-landed, probe-error, and detached-unreferenced
trees are kept BY NAME. Offer only; dry by default. Never apply on
the live box.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import List, Sequence

from floati import fixture_ids as ids
from floati.registry import Registry
from floati.row_tree_retire import offer_row_trees
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

GIT = "/usr/bin/git"


def _git(repo: Path, arguments: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
    env = {
        "GIT_AUTHOR_NAME": "retire-test",
        "GIT_AUTHOR_EMAIL": "retire@test",
        "GIT_COMMITTER_NAME": "retire-test",
        "GIT_COMMITTER_EMAIL": "retire@test",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "EMAIL": "retire@test",
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        [GIT, "-c", "init.defaultBranch=main", "-C", os.fspath(repo), *arguments],
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


class RowTreeRetireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.primary = self.base / "primary"
        self.origin = self.base / "origin.git"
        self.trees = self.base / "trees"
        self.trees.mkdir()
        self.primary.mkdir()
        _git(self.primary, ["init"])
        _git(self.primary, ["commit", "--allow-empty", "-m", "root"])
        subprocess.run(
            [GIT, "-c", "init.defaultBranch=main", "clone", "--bare", str(self.primary), str(self.origin)],
            check=True,
            capture_output=True,
            text=True,
            env={"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/var/empty", "PATH": "/usr/bin:/bin"},
        )
        _git(self.primary, ["remote", "add", "origin", str(self.origin)])
        _git(self.primary, ["push", "-u", "origin", "main"])
        landed_sha = _git(self.primary, ["rev-parse", "HEAD"]).stdout.strip()

        _git(self.primary, ["branch", "landed-clean", "HEAD"])
        _git(self.primary, ["worktree", "add", str(self.trees / "landed-clean"), "landed-clean"])
        self.landed_clean = (self.trees / "landed-clean").resolve()

        _git(self.primary, ["checkout", "-b", "landed-extra"])
        _git(self.primary, ["commit", "--allow-empty", "-m", "extra-local"])
        _git(self.primary, ["checkout", "main"])
        _git(self.primary, ["merge", "landed-extra"])
        _git(self.primary, ["worktree", "add", str(self.trees / "landed-unpushed"), "landed-extra"])
        self.landed_unpushed = (self.trees / "landed-unpushed").resolve()

        _git(self.primary, ["checkout", "-b", "wip"])
        _git(self.primary, ["commit", "--allow-empty", "-m", "unlanded"])
        _git(self.primary, ["checkout", "main"])
        _git(self.primary, ["worktree", "add", str(self.trees / "unlanded"), "wip"])
        self.unlanded = (self.trees / "unlanded").resolve()

        _git(self.primary, ["branch", "dirty-br", landed_sha])
        _git(self.primary, ["worktree", "add", str(self.trees / "dirty"), "dirty-br"])
        self.dirty = (self.trees / "dirty").resolve()
        (self.dirty / "dirt.txt").write_text("dirt\n", encoding="utf-8")

        _git(self.primary, ["branch", "claimed-br", landed_sha])
        _git(self.primary, ["worktree", "add", str(self.trees / "claimed"), "claimed-br"])
        self.claimed = (self.trees / "claimed").resolve()

        _git(self.primary, ["checkout", "-b", "doomed"])
        _git(self.primary, ["commit", "--allow-empty", "-m", "doomed"])
        doomed_sha = _git(self.primary, ["rev-parse", "HEAD"]).stdout.strip()
        _git(self.primary, ["checkout", "main"])
        _git(self.primary, ["worktree", "add", "--detach", str(self.trees / "detached"), doomed_sha])
        _git(self.primary, ["branch", "-D", "doomed"])
        self.detached = (self.trees / "detached").resolve()

        _git(self.primary, ["branch", "probe-br", landed_sha])
        _git(self.primary, ["worktree", "add", str(self.trees / "probe"), "probe-br"])
        self.probe = (self.trees / "probe").resolve()

        (self.home / "state").mkdir(parents=True, exist_ok=True)
        (self.home / "state" / "row-tree-repos.v0.json").write_text(
            json.dumps({"schema_version": 0, "repos": [str(self.primary.resolve())]}),
            encoding="utf-8",
        )

    def _git_run(self, repository: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess:
        if Path(repository).resolve() == self.probe and arguments and arguments[0] == "status":
            raise OSError("git status refused")
        return _git(Path(repository), list(arguments), check=False)

    def _kwargs(self) -> dict:
        return {
            "claimed_trees": (self.claimed,),
            "git_run": self._git_run,
            "now": "2026-10-11T00:00:00Z",
        }

    def _paths(self, rows: List[dict], *, reason: str | None = None) -> List[Path]:
        chosen = rows if reason is None else [row for row in rows if row.get("reason") == reason]
        return [Path(row["path"]).resolve() for row in chosen]

    def test_dry_run_offers_only_the_landed_clean_tree(self) -> None:
        result = offer_row_trees(self.root, apply=False, **self._kwargs())
        self.assertEqual([self.landed_clean], self._paths(result["offered"]))
        self.assertTrue(result["dry_run"])
        self.assertEqual([], result["retired"])
        for path in (
            self.landed_clean,
            self.landed_unpushed,
            self.unlanded,
            self.dirty,
            self.claimed,
            self.detached,
            self.probe,
        ):
            self.assertTrue(path.is_dir(), path)

    def test_apply_retires_only_the_landed_clean_tree(self) -> None:
        result = offer_row_trees(self.root, apply=True, **self._kwargs())
        self.assertEqual([self.landed_clean], self._paths(result["retired"]))
        self.assertFalse(self.landed_clean.exists())
        self.assertTrue(self.landed_unpushed.is_dir())
        self.assertTrue(self.unlanded.is_dir())
        self.assertTrue(self.dirty.is_dir())
        self.assertTrue(self.claimed.is_dir())
        self.assertTrue(self.detached.is_dir())
        self.assertTrue(self.probe.is_dir())
        self.assertIn("utc", result["retired"][0])
        self.assertIn("rule", result["retired"][0])

    def test_keep_reasons_are_named(self) -> None:
        result = offer_row_trees(self.root, apply=False, **self._kwargs())
        self.assertEqual([self.landed_unpushed], self._paths(result["kept"], reason="unpushed"))
        self.assertEqual([self.unlanded], self._paths(result["kept"], reason="not_landed"))
        self.assertEqual([self.dirty], self._paths(result["kept"], reason="dirty"))
        self.assertEqual([self.claimed], self._paths(result["kept"], reason="claimed"))
        self.assertEqual(
            [self.detached],
            self._paths(result["kept"], reason="detached_unreferenced"),
        )
        self.assertEqual([self.probe], self._paths(result["kept"], reason="probe_error"))

    def _plant_live_board_claim(self, workspace: Path) -> None:
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger,
            CodexWaitSessionLedger,
            resolve_participant,
        )

        node = ids.builder("floati")
        Registry(self.root).register(node, "worker")
        map_path = self.home / "codex-wait" / "workspaces.v0.json"
        map_path.parent.mkdir(parents=True, exist_ok=True)
        map_path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": self.home.name,
                    "mappings": [
                        {"workspace": str(workspace.resolve()), "node_id": node}
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        participant = resolve_participant(self.home, workspace)
        self.assertIsNotNone(participant)
        assert participant is not None
        consent = CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="retire-live-claim-consent",
        )
        CodexWaitSessionLedger(self.root).arm(
            participant.binding,
            consent,
            "retire-live-session",
            idempotency_key="retire-live-claim-arm",
        )

    def test_production_apply_keeps_a_claimed_workspace_worktree(self) -> None:
        import contextlib
        import io

        from floati.cli import main

        self._plant_live_board_claim(self.claimed)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["seat", "retire", "--root", str(self.root.path), "--apply"])
        artifact = json.loads(output.getvalue())
        self.assertEqual(0, rc, artifact)
        self.assertTrue(self.claimed.is_dir(), artifact)
        kept = {
            Path(row["path"]).resolve()
            for row in artifact["evidence"]["kept"]
            if row.get("reason") == "claimed"
        }
        self.assertIn(self.claimed, kept)
        self.assertFalse(self.landed_clean.exists())

    def test_cli_dry_run_is_the_default(self) -> None:
        import contextlib
        import io

        from floati.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["seat", "retire", "--root", str(self.root.path)])
        artifact = json.loads(output.getvalue())
        self.assertEqual(0, rc, artifact)
        self.assertTrue(artifact["evidence"]["dry_run"])
        self.assertEqual([], artifact["evidence"]["retired"])
        self.assertTrue(self.landed_clean.is_dir())

    def test_apply_never_passes_force(self) -> None:
        calls: List[Sequence[str]] = []

        def spy(repository: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess:
            calls.append(arguments)
            return self._git_run(repository, arguments)

        offer_row_trees(
            self.root,
            apply=True,
            claimed_trees=(self.claimed,),
            git_run=spy,
            now="2026-10-11T00:00:00Z",
        )
        for arguments in calls:
            self.assertNotIn("--force", arguments)
