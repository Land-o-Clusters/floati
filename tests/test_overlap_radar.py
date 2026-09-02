from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.git_process import fixed_git_command, fixed_git_environment
from tests.schema_validation import validate_json_schema


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        fixed_git_command("/usr/bin/git", repository, arguments),
        env=fixed_git_environment("/usr/bin/git"),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=Floati Fixture",
        "-c",
        "user.email=floati-fixture@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD")


def _write(repository: Path, relative: str, content: str) -> None:
    target = repository / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


class OverlapRadarSurfaceTests(unittest.TestCase):
    def test_r1_exposes_only_the_internal_report_surface(self) -> None:
        """Removing the bounded internal R1 entry points breaks every consumer."""

        self.assertIsNotNone(importlib.util.find_spec("floati.overlap_radar"))
        radar = importlib.import_module("floati.overlap_radar")
        self.assertTrue(callable(getattr(radar, "validate_signal", None)))
        self.assertTrue(callable(getattr(radar, "hard_concurrency_keys", None)))
        self.assertTrue(callable(getattr(radar, "derive_overlap_report", None)))

        from floati.cli import _parser

        command_action = next(
            action for action in _parser()._actions if action.dest == "command"
        )
        self.assertNotIn("overlap-radar", command_action.choices)

    def test_overlap_report_verb_is_the_product_consumer_and_stays_out_of_mcp(self) -> None:
        """WIRE-2 closes only when a real verb emits the existing bounded fact."""

        from floati.cli import _parser

        command_action = next(
            action for action in _parser()._actions if action.dest == "command"
        )
        self.assertIn("overlap", command_action.choices)
        overlap = command_action.choices["overlap"]
        subcommands = next(
            action for action in overlap._actions if action.dest == "overlap_command"
        )
        report = subcommands.choices["report"]
        self.assertEqual("never", report.floati_mcp_exposure)

    def test_fabricated_signal_stamp_refuses(self) -> None:
        """Accepting an unruled stamp lets heuristic evidence impersonate fact."""

        radar = importlib.import_module("floati.overlap_radar")
        with self.assertRaises(ProtocolRefusal) as caught:
            radar.validate_signal(
                {
                    "kind": "same_symbol",
                    "coordinate": "floati/core.py:build",
                    "stamp": "FABRICATED",
                    "hard_lock": False,
                    "detail": "fixture",
                }
            )

        self.assertEqual("overlap_signal_stamp_invalid", caught.exception.code)

    def test_heuristic_signal_cannot_drive_a_hard_lock(self) -> None:
        """Advisory evidence must never become a scheduler lock by accident."""

        radar = importlib.import_module("floati.overlap_radar")
        with self.assertRaises(ProtocolRefusal) as caught:
            radar.hard_concurrency_keys(
                [
                    {
                        "kind": "similar_text",
                        "coordinate": "floati/core.py",
                        "stamp": "HEURISTIC",
                        "hard_lock": True,
                        "detail": "fixture",
                    }
                ]
            )

        self.assertEqual(
            "overlap_heuristic_hard_lock_refused", caught.exception.code
        )


class OverlapRadarDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="floati-overlap-radar-")
        self.repository = Path(self.temporary.name).resolve()
        _git(self.repository, "init", "--initial-branch=main")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _base(self, module: str, *, with_schema: bool = False) -> str:
        _write(self.repository, "package/service.py", module)
        if with_schema:
            _write(
                self.repository,
                "schemas/v1/task.schema.json",
                json.dumps({"type": "object", "title": "Base"}) + "\n",
            )
        return _commit(self.repository, "base")

    def test_same_symbol_and_same_schema_are_measured(self) -> None:
        """Removing either exact detector loses a measured overlap coordinate."""

        base = self._base(
            "def shared():\n    return 'base'\n\ndef stable():\n    return True\n",
            with_schema=True,
        )
        _write(
            self.repository,
            "package/service.py",
            "def shared():\n    return 'left'\n\ndef stable():\n    return True\n",
        )
        _write(
            self.repository,
            "schemas/v1/task.schema.json",
            json.dumps({"type": "object", "title": "Left"}) + "\n",
        )
        left = _commit(self.repository, "left")

        _git(self.repository, "reset", "--hard", base)
        _write(
            self.repository,
            "package/service.py",
            "def shared():\n    return 'right'\n\ndef stable():\n    return True\n",
        )
        _write(
            self.repository,
            "schemas/v1/task.schema.json",
            json.dumps({"type": "object", "title": "Right"}) + "\n",
        )
        right = _commit(self.repository, "right")

        radar = importlib.import_module("floati.overlap_radar")
        report = radar.derive_overlap_report(self.repository, base, left, right)

        measured = {
            (signal["kind"], signal["coordinate"], signal["stamp"])
            for signal in report["signals"]
        }
        self.assertIn(
            ("same_symbol", "package/service.py:shared", "MEASURED"), measured
        )
        self.assertIn(
            ("same_schema", "schemas/v1/task.schema.json", "MEASURED"), measured
        )
        self.assertEqual(
            {
                "repository_root": str(self.repository),
                "base_ref": base,
                "base_sha": base,
                "left_ref": left,
                "left_sha": left,
                "right_ref": right,
                "right_sha": right,
            },
            report["inputs"],
        )

    def test_predispatch_report_is_a_fact_with_typed_attempt_absence(self) -> None:
        """A pre-dispatch read must not invent an attempt or claim to be a receipt."""

        base = self._base("def stable():\n    return True\n")

        radar = importlib.import_module("floati.overlap_radar")
        report = radar.derive_overlap_report(self.repository, base, base, base)

        self.assertEqual(1, report["schema_version"])
        self.assertEqual("absent_predispatch", report["attempt_binding"])
        self.assertNotIn("receipt", report)
        validate_json_schema(
            report, Path("schemas/v1/overlap-report-fact.schema.json")
        )

    def test_byte_disjoint_semantically_clean_changes_do_not_warn(self) -> None:
        """A shared file alone is not evidence that two branches overlap."""

        base = self._base(
            "def alpha():\n    return 'base-a'\n\ndef beta():\n    return 'base-b'\n"
        )
        _write(
            self.repository,
            "package/service.py",
            "def alpha():\n    return 'left-a'\n\ndef beta():\n    return 'base-b'\n",
        )
        left = _commit(self.repository, "left")

        _git(self.repository, "reset", "--hard", base)
        _write(
            self.repository,
            "package/service.py",
            "def alpha():\n    return 'base-a'\n\ndef beta():\n    return 'right-b'\n",
        )
        right = _commit(self.repository, "right")

        radar = importlib.import_module("floati.overlap_radar")
        report = radar.derive_overlap_report(self.repository, base, left, right)

        self.assertEqual([], report["signals"])
        self.assertEqual([], report["warnings"])
        self.assertEqual(
            str(self.repository), report["inputs"]["repository_root"]
        )

    def test_overlap_report_verb_emits_the_existing_fact_without_mutation(self) -> None:
        """The WIRE-2 verb must be a read-only caller, not a second derivation."""

        base = self._base(
            "def shared():\n    return 'base'\n\ndef stable():\n    return True\n",
            with_schema=True,
        )
        _write(
            self.repository,
            "package/service.py",
            "def shared():\n    return 'left'\n\ndef stable():\n    return True\n",
        )
        left = _commit(self.repository, "left")
        _git(self.repository, "reset", "--hard", base)
        _write(
            self.repository,
            "package/service.py",
            "def shared():\n    return 'right'\n\ndef stable():\n    return True\n",
        )
        right = _commit(self.repository, "right")
        before = {
            path.relative_to(self.repository): path.read_bytes()
            for path in self.repository.rglob("*")
            if path.is_file()
        }

        completed = subprocess.run(
            [
                "python3",
                "-m",
                "floati",
                "overlap",
                "report",
                "--repository",
                str(self.repository),
                "--base-ref",
                base,
                "--left-ref",
                left,
                "--right-ref",
                right,
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        self.assertEqual(1, len(completed.stdout.splitlines()))
        artifact = json.loads(completed.stdout)
        self.assertEqual("overlap", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(1, artifact["evidence"]["schema_version"])
        self.assertEqual("absent_predispatch", artifact["evidence"]["attempt_binding"])
        self.assertEqual(
            ["package/service.py:shared"],
            [signal["coordinate"] for signal in artifact["evidence"]["signals"]],
        )
        self.assertEqual(
            before,
            {
                path.relative_to(self.repository): path.read_bytes()
                for path in self.repository.rglob("*")
                if path.is_file()
            },
        )


if __name__ == "__main__":
    unittest.main()
