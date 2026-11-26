"""GATE of the FL-1 row, at gated tip 0db44a2e - clean room.

The deciding questions, constructed by the gate from the relayed contract:

G1 (pre-fix mechanism): with auto-maintenance forced on, native Trace2
    testimony proves a detached maintenance/GC fork and completed repack
    in the exact fixture lineage. The detached fork finishes after the
    trigger commit returns. The proof survives missed process samples.
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

import hashlib
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts.parallel_processes import ProcessQuery

_LOOSE_OBJECTS = 1400
_LOOSE_OBJECTS_NO_KNOB = 8000  # git's default gc.auto threshold is 6700
_WATCH_SECONDS = 3.0
_WATCH_STEP = 0.02
_ATTRIBUTION_ENV = "FLOATI_FL1_ATTRIBUTION"


class _MaintenanceHits(list):
    def __init__(self):
        super().__init__()
        self.observations = []


def _sample_maintenance(projection, samples, intervals):
    started = time.monotonic()
    hits = _maintenance_children_under(projection)
    ended = time.monotonic()
    observations = getattr(hits, 'observations', [(started, ended, hit) for hit in hits])
    intervals.extend(observations)
    samples.extend((begin, [hit]) for begin, end, hit in observations)
    if not hits:
        # A snapshot begun before return cannot prove post-return absence,
        # even when the census finishes afterward.
        samples.append((started, []))


def _fixture_marker(projection: Path, create: bool = False) -> str:
    """One private random marker outside the projection's object/work tree."""
    path = projection.resolve().with_name(projection.name + ".fl1-attribution.marker")
    if create:
        try:
            with path.open("x") as stream:
                stream.write(uuid.uuid4().hex)
        except FileExistsError:
            pass
    marker = path.read_text()
    if len(marker) != 32 or any(c not in "0123456789abcdef" for c in marker):
        raise RuntimeError("invalid fixture attribution marker")
    return marker


def _loose_payload(index: int) -> bytes:
    return f"gate object {index}\n".encode("utf-8")


def _git_environment(home: str | None = None, projection: Path | None = None) -> dict:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != _ATTRIBUTION_ENV
    }
    if home is not None:
        environment["HOME"] = home
    environment["GIT_AUTHOR_NAME"] = "gate"
    environment["GIT_AUTHOR_EMAIL"] = "gate@example.invalid"
    environment["GIT_COMMITTER_NAME"] = "gate"
    environment["GIT_COMMITTER_EMAIL"] = "gate@example.invalid"
    if projection is not None:
        environment[_ATTRIBUTION_ENV] = _fixture_marker(projection, create=True)
    return environment


def _write_gate_objects(base: Path, projection: Path, environment: dict) -> None:
    """Write the _LOOSE_OBJECTS gate blobs into the projection's object
    store as LOOSE objects. Nothing else in the fixture depends on how;
    ProjectionFixtureIdentityTests pins the resulting object store.

    ONE `hash-object -w --stdin-paths` child writes them all (it was one
    child per object: 1,400 spawns per fixture). --no-filters hashes the
    bytes as-is, exactly as the per-object --stdin form did (no path, so
    no attribute filter). hash-object never runs auto-maintenance. The
    payload files live beside the projection, never in its work tree."""

    payloads = base / "gate-object-payloads"
    payloads.mkdir()
    paths = []
    for index in range(_LOOSE_OBJECTS):
        path = payloads / f"object-{index}"
        path.write_bytes(_loose_payload(index))
        paths.append(str(path))
    subprocess.run(
        ["/usr/bin/git", "hash-object", "-w", "--no-filters", "--stdin-paths"],
        input=("\n".join(paths) + "\n").encode("utf-8"),
        cwd=projection, env=environment, check=True, capture_output=True,
    )


def _projection_repository(
    base: Path, knobs: tuple[str, ...] = (), home: str | None = None,
    *, environment: dict | None = None,
) -> Path:
    """One small real repository holding many loose objects: real work for
    auto-maintenance, the state the FL-1 receipt measured (1359 objects).
    EVERY repository-building git call carries the given knobs - the same
    shape the shipped helper uses, so the fixture never leaves a detached
    maintenance of its own behind the gate's back."""

    projection = base / "projection"
    projection.mkdir(parents=True)
    if environment is None:
        environment = _git_environment(home, projection)

    def git(*arguments: str) -> None:
        subprocess.run(
            ["/usr/bin/git", *knobs, *arguments],
            cwd=projection, env=environment, check=True, capture_output=True,
        )

    git("init", "-q")
    _write_gate_objects(base, projection, environment)
    (projection / "seed.txt").write_text("seed\n")
    git("add", "seed.txt")
    git("commit", "-q", "-m", "gate seed")
    return projection


