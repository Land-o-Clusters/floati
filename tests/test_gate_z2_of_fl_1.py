"""GATE of the FL-1 row, at gated tip 0db44a2e - clean room.

The deciding questions, constructed by the gate from the relayed contract:

G1 (pre-fix tree): with Apple-git auto-maintenance forced on, the detached
    `git maintenance run --auto --detach` child that commit spawns is still
    ALIVE and working in the projection's .git AFTER subprocess.run
    returns - observed as a PROCESS with a timestamp after the return,
    never as an error message.
G2 (gated tip): the shipped knob shape (gc.auto=0, gc.autoDetach=false)
    makes the maintenance run synchronous, so no git child exists after the
    commit returns (the repack, when it happens, happened INSIDE the run)
    and the directory cleanup never raises - under a CPU hog.
G3 (control): the module's own bind/listen fence still refuses a listener.

FL-1b adds the no-knob population, forced by object count instead of
knobs (git's default gc.auto threshold is 6700; the fixture holds 8000):
on a bare HOME the detached child still survives the commit (witness),
and under the suite's pinned HOME - the one FL-1b lever every fixture
git child reads even when GIT_* is stripped - none does (the property).

Run this file from a checkout of the tree under test with that tree's root
on PYTHONPATH; the FL-1 property is observed per tree, so the gate runs the
file twice (pre-fix base, then gated tip) and compares the sightings.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

_LOOSE_OBJECTS = 1400
_LOOSE_OBJECTS_NO_KNOB = 8000  # git's default gc.auto threshold is 6700
_WATCH_SECONDS = 3.0
_WATCH_STEP = 0.02


def _git_environment(home: str | None = None) -> dict:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    if home is not None:
        environment["HOME"] = home
    environment["GIT_AUTHOR_NAME"] = "gate"
    environment["GIT_AUTHOR_EMAIL"] = "gate@example.invalid"
    environment["GIT_COMMITTER_NAME"] = "gate"
    environment["GIT_COMMITTER_EMAIL"] = "gate@example.invalid"
    return environment


def _projection_repository(
    base: Path, knobs: tuple[str, ...] = (), home: str | None = None
) -> Path:
    """One small real repository holding many loose objects: real work for
    auto-maintenance, the state the FL-1 receipt measured (1359 objects).
    EVERY repository-building git call carries the given knobs - the same
    shape the shipped helper uses, so the fixture never leaves a detached
    maintenance of its own behind the gate's back."""

    projection = base / "projection"
    projection.mkdir(parents=True)
    environment = _git_environment(home)

    def git(*arguments: str) -> None:
        subprocess.run(
            ["/usr/bin/git", *knobs, *arguments],
            cwd=projection, env=environment, check=True, capture_output=True,
        )

    git("init", "-q")
    for index in range(_LOOSE_OBJECTS):
        payload = f"gate object {index}\n".encode("utf-8")
        subprocess.run(
            ["/usr/bin/git", "hash-object", "-w", "--stdin"],
            input=payload, cwd=projection, env=environment,
            check=True, capture_output=True,
        )
    (projection / "seed.txt").write_text("seed\n")
    git("add", "seed.txt")
    git("commit", "-q", "-m", "gate seed")
    return projection


def _maintenance_children_under(prefix: Path) -> list[str]:
    """Live Apple-git maintenance/gc child processes.

    The detached child DAEMONIZES (git chdirs it to / on detach), so a cwd
    check can never match; the observation is the PROCESS ITSELF: a
    `git maintenance run --auto --detach` / `git gc --auto` /
    `git multi-pack-index` process seen during the watch window. The
    projection linkage comes from the experiment: this gate is the only
    committer on the box during its window.
    """

    listing = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True
    ).stdout
    hits: list[str] = []
    for line in listing.splitlines():
        command = line.strip()
        if "ps -axo" in command:
            continue
        lowered = command.lower()
        if (
            "git maintenance" not in lowered
            and "git gc" not in lowered
            and "git-multi-pack-index" not in lowered
        ):
            continue
        hits.append(command)
    return hits


