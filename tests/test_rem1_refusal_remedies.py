from __future__ import annotations

from floati import fixture_ids as public_ids

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
INS1_FILES = frozenset({"floati/deploy.py", "floati/installer_shadow.py"})
DRILL_PROTOCOL_CODES = frozenset({
    "deployment_currency_unavailable",
    "deployment_shadow_unknown",
    "arguments_invalid",
    "ack_item_unknown",
    "solo_identity_ambiguous",
    "authority_holder_mismatch",
    "work_unknown",
    "run_id_invalid",
})
MCP_INVALID_PARAMS = -32602


def derived_protocol_refusal_codes(root: Path) -> dict[str, frozenset[str]]:
    """Literal ProtocolRefusal codes by product file, never a hand-copied roster."""

    by_file: dict[str, set[str]] = {}
    for path in sorted((root / "floati").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        codes: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None
            )
            if name != "ProtocolRefusal" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                codes.add(first.value)
        if codes:
            by_file[rel] = codes
    return {path: frozenset(codes) for path, codes in by_file.items()}


class Rem1RefusalRemedyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "fleet"

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "floati", *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def artifact(self, completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        self.assertEqual(1, len(stream.splitlines()), stream)
        return json.loads(stream)

    def assert_populated_remedy(self, remedy: object, *, detail: str | None = None) -> None:
        self.assertIsNotNone(remedy, "refusal serialized remedy:null")
        if isinstance(remedy, dict):
            self.assertEqual("none", remedy.get("kind"))
            self.assertTrue(str(remedy.get("why") or "").strip())
            return
        self.assertIsInstance(remedy, str)
        self.assertTrue(remedy.strip())
        self.assertFalse(remedy.startswith("the "), remedy)
        if detail is not None:
            self.assertNotEqual(detail, remedy)

    def test_derived_enumeration_contains_the_drill_codes(self) -> None:
        """Catches a drill code that is no longer a ProtocolRefusal literal."""

        by_file = derived_protocol_refusal_codes(REPOSITORY_ROOT)
        derived = frozenset().union(*by_file.values()) if by_file else frozenset()
        missing = DRILL_PROTOCOL_CODES - derived
        self.assertEqual(frozenset(), missing)
        shadow_files = {
            path
            for path, codes in by_file.items()
            if "deployment_shadow_unknown" in codes
        }
        self.assertEqual(frozenset({"floati/deploy.py"}), shadow_files)
        self.assertTrue(shadow_files <= INS1_FILES)

    def test_drill_protocol_codes_serialize_a_non_null_remedy(self) -> None:
        """Catches the refusal path still emitting JSON null for the WS-I / M4 codes."""

        init = self.run_cli(
            "init",
            "--root",
            str(self.home),
            "--solo",
            public_ids.worker("alpha"),
            "--harness",
            "Codex",
        )
        self.assertEqual(0, init.returncode, init.stderr)

        probes = {
            "arguments_invalid": self.run_cli("inbox", "--root", str(self.home)),
            "run_id_invalid": self.run_cli(
                "effects", "--root", str(self.home), "--run", "run-invalid"
            ),
            "work_unknown": self.run_cli(
                "work",
                "show",
                "--root",
                str(self.home),
                "--id",
                "work-00000000000070008000000000000000",
            ),
            "ack_item_unknown": self.run_cli(
                "ack",
                "--root",
                str(self.home),
                "--as",
                public_ids.worker("alpha"),
                "--session",
                "session-a",
                "--id",
                "msg-" + ("0" * 32),
            ),
        }
        not_git = Path(self.temporary.name) / "not-a-git"
        not_git.mkdir()
        from floati.doctor import _git as doctor_git

        with self.assertRaises(ProtocolRefusal) as currency:
            doctor_git(not_git, "rev-parse", "--verify", "HEAD^{commit}")
        self.assertEqual("deployment_currency_unavailable", currency.exception.code)
        self.assert_populated_remedy(currency.exception.remedy)
        second = self.run_cli(
            "register",
            "--root",
            str(self.home),
            public_ids.worker("bravo"),
            "--harness",
            "Codex",
        )
        self.assertEqual(0, second.returncode, second.stderr)
        probes["solo_identity_ambiguous"] = self.run_cli(
            "work", "add", "--root", str(self.home), "--title", "rem1"
        )

        for code, completed in probes.items():
            with self.subTest(code=code):
                self.assertEqual(20, completed.returncode, completed.stderr)
                artifact = self.artifact(completed)
                evidence = artifact["evidence"]
                self.assertEqual(code, evidence["code"])
                self.assert_populated_remedy(
                    evidence.get("remedy"),
                    detail=str(evidence.get("detail")),
                )
                if isinstance(evidence.get("remedy"), str):
                    self.assertNotIn("the checkout is not current", evidence["remedy"])

        root = FloatiRoot.open_direct_home(self.home)
        from floati.work import WorkLog

        work = WorkLog(root)
        item = work.add("rem1-authority", public_ids.worker("alpha"), [])
        grant = AuthorityGrantStore(root).claim(
            "work-claims", public_ids.worker("alpha"), 60, 60, NOW
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            work.claim(
                item["id"],
                public_ids.worker("bravo"),
                "work-claims",
                int(grant["epoch"]),
                now=NOW,
            )
        self.assertEqual("authority_holder_mismatch", caught.exception.code)
        self.assert_populated_remedy(caught.exception.remedy)

        with self.assertRaises(ProtocolRefusal) as caught_shadow:
            raise ProtocolRefusal(
                "deployment_shadow_unknown",
                "Some PATH entries could not be read; shadow state unknown.",
            )
        # Constructor default must populate; INS-1 still owns the action text.
        self.assert_populated_remedy(caught_shadow.exception.remedy)

    def test_mcp_invalid_params_error_carries_a_non_null_remedy(self) -> None:
        """Catches JSON-RPC -32602 still omitting any action."""

        root = FloatiRoot.open_direct_home(self.home, create=True)
        Registry(root).register(public_ids.builder("a"), "Codex")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "floati",
                "mcp",
                "serve",
                "--root",
                str(root.path),
                "--as",
                public_ids.builder("a"),
                "--session",
                "session-a",
            ],
            cwd=REPOSITORY_ROOT,
            input="\n".join((
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "floati-test", "version": "1"},
                    },
                }, separators=(",", ":")),
                json.dumps({
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }, separators=(",", ":")),
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"arguments": {}},
                }, separators=(",", ":")),
            )) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        rows = [json.loads(line) for line in completed.stdout.splitlines()]
        error = rows[-1]["error"]
        self.assertEqual(MCP_INVALID_PARAMS, error["code"])
        remedy = (error.get("data") or {}).get("remedy")
        self.assert_populated_remedy(remedy)


if __name__ == "__main__":
    unittest.main()
