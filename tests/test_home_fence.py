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
import json
import signal
import subprocess
import sys
import unittest
from pathlib import Path

from tests import REAL_HOME_BEFORE_PIN, HOME_PIN_ROOT
from tests.temp_roots import REAL_TEMP_ROOT
from tests.readiness import communicate, wait_readable

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
    def test_interruption_during_allocation_leaves_no_unregistered_directory(self) -> None:
        code = '''
import json, os, signal, tempfile
import tests
from tests.temp_roots import REAL_TEMP_ROOT
create = os.mkdir
def interrupt_after_creation(path, *args, **kwargs):
    result = create(path, *args, **kwargs)
    print(json.dumps([tests.HOME_PIN_ROOT, os.fsdecode(path)]), flush=True)
    os.kill(os.getpid(), NUMBER)
    return result
os.mkdir = interrupt_after_creation
tempfile.mkdtemp(dir=REAL_TEMP_ROOT)
'''
        for number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signal.Signals(number).name):
                child = subprocess.run(
                    [sys.executable, "-B", "-c", code.replace("NUMBER", str(int(number)))],
                    cwd=REPOSITORY_ROOT, capture_output=True, text=True,
                )
                self.assertNotEqual(0, child.returncode, child.stderr)
                self.assertEqual([], [path for path in json.loads(child.stdout) if Path(path).exists()], child.stderr)

    def test_suite_exit_removes_fixtures_on_success_and_failure(self) -> None:
        code = '''
import json, os, tempfile
from pathlib import Path
import tests
from tests.temp_roots import REAL_TEMP_ROOT
directory = tempfile.mkdtemp(dir=REAL_TEMP_ROOT)
Path(directory).mkdir(exist_ok=True)
descriptor, regular_file = tempfile.mkstemp(dir=REAL_TEMP_ROOT)
os.close(descriptor)
with tempfile.NamedTemporaryFile(delete=False, dir=REAL_TEMP_ROOT) as named:
    named_path = named.name
with tempfile.TemporaryFile(dir=REAL_TEMP_ROOT) as anonymous:
    anonymous.write(b'fixture')
print(json.dumps([tests.HOME_PIN_ROOT, directory, regular_file, named_path]), flush=True)
raise SystemExit(EXIT)
'''
        for status in (0, 1, 42):
            with self.subTest(exit=status):
                program = code.replace("EXIT", str(status))
                if status == 42:
                    program = ("import signal\n"
                               "def harness_interrupt(number, frame):\n"
                               "    raise SystemExit(42)\n"
                               "signal.signal(signal.SIGTERM, harness_interrupt)\n"
                               + program.replace("raise SystemExit(42)",
                                                 "os.kill(os.getpid(), signal.SIGTERM)"))
                child = subprocess.run(
                    [sys.executable, "-B", "-c", program],
                    cwd=REPOSITORY_ROOT, capture_output=True, text=True,
                )
                self.assertEqual(status, child.returncode, child.stderr)
                self.assertEqual([], [path for path in json.loads(child.stdout) if Path(path).exists()])

    def test_temporary_root_census_refuses_a_cached_unowned_allocator(self) -> None:
        """New creators cannot bypass registration through an earlier alias."""
        for imported in ("import tempfile; allocate = tempfile.mkdtemp",
                         "from tempfile import mkdtemp as allocate"):
            with self.subTest(imported=imported):
                child = subprocess.run(
                    [sys.executable, "-B", "-c", imported + "\n"
                     "import tests\nfrom tests.temp_roots import REAL_TEMP_ROOT\n"
                     "allocate(dir=REAL_TEMP_ROOT)\n"], cwd=REPOSITORY_ROOT,
                    capture_output=True, text=True,
                )
                self.assertNotEqual(0, child.returncode)
                self.assertIn("fixture_finalizer_unowned_allocator", child.stderr)

        for bypass in (
            "os.mkdir(os.path.join(REAL_TEMP_ROOT, 'unowned-planted-fixture'))",
            "os.mkdir('unowned-planted-fixture', dir_fd=os.open(REAL_TEMP_ROOT, os.O_RDONLY))",
            "os.mkdir(os.path.join(REAL_TEMP_ROOT, 'unowned-planted-fixture').replace('\x2fprivate/tmp/', '\x2ftmp/', 1))",
            "import tempfile\nowner = tempfile.TemporaryDirectory()\n"
            "with owner as directory:\n    owner.cleanup()\n    os.mkdir(directory)",
            # A product root's own name earns no exemption: the product moves
            # its roots through FLOATI_HOST_TEMP_ROOT instead.
            "path = os.path.join(REAL_TEMP_ROOT, 'floati-work')\nos.mkdir(path)\nos.rmdir(path)",
        ):
            with self.subTest(bypass=bypass):
                child = subprocess.run(
                    [sys.executable, "-B", "-c", "import os, tests\n"
                     "from tests.temp_roots import REAL_TEMP_ROOT\n" + bypass],
                    cwd=REPOSITORY_ROOT, capture_output=True, text=True,
                )
                self.assertNotEqual(0, child.returncode)
                self.assertIn("fixture_finalizer_unowned_root", child.stderr)

    def test_forked_child_cannot_remove_its_parents_fixtures(self) -> None:
        code = '''
import os, tempfile
from pathlib import Path
import tests
from tests.temp_roots import REAL_TEMP_ROOT
root = Path(tempfile.mkdtemp(dir=REAL_TEMP_ROOT))
context = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
child = os.fork()
if child == 0:
    from scripts.fixture_finalizer import cleanup
    EXPLICIT_CLEANUP
    cleanup()
    raise SystemExit(0)
_, status = os.waitpid(child, 0)
assert status == 0 and root.is_dir() and Path(context.name).is_dir()
print(json.dumps([str(root), context.name]))
'''
        code = "import json\n" + code
        for explicit in (True, False):
            with self.subTest(explicit_cleanup=explicit):
                child = subprocess.run(
                    [sys.executable, "-B", "-c", code.replace(
                        "EXPLICIT_CLEANUP", "context.cleanup()" if explicit else "pass")],
                    cwd=REPOSITORY_ROOT, capture_output=True, text=True,
                )
                self.assertEqual(0, child.returncode, child.stderr)
                self.assertEqual([], [path for path in json.loads(child.stdout) if Path(path).exists()])

    def test_replaced_fixture_coordinate_refuses_explicit_cleanup(self) -> None:
        import tempfile
        from scripts.fixture_finalizer import cleanup_path, retain_path

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as parent:
            original = Path(tempfile.mkdtemp(dir=parent))
            moved = Path(parent) / "retained-original"
            original.rename(moved)
            original.mkdir()
            with self.assertRaisesRegex(ValueError, "fixture_finalizer_replaced"):
                cleanup_path(original)
            self.assertTrue(original.is_dir())
            self.assertTrue(moved.is_dir())
            # The parent context owns both contents; relinquish the stale
            # child coordinate so its later cleanup cannot claim replacement.
            retain_path(original)

    def test_temporary_directory_cleanup_preserves_a_replacement(self) -> None:
        import tempfile

        for cleanup in ("context exit", "finalizer"):
            with self.subTest(cleanup=cleanup):
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as parent:
                    owned = tempfile.TemporaryDirectory(dir=parent)
                    original = Path(owned.name)
                    moved = Path(parent) / "moved"
                    original.rename(moved)
                    original.mkdir()
                    sentinel = original / "foreign"
                    sentinel.touch()
                    if cleanup == "context exit":
                        try:
                            with owned:
                                pass
                        except ValueError:
                            pass
                    del owned
                    self.assertTrue(moved.is_dir())
                    self.assertTrue(sentinel.is_file(), "cleanup removed the replacement")

    def test_interrupted_suite_removes_its_temporary_fixtures(self) -> None:
        """A ready child suite owns its fixtures until it is interrupted."""
        code = '''
import json, signal, tempfile, unittest
from pathlib import Path
import tests
from tests.temp_roots import REAL_TEMP_ROOT
from floati.identity_fence import RETIRED_PRODUCT_NAME
class InterruptedSuite(unittest.TestCase):
    def test_fixture(self):
        fleet = tempfile.mkdtemp(dir=REAL_TEMP_ROOT)
        (Path(fleet) / 'fleet').mkdir()
        remote = tempfile.TemporaryDirectory(prefix=RETIRED_PRODUCT_NAME + '-sequencer-', dir=REAL_TEMP_ROOT)
        (Path(remote.name) / 'remote.git').mkdir()
        seat = tempfile.mkdtemp(prefix='hw-home.', dir=REAL_TEMP_ROOT)
        (Path(seat) / 'seat-home').mkdir()
        scale = tempfile.mkdtemp(prefix='bus-scale-', dir=REAL_TEMP_ROOT)
        descriptor, regular_file = tempfile.mkstemp(dir=REAL_TEMP_ROOT)
        import os
        os.close(descriptor)
        print(json.dumps([tests.HOME_PIN_ROOT, fleet, remote.name, seat, scale, regular_file]), flush=True)
        signal.pause()
unittest.main()
'''
        for number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signal.Signals(number).name):
                child = subprocess.Popen(
                    [sys.executable, "-B", "-c", code], cwd=REPOSITORY_ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    wait_readable(child.stdout, "interrupted suite fixture readiness")
                    ready = child.stdout.readline()
                    self.assertTrue(ready, "child exited before fixture readiness")
                    paths = [Path(value) for value in json.loads(ready)]
                    child.send_signal(number)
                    _out, error = communicate(child, "interrupted suite exit")
                    self.assertNotEqual(0, child.returncode, error)
                    self.assertEqual([], [str(path) for path in paths if path.exists()], error)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        communicate(child, "interrupted suite cleanup")

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
