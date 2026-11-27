"""User-level Cursor stop hook: the seat comes from a workspace marker.

Scratch home and scratch workspaces only. Nothing here reads or writes
the operator home's ``.cursor`` or this repository's project ``.cursor``.
No Cursor process is started.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from tests.readiness import wait_process
import sys
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.cursor_wait import (
    CURSOR_LOOP_LIMIT,
    DEFAULT_DEADLINE_SECONDS,
    HOOK_TIMEOUT_SECONDS,
    install_cursor_hook,
)
from floati.mcp import run_cli_artifact
from floati.root import FloatiRoot
from tests import HOME_PIN_ROOT, REAL_HOME_BEFORE_PIN
from tests.temp_roots import REAL_TEMP_ROOT


NODE = public_ids.builder("c")
OTHER = public_ids.builder("d")
MARKER_NAME = "floati-seat.json"
SCRIPT_NAME = "floati-cursor-user-stop.py"
JOURNAL_NAME = "floati-seat-journal.jsonl"


class CursorUserHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        home = Path.home()
        self.assertEqual(Path(HOME_PIN_ROOT), home)
        self.assertNotEqual(Path(REAL_HOME_BEFORE_PIN), home)
        self.home = home
        cursor = home / ".cursor"
        if cursor.exists():
            self._remove_tree(cursor)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.runtime = self.base / "runtime"
        self.write_launcher(self.runtime)
        self.argv_path = self.base / "hook-argv.txt"

    def _remove_tree(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return
        for child in path.iterdir():
            self._remove_tree(child)
        path.rmdir()

    def write_launcher(self, runtime: Path) -> None:
        scripts = runtime / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        launcher = scripts / "floati"
        launcher.write_text(
            "#!/bin/sh\n"
            "out=\"${FLOATI_HOOK_ARGV:-}\"\n"
            "if [ -n \"$out\" ]; then\n"
            "  printf '%s\\n' \"$@\" > \"$out\"\n"
            "fi\n"
            "printf '%s\\n' '{}'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)

    def marker_path(self, workspace: Path) -> Path:
        return workspace / ".cursor" / MARKER_NAME

    @property
    def user_hooks(self) -> Path:
        return self.home / ".cursor" / "hooks.json"

    @property
    def user_script(self) -> Path:
        return self.home / ".cursor" / SCRIPT_NAME

    @property
    def journal_path(self) -> Path:
        return self.home / ".cursor" / JOURNAL_NAME

    def write_marker(
        self,
        workspace: Path,
        *,
        node: str,
        root: Path,
        runtime: Path,
        workspace_field: str | None = None,
        raw: str | None = None,
    ) -> Path:
        path = self.marker_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return path
        body = {
            "harness": "cursor",
            "node": node,
            "root": str(root),
            "runtime": str(runtime),
            "schema_version": 0,
            "workspace": workspace_field if workspace_field is not None else str(workspace),
        }
        path.write_text(json.dumps(body, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def install(self, workspace: Path, node: str, runtime: Path | None = None) -> None:
        install_cursor_hook(
            workspace=workspace,
            root=self.root.path,
            node=node,
            runtime=runtime or self.runtime,
        )

    def resolve(self, roots: list[Path]):
        from floati.cursor_user_hook import resolve_cursor_seat

        return resolve_cursor_seat([str(root) for root in roots])

    def journal_records(self) -> list[dict]:
        if not self.journal_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def run_installed_hook(self, roots: list[Path], *, cwd: Path) -> subprocess.CompletedProcess:
        if self.argv_path.exists():
            self.argv_path.unlink()
        env = os.environ.copy()
        env["FLOATI_HOOK_ARGV"] = str(self.argv_path)
        proc = subprocess.Popen(
            [sys.executable, str(self.user_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            text=True,
            start_new_session=True,
        )

        def reap() -> None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                return
            wait_process(proc, "collect the stopped Cursor user-hook fixture")

        self.addCleanup(reap)
        payload = json.dumps({"hook_event_name": "stop", "workspace_roots": [str(root) for root in roots]})
        try:
            out, err = proc.communicate(payload, timeout=10)
        except subprocess.TimeoutExpired:
            reap()
            self.fail("user hook did not exit")
        return subprocess.CompletedProcess(
            args=proc.args, returncode=proc.returncode, stdout=out, stderr=err,
        )

    def test_resolve_returns_the_seat_named_by_the_marker(self) -> None:
        self.write_marker(self.workspace, node=NODE, root=self.root.path, runtime=self.runtime)
        outcome = self.resolve([self.workspace])
        self.assertEqual("resolved", outcome["status"])
        self.assertEqual(NODE, outcome["node"])
        self.assertEqual(str(self.root.path), outcome["root"])
        self.assertEqual(str(self.workspace), outcome["workspace"])
        self.assertEqual(str(self.runtime), outcome["runtime"])

    def test_absent_marker_fails_closed_with_a_journal_line(self) -> None:
        outcome = self.resolve([self.workspace])
        self.assertEqual("refused", outcome["status"])
        self.assertNotEqual(NODE, outcome.get("node"))
        record = self.journal_records()[-1]
        self.assertEqual("seat_unresolved", record["event"])
        self.assertEqual("cursor_seat_marker_absent", record["code"])
        self.assertEqual(str(self.marker_path(self.workspace)), record["marker"])

    def test_unreadable_marker_fails_closed_with_a_journal_line(self) -> None:
        path = self.write_marker(
            self.workspace, node=NODE, root=self.root.path, runtime=self.runtime, raw="not-json\n",
        )
        outcome = self.resolve([self.workspace])
        self.assertEqual("refused", outcome["status"])
        self.assertNotEqual(NODE, outcome.get("node"))
        record = self.journal_records()[-1]
        self.assertEqual("seat_unresolved", record["event"])
        self.assertEqual("cursor_seat_marker_unreadable", record["code"])
        self.assertEqual(str(path), record["marker"])

    def test_two_markers_fail_closed(self) -> None:
        other = self.base / "other-workspace"
        other.mkdir()
        other_runtime = self.base / "other-runtime"
        self.write_launcher(other_runtime)
        self.write_marker(self.workspace, node=NODE, root=self.root.path, runtime=self.runtime)
        self.write_marker(other, node=OTHER, root=self.root.path, runtime=other_runtime)
        outcome = self.resolve([self.workspace, other])
        self.assertEqual("refused", outcome["status"])
        self.assertNotIn(outcome.get("node"), (NODE, OTHER))
        record = self.journal_records()[-1]
        self.assertEqual("seat_unresolved", record["event"])
        self.assertEqual("cursor_seat_marker_ambiguous", record["code"])
        self.assertEqual(
            {str(self.marker_path(self.workspace)), str(self.marker_path(other))},
            set(record["markers"]),
        )

    def test_marker_for_another_workspace_is_not_used(self) -> None:
        other = self.base / "other-workspace"
        other.mkdir()
        self.write_marker(other, node=OTHER, root=self.root.path, runtime=self.runtime)
        outcome = self.resolve([self.workspace])
        self.assertEqual("refused", outcome["status"])
        self.assertNotEqual(OTHER, outcome.get("node"))
        self.assertNotEqual(str(other), outcome.get("workspace"))
        record = self.journal_records()[-1]
        self.assertEqual("cursor_seat_marker_absent", record["code"])
        self.assertEqual(str(self.marker_path(self.workspace)), record["marker"])
        self.assertNotIn(str(self.marker_path(other)), json.dumps(record))

    def test_install_writes_the_marker_and_a_seat_free_user_hook(self) -> None:
        self.install(self.workspace, NODE)
        body = json.loads(self.marker_path(self.workspace).read_text(encoding="utf-8"))
        self.assertEqual(str(self.root.path), body["root"])
        self.assertEqual(NODE, body["node"])
        self.assertEqual(str(self.workspace), body["workspace"])
        self.assertEqual(str(self.runtime), body["runtime"])
        document = json.loads(self.user_hooks.read_text(encoding="utf-8"))
        commands = [
            row["command"]
            for row in document["hooks"]["stop"]
            if isinstance(row, dict) and isinstance(row.get("command"), str)
        ]
        owned = [command for command in commands if SCRIPT_NAME in command]
        self.assertEqual(NODE, self.resolve([self.workspace])["node"])
        self.assertNotIn(NODE, owned[0])
        self.assertNotIn(str(self.root.path), owned[0])
        self.assertTrue(self.user_script.is_file())
        self.assertNotEqual(Path(REAL_HOME_BEFORE_PIN) / ".cursor", self.user_hooks.parent)

    def test_uninstall_removes_the_marker(self) -> None:
        path = self.write_marker(self.workspace, node=NODE, root=self.root.path, runtime=self.runtime)
        code, artifact = run_cli_artifact([
            "hook", "uninstall",
            "--harness", "cursor",
            "--root", str(self.root.path),
            "--as", NODE,
            "--workspace", str(self.workspace),
            "--runtime", str(self.runtime),
        ])
        self.assertEqual(0, code, artifact)
        self.assertFalse(path.exists())

    def test_second_install_matches_the_first_marker_and_user_hook_bytes(self) -> None:
        foreign = {"command": "/usr/bin/true", "timeout": 5}
        self.user_hooks.parent.mkdir(parents=True)
        self.user_hooks.write_text(json.dumps({
            "version": 1,
            "hooks": {"stop": [foreign]},
        }) + "\n", encoding="utf-8")
        self.install(self.workspace, NODE)
        marker = self.marker_path(self.workspace).read_bytes()
        hooks = self.user_hooks.read_bytes()
        script = self.user_script.read_bytes()
        self.install(self.workspace, NODE)
        self.assertEqual(marker, self.marker_path(self.workspace).read_bytes())
        self.assertEqual(hooks, self.user_hooks.read_bytes())
        self.assertEqual(script, self.user_script.read_bytes())
        document = json.loads(self.user_hooks.read_text(encoding="utf-8"))
        commands = [
            row["command"]
            for row in document["hooks"]["stop"]
            if isinstance(row, dict) and isinstance(row.get("command"), str)
        ]
        self.assertIn("/usr/bin/true", commands)
        self.assertEqual(1, sum(1 for command in commands if SCRIPT_NAME in command))

    def test_installed_hook_runs_the_marker_seat_and_not_another(self) -> None:
        self.install(self.workspace, NODE)
        other = self.base / "other-workspace"
        other.mkdir()
        self.write_marker(other, node=OTHER, root=self.root.path, runtime=self.runtime)
        completed = self.run_installed_hook([self.workspace], cwd=other)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({}, json.loads(completed.stdout))
        # The launcher records "$@", which starts at the verb.
        argv = self.argv_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(["wake", "wait"], argv[:2])
        flags = dict(zip(argv[2::2], argv[3::2]))
        self.assertEqual("cursor", flags["--harness"])
        self.assertEqual(str(self.root.path), flags["--root"])
        self.assertEqual(NODE, flags["--as"])
        self.assertEqual(str(self.runtime), flags["--runtime"])
        self.assertEqual(str(int(DEFAULT_DEADLINE_SECONDS)), flags["--deadline-seconds"])
        self.assertEqual(str(int(HOOK_TIMEOUT_SECONDS)), flags["--hook-timeout-seconds"])
        self.assertEqual(str(int(CURSOR_LOOP_LIMIT)), flags["--loop-limit"])
        self.assertNotIn(OTHER, argv)

    def test_installed_hook_fails_closed_when_the_marker_is_absent(self) -> None:
        other = self.base / "other-workspace"
        other.mkdir()
        other_runtime = self.base / "other-runtime"
        self.write_launcher(other_runtime)
        self.install(self.workspace, NODE)
        self.marker_path(self.workspace).unlink()
        for markers in ("absent", "ambiguous"):
            with self.subTest(markers=markers):
                if markers == "ambiguous":
                    self.install(self.workspace, NODE)
                    self.install(other, OTHER, other_runtime)
                workspaces = [other] if markers == "absent" else [self.workspace, other]
                cwd = self.workspace if markers == "absent" else self.base
                completed = self.run_installed_hook(workspaces, cwd=cwd)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual({}, json.loads(completed.stdout))
                self.assertFalse(self.argv_path.exists(), "unresolved markers must not start a waiter")
                record = self.journal_records()[-1]
                self.assertEqual("cursor_seat_marker_" + markers, record["code"])
                if markers == "absent":
                    self.assertEqual(str(self.marker_path(other)), record["marker"])
                else:
                    self.assertEqual({str(self.marker_path(self.workspace)), str(self.marker_path(other))},
                        set(record["markers"]))


if __name__ == "__main__":
    unittest.main()