def _run_commit_and_watch(
    projection: Path, knobs: tuple[str, ...], home: str | None = None
) -> tuple[float, list[tuple[float, list[str]]]]:
    """One real commit with a watcher thread sampling from BEFORE the run
    starts. Returns (return_monotonic, samples); each sample is
    (monotonic, hits-at-that-instant), so the caller can ask whether any
    child was alive AFTER the commit returned - the FL-1 property."""

    environment = _git_environment(home)
    samples: list[tuple[float, list[str]]] = []
    watching = threading.Event()

    def watch() -> None:
        watching.wait()
        deadline = time.monotonic() + _WATCH_SECONDS
        while time.monotonic() < deadline:
            samples.append(
                (time.monotonic(), _maintenance_children_under(projection))
            )
            time.sleep(_WATCH_STEP)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    watching.set()

    (projection / "trigger.txt").write_text("trigger\n")
    subprocess.run(
        ["/usr/bin/git", *knobs, "add", "trigger.txt"],
        cwd=projection, env=environment, check=True, capture_output=True,
    )
    completed = subprocess.run(
        ["/usr/bin/git", *knobs, "commit", "-q", "-m", "gate trigger"],
        cwd=projection, env=environment, check=True, capture_output=True,
    )
    returned_at = time.monotonic()
    watcher.join(timeout=_WATCH_SECONDS + 2.0)
    assert not completed.stderr, completed.stderr
    return returned_at, samples


FORCE_KNOBS = ("-c", "gc.auto=1", "-c", "gc.autoDetach=true")
SHIPPED_KNOBS = ("-c", "gc.auto=0", "-c", "gc.autoDetach=false")


class GateForcedMaintenanceLeavesADetachedChild(unittest.TestCase):
    def test_g1_pre_fix_commit_leaves_a_detached_git_child_after_return(
        self,
    ) -> None:
        """The mechanism is observed from a bare HOME on purpose: the
        detached child re-reads config from files (it inherits no -c
        knob), so the FL-1b gitconfig in the suite's pinned HOME makes
        an unprotected child exit before it can be seen. G1 witnesses
        the race itself, outside the remedy's scope."""

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bare_home = base / "bare-home"
            bare_home.mkdir()
            projection = _projection_repository(
                base, home=str(bare_home)
            )
            returned_at, samples = _run_commit_and_watch(
                projection, FORCE_KNOBS, home=str(bare_home)
            )
            after_return = [
                hit
                for moment, hits in samples
                if moment > returned_at
                for hit in hits
            ]
            self.assertGreater(
                len(after_return), 0,
                "no detached git child was alive after the commit returned; "
                f"the mechanism did not fire ({len(samples)} samples)",
            )


class GateNothingOutlivesTheWithBlock(unittest.TestCase):
    def test_g2_shipped_knobs_leave_no_child_after_return_under_a_hog(
        self,
    ) -> None:
        """On the GATED tree the helper's own knob shape holds under a CPU
        hog: any maintenance the commit triggers runs SYNCHRONOUSLY inside
        the run, no git child is alive after the commit returns, and the
        directory cleanup never raises."""

        hogs = [
            subprocess.Popen(["/usr/bin/yes"], stdout=subprocess.DEVNULL)
            for _ in range(os.cpu_count() or 4)
        ]
        try:
            with tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                projection = _projection_repository(base, SHIPPED_KNOBS)
                returned_at, samples = _run_commit_and_watch(
                    projection, SHIPPED_KNOBS
                )
                after_return = [
                    hit
                    for moment, hits in samples
                    if moment > returned_at
                    for hit in hits
                ]
                self.assertEqual(
                    [], after_return,
                    "git children outlived the commit: " + repr(after_return),
                )
        finally:
            for hog in hogs:
                hog.kill()
                hog.wait()

    def test_g3_control_the_fence_still_refuses_a_listener(self) -> None:
        """The FL-1 knobs must not weaken the module's own fence: the
        bind/listen enumeration still refuses every listener outside the
        ruled AF_UNIX supervisor."""

        import io

        from tests.test_no_listener_fence import WholeProductNoListenerFenceTests

        result = unittest.TextTestRunner(stream=io.StringIO()).run(
            WholeProductNoListenerFenceTests(
                "test_bind_and_listen_calls_exist_only_in_the_ruled_af_unix_supervisor"
            )
        )
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)