def _maintenance_children_under(prefix: Path) -> list[str]:
    """Matching maintenance bearing this experiment's exact scratch marker.

    Detach may reparent/chdir; the private environment marker survives both.
    Candidate PID/start identity is revalidated around the native marker
    lookup. Environment text is private and never enters samples/errors.
    A failed live observation refuses instead of proving an empty census.
    """
    marker = _ATTRIBUTION_ENV + "=" + _fixture_marker(prefix)
    deadline = time.monotonic() + 2.0
    listing = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True,
        timeout=2,
    )
    if listing.returncode or listing.stderr:
        raise RuntimeError("native maintenance census unavailable")
    query = ProcessQuery()
    hits = _MaintenanceHits()
    for line in listing.stdout.splitlines():
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
        pid_text, _, _ = command.partition(" ")
        if not pid_text.isdecimal():
            raise RuntimeError("invalid maintenance candidate PID")
        pid = int(pid_text)
        before = query.identity(pid)
        if before is None or before['zombie']:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("maintenance attribution census bound exhausted")
        observed = subprocess.run(
            ["/bin/ps", "eww", "-p", str(pid), "-o", "pid=,command="],
            capture_output=True, text=True, timeout=min(1.0, remaining),
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("maintenance attribution census bound exhausted")
        command_now = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "pid=,command="],
            capture_output=True, text=True, timeout=min(1.0, remaining),
        )
        observed_start = time.monotonic()
        after = query.identity(pid)
        observed_end = time.monotonic()
        if after is None or after['zombie'] or before['birth'] != after['birth']:
            continue
        if observed.returncode or observed.stderr or command_now.returncode or command_now.stderr:
            raise RuntimeError("live maintenance marker unavailable for PID " + str(pid))
        fields = observed.stdout.split()
        if not fields or fields[0] != str(pid):
            raise RuntimeError("live maintenance marker PID mismatch")
        fresh = command_now.stdout.strip()
        fresh_pid, _, fresh_command = fresh.partition(" ")
        if fresh_pid != str(pid):
            raise RuntimeError("live maintenance command PID mismatch")
        if marker in fields and any(term in fresh_command.lower() for term in
                                    ("git maintenance", "git gc", "git-multi-pack-index")):
            hits.append(fresh)
            hits.observations.append((observed_start, observed_end, fresh))
    return hits


def _run_commit_and_watch(
    projection: Path, knobs: tuple[str, ...], home: str | None = None
) -> tuple[float, list[tuple[float, list[str]]]]:
    """One real commit with a watcher thread sampling from BEFORE the run
    starts. Returns (return_monotonic, samples); each sample is
    (monotonic, hits-at-that-instant), so the caller can ask whether any
    child was alive AFTER the commit returned - the FL-1 property."""

    environment = _git_environment(home, projection)
    samples: list[tuple[float, list[str]]] = []
    intervals = []
    watching = threading.Event()
    stop = threading.Event()
    observation_errors: list[Exception] = []

    def watch() -> None:
        watching.wait()
        deadline = time.monotonic() + _WATCH_SECONDS
        try:
            while time.monotonic() < deadline and not stop.is_set():
                _sample_maintenance(projection, samples, intervals)
                stop.wait(_WATCH_STEP)
        except Exception as exc:
            observation_errors.append(exc)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    watching.set()

    try:
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
    except BaseException:
        stop.set()
        raise
    finally:
        watcher.join(timeout=_WATCH_SECONDS + 2.0)
        if watcher.is_alive():
            raise RuntimeError("maintenance watcher did not stop")
    if observation_errors:
        raise RuntimeError("maintenance watcher observation failed") from observation_errors[0]
    # A slow commit may outlast the unchanged three-second watch. Take one
    # explicit post-return census so a no-child result cannot be vacuous.
    if not any(moment > returned_at for moment, _ in samples):
        _sample_maintenance(projection, samples, intervals)
    if (any(begin <= returned_at < end for begin, end, hit in intervals)
            and not any(moment > returned_at and hits for moment, hits in samples)):
        raise RuntimeError("maintenance observation crossed commit return; attribution timing unknown")
    assert not completed.stderr, completed.stderr
    return returned_at, samples


