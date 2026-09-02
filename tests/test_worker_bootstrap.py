"""Fresh-exec and post-isolation runtime tests for the Worker bootstrap."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import inspect
import multiprocessing.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from floati.worker_bootstrap import bootstrap_main
from floati.worker_bootstrap import close_all_descriptors_except
from floati.worker_bootstrap import _construct_adapter
from floati.worker_bootstrap import _trusted_imports
from floati.worker_errors import WorkerAdapterFailure
from floati.worker_adapter_runtime import run_adapter_session
from floati.worker_bootstrap_protocol import (
    BootstrapChannel,
    BuiltInAdapterSpec,
    isolation_policy_to_payload,
    validate_isolation_backend,
)
from floati.worker_isolation import (
    WorkerIsolationPolicy,
    cleanup_worker_isolation,
    prepare_worker_isolation,
)
from tests.temp_roots import REAL_TEMP_ROOT


# The backend a Worker reports is chosen BY PLATFORM inside the product
# (floati/worker_isolation.py:674): darwin applies the macOS sandbox and names
# it "macos-sandbox" (:560), Linux applies Landlock and names it
# f"linux-landlock-v{abi}" (:644) where the ABI is whatever the RUNNING KERNEL
# reports — v7 on the CI runner today and some other integer on its next
# kernel. A fixture that accepts one host's answer as a literal rejects the
# other host, and would reject this one's own next kernel too.
#
# ⇒ AN ALLOW-LIST THAT NAMES ONE HOST'S ANSWER IS A HOST FACT, NOT A CONTRACT.
#
# So the vocabulary is not invented here: `validate_isolation_backend` in
# floati/worker_bootstrap_protocol.py is the product's own allow-list and every
# reported backend is put through it. Only the platform→family choice is
# mirrored, from the same branch the product applies. A platform the product
# has no backend for is None: there the ONLY lawful first frame is the typed
# failure, which is what apply_worker_isolation's ENOTSUP produces.
HOST_ISOLATION_BACKEND_PREFIX = (
    "macos-sandbox"
    if sys.platform == "darwin"
    else "linux-landlock-v"
    if sys.platform.startswith("linux")
    else None
)

BOOTSTRAP = (Path(__file__).parents[1] / "floati" / "worker_bootstrap.py").resolve()
PYTHON = str(Path(sys.executable).resolve())
SESSION_ID = "worker-018f7e9b3c117abc8def0123456789ab"

# CI-GREEN-22 (family F9). The descriptor probe below used to assert that a
# bare fd NUMBER chosen by the parent's own allocator was no longer `fstat`-able
# inside the child. That number is not the test's to choose, and the launcher
# re-binds a fixed low range of it: `spawn_effect_worker` dup2s the bootstrap
# channel onto `bootstrap_main`'s descriptor and the four prelude sources onto
# the numbers `_PRELUDE_SOURCES` declares. When the parent's next free
# descriptor landed inside that range the child's `fstat` succeeded against the
# CHANNEL, the probe called it a surviving tenant descriptor, and the bootstrap
# exited 1 — measured on ubuntu-latest, run 33593224194: the parent planted the
# probe on 3 and the child's 3 was a socket with a different (device, inode)
# while its whole open set was exactly the ruled {0,1,2,3}.
#
# ⇒ A PROBE ON A DESCRIPTOR NUMBER MEASURES THE HOST'S ALLOCATOR, NOT THE CODE.
#   The same commit was RED on ubuntu-latest and GREEN on macos-latest.
#
# So the probe is planted CLEAR of the re-bound numbers and judged by the
# object's identity, and the child reports its whole open set so the closure
# claim is never gated behind the aliasing one.
_DESCRIPTOR_SCAN_END = 64


def _descriptor_identities(end: int = _DESCRIPTOR_SCAN_END) -> str:
    """Render this process's open descriptors as `number:dev:inode` tokens.

    A fixed low range rather than the product's `/proc/self/fd` ÷ `/dev/fd`
    enumeration on purpose: this is a diagnostic that must never under-report,
    and darwin's `/dev/fd` is allowed to omit entries — the product carries its
    own test for exactly that omission. A range scan can only be short at the
    top, never wrong below its end, so what it cannot see is stated instead of
    hidden: a descriptor at or above `end`.
    """

    rows = []
    for number in range(end):
        try:
            status = os.fstat(number)
        except OSError:
            continue
        rows.append("%d:%d:%d" % (number, status.st_dev, status.st_ino))
    return " ".join(rows)


def _launcher_rebound_descriptors() -> frozenset:
    """The descriptor numbers the launcher re-binds inside the bootstrap child.

    Derived from the product's own declarations rather than written down: the
    channel is `bootstrap_main`'s own default parameter and the prelude targets
    are the third field of every `_PRELUDE_SOURCES` record. A probe planted on
    one of these is overwritten by `posix_spawn`'s dup2 before the closure loop
    runs, so it can witness nothing; a literal copy of the set here would go
    stale silently the day the launcher gains a fifth prelude source.
    """

    from floati.worker_exec import _PRELUDE_SOURCES

    channel = inspect.signature(bootstrap_main).parameters["descriptor"].default
    return frozenset({channel, *(record[2] for record in _PRELUDE_SOURCES)})


def _parse_child_descriptor_report(text: str) -> dict:
    """Read the `key=value` report the stub isolation writes inside the child."""

    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    return fields


# The stub `apply_worker_isolation` body used by every descriptor probe below.
# It classifies the probed number into exactly three typed verdicts and writes
# both of them out before acting, so a failure names what the child saw:
#   closed   — EBADF: the closure loop closed the descriptor. The claim.
#   rebound  — live, but a DIFFERENT object: the number was re-bound by the
#              launcher and this run witnessed nothing. Not a pass.
#   survived — live, and the parent's own object: a real inherited leak.
_CHILD_DESCRIPTOR_PROBE = (
    "import os\n"
    "leak = int(os.environ['FLOATI_TEST_LEAK_FD'])\n"
    "expected = os.environ['FLOATI_TEST_LEAK_IDENTITY']\n"
    "scan_end = max(leak, 63) + 1\n"
    "rows = []\n"
    "for number in range(scan_end):\n"
    "    try:\n"
    "        status = os.fstat(number)\n"
    "    except OSError:\n"
    "        continue\n"
    "    rows.append('%d:%d:%d' % (number, status.st_dev, status.st_ino))\n"
    "try:\n"
    "    leak_status = os.fstat(leak)\n"
    "except OSError as error:\n"
    "    verdict, observed = 'closed', 'errno=%d' % (error.errno,)\n"
    "else:\n"
    "    observed = '%d:%d' % (leak_status.st_dev, leak_status.st_ino)\n"
    "    verdict = 'survived' if observed == expected else 'rebound'\n"
    "report = (\n"
    "    'verdict=%s' % verdict,\n"
    "    'leak_fd=%d' % leak,\n"
    "    'leak_expected=%s' % expected,\n"
    "    'leak_observed=%s' % observed,\n"
    "    'scan_end=%d' % scan_end,\n"
    "    'open_fds=%s' % ' '.join(rows),\n"
    ")\n"
    "Path(os.environ['FLOATI_TEST_CHILD_REPORT']).write_text(\n"
    "    '\\n'.join(report) + '\\n', encoding='utf-8')\n"
    "Path(os.environ['FLOATI_BOOTSTRAP_TRACE']).write_text(\n"
    "    verdict + '\\n', encoding='utf-8')\n"
    "if verdict == 'survived':\n"
    "    raise RuntimeError('descriptor survived')\n"
    "return 'macos-sandbox'"
)


class _AfterForkSentinel:
    pass


class _RecordingChannel:
    def __init__(self, incoming: tuple[tuple[str, object], ...] = ()) -> None:
        self.sent: list[tuple[str, object]] = []
        self.incoming = list(incoming)
        self.closed = False

    def send(self, frame: tuple[str, object]) -> None:
        self.sent.append(frame)

    def poll(self, _timeout: float) -> bool:
        return bool(self.incoming)

    def recv(self) -> tuple[str, object]:
        return self.incoming.pop(0)

    def close(self) -> None:
        self.closed = True


class WorkerBootstrapTests(unittest.TestCase):
    maxDiff = None

    def test_bootstrap_requires_exact_preloaded_trusted_modules(self) -> None:
        """Catches bootstrap falling back to ordinary pre-isolation imports."""
        with mock.patch.dict(_trusted_imports.__globals__, {}, clear=False):
            _trusted_imports.__globals__.pop("_FLOATI_PRELOADED_MODULES", None)
            with self.assertRaises(WorkerAdapterFailure):
                _trusted_imports()

    def test_bootstrap_rejects_metadata_identical_substitute_prelude_module(self) -> None:
        """Catches metadata equality replacing loader-proven object identity."""
        import floati
        import floati.worker_bootstrap_protocol as protocol
        import floati.worker_errors as errors
        import floati.worker_isolation as isolation

        preloaded = (floati, errors, isolation, protocol)
        with mock.patch.dict(
            _trusted_imports.__globals__,
            {"_FLOATI_PRELOADED_MODULES": preloaded},
            clear=False,
        ):
            self.assertEqual(6, len(_trusted_imports()))

        impostor = types.ModuleType(isolation.__name__)
        impostor.__dict__.update(isolation.__dict__)
        installed = {
            "floati": floati,
            "floati.worker_errors": errors,
            "floati.worker_isolation": impostor,
            "floati.worker_bootstrap_protocol": protocol,
        }
        with mock.patch.dict(sys.modules, installed, clear=False), mock.patch.dict(
            _trusted_imports.__globals__,
            {"_FLOATI_PRELOADED_MODULES": preloaded},
            clear=False,
        ):
            with self.assertRaises(WorkerAdapterFailure):
                _trusted_imports()

    def _policy(self, root: Path, *, workspace: bool = True) -> WorkerIsolationPolicy:
        tenant = root / "tenant"
        effects = tenant / "effects"
        scratch = root / "scratch"
        work = root / "workspace" if workspace else None
        effects.mkdir(parents=True)
        scratch.mkdir()
        if work is not None:
            work.mkdir()
        probe = effects / "probe"
        probe.touch()
        scratch_stat = scratch.stat()
        probe_stat = probe.stat()
        work_stat = None if work is None else work.stat()
        return WorkerIsolationPolicy(
            tenant_home=tenant.resolve(),
            workspace=None if work is None else work.resolve(),
            scratch=scratch.resolve(),
            write_probe=probe.resolve(),
            workspace_identity=(
                None if work_stat is None
                else (work_stat.st_dev, work_stat.st_ino)
            ),
            scratch_identity=(scratch_stat.st_dev, scratch_stat.st_ino),
            probe_identity=(probe_stat.st_dev, probe_stat.st_ino),
        )

    def _launch_payload(
        self,
        policy: WorkerIsolationPolicy,
        *,
        kind: str = "codex",
        command: tuple[str, ...] = ("/bin/echo",),
        item: object = None,
        spawn_context: object = None,
        effect_context: object = None,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "session_id": SESSION_ID,
            "adapter": {"kind": kind, "command": list(command)},
            "item": {"id": "work-a"} if item is None else item,
            "deadline_millis": 2_000,
            "spawn_context": spawn_context,
            "effect_context": effect_context,
            "isolation_policy": isolation_policy_to_payload(policy),
        }

    def _spawn(
        self,
        bootstrap: Path,
        launch_payload: dict[str, object],
        *,
        environment: Optional[dict[str, str]] = None,
        inherited_descriptors: tuple[int, ...] = (),
    ) -> tuple[int, BootstrapChannel]:
        from floati.worker_exec import spawn_effect_worker

        for descriptor in inherited_descriptors:
            os.set_inheritable(descriptor, True)
        child_environment = dict(os.environ if environment is None else environment)
        with mock.patch.dict(os.environ, child_environment, clear=True):
            process, channel = spawn_effect_worker(bootstrap, launch_payload)
        return process.pid, channel

    def _wait(self, pid: int) -> int:
        waited, status = os.waitpid(pid, 0)
        self.assertEqual(pid, waited)
        return os.waitstatus_to_exitcode(status)

    def _receive_until_eof(
        self, channel: BootstrapChannel, *, timeout: float = 3.0,
    ) -> list[tuple[str, object]]:
        frames = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not channel.poll(0.05):
                continue
            try:
                frames.append(channel.recv())
            except Exception:
                break
        return frames

    def _instrumented_package(
        self,
        root: Path,
        *,
        isolation_source: str,
        adapter_source: str,
    ) -> Path:
        package = root / "floati"
        adapters = package / "adapters"
        adapters.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (adapters / "__init__.py").write_text("", encoding="utf-8")
        source_package = Path(__file__).parents[1] / "floati"
        for name in (
            "worker_errors.py",
            "worker_bootstrap_protocol.py",
            "worker_adapter_runtime.py",
            "worker_bootstrap.py",
        ):
            shutil.copyfile(source_package / name, package / name)
        (package / "worker_isolation.py").write_text(
            isolation_source, encoding="utf-8"
        )
        for name in ("codex_live.py", "claude.py", "pi.py"):
            (adapters / name).write_text(adapter_source, encoding="utf-8")
        return (package / "worker_bootstrap.py").resolve()

    @staticmethod
    def _stub_isolation(body: str) -> str:
        return (
            "from dataclasses import dataclass\n"
            "from pathlib import Path\n"
            "from typing import Optional\n"
            "@dataclass(frozen=True)\n"
            "class WorkerIsolationPolicy:\n"
            "    tenant_home: Path\n"
            "    workspace: Optional[Path]\n"
            "    scratch: Path\n"
            "    write_probe: Path\n"
            "    workspace_identity: Optional[tuple]\n"
            "    scratch_identity: tuple\n"
            "    probe_identity: tuple\n"
            "def apply_worker_isolation(policy):\n"
            + "\n".join("    " + line for line in body.splitlines())
            + "\n"
        )

    @staticmethod
    def _stub_adapter(body: str = "") -> str:
        common = (
            "import os\n"
            "from pathlib import Path\n"
            "TRACE = Path(os.environ['FLOATI_BOOTSTRAP_TRACE'])\n"
            "with TRACE.open('a', encoding='utf-8') as stream:\n"
            "    stream.write('import\\n')\n"
            "class Adapter:\n"
            "    requires_workspace = False\n"
            "    def __init__(self, command, *, isolate_process_group=True):\n"
            "        self.command = tuple(command)\n"
            "        with TRACE.open('a', encoding='utf-8') as stream:\n"
            "            stream.write('construct:' + '|'.join(command) + '\\n')\n"
            "    def set_process_group_registrar(self, registrar):\n"
            "        self.registrar = registrar\n"
            "    def spawn(self, item, *, deadline_seconds):\n"
            "        with TRACE.open('a', encoding='utf-8') as stream:\n"
            "            stream.write('spawn\\n')\n"
            + body
            + "        return object()\n"
            "    def drive(self, handle, item, *, deadline_seconds):\n"
            "        with TRACE.open('a', encoding='utf-8') as stream:\n"
            "            stream.write('drive\\n')\n"
            "        return []\n"
            "    def cancel(self):\n"
            "        with TRACE.open('a', encoding='utf-8') as stream:\n"
            "            stream.write('cancel\\n')\n"
        )
        return common + (
            "CodexAppServerAdapter = Adapter\n"
            "ClaudeHeadlessAdapter = Adapter\n"
            "PiRpcAdapter = Adapter\n"
        )

    def test_fresh_exec_runs_no_multiprocessing_or_os_atfork_callback_before_isolation(self) -> None:
        """Catches regression from fresh exec to any fork-based child launch."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            multiprocessing_proof = root / "multiprocessing-fork-hook-ran"
            os_proof = root / "os-fork-hook-ran"

            def mark_from_multiprocessing(_sentinel: object) -> None:
                if multiprocessing_proof.parent.exists():
                    multiprocessing_proof.write_text(
                        "multiprocessing", encoding="utf-8"
                    )

            def mark_from_os() -> None:
                if os_proof.parent.exists():
                    os_proof.write_text("os", encoding="utf-8")

            sentinel = _AfterForkSentinel()
            multiprocessing.util.register_after_fork(
                sentinel, mark_from_multiprocessing
            )
            os.register_at_fork(after_in_child=mark_from_os)
            positive = multiprocessing.get_context("fork").Process(target=time.monotonic)
            positive.start()
            positive.join(2.0)
            self.assertEqual(0, positive.exitcode)
            self.assertTrue(multiprocessing_proof.is_file())
            self.assertTrue(os_proof.is_file())
            multiprocessing_proof.unlink()
            os_proof.unlink()

            tenant = root / "tenant"
            (tenant / "effects").mkdir(parents=True)
            workspace = root / "workspace"
            policy = prepare_worker_isolation(tenant, workspace, SESSION_ID)
            self.addCleanup(cleanup_worker_isolation, policy)
            pid, channel = self._spawn(BOOTSTRAP, self._launch_payload(policy))
            self.addCleanup(channel.close)

            frames = self._receive_until_eof(channel, timeout=10.0)
            exit_code = self._wait(pid)

            self.assertFalse(multiprocessing_proof.exists())
            self.assertFalse(os_proof.exists())
            self.assertEqual(1, len(frames[:1]), frames[:1])
            kind, payload = frames[0]
            if kind == "failure":
                self.assertEqual("effect_worker_isolation_unavailable", payload)
                self.assertNotEqual(0, exit_code)
            else:
                self.assertEqual("isolation_ready", kind)
                self.assertEqual({"backend"}, set(payload))
                backend = payload["backend"]
                # The product's own allow-list decides whether the NAME is
                # lawful; this host's platform decides which family it must be
                # in. Neither is spelled out per-kernel here.
                self.assertEqual(backend, validate_isolation_backend(backend))
                self.assertIsNotNone(
                    HOST_ISOLATION_BACKEND_PREFIX,
                    "the product has no isolation backend for this platform, so "
                    "the only lawful first frame is the typed failure",
                )
                self.assertTrue(
                    backend.startswith(HOST_ISOLATION_BACKEND_PREFIX), backend
                )

    def test_fresh_exec_ignores_pythonpath_sitecustomize_usercustomize_and_pth(self) -> None:
        """Catches Python startup hooks executing before the isolation boundary."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            hooks = root / "hooks"
            hooks.mkdir()
            proof = root / "startup-hook-ran"
            payload = "from pathlib import Path; Path(%r).write_text('ran')\n" % str(proof)
            (hooks / "sitecustomize.py").write_text(payload, encoding="utf-8")
            (hooks / "usercustomize.py").write_text(payload, encoding="utf-8")
            (hooks / "hostile.pth").write_text(payload, encoding="utf-8")
            policy = self._policy(root / "policy")
            trace = root / "trace"
            bootstrap = self._instrumented_package(
                root / "instrumented",
                isolation_source=self._stub_isolation(
                    "from pathlib import Path\n"
                    "Path(__import__('os').environ['FLOATI_BOOTSTRAP_TRACE']).write_text('isolated\\n', encoding='utf-8')\n"
                    "return 'macos-sandbox'"
                ),
                adapter_source=self._stub_adapter(),
            )
            environment = dict(os.environ)
            environment.update({
                "PYTHONPATH": str(hooks),
                "PYTHONUSERBASE": str(root / "userbase"),
                "FLOATI_BOOTSTRAP_TRACE": str(trace),
            })
            pid, channel = self._spawn(
                bootstrap, self._launch_payload(policy), environment=environment,
            )
            self.addCleanup(channel.close)
            frames = self._receive_until_eof(channel)

            self.assertEqual(0, self._wait(pid))
            self.assertFalse(proof.exists())
            self.assertEqual("isolation_ready", frames[0][0])

    @staticmethod
    def _descriptor_identity(descriptor: int) -> str:
        """Name the OBJECT a descriptor currently points at, never the number."""

        status = os.fstat(descriptor)
        return "%d:%d" % (status.st_dev, status.st_ino)

    def _run_descriptor_probe(
        self,
        root: Path,
        *,
        inherited: tuple,
        probe_descriptor: int,
        probe_identity: str,
    ) -> dict:
        """Spawn one bootstrap child that classifies `probe_descriptor` for us."""

        policy = self._policy(root / "policy")
        trace = root / "trace"
        child_report = root / "child-descriptor-report"
        bootstrap = self._instrumented_package(
            root / "instrumented",
            isolation_source=self._stub_isolation(_CHILD_DESCRIPTOR_PROBE),
            adapter_source=self._stub_adapter(),
        )
        environment = dict(os.environ)
        environment.update({
            "FLOATI_TEST_LEAK_FD": str(probe_descriptor),
            "FLOATI_TEST_LEAK_IDENTITY": probe_identity,
            "FLOATI_BOOTSTRAP_TRACE": str(trace),
            "FLOATI_TEST_CHILD_REPORT": str(child_report),
        })
        parent_open_fds = _descriptor_identities()
        pid, channel = self._spawn(
            bootstrap, self._launch_payload(policy), environment=environment,
            inherited_descriptors=inherited,
        )
        self.addCleanup(channel.close)
        frames = self._receive_until_eof(channel)
        exit_code = self._wait(pid)
        fields = (
            _parse_child_descriptor_report(child_report.read_text(encoding="utf-8"))
            if child_report.exists()
            else {}
        )
        return {
            "probe_descriptor": probe_descriptor,
            "probe_identity": probe_identity,
            "parent_open_fds": parent_open_fds,
            "fields": fields,
            "open_fds": sorted(
                int(token.split(":")[0])
                for token in fields.get("open_fds", "").split()
            ),
            "trace": trace.read_text(encoding="utf-8") if trace.exists() else None,
            "frames": frames,
            "exit_code": exit_code,
        }

    @staticmethod
    def _probe_report(observation: dict) -> str:
        """Carry both descriptor tables into the failure message that needs them."""

        return (
            "\nprobe fd=%(probe_descriptor)d identity=%(probe_identity)s"
            "\nparent open_fds=[%(parent_open_fds)s]"
            "\nchild %(fields)r"
            "\nchild open_fds=%(open_fds)r"
            "\ntrace=%(trace)r"
            "\nframes=%(frames)r"
            "\nexit_code=%(exit_code)r" % observation
        )

    def test_bootstrap_closes_every_unruled_inherited_descriptor_before_apply(self) -> None:
        """Catches inherited tenant descriptors surviving until policy activation."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            leaked_path = root / "inherited"
            leaked_path.touch()
            rebound = _launcher_rebound_descriptors()
            opened = os.open(leaked_path, os.O_RDONLY)
            try:
                # Plant the probe CLEAR of every number the launcher re-binds,
                # so what the child reports is the closure loop's work and not
                # the parent allocator's luck. F_DUPFD takes the lowest free
                # descriptor at or above the floor, so an occupied number is
                # stepped over rather than clobbered.
                leaked = fcntl.fcntl(opened, fcntl.F_DUPFD, max(rebound) + 1)
            finally:
                os.close(opened)
            self.addCleanup(os.close, leaked)
            self.assertNotIn(leaked, rebound)

            observation = self._run_descriptor_probe(
                root,
                inherited=(leaked,),
                probe_descriptor=leaked,
                probe_identity=self._descriptor_identity(leaked),
            )
            report = self._probe_report(observation)

            self.assertEqual(0, observation["exit_code"], report)
            # The closure claim FIRST, so it is never gated behind the aliasing
            # one: the child's whole open set is the ruled set and nothing else.
            self.assertEqual([0, 1, 2, 3], observation["open_fds"], report)
            self.assertEqual("closed", observation["fields"].get("verdict"), report)
            self.assertEqual(
                errno.EBADF,
                int(observation["fields"]["leak_observed"].split("=")[1]),
                report,
            )
            self.assertEqual("closed", observation["trace"].splitlines()[0], report)
            self.assertEqual("isolation_ready", observation["frames"][0][0], report)

    def test_descriptor_probe_calls_a_relaunched_number_rebound_and_not_closed(
        self,
    ) -> None:
        """Controls the probe: the object decides the verdict, never the number."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            leaked_path = root / "inherited"
            leaked_path.touch()
            leaked = os.open(leaked_path, os.O_RDONLY)
            self.addCleanup(os.close, leaked)
            channel_descriptor = (
                inspect.signature(bootstrap_main).parameters["descriptor"].default
            )
            self.assertIn(channel_descriptor, _launcher_rebound_descriptors())

            # The CI condition, staged without touching this process's own
            # descriptor table: aim the probe at a number the launcher re-binds
            # while the parent's object lives somewhere else. Before CI-GREEN-22
            # this exact state exited 1 on ubuntu-latest and passed on
            # macos-latest at one commit; now it is neither pass nor leak but a
            # third, named outcome.
            observation = self._run_descriptor_probe(
                root,
                inherited=(leaked,),
                probe_descriptor=channel_descriptor,
                probe_identity=self._descriptor_identity(leaked),
            )
            report = self._probe_report(observation)

            self.assertEqual(0, observation["exit_code"], report)
            self.assertEqual("rebound", observation["fields"].get("verdict"), report)
            self.assertNotEqual(
                observation["fields"]["leak_expected"],
                observation["fields"]["leak_observed"],
                report,
            )
            self.assertEqual([0, 1, 2, 3], observation["open_fds"], report)
            self.assertEqual("isolation_ready", observation["frames"][0][0], report)

    def test_descriptor_probe_still_fails_when_a_descriptor_genuinely_survives(
        self,
    ) -> None:
        """Controls the probe the other way: a real survivor must still exit 1."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            # Standard error is the one descriptor the bootstrap child both
            # keeps by design AND inherits unchanged, so it is the only way to
            # stage a genuine survival without breaking the product. If the
            # comparison ever degraded from the object back to "the number is
            # live", this case would go green and the probe would be blind to a
            # real inherited leak.
            observation = self._run_descriptor_probe(
                root,
                inherited=(),
                probe_descriptor=2,
                probe_identity=self._descriptor_identity(2),
            )
            report = self._probe_report(observation)

            self.assertEqual("survived", observation["fields"].get("verdict"), report)
            self.assertEqual(
                observation["fields"]["leak_expected"],
                observation["fields"]["leak_observed"],
                report,
            )
            # The exit path CI-GREEN-22 had to determine, now pinned by a test:
            # the RuntimeError is raised inside apply_worker_isolation, i.e.
            # before `ready`, so the frame is the typed isolation failure and
            # the status is 1 rather than a SystemExit code.
            self.assertEqual(1, observation["exit_code"], report)
            self.assertEqual(
                [("failure", "effect_worker_isolation_unavailable")],
                observation["frames"],
                report,
            )

    def test_descriptor_closure_ignores_resource_limit_and_verifies_real_open_set(
        self,
    ) -> None:
        """Catches an inherited descriptor surviving above a sampled RLIMIT ceiling."""
        read_descriptor, write_descriptor = os.pipe()
        try:
            with mock.patch(
                "floati.worker_bootstrap.os.listdir",
                return_value=[str(read_descriptor), str(write_descriptor)],
            ), mock.patch(
                "floati.worker_bootstrap.os.closerange",
            ):
                close_all_descriptors_except({0, 1, 2, read_descriptor})
            with self.assertRaises(OSError) as caught:
                os.fstat(write_descriptor)
            self.assertEqual(errno.EBADF, caught.exception.errno)
        finally:
            for descriptor in (read_descriptor, write_descriptor):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @unittest.skipUnless(sys.platform == "darwin", "macOS descriptor surface")
    def test_descriptor_closure_does_not_trust_incomplete_dev_fd(self) -> None:
        """Catches Darwin /dev/fd omissions leaving inherited descriptors open."""
        leaked_read, leaked_write = os.pipe()
        proof_read, proof_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(proof_read)
            try:
                with mock.patch(
                    "floati.worker_bootstrap.os.listdir",
                    return_value=["0", "1", "2", str(proof_write)],
                ):
                    close_all_descriptors_except({0, 1, 2, proof_write})
                try:
                    os.fstat(leaked_read)
                except OSError as exc:
                    outcome = b"closed" if exc.errno == errno.EBADF else b"error"
                else:
                    outcome = b"open"
                os.write(proof_write, outcome)
                os._exit(0)
            except BaseException:
                os._exit(97)
        os.close(proof_write)
        try:
            outcome = os.read(proof_read, 16)
            waited, status = os.waitpid(pid, 0)
            self.assertEqual(pid, waited)
            self.assertEqual(0, os.waitstatus_to_exitcode(status))
            self.assertEqual(b"closed", outcome)
        finally:
            os.close(proof_read)
            for descriptor in (leaked_read, leaked_write):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_darwin_close_end_is_derived_at_use_and_clamped(self) -> None:
        """Catches import-time derivation or closure above maxfilesperproc."""
        from floati import effect_reconciliation_observer as observer
        from floati import worker_bootstrap as bootstrap

        modules_and_closers = (
            (observer, observer._close_unruled_descriptors),
            (bootstrap, bootstrap.close_all_descriptors_except),
        )
        cases = ((64, 256), (4096, 4096), (200_000, 122_880))
        for module, closer in modules_and_closers:
            for soft_limit, expected in cases:
                with self.subTest(module=module.__name__, soft_limit=soft_limit):
                    ranges: list[tuple[int, int]] = []
                    with mock.patch.object(
                        module.sys,
                        "platform",
                        "darwin",
                    ), mock.patch.object(
                        module.resource,
                        "getrlimit",
                        return_value=(soft_limit, soft_limit),
                    ), mock.patch.object(
                        module,
                        "_darwin_maxfilesperproc",
                        return_value=122_880,
                        create=True,
                    ), mock.patch.object(
                        module,
                        "_open_descriptors",
                        return_value=set(),
                    ), mock.patch.object(
                        module.os,
                        "closerange",
                        side_effect=lambda start, end: ranges.append((start, end)),
                    ):
                        closer({0, 1, 2})

                    self.assertEqual((3, expected), ranges[-1])

    @unittest.skipUnless(sys.platform == "darwin", "macOS descriptor surface")
    def test_darwin_maxfilesperproc_uses_inprocess_sysctlbyname(self) -> None:
        """Catches a subprocess or hard-coded Darwin descriptor ceiling."""
        from floati import effect_reconciliation_observer as observer
        from floati import worker_bootstrap as bootstrap

        class FakeSysctl:
            argtypes = None
            restype = None

            def __init__(self) -> None:
                self.names: list[bytes] = []

            def __call__(self, name, output, output_size, replacement, replacement_size):
                self.names.append(name)
                ctypes.cast(output, ctypes.POINTER(ctypes.c_int))[0] = 777
                ctypes.cast(output_size, ctypes.POINTER(ctypes.c_size_t))[0] = 4
                return 0

        for module in (observer, bootstrap):
            with self.subTest(module=module.__name__):
                sysctl = FakeSysctl()
                libc = types.SimpleNamespace(sysctlbyname=sysctl)
                with mock.patch.object(
                    module.ctypes,
                    "CDLL",
                    return_value=libc,
                ) as load_libc:
                    self.assertEqual(777, module._darwin_maxfilesperproc())

                load_libc.assert_called_once_with(None, use_errno=True)
                self.assertEqual([b"kern.maxfilesperproc"], sysctl.names)

    def test_descriptor_enumeration_failure_sends_typed_unavailable_result(self) -> None:
        """Catches descriptor-directory failure becoming silent bootstrap death."""
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        channel = BootstrapChannel(parent.detach())
        self.addCleanup(channel.close)
        with mock.patch("floati.worker_bootstrap.os.setsid"), mock.patch(
            "floati.worker_bootstrap.os.chdir",
        ), mock.patch(
            "floati.worker_bootstrap.os.listdir", side_effect=OSError("unavailable"),
        ), mock.patch(
            "floati.worker_bootstrap.os.closerange",
        ):
            result = bootstrap_main(child.fileno())
        child.close()

        self.assertEqual(1, result)
        self.assertEqual(
            ("failure", "effect_worker_isolation_unavailable"), channel.recv(),
        )

    def test_bootstrap_rejects_unknown_adapter_factory_module_class_and_pickle_fields(self) -> None:
        """Catches open-ended launch configuration reaching imports or callbacks."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            for field in ("factory", "module", "class", "pickle"):
                with self.subTest(field=field):
                    case = root / field
                    case.mkdir()
                    policy = self._policy(case / "policy")
                    trace = case / "trace"
                    bootstrap = self._instrumented_package(
                        case / "instrumented",
                        isolation_source=self._stub_isolation(
                            "raise AssertionError('isolation must not run')"
                        ),
                        adapter_source=self._stub_adapter(),
                    )
                    environment = dict(os.environ)
                    environment["FLOATI_BOOTSTRAP_TRACE"] = str(trace)
                    launch = self._launch_payload(policy)
                    launch[field] = "hostile"
                    pid, channel = self._spawn(
                        bootstrap, launch, environment=environment,
                    )
                    frames = self._receive_until_eof(channel)
                    channel.close()

                    self.assertNotEqual(0, self._wait(pid))
                    self.assertEqual(
                        [("failure", "effect_worker_isolation_unavailable")], frames
                    )
                    self.assertFalse(trace.exists())

    def test_bootstrap_unsupported_backend_sends_one_typed_failure_and_imports_no_adapter(self) -> None:
        """Catches adapter import or duplicate testimony after isolation refusal."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            policy = self._policy(root / "policy")
            trace = root / "trace"
            bootstrap = self._instrumented_package(
                root / "instrumented",
                isolation_source=self._stub_isolation(
                    "from floati.worker_errors import WorkerAdapterFailure\n"
                    "raise WorkerAdapterFailure('effect_worker_isolation_unavailable')"
                ),
                adapter_source=self._stub_adapter(),
            )
            environment = dict(os.environ)
            environment["FLOATI_BOOTSTRAP_TRACE"] = str(trace)
            pid, channel = self._spawn(
                bootstrap, self._launch_payload(policy), environment=environment,
            )
            self.addCleanup(channel.close)

            self.assertEqual(
                [("failure", "effect_worker_isolation_unavailable")],
                self._receive_until_eof(channel),
            )
            self.assertNotEqual(0, self._wait(pid))
            self.assertFalse(trace.exists())

    def test_bootstrap_isolation_ready_precedes_builtin_import_and_every_callback(self) -> None:
        """Catches any built-in import or callback before successful activation."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            policy = self._policy(root / "policy")
            trace = root / "trace"
            readiness_gate = root / "readiness-observed"
            adapter_source = (
                "import os\n"
                "import time\n"
                "from pathlib import Path\n"
                "TRACE = Path(os.environ['FLOATI_BOOTSTRAP_TRACE'])\n"
                "GATE = Path(os.environ['FLOATI_BOOTSTRAP_READY_GATE'])\n"
                "deadline = time.monotonic() + 2.0\n"
                "while not GATE.exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.01)\n"
                "with TRACE.open('a', encoding='utf-8') as stream:\n"
                "    stream.write(('import' if GATE.exists() else 'import-before-ready') + '\\n')\n"
                "class Adapter:\n"
                "    requires_workspace = False\n"
                "    def __init__(self, command, *, isolate_process_group=True):\n"
                "        with TRACE.open('a', encoding='utf-8') as stream:\n"
                "            stream.write('construct:/bin/echo\\n')\n"
                "    def spawn(self, item, *, deadline_seconds):\n"
                "        with TRACE.open('a', encoding='utf-8') as stream:\n"
                "            stream.write('spawn\\n')\n"
                "        return object()\n"
                "    def drive(self, handle, item, *, deadline_seconds):\n"
                "        with TRACE.open('a', encoding='utf-8') as stream:\n"
                "            stream.write('drive\\n')\n"
                "        return []\n"
                "CodexAppServerAdapter = Adapter\n"
                "ClaudeHeadlessAdapter = Adapter\n"
                "PiRpcAdapter = Adapter\n"
            )
            bootstrap = self._instrumented_package(
                root / "instrumented",
                isolation_source=self._stub_isolation(
                    "Path(__import__('os').environ['FLOATI_BOOTSTRAP_TRACE']).write_text('apply\\n', encoding='utf-8')\n"
                    "return 'macos-sandbox'"
                ),
                adapter_source=adapter_source,
            )
            environment = dict(os.environ)
            environment["FLOATI_BOOTSTRAP_TRACE"] = str(trace)
            environment["FLOATI_BOOTSTRAP_READY_GATE"] = str(readiness_gate)
            pid, channel = self._spawn(
                bootstrap, self._launch_payload(policy), environment=environment,
            )
            self.addCleanup(channel.close)
            first = channel.recv()
            self.assertEqual("isolation_ready", first[0])
            readiness_gate.touch()
            frames = [first, *self._receive_until_eof(channel)]

            self.assertEqual(0, self._wait(pid))
            self.assertEqual(["isolation_ready", "spawned", "result"], [f[0] for f in frames])
            self.assertEqual(
                ["apply", "import", "construct:/bin/echo", "spawn", "drive"],
                trace.read_text(encoding="utf-8").splitlines(),
            )

    def test_bootstrap_reconstructs_codex_claude_and_pi_from_closed_specs(self) -> None:
        """Catches wrong literal adapter selection or command reconstruction."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            for kind in ("codex", "claude", "pi"):
                with self.subTest(kind=kind):
                    case = root / kind
                    case.mkdir()
                    policy = self._policy(case / "policy")
                    trace = case / "trace"
                    bootstrap = self._instrumented_package(
                        case / "instrumented",
                        isolation_source=self._stub_isolation(
                            "Path(__import__('os').environ['FLOATI_BOOTSTRAP_TRACE']).write_text('apply\\n', encoding='utf-8')\n"
                            "return 'macos-sandbox'"
                        ),
                        adapter_source=self._stub_adapter(),
                    )
                    environment = dict(os.environ)
                    environment["FLOATI_BOOTSTRAP_TRACE"] = str(trace)
                    command = ("/bin/echo", kind, "detached")
                    pid, channel = self._spawn(
                        bootstrap,
                        self._launch_payload(policy, kind=kind, command=command),
                        environment=environment,
                    )
                    frames = self._receive_until_eof(channel)
                    channel.close()

                    self.assertEqual(0, self._wait(pid))
                    self.assertEqual(["isolation_ready", "spawned", "result"], [f[0] for f in frames])
                    self.assertIn(
                        "construct:/bin/echo|%s|detached" % kind,
                        trace.read_text(encoding="utf-8").splitlines(),
                    )

    def test_post_isolation_runtime_preserves_spawn_effect_result_close_and_process_group_frames(self) -> None:
        """Catches semantic drift while extracting the legacy callback runtime."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            policy = self._policy(Path(temporary))
            channel = _RecordingChannel((
                ("effect_reporting_closed", None),
                ("observation_closed", None),
            ))
            calls: list[object] = []

            class Adapter:
                requires_workspace = True

                def set_prepared_workspace(self, path: str, device: int, inode: int) -> None:
                    calls.append(("workspace", path, device, inode))

                def set_process_group_registrar(self, registrar: object) -> None:
                    self.registrar = registrar

                def set_spawn_context(self, context: dict[str, object], emit: object) -> None:
                    calls.append(("spawn_context", context))
                    self.emit_descendant = emit

                def set_effect_context(self, context: dict[str, object], emit: object) -> None:
                    calls.append(("effect_context", context))
                    self.emit_effect = emit

                def spawn(self, item: dict[str, object], *, deadline_seconds: float) -> object:
                    self.registrar(321)
                    self.emit_descendant({"state": "observed"})
                    self.emit_effect({"verb": "intent"})
                    calls.append(("spawn", item))
                    return "handle"

                def drive(self, handle: object, item: dict[str, object], *, deadline_seconds: float) -> list[dict[str, str]]:
                    calls.append(("drive", handle, item))
                    return [{"repo": "floati", "sha": "a" * 40, "doc": "README.md"}]

            run_adapter_session(
                channel, Adapter(), {"id": "work-a"}, 2.0,
                {"subagents_mode": "observed_only"}, {"effect": "governed"}, policy,
            )

            self.assertEqual(
                [
                    "process_group", "descendant", "effect", "spawned", "result",
                    "effect_reporting_closed_ack", "observation_closed_ack",
                ],
                [frame[0] for frame in channel.sent],
            )
            self.assertTrue(channel.closed)
            self.assertEqual("workspace", calls[0][0])
            self.assertEqual(("spawn", {"id": "work-a"}), calls[3])
            self.assertEqual(("drive", "handle", {"id": "work-a"}), calls[4])

    def test_reconstructed_built_in_adapters_accept_spawn_and_effect_contexts(self) -> None:
        """Catches real built-ins degrading only on the supported isolation path."""
        for kind in ("codex", "claude", "pi"):
            with self.subTest(kind=kind):
                adapter = _construct_adapter(
                    BuiltInAdapterSpec(kind, ("/bin/echo",))
                )
                channel = _RecordingChannel((
                    ("effect_reporting_closed", None),
                    ("observation_closed", None),
                ))
                with (
                    mock.patch.object(adapter, "spawn", return_value="handle"),
                    mock.patch.object(adapter, "drive", return_value=[]),
                ):
                    run_adapter_session(
                        channel,
                        adapter,
                        {"id": "work-a"},
                        2.0,
                        {"subagents_mode": "observed_only"},
                        {"effect": "governed"},
                        None,
                    )

                self.assertEqual(
                    [
                        "spawned",
                        "result",
                        "effect_reporting_closed_ack",
                        "observation_closed_ack",
                    ],
                    [frame[0] for frame in channel.sent],
                )

    def test_exec_runtime_never_installs_child_selected_process_group_registrar(self) -> None:
        """Catches the exec runtime granting an adapter process-group testimony."""
        channel = _RecordingChannel()
        calls: list[object] = []

        class Adapter:
            requires_workspace = False

            def set_process_group_registrar(self, registrar: object) -> None:
                calls.append(("registrar", registrar))

            def spawn(self, item: dict[str, object], *, deadline_seconds: float) -> object:
                calls.append(("spawn", item))
                return "handle"

            def drive(
                self,
                handle: object,
                item: dict[str, object],
                *,
                deadline_seconds: float,
            ) -> list[dict[str, str]]:
                calls.append(("drive", handle, item))
                return []

        run_adapter_session(
            channel,
            Adapter(),
            {"id": "work-a"},
            2.0,
            None,
            None,
            None,
            process_group_mode="inherited",
        )

        self.assertEqual(
            [("spawn", {"id": "work-a"}), ("drive", "handle", {"id": "work-a"})],
            calls,
        )
        self.assertEqual(["spawned", "result"], [frame[0] for frame in channel.sent])
        self.assertTrue(channel.closed)

    def test_bootstrap_signal_cancel_occurs_only_after_isolation_ready(self) -> None:
        """Catches signal cancellation becoming executable before readiness."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            policy = self._policy(root / "policy")
            trace = root / "trace"
            bootstrap = self._instrumented_package(
                root / "instrumented",
                isolation_source=self._stub_isolation(
                    "Path(__import__('os').environ['FLOATI_BOOTSTRAP_TRACE']).write_text('apply\\n', encoding='utf-8')\n"
                    "return 'macos-sandbox'"
                ),
                adapter_source=self._stub_adapter(
                    "        while True:\n"
                    "            __import__('time').sleep(0.05)\n"
                ),
            )
            environment = dict(os.environ)
            environment["FLOATI_BOOTSTRAP_TRACE"] = str(trace)
            pid, channel = self._spawn(
                bootstrap, self._launch_payload(policy), environment=environment,
            )
            self.addCleanup(channel.close)
            first = channel.recv()
            self.assertEqual("isolation_ready", first[0])
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if trace.exists() and "spawn" in trace.read_text(encoding="utf-8").splitlines():
                    break
                time.sleep(0.01)
            else:
                self.fail("adapter did not enter spawn after isolation readiness")

            os.kill(pid, signal.SIGTERM)
            self.assertEqual(143, self._wait(pid))
            rows = trace.read_text(encoding="utf-8").splitlines()
            self.assertEqual("apply", rows[0])
            self.assertEqual("cancel", rows[-1])
            self.assertGreater(rows.index("cancel"), rows.index("import"))


if __name__ == "__main__":
    unittest.main()