class GateNoKnobFixtureGit(unittest.TestCase):
    """FL-1b: the race must also be held for the ~35 fixture files that
    spawn bare `git add`/`git commit` with NO per-call knobs and an env
    that strips GIT_* but keeps HOME. Forced by population (8000 loose
    objects > git's default gc.auto threshold of 6700), so no knob,
    no repo config, and no env var other than HOME distinguishes the
    fixture from the racing shape."""

    def _no_knob_environment(self, home: str) -> dict:
        environment = _git_environment()
        environment["HOME"] = home
        return environment

    def _no_knob_projection(self, base: Path, home: str) -> Path:
        """One real repository holding more loose objects than git's
        default gc.auto threshold, built entirely with bare git calls:
        no -c knobs, no repo config, the given HOME the only lever."""

        projection = base / "projection"
        projection.mkdir(parents=True)
        environment = self._no_knob_environment(home)
        payload_dir = projection / "payload"
        payload_dir.mkdir()

        def git(*arguments: str) -> None:
            subprocess.run(
                ["/usr/bin/git", *arguments],
                cwd=projection, env=environment, check=True,
                capture_output=True,
            )

        git("init", "-q")
        for index in range(_LOOSE_OBJECTS_NO_KNOB):
            (payload_dir / f"object-{index}.txt").write_text(
                f"no-knob object {index}\n"
            )
        git("add", "-A")
        git("commit", "-q", "-m", "no-knob seed")
        return projection

    def _run_no_knob_commit_and_watch(
        self, projection: Path, home: str
    ) -> tuple[float, list[tuple[float, list[str]]]]:
        environment = self._no_knob_environment(home)
        samples: list[tuple[float, list[str]]] = []
        watching = threading.Event()

        def watch() -> None:
            watching.wait()
            deadline = time.monotonic() + _WATCH_SECONDS
            while time.monotonic() < deadline:
                samples.append(
                    (time.monotonic(), _maintenance_children_under(projection))
                )
                time.sleep(_WATCH_STEP)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        watching.set()

        (projection / "trigger.txt").write_text("trigger\n")
        subprocess.run(
            ["/usr/bin/git", "add", "trigger.txt"],
            cwd=projection, env=environment, check=True, capture_output=True,
        )
        completed = subprocess.run(
            ["/usr/bin/git", "commit", "-q", "-m", "no-knob trigger"],
            cwd=projection, env=environment, check=True, capture_output=True,
        )
        returned_at = time.monotonic()
        watcher.join(timeout=_WATCH_SECONDS + 2.0)
        assert not completed.stderr, completed.stderr
        return returned_at, samples

    def test_mechanism_without_knobs_on_a_bare_home_leaves_a_child(
        self,
    ) -> None:
        """The construction witness: with NO knobs, NO repo config and a
        bare HOME carrying no gitconfig, the commit's detached auto-
        maintenance child is alive after the commit returns. This is the
        race the ~35-file population ships, observed once here so the
        property below can never pass vacuously."""

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bare_home = base / "bare-home"
            bare_home.mkdir()
            projection = self._no_knob_projection(base, str(bare_home))
            returned_at, samples = self._run_no_knob_commit_and_watch(
                projection, str(bare_home)
            )
            after_return = [
                hit
                for moment, hits in samples
                if moment > returned_at
                for hit in hits
            ]
            self.assertGreater(
                len(after_return), 0,
                "no detached git child was alive after the no-knob commit "
                f"returned; the mechanism did not fire ({len(samples)} "
                "samples)",
            )

    def test_no_knob_fixture_under_the_suite_pin_leaves_no_child(
        self,
    ) -> None:
        """The FL-1b property: the same no-knob fixture run under the
        suite's pinned HOME leaves no git child alive after the commit
        returns - the suite-wide HOME gitconfig (FL-1b's one-site remedy)
        must reach every fixture git child, since a test that strips
        GIT_* from the env still carries HOME."""

        from tests import HOME_PIN_ROOT

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            projection = self._no_knob_projection(base, HOME_PIN_ROOT)
            returned_at, samples = self._run_no_knob_commit_and_watch(
                projection, HOME_PIN_ROOT
            )
            after_return = [
                hit
                for moment, hits in samples
                if moment > returned_at
                for hit in hits
            ]
            self.assertEqual(
                [], after_return,
                "git children outlived the no-knob commit under the "
                "suite's pinned HOME: " + repr(after_return),
            )


if __name__ == "__main__":
    unittest.main()
