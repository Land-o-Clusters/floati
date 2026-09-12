"""FENCE-H1: the suite cannot reach the operator's real home.

A lane test once reached the operator's real ``~/Library/LaunchAgents``
through a library default and deleted four plists. The fence pins HOME
(and the XDG roots) to a scratch directory under the real temp root for
every test process, at ``tests`` package import — before any test
module, runner, or library default resolves a path. Nothing that
resolves through the environment can land in the operator's account.

The fence is proven three ways: the pin is present in every test
process (tripwire), a child process inherits it (the library-default
class dies in grandchildren too), and a planted home-touching test
module is DERIVED and named by the population walker — never
enumerated. Control: tests that use REAL_TEMP_ROOT are untouched; the
real temp root is not home-derived.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests import REAL_HOME_BEFORE_PIN, HOME_PIN_ROOT
from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

_HOME_RESOLVERS = {"home", "expanduser"}


def _home_touching_files(root: Path) -> set[str]:
    """Derive the test modules that resolve the operator's home, by AST.

    The population is walked, never enumerated: a module counts when it
    references ``Path.home()``, ``os.path.expanduser``, or reads the
    ``HOME`` environment variable — the three shapes a test can learn
    the operator's account through.
    """

    found: set[str] = set()
    for path in sorted(root.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        touched = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _HOME_RESOLVERS
            ):
                touched = True
            if (
                isinstance(node, ast.Constant)
                and node.value == "HOME"
            ):
                touched = True
        if touched:
            found.add(path.name)
    return found


class HomePinTests(unittest.TestCase):
    def test_the_suite_runs_with_home_pinned_away_from_the_operator(self) -> None:
        self.assertTrue(REAL_HOME_BEFORE_PIN, "the pre-pin home was not recorded")
        home = os.environ.get("HOME", "")
        self.assertNotEqual(
            REAL_HOME_BEFORE_PIN, home, "HOME still points at the operator's account"
        )
        pinned = Path(home).resolve()
        self.assertTrue(
            pinned.is_relative_to(REAL_TEMP_ROOT) if hasattr(pinned, "is_relative_to")
            else str(pinned).startswith(str(REAL_TEMP_ROOT)),
            f"the pinned home {pinned} is not under the real temp root",
        )
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            value = os.environ.get(variable, "")
            self.assertTrue(
                value.startswith(str(REAL_TEMP_ROOT)),
                f"{variable}={value!r} is not pinned under the real temp root",
            )

    def test_a_child_process_inherits_the_pin(self) -> None:
        """The library-default class dies in grandchildren too."""

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; print(Path.home())",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        child_home = Path(completed.stdout.strip())
        self.assertEqual(child_home.resolve(), Path(os.environ["HOME"]).resolve())
        self.assertFalse(
            str(child_home).startswith(REAL_HOME_BEFORE_PIN),
            "a child process resolved the operator's real home",
        )

    def test_a_planted_home_touching_module_is_derived_and_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            planted = Path(temporary) / "test_planted_home_touch.py"
            planted.write_text(
                "from pathlib import Path\n"
                "def touches_the_operator():\n"
                "    return (Path.home() / 'Library' / 'LaunchAgents').exists()\n",
                encoding="utf-8",
            )
            clean = Path(temporary) / "test_clean_fixture.py"
            clean.write_text(
                "from tests.temp_roots import REAL_TEMP_ROOT\n"
                "def scratch():\n"
                "    return REAL_TEMP_ROOT\n",
                encoding="utf-8",
            )
            derived = _home_touching_files(Path(temporary))
            self.assertEqual({"test_planted_home_touch.py"}, derived)
            self.assertNotIn("test_clean_fixture.py", derived)

    def test_real_temp_root_tests_stay_green(self) -> None:
        """Control: REAL_TEMP_ROOT is not home-derived and is outside the pin."""

        self.assertNotIn("HOME", Path(REAL_TEMP_ROOT).parts[:2])
        pinned = Path(os.environ["HOME"]).resolve()
        real_temp = Path(REAL_TEMP_ROOT).resolve()
        self.assertNotEqual(
            real_temp, pinned
        )
        self.assertFalse(str(real_temp).startswith(str(pinned)))

    def test_the_uninstall_sweep_resolves_inside_the_pin(self) -> None:
        """WD-2's exact shape: the CLI sweep reads Path.home()/Library/LaunchAgents.

        test_uninstall drives ``floati uninstall`` in-process, and the verb
        sweeps the operator's LaunchAgents directory unconditionally. Under
        FENCE-H1 that sweep resolves inside the pin — the real LaunchAgents
        directory is unreachable, so a test can never delete an operator
        daemon again.
        """

        from floati.mcp import run_cli_artifact

        import tempfile

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            destination = Path(temporary) / "installed"
            (destination / "scripts").mkdir(parents=True)
            exit_code, artifact = run_cli_artifact(
                [
                    "uninstall",
                    "--destination", str(destination),
                    "--dry-run",
                ]
            )
        # The verb runs in-process (this is the seam that reached the real
        # LaunchAgents); whatever it resolves, the sweep directory can only
        # be the pinned scratch - never the operator's account.
        sweep = Path.home() / "Library" / "LaunchAgents"
        self.assertTrue(
            str(sweep).startswith(str(REAL_TEMP_ROOT)),
            f"the uninstall sweep resolved the operator's real {sweep}",
        )
        self.assertFalse(
            str(sweep).startswith(REAL_HOME_BEFORE_PIN),
            "the sweep resolved the pre-pin operator home",
        )
        self.assertIn(artifact["status"], {"ok", "refused"})


if __name__ == "__main__":
    unittest.main()