FORCE_KNOBS = ("-c", "gc.auto=1", "-c", "gc.autoDetach=true")
SHIPPED_KNOBS = ("-c", "gc.auto=0", "-c", "gc.autoDetach=false")


class ProjectionFixtureIdentityTests(unittest.TestCase):
    """HARBOR-SPEED-R-D identity pin on the step that round changes: after
    _write_gate_objects, the object store holds EXACTLY the _LOOSE_OBJECTS
    gate blobs, every one LOOSE, no pack, and the work tree is untouched.
    That store is the whole input the seed commit (and so the
    auto-maintenance G1/G2 observe) receives from this step. Expected ids
    are derived here from the payload bytes with git's blob framing, not
    read back from git, so a writer that drops, alters, filters or packs
    an object reds this pin. The pin stops before the seed commit: what
    git does at that commit (a synchronous repack under the shipped knobs,
    a detached one from a bare HOME) is git's behaviour on this input and
    is what the gate classes observe. It runs no process census and spawns
    no maintenance, so it carries no serial reason."""

    def test_gate_objects_are_exactly_the_payload_blobs_all_loose(self) -> None:
        expected = {
            hashlib.sha1(
                b"blob %d\0" % len(_loose_payload(index)) + _loose_payload(index)
            ).hexdigest()
            for index in range(_LOOSE_OBJECTS)
        }
        self.assertEqual(_LOOSE_OBJECTS, len(expected))
        for shape in ("bare-home", "suite-home"):
            with self.subTest(shape=shape),\
                    tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                home = None
                if shape == "bare-home":
                    bare_home = base / "bare-home"
                    bare_home.mkdir()
                    home = str(bare_home)
                environment = _git_environment(home)
                projection = base / "projection"
                projection.mkdir()
                subprocess.run(
                    ["/usr/bin/git", "init", "-q"], cwd=projection,
                    env=environment, check=True, capture_output=True,
                )
                _write_gate_objects(base, projection, environment)
                objects = projection / ".git" / "objects"
                loose = {
                    fan.name + entry.name
                    for fan in objects.iterdir()
                    if len(fan.name) == 2 and fan.is_dir()
                    for entry in fan.iterdir()
                }
                self.assertEqual(expected, loose)
                self.assertEqual([], sorted(
                    entry.name for entry in (objects / "pack").iterdir()
                ))
                self.assertEqual([".git"], sorted(
                    entry.name for entry in projection.iterdir()
                ))


class GateForcedMaintenanceLeavesADetachedChild(unittest.TestCase):
    def test_g1_pre_fix_commit_leaves_a_detached_git_child_after_return(
        self,
    ) -> None:
        """Prove the bare-HOME detachment through durable native testimony.

        G1 is the positive mechanism control: instantaneous process polls
        can miss a completed fork. G2 retains its own strict observer for
        the shipped knobs' claim that nothing remains after return.
        """
        from tests.g1_trace_fixture import run_positive_fixture

        witness = run_positive_fixture()
        self.assertTrue(witness["completed_repack_sids"], witness)

    def test_g1_reserves_loose_objects_for_the_trigger(self) -> None:
        from tests.g1_trace_fixture import run_positive_fixture

        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "git"
            config.mkdir()
            (config / "config").write_text(
                "[maintenance]\nstrategy = gc\n"
                "[gc]\nauto = 1\nautoDetach = false\npruneExpire = now\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
                witness = run_positive_fixture()
        self.assertTrue(witness["completed_repack_sids"], witness)


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


class GateNoKnobFixtureGit(unittest.TestCase):
    """FL-1b: the race must also be held for the ~35 fixture files that
    spawn bare `git add`/`git commit` with NO per-call knobs and an env
    that strips GIT_* but keeps HOME. Forced by population (8000 loose
    objects > git's default gc.auto threshold of 6700), so no knob,
    no repo config, and no env var other than HOME distinguishes the
    fixture from the racing shape."""

    def _no_knob_environment(self, home: str, projection: Path | None = None) -> dict:
        environment = _git_environment(projection=projection)
        environment["HOME"] = home
        return environment

    def _no_knob_projection(self, base: Path, home: str) -> Path:
        """One real repository holding more loose objects than git's
        default gc.auto threshold, built entirely with bare git calls:
        no -c knobs, no repo config, the given HOME the only lever."""

        projection = base / "projection"
        projection.mkdir(parents=True)
        environment = self._no_knob_environment(home, projection)
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
        return _run_commit_and_watch(projection, (), home)

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
