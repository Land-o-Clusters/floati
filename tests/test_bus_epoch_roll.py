from __future__ import annotations

import ast
import builtins
from collections import Counter, defaultdict
import hashlib
import importlib
import inspect
import io
import json
import os
import re
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from unittest import mock

from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.cursor import SparseCursor
from floati.doctor import Doctor
from floati.doctor_probe import DoctorProbe
from floati.errors import IntegrityFailure, ProtocolRefusal, SnapshotRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.projection import FleetProjection, iter_deltas
from floati.planes import AuthorityGrantStore
from floati.records import validate_record
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.snapshot import SnapshotStore
from floati.tui import model_from_root
from floati.wake_control import WakeController
from floati.wake_daemon import WakeDaemon
from floati.wake_daemon_adapters import AdapterBinding, WakeAdapterResult, adapter_contract_digest
from floati.wake_daemon_contract import AdapterBindingStore, DaemonConsentLedger, DaemonCoordinate
from floati.wake_hold import WakeAttemptLedger, WakeHoldController
from tests.schema_validation import SchemaValidationError, validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FOLLOWER_CLASSES = ["tail_followers", "waiters", "monitors"]
RECEIPT_FIELDS = {
    "schema_version", "id", "tenant_id", "timestamp", "kind", "archive_path",
    "actor", "idempotency_key", "invalidated_followers", "epoch_id", "span",
    "archive_sha256", "archive_file_count", "plane_counts",
}
READER_FAMILIES = {
    "snapshot_inbox_status_board", "sparse_inbox_ack_cursor", "wake_prefix_attempt",
    "watch_board_probe", "codex_waiter", "wake_daemon", "external_path_follower",
}
# Test-owned, occurrence-counted assignments.  Targets preserve receiver syntax so
# the daemon's tide evaluator cannot masquerade as either wake-controller read.
READER_ASSIGNMENTS = (
    ("floati/events.py", "EventLog._inbox_snapshot_store", "SnapshotStore", 1, "snapshot_inbox_status_board"),
    ("floati/projection.py", "FleetProjection._status_snapshot_store", "SnapshotStore", 1, "snapshot_inbox_status_board"),
    ("floati/tui.py", "model_from_root", "SnapshotStore", 1, "snapshot_inbox_status_board"),
    ("floati/cli.py", "_ack", "SparseCursor", 1, "sparse_inbox_ack_cursor"),
    ("floati/conformance.py", "ReferenceAdapter.__init__", "SparseCursor", 1, "sparse_inbox_ack_cursor"),
    ("floati/conformance.py", "run_live_root_smoke", "SparseCursor", 1, "sparse_inbox_ack_cursor"),
    ("floati/demo.py", "seed_demo", "SparseCursor", 1, "sparse_inbox_ack_cursor"),
    ("floati/events.py", "EventLog._present_from_frames", "SparseCursor", 1, "sparse_inbox_ack_cursor"),
    ("floati/events.py", "EventLog._present_from_frames", "SparseCursor(self.root).state_for", 1, "sparse_inbox_ack_cursor"),
    ("floati/events.py", "EventLog._messages_from_snapshot", "SparseCursor", 1, "sparse_inbox_ack_cursor"),
    ("floati/events.py", "EventLog._messages_from_snapshot", "SparseCursor(self.root)._relative_path_for", 1, "sparse_inbox_ack_cursor"),
    ("floati/lane_scaling.py", "LaneScalingService._drain", "SparseCursor", 1, "sparse_inbox_ack_cursor"),
    ("floati/lane_scaling.py", "LaneScalingService._drain", "SparseCursor(self.root).acked_ids", 1, "sparse_inbox_ack_cursor"),
    ("floati/tui.py", "acknowledge_visible", "SparseCursor", 1, "sparse_inbox_ack_cursor"),
    ("floati/cursor.py", "SparseCursor.path_for", "self._relative_path_for", 1, "sparse_inbox_ack_cursor"),
    ("floati/cursor.py", "SparseCursor.delivery_path_for", "self._delivery_relative_path_for", 1, "sparse_inbox_ack_cursor"),
    ("floati/cursor.py", "SparseCursor.acked_ids", "self.state_for", 1, "sparse_inbox_ack_cursor"),
    ("floati/cursor.py", "SparseCursor.state_for", "self._relative_path_for", 1, "sparse_inbox_ack_cursor"),
    ("floati/cursor.py", "SparseCursor.validate_deliveries", "self._delivery_relative_path_for", 1, "sparse_inbox_ack_cursor"),
    ("floati/cursor.py", "SparseCursor._ack_already_guarded", "self._relative_path_for", 1, "sparse_inbox_ack_cursor"),
    ("floati/wake_hold.py", "WakeHoldLedger.__init__", "SparseCursor", 1, "wake_prefix_attempt"),
    ("floati/wake_hold.py", "WakeHoldLedger.__init__", "SparseCursor(root)._delivery_relative_path_for", 1, "wake_prefix_attempt"),
    ("floati/wake_hold.py", "WakeAttemptLedger.record", "SparseCursor", 1, "wake_prefix_attempt"),
    ("floati/wake_hold.py", "WakeAttemptLedger.record", "SparseCursor(self.root)._delivery_relative_path_for", 1, "wake_prefix_attempt"),
    ("floati/wake_hold.py", "WakeHoldController._read", "SparseCursor", 1, "wake_prefix_attempt"),
    ("floati/wake_hold.py", "WakeHoldController._read", "cursor._delivery_relative_path_for", 1, "wake_prefix_attempt"),
    ("floati/wake_hold.py", "WakeHoldController._read", "cursor._relative_path_for", 1, "wake_prefix_attempt"),
    ("floati/wake_hold.py", "WakeHoldController._read", "read_records_with_prefix_digests", 3, "wake_prefix_attempt"),
    ("floati/wake_hold.py", "WakeHoldController._append_receipt_already_guarded", "SparseCursor", 1, "wake_prefix_attempt"),
    ("floati/wake_hold.py", "WakeHoldController._append_receipt_already_guarded", "SparseCursor(self.root)._delivery_relative_path_for", 1, "wake_prefix_attempt"),
    ("floati/projection.py", "iter_deltas", "<definition>", 1, "watch_board_probe"),
    ("floati/doctor_probe.py", "DoctorProbe._drained", "<definition>", 1, "watch_board_probe"),
    ("floati/codex_wait.py", "run_stop_waiter", "<definition>", 1, "codex_waiter"),
    ("floati/codex_wait.py", "run_stop_waiter", "controller.evaluate", 1, "codex_waiter"),
    ("floati/wake_daemon.py", "WakeDaemon.run_cycle", "<definition>", 1, "wake_daemon"),
    ("floati/wake_daemon.py", "WakeDaemon.serve", "<definition>", 1, "wake_daemon"),
    ("floati/wake_daemon.py", "WakeDaemon.run_cycle", "controller.evaluate", 2, "wake_daemon"),
)
WAIT_SECONDS = 3.0
SUBPROCESS_SECONDS = 8.0


@dataclass(frozen=True)
class _MutationEvent:
    primitive: str
    target: str
    before: tuple[tuple[object, ...], ...]
    after: tuple[tuple[object, ...], ...]
    durability: bool
    guarded: bool = False


@dataclass(frozen=True)
class _BoundaryEvent:
    name: str
    snapshot: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class _ReaderAnchor:
    path: str
    owner: str
    target: str
    ordinal: int
    line: int
    column: int

    @property
    def stable_coordinate(self) -> str:
        return f"{self.path}::{self.owner}::{self.target}#{self.ordinal}"


@dataclass(frozen=True)
class _ReaderCase:
    family: str
    helper_name: str


class _WriteProxy:
    """Record effects of buffered file mutations without changing file semantics."""

    def __init__(self, handle: object, probe: "_MutationProbe", target: Path) -> None:
        self._handle, self._probe, self._target = handle, probe, target
        self._fd = probe.associate_handle(handle, target)

    def __enter__(self) -> "_WriteProxy":
        self._handle.__enter__()  # type: ignore[attr-defined]
        return self

    def __exit__(self, *args: object) -> object:
        try:
            return self._probe.invoke(
                "file.__exit__", self._target, self._handle.__exit__, *args  # type: ignore[attr-defined]
            )
        finally:
            self._probe.forget_fd(self._fd)

    def __iter__(self):
        return iter(self._handle)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self._handle, name)

    def write(self, payload: object) -> object:
        return self._probe.invoke("file.write", self._target, self._handle.write, payload)  # type: ignore[attr-defined]

    def writelines(self, payload: object) -> object:
        return self._probe.invoke(
            "file.writelines", self._target, self._handle.writelines, payload  # type: ignore[attr-defined]
        )

    def truncate(self, *args: object) -> object:
        return self._probe.invoke(
            "file.truncate", self._target, self._handle.truncate, *args  # type: ignore[attr-defined]
        )

    def flush(self) -> object:
        return self._probe.invoke("file.flush", self._target, self._handle.flush)  # type: ignore[attr-defined]

    def close(self) -> object:
        try:
            return self._probe.invoke("file.close", self._target, self._handle.close)  # type: ignore[attr-defined]
        finally:
            self._probe.forget_fd(self._fd)


class _MutationProbe:
    """Test-owned observation of concrete root-scoped filesystem effects."""

    DURABILITY = frozenset({"os.fsync", "os.fdatasync"})

    def __init__(
        self, root: FloatiRoot, *, abort_after: int | None = None,
        stack_prefix: str = "floati.bus_epoch",
        guard_active: Callable[[], bool] | None = None,
    ) -> None:
        self.root = root
        self.abort_after = abort_after
        self.thread_id = threading.get_ident()
        self.events: list[_MutationEvent | _BoundaryEvent] = []
        self._lock = threading.Lock()
        self._active = False
        self._depth = 0
        self._fds: dict[int, Path] = {}
        self._original_exit = os._exit
        self._stack_prefix = stack_prefix
        self._guard_active = guard_active or (lambda: False)

    @staticmethod
    def snapshot(root: FloatiRoot) -> tuple[tuple[object, ...], ...]:
        rows: list[tuple[object, ...]] = []
        for path in sorted(root.tenant_home.rglob("*")):
            status = path.lstat()
            relative = path.relative_to(root.tenant_home).as_posix()
            if path.is_symlink():
                kind, payload = "symlink", os.readlink(path)
            elif path.is_dir():
                kind, payload = "directory", None
            elif stat.S_ISREG(status.st_mode):
                kind, payload = "file", path.read_bytes()
            else:
                kind, payload = "nonregular", stat.S_IFMT(status.st_mode)
            rows.append((relative, kind, status.st_mode, status.st_dev, status.st_ino, status.st_size,
                         status.st_mtime_ns, status.st_ctime_ns, payload))
        return tuple(rows)

    def _path(self, candidate: object) -> Path | None:
        if isinstance(candidate, int):
            return self._fds.get(candidate)
        try:
            path = Path(os.fspath(candidate))  # type: ignore[arg-type]
        except TypeError:
            return None
        if not path.is_absolute():
            path = Path.cwd() / path
        normalized = Path(os.path.abspath(path))
        home = Path(os.path.abspath(self.root.tenant_home))
        return normalized if normalized == home or home in normalized.parents else None

    def associate_handle(self, handle: object, target: Path) -> int | None:
        try:
            descriptor = int(handle.fileno())  # type: ignore[attr-defined]
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        self._fds[descriptor] = target
        return descriptor

    def forget_fd(self, descriptor: int | None) -> None:
        if descriptor is not None:
            self._fds.pop(descriptor, None)

    def _roll_call(self) -> bool:
        if not self._active or threading.get_ident() != self.thread_id:
            return False
        return any(
            str(frame.frame.f_globals.get("__name__", "")).startswith(self._stack_prefix)
            for frame in inspect.stack(context=0)
        )

    def _record(
        self, primitive: str, target: Path, before: tuple[tuple[object, ...], ...],
        after: tuple[tuple[object, ...], ...],
    ) -> None:
        durability = primitive in self.DURABILITY
        if before == after and not durability:
            return
        event = _MutationEvent(
            primitive, target.relative_to(self.root.tenant_home).as_posix(),
            before, after, durability, self._guard_active(),
        )
        with self._lock:
            self.events.append(event)
            ordinal = sum(isinstance(item, _MutationEvent) for item in self.events)
        if self.abort_after == ordinal:
            self._original_exit(91)

    def invoke(
        self, primitive: str, candidate: object, function: Callable[..., object],
        *args: object, **kwargs: object,
    ) -> object:
        target = self._path(candidate)
        if target is None or self._depth or not self._roll_call():
            return function(*args, **kwargs)
        self._depth += 1
        try:
            before = self.snapshot(self.root)
            result = function(*args, **kwargs)
            after = self.snapshot(self.root)
        finally:
            self._depth -= 1
        self._record(primitive, target, before, after)
        return result

    def boundary(self, name: str) -> None:
        if not self._roll_call():
            raise AssertionError("fault callback escaped the exact roll thread/call stack")
        self._depth += 1
        try:
            snapshot = self.snapshot(self.root)
        finally:
            self._depth -= 1
        with self._lock:
            self.events.append(_BoundaryEvent(name, snapshot))

    @contextmanager
    def installed(self):
        originals = {
            name: getattr(os, name) for name in (
                "open", "write", "fsync", "rename", "replace", "unlink", "remove",
                "rmdir", "mkdir", "makedirs", "truncate", "ftruncate", "close",
            ) if hasattr(os, name)
        }
        for optional in ("pwrite", "writev", "fdatasync"):
            if hasattr(os, optional):
                originals[optional] = getattr(os, optional)
        builtin_open, io_open = builtins.open, io.open

        def open_wrapper(
            file: object, mode: str = "r", *args: object, **kwargs: object
        ) -> object:
            handle = self.invoke(
                "builtins.open", file, builtin_open, file, mode, *args, **kwargs
            )
            target = self._path(file)
            if target is not None and any(flag in mode for flag in "wax+"):
                return _WriteProxy(handle, self, target)
            return handle

        def io_open_wrapper(
            file: object, mode: str = "r", *args: object, **kwargs: object
        ) -> object:
            handle = self.invoke("io.open", file, io_open, file, mode, *args, **kwargs)
            target = self._path(file)
            if target is not None and any(flag in mode for flag in "wax+"):
                return _WriteProxy(handle, self, target)
            return handle

        def os_wrapper(name: str, original: Callable[..., object]):
            def wrapped(*args: object, **kwargs: object) -> object:
                candidate = args[0] if args else kwargs.get("path", kwargs.get("fd"))
                directory_fd_name = {
                    "open": "dir_fd", "unlink": "dir_fd", "remove": "dir_fd",
                    "rmdir": "dir_fd", "mkdir": "dir_fd",
                    "rename": "dst_dir_fd", "replace": "dst_dir_fd",
                }.get(name)
                if name in {"rename", "replace"}:
                    candidate = args[1] if len(args) > 1 else kwargs.get("dst")
                if directory_fd_name is not None and not isinstance(candidate, int):
                    try:
                        candidate_path = Path(os.fspath(candidate))  # type: ignore[arg-type]
                    except TypeError:
                        candidate_path = None
                    directory_fd = kwargs.get(directory_fd_name)
                    if candidate_path is not None and not candidate_path.is_absolute()\
                            and isinstance(directory_fd, int) and directory_fd in self._fds:
                        candidate = self._fds[directory_fd] / candidate_path
                result = self.invoke(f"os.{name}", candidate, original, *args, **kwargs)
                if name == "open" and isinstance(result, int):
                    path = self._path(candidate)
                    if path is not None:
                        self._fds[result] = path
                elif name == "close" and isinstance(candidate, int):
                    self._fds.pop(candidate, None)
                return result
            return wrapped

        with ExitStack() as stack:
            replacements: dict[object, object] = {
                builtin_open: open_wrapper,
                io_open: io_open_wrapper,
            }
            stack.enter_context(mock.patch.object(builtins, "open", open_wrapper))
            stack.enter_context(mock.patch.object(io, "open", io_open_wrapper))
            for name, original in originals.items():
                replacement = os_wrapper(name, original)
                replacements[original] = replacement
                stack.enter_context(mock.patch.object(os, name, replacement))
            for module in tuple(sys.modules.values()):
                if module is None or not str(getattr(module, "__name__", "")).startswith("floati"):
                    continue
                for name, value in tuple(vars(module).items()):
                    for original, replacement in replacements.items():
                        if value is original:
                            stack.enter_context(mock.patch.object(module, name, replacement))
                            break
                    if not inspect.isclass(value) or value.__module__ != module.__name__:
                        continue
                    for member_name, member in tuple(vars(value).items()):
                        raw = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
                        for original, replacement in replacements.items():
                            if raw is not original:
                                continue
                            installed: object = replacement
                            if isinstance(member, staticmethod):
                                installed = staticmethod(replacement)
                            elif isinstance(member, classmethod):
                                installed = classmethod(replacement)
                            stack.enter_context(mock.patch.object(value, member_name, installed))
                            break
            self._active = True
            try:
                yield self
            finally:
                self._active = False


class _DaemonAdapter:
    def __init__(self, root: FloatiRoot, coordinate: DaemonCoordinate) -> None:
        self.store = AdapterBindingStore(root)
        self.coordinate = coordinate
        self.calls: list[str] = []

    def exact_binding(self) -> AdapterBinding:
        return AdapterBinding.from_record(self.store.read(self.coordinate))

    def observe_session(self, _binding: AdapterBinding) -> str:
        return "unknown"

    def request_wake(
        self, _binding: AdapterBinding, reason: str, _deadline_seconds: int
    ) -> WakeAdapterResult:
        self.calls.append(reason)
        return WakeAdapterResult("woke", None, 0, "e" * 64)


class GovernedBusEpochRollTests(unittest.TestCase):
    """G5 rotates one byte-exact event/delivery/ack epoch as a unit."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "epoch-tenant"
        self.root = self._new_root(self.home)
        self._pipe_buffers: dict[int, bytes] = {}
        self._discovered_authority_subject: str | None = None
        self._archive_verifier_fn: Callable[[Path], object] | None = None
        self._archive_verifier_identity: Callable[..., object] | None = None
        self._roll_adapter: Callable[..., object] | None = None
        self._fault_boundaries: tuple[str, ...] | None = None
        self._roll_target: tuple[object, str, Callable[..., object], str, str] | None = None

    @staticmethod
    def _new_root(home: Path) -> FloatiRoot:
        root = FloatiRoot.open_direct_home(home, create=True)
        registry = Registry(root)
        for node in ("actor-a", "actor-b", "recipient", "retired-actor"):
            registry.register(node, "Codex")
        registry.retire("retired-actor")
        return root

    def _run_roll(
        self, *, root: FloatiRoot | None = None, actor: str = "actor-a",
        key: str = "epoch-roll-1",
    ) -> subprocess.CompletedProcess[str]:
        selected = self.root if root is None else root
        return subprocess.run(
            [sys.executable, "-m", "floati", "epoch", "roll", "--root",
             str(selected.path), "--as", actor, "--idempotency-key", key],
            cwd=REPOSITORY_ROOT, env=dict(os.environ), text=True,
            capture_output=True, check=False, timeout=SUBPROCESS_SECONDS,
        )

    def _roll_authority_subject(self) -> str:
        if self._discovered_authority_subject is not None:
            return self._discovered_authority_subject
        _module, roll = self._public_roll_contract()
        probe = self._new_root(self.base / "authority-subject-probe" / self.root.tenant_id)
        before = self._snapshot(probe)
        seen: list[str] = []
        original = AuthorityGrantStore.exact_tail

        def capture(store: AuthorityGrantStore, subject: str) -> dict[str, object]:
            seen.append(subject)
            return original(store, subject)

        with mock.patch.object(AuthorityGrantStore, "exact_tail", capture):
            with self.assertRaises(ProtocolRefusal):
                roll(
                    probe, actor="actor-a", idempotency_key="discover-roll-subject",
                    fault=lambda _boundary: None,
                )
        self.assertEqual(before, self._snapshot(probe), "ungranted subject probe must not mutate")
        if len(set(seen)) != 1 or not seen[0]:
            raise AssertionError("roll must ask existing grant vocabulary for one stable subject")
        AuthorityGrantStore(probe).path_for(seen[0])
        self._discovered_authority_subject = seen[0]
        return seen[0]

    def _grant_roll_authority(self, root: FloatiRoot, actor: str) -> dict[str, object]:
        store = AuthorityGrantStore(root)
        subject = self._roll_authority_subject()
        now = datetime.now(timezone.utc)
        try:
            prior = store.exact_tail(subject)
        except ProtocolRefusal as exc:
            if exc.code != "authority_missing":
                raise
            epoch = 1
        else:
            expires_at = datetime.fromisoformat(
                str(prior["expires_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if (
                prior.get("state") == "active"
                and prior.get("holder") == actor
                and now < expires_at
            ):
                epoch = int(prior["epoch"])
            else:
                epoch = int(prior["epoch"]) + 1
        return store.grant_exact(subject, actor, epoch, now)

    @staticmethod
    def _artifact(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        return json.loads(result.stdout if result.returncode == 0 else result.stderr)

    def _roll_success(
        self, *, root: FloatiRoot | None = None, actor: str = "actor-a",
        key: str = "epoch-roll-1",
    ) -> dict[str, object]:
        selected = self.root if root is None else root
        self._grant_roll_authority(selected, actor)
        grant_path = AuthorityGrantStore(selected).path_for(self._roll_authority_subject())
        grant_before = self._identity(grant_path)
        result = self._run_roll(root=root, actor=actor, key=key)
        self.assertEqual(0, result.returncode, "missing G5 public epoch roll: " + result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        artifact = self._artifact(result)
        self.assertEqual(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            result.stdout,
        )
        self.assertEqual({"artifact_version", "command", "status", "evidence"}, set(artifact))
        self.assertEqual((0, "epoch", "ok"),
                         (artifact["artifact_version"], artifact["command"], artifact["status"]))
        evidence = artifact["evidence"]
        self.assertIsInstance(evidence, dict)
        assert isinstance(evidence, dict)
        self.assertEqual({"root", "tenant_id", "receipt"}, set(evidence))
        self.assertEqual(str(selected.path.resolve()), evidence["root"])
        self.assertEqual(selected.tenant_id, evidence["tenant_id"])
        receipt = evidence["receipt"]
        self.assertIsInstance(receipt, dict)
        assert isinstance(receipt, dict)
        self.assertEqual((actor, key), (receipt.get("actor"), receipt.get("idempotency_key")))
        live = selected.resolve_relative("events.jsonl")
        self.assertEqual(
            encode_frame(receipt),
            live.read_bytes(),
            "returned receipt must be the one canonical physical record one",
        )
        self.assertEqual([receipt], decode_frames(live.read_bytes()))
        for plane in ("receipts/deliveries", "receipts/acks"):
            directory = selected.resolve_relative(plane)
            self.assertFalse(
                any(path.is_symlink() or path.is_file() for path in directory.rglob("*.jsonl")),
                f"roll must leave no live selected {plane} JSONL data",
            )
        self.assertEqual(grant_before, self._identity(grant_path))
        return receipt

    def _roll_success_in_process(
        self, root: FloatiRoot, *, actor: str, key: str,
    ) -> dict[str, object]:
        self._grant_roll_authority(root, actor)
        _module, roll = self._public_roll_contract()
        receipt = self._receipt_from_roll_result(
            roll(root, actor=actor, idempotency_key=key, fault=lambda _name: None)
        )
        self.assertEqual(encode_frame(receipt), root.resolve_relative("events.jsonl").read_bytes())
        return receipt

    def _assert_refusal(
        self, result: subprocess.CompletedProcess[str], *, contract: str,
        root: FloatiRoot | None = None,
    ) -> dict[str, object]:
        selected = self.root if root is None else root
        self.assertEqual(20, result.returncode, result.stdout or result.stderr)
        self.assertEqual("", result.stdout, contract)
        self.assertEqual(1, len(result.stderr.splitlines()), contract)
        artifact = json.loads(result.stderr)
        self.assertEqual(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            result.stderr, contract,
        )
        self.assertEqual({"artifact_version", "command", "status", "evidence"}, set(artifact), contract)
        self.assertEqual((0, "epoch", "refused"),
                         (artifact["artifact_version"], artifact["command"], artifact["status"]), contract)
        evidence = artifact["evidence"]
        self.assertIsInstance(evidence, dict, contract)
        assert isinstance(evidence, dict)
        self.assertTrue({"root", "tenant_id", "code"} <= set(evidence), contract)
        self.assertEqual(str(selected.path.resolve()), evidence["root"], contract)
        self.assertEqual(selected.tenant_id, evidence["tenant_id"], contract)
        self.assertIsInstance(evidence["code"], str, contract)
        self.assertTrue(evidence["code"], contract)
        self.assertNotEqual("arguments_invalid", evidence["code"],
                            "G5 must reach its governed refusal, not reject epoch grammar")
        return artifact

    def _assert_zero_mutation_refusal(
        self, root: FloatiRoot, *, actor: str, key: str, code: str,
    ) -> dict[str, object]:
        parsed, handler = self._public_roll_arguments(root, actor, key)
        domain_before = (self._identity(root.tenant_home), self._path_tree_snapshot(root.path.parent))
        probe = _MutationProbe(root, stack_prefix="floati")
        with probe.installed():
            with self.assertRaises(ProtocolRefusal) as caught:
                handler(parsed)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual([], probe.events, "refusal must perform no transient filesystem mutation")
        self.assertEqual(
            domain_before,
            (self._identity(root.tenant_home), self._path_tree_snapshot(root.path.parent)),
        )
        artifact = self._assert_refusal(
            self._run_roll(root=root, actor=actor, key=key), root=root,
            contract=f"{code} refusal must be canonical and root-bound",
        )
        evidence = artifact["evidence"]
        assert isinstance(evidence, dict)
        self.assertEqual(code, evidence["code"])
        return artifact

    @staticmethod
    def _identity(path: Path) -> tuple[object, ...]:
        status = path.lstat()
        if path.is_symlink():
            payload: object = ("symlink", os.readlink(path))
        elif path.is_dir():
            payload = ("directory", tuple(sorted(child.name for child in path.iterdir())))
        elif stat.S_ISREG(status.st_mode):
            payload = ("file", path.read_bytes())
        else:
            payload = ("nonregular", stat.S_IFMT(status.st_mode))
        return (status.st_dev, status.st_ino, status.st_mode, status.st_size,
                status.st_mtime_ns, status.st_ctime_ns, payload)

    @classmethod
    def _snapshot(cls, root: FloatiRoot) -> tuple[tuple[object, ...], ...]:
        return tuple((path.relative_to(root.tenant_home).as_posix(), *cls._identity(path))
                     for path in sorted(root.tenant_home.rglob("*")))

    @classmethod
    def _path_tree_snapshot(cls, base: Path) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (path.relative_to(base).as_posix(), *cls._identity(path))
            for path in sorted(base.rglob("*"))
        )

    def _wait(self, event: threading.Event, contract: str) -> None:
        self.assertTrue(event.wait(WAIT_SECONDS), contract)

    def _barrier(self, barrier: threading.Barrier, contract: str) -> None:
        try:
            barrier.wait(timeout=WAIT_SECONDS)
        except threading.BrokenBarrierError as exc:
            self.fail(contract + f": {exc}")

    def _join(self, thread: threading.Thread, contract: str) -> None:
        thread.join(timeout=WAIT_SECONDS)
        self.assertFalse(thread.is_alive(), contract)

    def _read_json_line(self, stream: object) -> dict[str, object]:
        descriptor = stream.fileno()  # type: ignore[attr-defined]
        buffered = self._pipe_buffers.get(descriptor, b"")
        deadline = time.monotonic() + WAIT_SECONDS
        rejected: list[bytes] = []
        while True:
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                self._pipe_buffers[descriptor] = buffered
                if not line.strip():
                    rejected.append(line)
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    rejected.append(line)
                    continue
                self.assertIsInstance(value, dict)
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                self.fail(
                    "bounded pipe produced no JSON object before timeout; "
                    f"discarded_lines={len(rejected)}"
                )
            ready, _, _ = select.select([descriptor], [], [], remaining)
            if not ready:
                self.fail(
                    "bounded pipe produced no JSON object before timeout; "
                    f"discarded_lines={len(rejected)}"
                )
            chunk = os.read(descriptor, 4096)
            self.assertTrue(chunk, "bounded pipe closed before one complete line")
            buffered += chunk
            self.assertLessEqual(len(buffered), 65536, "bounded pipe line is oversized")

    @staticmethod
    def _write(root: FloatiRoot, relative: str, payload: bytes) -> Path:
        path = root.resolve_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def _seed_opaque_triple(self, root: FloatiRoot | None = None) -> dict[str, bytes]:
        selected_root = self.root if root is None else root
        cursor = SparseCursor(selected_root)
        nested_delivery = cursor.delivery_path_for(
            "recipient", worker_session_id="nested-session"
        ).relative_to(selected_root.tenant_home).as_posix()
        nested_ack = cursor.path_for(
            "recipient", worker_session_id="nested-session"
        ).relative_to(selected_root.tenant_home).as_posix()
        selected = {
            "events.jsonl": b"opaque events bytes that strict readers reject\r\n\xff\n",
            "receipts/deliveries/recipient.jsonl": b"top delivery\r\n",
            nested_delivery: b"nested delivery\x00\n",
            "receipts/deliveries/foreign/deep.jsonl": b"opaque deep delivery\n",
            "receipts/deliveries/zeta-β.jsonl": b"utf8 path order\n",
            "receipts/acks/recipient.jsonl": b"top ack\r\n",
            nested_ack: b"nested ack\xff\n",
        }
        for relative, payload in selected.items():
            self._write(selected_root, relative, payload)
        for relative in (path + ".lock" for path in selected):
            self._write(selected_root, relative, b"lock-sentinel\n")
        self._write(selected_root, "receipts/deliveries/foreign.bin", b"foreign delivery\n")
        self._write(selected_root, "receipts/acks/foreign.txt", b"foreign ack\n")
        selected_root.resolve_relative("receipts/deliveries/empty-directory").mkdir()
        selected_root.resolve_relative("receipts/acks/empty-directory").mkdir()
        return selected

    def _seed_control_state(self, root: FloatiRoot | None = None) -> tuple[Path, ...]:
        """Seed concrete non-triple state whose bytes and inodes must stay live."""

        selected = self.root if root is None else root
        workspace = self.base / f"control-workspace-{selected.path.name}"
        workspace.mkdir()
        mapping = selected.resolve_relative("codex-wait/workspaces.v0.json")
        mapping.parent.mkdir(parents=True, exist_ok=True)
        mapping.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": selected.tenant_id,
                    "mappings": [{"workspace": str(workspace), "node_id": "actor-a"}],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        participant = resolve_participant(selected.path, workspace)
        self.assertIsNotNone(participant)
        assert participant is not None
        consent = CodexWaitConsentLedger(selected).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="control-consent",
        )
        CodexWaitSessionLedger(selected).arm(
            participant.binding,
            consent,
            "control-session",
            idempotency_key="control-session-claim",
        )
        pause = WakeController(selected).pause(
            "actor-a", "paused-session", idempotency_key="control-pause"
        )
        coordinate = DaemonCoordinate(selected, "actor-a", "codex")
        DaemonConsentLedger(selected).consent(
            coordinate,
            adapter_version="1",
            adapter_digest="a" * 64,
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=1,
            idempotency_key="control-daemon-consent",
        )
        breaker = self._write(
            selected, "state/codex-wait/actor-a/breaker.json", b'{"hits":[1000.0]}\n'
        )
        message = EventLog(selected).send(
            "actor-a",
            "recipient",
            "floati",
            "0" * 40,
            "docs/evidence/control.md",
            "control seed",
            idempotency_key="control-message",
        )
        EventLog(selected).present("recipient")
        SparseCursor(selected).ack(
            "recipient", [message["id"]], acting_session_id="control-ack"
        )
        unrelated = self._write(
            selected, "receipts/denials.jsonl", b"unrelated-receipt-bytes\n"
        )
        return (
            Registry(selected).path,
            mapping,
            selected.resolve_relative("receipts/codex-wait-consent/actor-a.jsonl"),
            selected.resolve_relative("receipts/codex-wait-session/actor-a.jsonl"),
            selected.resolve_relative("receipts/wake-control/actor-a.jsonl"),
            Path(str(pause["marker"])),
            selected.resolve_relative("receipts/wake-daemon/actor-a.jsonl"),
            breaker,
            selected.resolve_relative("receipts/wake-coordination/recipient/lane.lock"),
            unrelated,
        )

    @staticmethod
    def _inventory(selected: Mapping[str, bytes]) -> tuple[list[dict[str, object]], str]:
        rows = []
        for path, payload in sorted(
            selected.items(), key=lambda item: item[0].encode("utf-8")
        ):
            plane = ("events" if path == "events.jsonl" else
                     "deliveries" if path.startswith("receipts/deliveries/") else "acks")
            rows.append({"path": path, "plane": plane, "byte_length": len(payload),
                         "sha256": hashlib.sha256(payload).hexdigest()})
        canonical = b"".join(
            json.dumps(
                row,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            for row in rows
        )
        return rows, hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _archive_files(archive: Path) -> dict[str, bytes]:
        root_status = archive.lstat()
        if archive.is_symlink() or not stat.S_ISDIR(root_status.st_mode):
            raise ProtocolRefusal("archive_root_invalid", "archive root must be an ordinary directory")
        files: dict[str, bytes] = {}
        for path in archive.rglob("*"):
            status = path.lstat()
            if path.is_dir() and not path.is_symlink():
                continue
            if path.is_symlink() or not path.is_file():
                raise ProtocolRefusal("archive_member_invalid", str(path))
            if not stat.S_ISREG(status.st_mode):
                raise ProtocolRefusal("archive_member_invalid", str(path))
            relative = path.relative_to(archive).as_posix()
            selected = (
                relative == "events.jsonl"
                or relative.startswith("receipts/deliveries/")
                or relative.startswith("receipts/acks/")
            ) and relative.endswith(".jsonl")
            if not selected:
                raise ProtocolRefusal("archive_member_invalid", relative)
            files[relative] = path.read_bytes()
        return files

    def _assert_receipt(
        self, root: FloatiRoot, receipt: Mapping[str, object], selected: Mapping[str, bytes]
    ) -> Path:
        self.assertEqual(RECEIPT_FIELDS, set(receipt))
        self.assertEqual((1, "bus_epoch_roll_receipt", root.tenant_id),
                         (receipt["schema_version"], receipt["kind"], receipt["tenant_id"]))
        self.assertEqual(FOLLOWER_CLASSES, receipt["invalidated_followers"])
        rows, digest = self._inventory(selected)
        self.assertEqual(
            sorted((str(row["path"]) for row in rows), key=lambda value: value.encode("utf-8")),
            [row["path"] for row in rows],
            "inventory ordering is fixed UTF-8 byte order, never locale order",
        )
        self.assertEqual(digest, receipt["archive_sha256"])
        self.assertEqual(len(rows), receipt["archive_file_count"])
        self.assertEqual(
            {plane: sum(row["plane"] == plane for row in rows)
             for plane in ("events", "deliveries", "acks")}, receipt["plane_counts"])
        self.assertEqual({"byte_start": 0, "byte_end": sum(map(len, selected.values()))},
                         receipt["span"])
        self.assertIsInstance(receipt["epoch_id"], str)
        self.assertTrue(receipt["epoch_id"])
        validate_record(dict(receipt), root.tenant_id,
                        frozenset({"bus_epoch_roll_receipt"}), integrity=True)
        validate_json_schema(dict(receipt), Path("schemas/v1/bus-epoch-roll-receipt.schema.json"))
        hostile_records = [("extra", dict(receipt, unexpected_epoch_fact=True))]
        hostile_records.extend(
            ("missing-" + field, {key: value for key, value in receipt.items() if key != field})
            for field in sorted(RECEIPT_FIELDS)
        )
        for nested, required in (
            ("span", ("byte_start", "byte_end")),
            ("plane_counts", ("events", "deliveries", "acks")),
        ):
            source = receipt[nested]
            assert isinstance(source, dict)
            hostile_records.append((
                f"extra-{nested}",
                dict(receipt, **{nested: dict(source, unexpected=1)}),
            ))
            hostile_records.extend(
                (
                    f"missing-{nested}-{field}",
                    dict(
                        receipt,
                        **{nested: {key: value for key, value in source.items() if key != field}},
                    ),
                )
                for field in required
            )
        for label, hostile in hostile_records:
            with self.subTest(receipt_shape=label):
                with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
                    validate_record(
                        hostile,
                        root.tenant_id,
                        frozenset({"bus_epoch_roll_receipt"}),
                        integrity=True,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(
                        hostile, Path("schemas/v1/bus-epoch-roll-receipt.schema.json")
                    )
        archive = Path(str(receipt["archive_path"]))
        self.assertTrue(archive.is_absolute())
        self.assertTrue(archive.is_relative_to(root.tenant_home))
        self.assertNotEqual(root.tenant_home, archive)
        archive_status = archive.lstat()
        self.assertFalse(archive.is_symlink())
        self.assertTrue(stat.S_ISDIR(archive_status.st_mode))
        self.assertEqual(selected, self._archive_files(archive))
        self.assertIn(str(receipt["epoch_id"]), archive.name)
        span = receipt["span"]
        assert isinstance(span, dict)
        self.assertIn(str(span["byte_start"]), archive.name)
        self.assertIn(str(span["byte_end"]), archive.name)
        self._terminal_sequence(receipt)
        return archive

    @staticmethod
    def _terminal_sequence(receipt: Mapping[str, object]) -> int:
        name = Path(str(receipt["archive_path"])).name
        span = receipt["span"]
        assert isinstance(span, dict)
        epoch = re.escape(str(receipt["epoch_id"]))
        self_pattern = rf"(?<![A-Za-z0-9]){epoch}(?![A-Za-z0-9])"
        if re.search(self_pattern, name) is None:
            raise AssertionError("archive basename must token-bind epoch_id")
        start = re.escape(str(span["byte_start"]))
        end = re.escape(str(span["byte_end"]))
        if re.search(rf"(?<!\d){start}\D+{end}(?!\d)", name) is None:
            raise AssertionError("archive basename must token-bind the ordered byte span")
        suffix = re.search(r"(?<!\d)(\d+)$", name)
        if suffix is None:
            raise AssertionError("archive basename must end in one numeric sequence suffix")
        return int(suffix.group(1))

    def _derive_archive_facts(self, archive: Path) -> dict[str, object]:
        files = self._archive_files(archive)
        rows, digest = self._inventory(files)
        return {
            "archive_sha256": digest,
            "archive_file_count": len(rows),
            "plane_counts": {
                plane: sum(row["plane"] == plane for row in rows)
                for plane in ("events", "deliveries", "acks")
            },
            "span": {"byte_start": 0, "byte_end": sum(map(len, files.values()))},
        }

    def _verify_archive_facts(self, archive: Path) -> dict[str, object]:
        expected = self._derive_archive_facts(archive)
        if self._archive_verifier_fn is None:
            module = self._epoch_module()
            matches: list[tuple[Callable[..., object], Callable[[Path], object]]] = []
            values: list[Callable[..., object]] = [
                value for value in vars(module).values() if callable(value)
            ]
            for value in vars(module).values():
                if inspect.isclass(value) and value.__module__ == module.__name__:
                    values.extend(
                        getattr(value, name) for name, member in vars(value).items()
                        if isinstance(member, (staticmethod, classmethod))
                    )
            for value in values:
                if not callable(value):
                    continue
                try:
                    signature = inspect.signature(value)
                except (TypeError, ValueError):
                    continue
                invocations: list[Callable[[Path], object]] = []
                try:
                    signature.bind(archive)
                except TypeError:
                    pass
                else:
                    invocations.append(lambda selected, value=value: value(selected))
                for parameter in signature.parameters.values():
                    try:
                        signature.bind(**{parameter.name: archive})
                    except TypeError:
                        continue
                    invocations.append(
                        lambda selected, value=value, name=parameter.name: value(**{name: selected})
                    )
                if not invocations:
                    continue
                before = self._path_tree_snapshot(archive.parent)
                try:
                    probe = invocations[0](archive)
                except AssertionError:
                    raise
                except Exception:
                    if before != self._path_tree_snapshot(archive.parent):
                        raise AssertionError(
                            "an unrelated verifier candidate mutated the archive domain"
                        )
                    continue
                if before != self._path_tree_snapshot(archive.parent):
                    raise AssertionError("archive verifier must be read-only")
                candidate = probe.get("facts") if isinstance(probe, Mapping) else None
                if candidate is None:
                    candidate = probe
                if isinstance(candidate, Mapping) and set(candidate) == set(expected) and dict(candidate) == expected:
                    matches.append((value, invocations[0]))
            if len(matches) != 1:
                raise AssertionError(
                    "G5 needs one behaviorally discoverable archive-only verifier"
                )
            self._archive_verifier_identity, self._archive_verifier_fn = matches[0]
        before = self._path_tree_snapshot(archive.parent)
        result = self._archive_verifier_fn(archive)
        self.assertEqual(
            before,
            self._path_tree_snapshot(archive.parent),
            "verify must not mutate the archive or any sibling path",
        )
        if isinstance(result, Mapping) and isinstance(result.get("facts"), Mapping):
            result = result["facts"]
        if not isinstance(result, Mapping):
            raise AssertionError("archive-only verifier must return derived facts")
        facts = dict(result)
        self.assertEqual(set(expected), set(facts), "archive verifier core facts are closed")
        if facts != expected:
            raise AssertionError("archive-only verifier must expose every derived receipt fact")
        return facts

    def _assert_archive_verifier_refuses(self, archive: Path) -> None:
        if self._archive_verifier_fn is None:
            raise AssertionError("discover verifier on a valid detached archive first")
        before = self._path_tree_snapshot(archive.parent)
        with self.assertRaises((ProtocolRefusal, IntegrityFailure, OSError, ValueError)):
            self._archive_verifier_fn(archive)
        self.assertEqual(before, self._path_tree_snapshot(archive.parent))

    @staticmethod
    def _epoch_module():
        try:
            return importlib.import_module("floati.bus_epoch")
        except ModuleNotFoundError as exc:
            raise AssertionError("G5 must export floati.bus_epoch for barrier/fault contracts") from exc

    def _public_roll_arguments(
        self, root: FloatiRoot, actor: str, key: str,
    ) -> tuple[object, Callable[[object], object]]:
        from floati.cli import _parser

        try:
            parsed = _parser().parse_args([
                "epoch", "roll", "--root", str(root.path), "--as", actor,
                "--idempotency-key", key,
            ])
        except (ProtocolRefusal, SystemExit) as exc:
            raise AssertionError("G5 must register the public epoch roll CLI") from exc
        handler = getattr(parsed, "handler", None)
        if not callable(handler):
            raise AssertionError("public epoch roll must resolve to an artifact handler")
        return parsed, handler

    @staticmethod
    def _module_callable_targets(module: object) -> list[tuple[object, str, Callable[..., object], str]]:
        targets: list[tuple[object, str, Callable[..., object], str]] = []
        for name, value in vars(module).items():
            if inspect.isfunction(value):
                targets.append((module, name, value, "function"))
            elif inspect.isclass(value) and value.__module__ == module.__name__:
                for member_name, member in vars(value).items():
                    if inspect.isfunction(member):
                        targets.append((value, member_name, member, "method"))
                    elif isinstance(member, staticmethod):
                        targets.append((value, member_name, member.__func__, "staticmethod"))
                    elif isinstance(member, classmethod):
                        targets.append((value, member_name, member.__func__, "classmethod"))
        return targets

    @staticmethod
    def _install_callable_patch(
        stack: ExitStack,
        owner: object,
        name: str,
        original: Callable[..., object],
        kind: str,
        replacement: Callable[..., object],
    ) -> None:
        installed: object = replacement
        if kind == "staticmethod":
            installed = staticmethod(replacement)
        elif kind == "classmethod":
            installed = classmethod(replacement)
        stack.enter_context(mock.patch.object(owner, name, installed))
        if kind != "function":
            return
        patched = {(id(owner), name)}
        for module in tuple(sys.modules.values()):
            if module is None or not str(getattr(module, "__name__", "")).startswith("floati"):
                continue
            for alias, value in tuple(vars(module).items()):
                coordinate = (id(module), alias)
                if value is original and coordinate not in patched:
                    stack.enter_context(mock.patch.object(module, alias, replacement))
                    patched.add(coordinate)

    @staticmethod
    def _patch_floati_callable_identity(
        stack: ExitStack, original: Callable[..., object], replacement: Callable[..., object]
    ) -> None:
        patched: set[tuple[int, str]] = set()
        for module in tuple(sys.modules.values()):
            if module is None or not str(getattr(module, "__name__", "")).startswith("floati"):
                continue
            for name, value in tuple(vars(module).items()):
                coordinate = (id(module), name)
                if value is original and coordinate not in patched:
                    stack.enter_context(mock.patch.object(module, name, replacement))
                    patched.add(coordinate)
                if not inspect.isclass(value) or not str(getattr(value, "__module__", "")).startswith("floati"):
                    continue
                for member_name, member in tuple(vars(value).items()):
                    raw = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
                    coordinate = (id(value), member_name)
                    if raw is not original or coordinate in patched:
                        continue
                    installed: object = replacement
                    if isinstance(member, staticmethod):
                        installed = staticmethod(replacement)
                    elif isinstance(member, classmethod):
                        installed = classmethod(replacement)
                    stack.enter_context(mock.patch.object(value, member_name, installed))
                    patched.add(coordinate)
        if not patched:
            raise AssertionError("callable identity must be installed on a live Floati path")

    def _public_roll_contract(self) -> tuple[object, Callable[..., object]]:
        if self._roll_adapter is not None:
            return self._epoch_module(), self._roll_adapter
        module = self._epoch_module()
        probe = self._new_root(self.base / "public-roll-contract" / self.root.tenant_id)
        actor, key = "actor-a", "public-roll-contract-key"
        parsed, handler = self._public_roll_arguments(probe, actor, key)
        before = self._snapshot(probe)
        calls: list[tuple[object, str, Callable[..., object], str, tuple[object, ...], dict[str, object]]] = []
        targets = self._module_callable_targets(module)

        with ExitStack() as stack:
            for owner, name, original, kind in targets:
                def observe(
                    *args: object,
                    _owner: object = owner,
                    _name: str = name,
                    _original: Callable[..., object] = original,
                    _kind: str = kind,
                    **kwargs: object,
                ) -> object:
                    calls.append((_owner, _name, _original, _kind, args, dict(kwargs)))
                    return _original(*args, **kwargs)

                self._install_callable_patch(
                    stack, owner, name, original, kind, observe
                )
            with self.assertRaises(ProtocolRefusal):
                handler(parsed)
        self.assertEqual(before, self._snapshot(probe), "public ungranted discovery must not mutate")

        matches = []
        for owner, name, original, kind, args, kwargs in calls:
            try:
                signature = inspect.signature(original)
                bound = signature.bind_partial(*args, **kwargs)
            except (TypeError, ValueError):
                continue
            root_parameters = [
                parameter for parameter, value in bound.arguments.items()
                if isinstance(value, FloatiRoot) and value.path == probe.path
            ]
            actor_parameters = [
                parameter for parameter, value in bound.arguments.items() if value == actor
            ]
            key_parameters = [
                parameter for parameter, value in bound.arguments.items() if value == key
            ]
            callback_parameters = [
                parameter.name
                for parameter in signature.parameters.values()
                if (
                    (parameter.name not in bound.arguments and parameter.default is None)
                    or bound.arguments.get(parameter.name, object()) is None
                )
                and parameter.kind not in {
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                }
            ]
            if all((root_parameters, actor_parameters, key_parameters)) and len(callback_parameters) == 1:
                matches.append((owner, name, original, callback_parameters[0], kind))
        if not matches:
            raise AssertionError(
                "the public epoch handler must traverse one deterministic fault-capable roll seam"
            )
        target = matches[0]
        self._roll_target = target

        def adapter(
            root: FloatiRoot,
            *,
            actor: str,
            idempotency_key: str,
            fault: Callable[[str], None],
        ) -> object:
            parsed_call, public_handler = self._public_roll_arguments(
                root, actor, idempotency_key
            )
            owner, name, original, fault_parameter, kind = target
            signature = inspect.signature(original)
            invoked = 0

            def inject(*args: object, **kwargs: object) -> object:
                nonlocal invoked
                invoked += 1
                bound = signature.bind_partial(*args, **kwargs)
                bound.arguments[fault_parameter] = fault
                return original(*bound.args, **bound.kwargs)

            with ExitStack() as stack:
                self._install_callable_patch(
                    stack, owner, name, original, kind, inject
                )
                result = public_handler(parsed_call)
            self.assertEqual(1, invoked, "public epoch handler must use its discovered roll seam once")
            self.assertIsInstance(result, tuple)
            assert isinstance(result, tuple)
            self.assertEqual(3, len(result))
            status, evidence, return_code = result
            self.assertEqual(("ok", 0), (status, return_code))
            self.assertIsInstance(evidence, Mapping)
            return evidence

        self._roll_adapter = adapter
        return module, adapter

    @classmethod
    def _epoch_guard_contract(cls) -> tuple[object, Callable[..., object]]:
        module = cls._epoch_module()
        guards = []
        probe_root = FloatiRoot.open_direct_home(
            Path(tempfile.mkdtemp(dir="\x2fprivate\x2ftmp")) / "epoch-guard-probe", create=True
        )
        for value in vars(module).values():
            if not callable(value):
                continue
            try:
                parameters = tuple(inspect.signature(value).parameters.values())
            except (TypeError, ValueError):
                continue
            manager = None
            for root_parameter in parameters:
                for mode_parameter in parameters:
                    if root_parameter is mode_parameter:
                        continue
                    supplied = {root_parameter.name: probe_root, mode_parameter.name: False}
                    if any(
                        parameter.name not in supplied
                        and parameter.default is inspect.Parameter.empty
                        and parameter.kind not in {
                            inspect.Parameter.VAR_POSITIONAL,
                            inspect.Parameter.VAR_KEYWORD,
                        }
                        for parameter in parameters
                    ):
                        continue
                    try:
                        candidate_manager = value(**supplied)
                    except Exception:
                        continue
                    if hasattr(candidate_manager, "__enter__") and hasattr(candidate_manager, "__exit__"):
                        manager = candidate_manager
                        break
                if manager is not None:
                    break
            if manager is None:
                continue
            referenced_by_writer = any(
                candidate is not None
                and str(getattr(candidate, "__name__", "")).startswith("floati")
                and candidate is not module
                and any(alias is value for alias in vars(candidate).values())
                for candidate in tuple(sys.modules.values())
            )
            if referenced_by_writer:
                guards.append(value)
        shutil.rmtree(probe_root.path.parent)
        if len(guards) != 1:
            raise AssertionError(
                "public roll and ordinary writers must share one root/mode context guard"
            )
        return module, guards[0]

    @staticmethod
    def _patch_guard_references(
        stack: ExitStack,
        module: object,
        guard: Callable[..., object],
        replacement: Callable[..., object],
    ) -> None:
        patched: set[tuple[int, str]] = set()
        for candidate in tuple(sys.modules.values()) + (module,):
            if candidate is None or not str(getattr(candidate, "__name__", "")).startswith("floati"):
                continue
            for name, value in tuple(vars(candidate).items()):
                coordinate = (id(candidate), name)
                if value is guard and coordinate not in patched:
                    stack.enter_context(mock.patch.object(candidate, name, replacement))
                    patched.add(coordinate)
        if not patched:
            raise AssertionError("test must patch the exact dynamic epoch-guard references")

    def _fault_contract(self) -> tuple[tuple[str, ...], Callable[..., object]]:
        _module, roll = self._public_roll_contract()
        if self._fault_boundaries is None:
            root = self._new_root(self.base / "fault-boundary-contract" / self.root.tenant_id)
            self._grant_roll_authority(root, "actor-a")
            self._seed_opaque_triple(root)
            observed: list[str] = []
            roll(
                root,
                actor="actor-a",
                idempotency_key="fault-boundary-contract",
                fault=observed.append,
            )
            if not observed or len(observed) != len(set(observed)):
                raise AssertionError(
                    "public roll must emit one complete ordered unique boundary sequence"
                )
            self._fault_boundaries = tuple(observed)
        return self._fault_boundaries, roll

    @classmethod
    def _assert_no_native_mutation_escape(cls) -> None:
        module = cls._epoch_module()
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        forbidden_modules = {"ctypes", "mmap", "subprocess", "multiprocessing"}
        forbidden_calls = {
            "syscall", "system", "popen", "posix_spawn", "fork", "execv", "execve",
            "makedirs",
        }
        primitive_aliases = {
            "open", "write", "writelines", "truncate", "flush", "fsync", "fdatasync",
            "rename", "replace", "unlink", "remove", "rmdir", "mkdir", "ftruncate",
        }
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                violations.extend(alias.name for alias in node.names
                                  if alias.name.split(".")[0] in forbidden_modules)
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in forbidden_modules:
                violations.append(node.module or "")
            elif isinstance(node, ast.Call):
                called = node.func.attr if isinstance(node.func, ast.Attribute) else (
                    node.func.id if isinstance(node.func, ast.Name) else ""
                )
                if called in forbidden_calls:
                    violations.append(called)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Attribute) and value.attr in primitive_aliases:
                    violations.append("captured-io-alias:" + value.attr)
        for _owner, name, value, _kind in cls._module_callable_targets(module):
            captured = tuple(value.__defaults__ or ()) + tuple((value.__kwdefaults__ or {}).values())
            if value.__closure__:
                captured += tuple(cell.cell_contents for cell in value.__closure__)
            if any(callable(item) and getattr(item, "__name__", "") in primitive_aliases
                   for item in captured):
                violations.append("captured-io-default:" + name)
        if violations:
            raise AssertionError(
                "G5 roll bypasses test-observable Python filesystem primitives: "
                + ", ".join(sorted(violations))
            )

    @staticmethod
    def _receipt_from_roll_result(result: object) -> dict[str, object]:
        if isinstance(result, dict) and result.get("kind") == "bus_epoch_roll_receipt":
            return result
        if isinstance(result, dict) and isinstance(result.get("receipt"), dict):
            return result["receipt"]  # type: ignore[return-value]
        raise AssertionError("exported roll must return or directly bind its receipt")

    @staticmethod
    def _reader_anchor_inventory() -> tuple[_ReaderAnchor, ...]:
        discovered: list[_ReaderAnchor] = []
        replay_snapshots: list[Path] = []
        interesting_leaves = {
            "SnapshotStore", "SparseCursor", "_relative_path_for",
            "_delivery_relative_path_for", "state_for", "acked_ids",
            "read_records_with_prefix_digests", "evaluate",
        }
        for path in sorted((REPOSITORY_ROOT / "floati").rglob("*.py")):
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            def owner(node: ast.AST) -> str:
                names = []
                cursor = node
                while cursor in parents:
                    cursor = parents[cursor]
                    if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.append(cursor.name)
                return ".".join(reversed(names))

            candidates: list[tuple[str, str, ast.AST]] = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = ast.unparse(node.func)
                leaf = target.rsplit(".", 1)[-1]
                if leaf not in interesting_leaves:
                    continue
                selected = False
                if leaf == "SnapshotStore":
                    if relative == "floati/replay.py":
                        replay_snapshots.append(path)
                        continue
                    selected = True
                elif leaf in {"SparseCursor", "_relative_path_for", "_delivery_relative_path_for",
                              "state_for", "acked_ids"}:
                    selected = relative in {
                        "floati/cli.py", "floati/conformance.py", "floati/cursor.py",
                        "floati/demo.py", "floati/events.py", "floati/lane_scaling.py",
                        "floati/tui.py", "floati/wake_hold.py",
                    }
                elif leaf == "read_records_with_prefix_digests":
                    selected = relative == "floati/wake_hold.py"
                elif leaf == "evaluate":
                    selected = (
                        relative == "floati/codex_wait.py"
                        and target == "controller.evaluate"
                    ) or (
                        relative == "floati/wake_daemon.py" and target == "controller.evaluate"
                    )
                if not selected:
                    continue
                candidates.append((owner(node), target, node))
            for qualified in (
                "iter_deltas", "DoctorProbe._drained", "run_stop_waiter",
                "WakeDaemon.run_cycle", "WakeDaemon.serve",
            ):
                module_name, _, symbol = qualified.partition(".")
                expected_file = {
                    "iter_deltas": "floati/projection.py",
                    "DoctorProbe": "floati/doctor_probe.py",
                    "run_stop_waiter": "floati/codex_wait.py",
                    "WakeDaemon": "floati/wake_daemon.py",
                }[module_name]
                if relative != expected_file:
                    continue
                target_name = symbol or module_name
                definitions = [node for node in ast.walk(tree)
                               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                               and node.name == target_name]
                for definition in definitions:
                    candidates.append((qualified, "<definition>", definition))
            ordinals: defaultdict[tuple[str, str], int] = defaultdict(int)
            for selected_owner, target, node in sorted(
                candidates, key=lambda row: (row[2].lineno, row[2].col_offset)
            ):
                ordinals[(selected_owner, target)] += 1
                discovered.append(_ReaderAnchor(
                    relative, selected_owner, target,
                    ordinals[(selected_owner, target)], node.lineno, node.col_offset,
                ))
        from floati.replay import REPLAY_SOURCES
        selected = tuple(Path(value) for value in (
            "events.jsonl", "receipts/deliveries", "receipts/acks"
        ))
        if any(source == plane or plane in source.parents
               for source, _ in REPLAY_SOURCES for plane in selected):
            raise AssertionError("replay snapshot sources must stay disjoint from rolled planes")
        if len(replay_snapshots) != 1:
            raise AssertionError("all SnapshotStore constructors must be classified explicitly")
        return tuple(discovered)

    def _run_live_reader_fixture(
        self, label: str, test: Callable[[], None]
    ) -> dict[str, object]:
        """Run a real migration scenario on an isolated root and return evidence."""
        original = (
            self.base, self.root, self._discovered_authority_subject,
            self._archive_verifier_fn, self._archive_verifier_identity,
            self._roll_adapter, self._fault_boundaries,
            self._roll_target,
        )
        fixture_base = original[0] / ("reader-dispatch-" + label)  # type: ignore[operator]
        fixture_base.mkdir()
        self.base = fixture_base
        self.root = self._new_root(fixture_base / "epoch-tenant")
        self._archive_verifier_fn = None
        self._archive_verifier_identity = None
        self._roll_adapter = None
        self._fault_boundaries = None
        self._roll_target = None
        receipts: list[dict[str, object]] = []
        real_roll = self._roll_success

        def recording_roll(*args: object, **kwargs: object) -> dict[str, object]:
            receipt = real_roll(*args, **kwargs)  # type: ignore[arg-type]
            receipts.append(receipt)
            return receipt

        try:
            with mock.patch.object(self, "_roll_success", recording_roll):
                test()
            self.assertTrue(receipts, f"{label} fixture must perform a live roll")
            return {
                "root": str(self.root.path),
                "receipts": tuple(receipts),
                "live_ids": tuple(str(row["id"]) for row in EventLog(self.root).records()),
            }
        finally:
            (
                self.base, self.root, self._discovered_authority_subject,
                self._archive_verifier_fn, self._archive_verifier_identity,
                self._roll_adapter, self._fault_boundaries,
                self._roll_target,
            ) = original

    def _fixture_snapshot_family(self) -> dict[str, object]:
        return self._run_live_reader_fixture(
            "snapshot", self.test_snapshot_cursor_and_wake_readers_detect_archive_and_rebuild
        )

    def _fixture_sparse_family(self) -> dict[str, object]:
        return self._run_live_reader_fixture(
            "sparse", self.test_snapshot_cursor_and_wake_readers_detect_archive_and_rebuild
        )

    def _fixture_wake_prefix_family(self) -> dict[str, object]:
        return self._run_live_reader_fixture(
            "wake", self.test_snapshot_cursor_and_wake_readers_detect_archive_and_rebuild
        )

    def _fixture_watch_probe_family(self) -> dict[str, object]:
        return self._run_live_reader_fixture(
            "watch", self.test_watch_probe_waiter_and_daemon_loops_observe_post_roll_mail
        )

    def _fixture_waiter_family(self) -> dict[str, object]:
        return self._run_live_reader_fixture(
            "waiter", self.test_watch_probe_waiter_and_daemon_loops_observe_post_roll_mail
        )

    def _fixture_daemon_family(self) -> dict[str, object]:
        return self._run_live_reader_fixture(
            "daemon", self.test_watch_probe_waiter_and_daemon_loops_observe_post_roll_mail
        )

    def _fixture_tail_family(self) -> dict[str, object]:
        return self._run_live_reader_fixture(
            "tail", self.test_external_tail_f_follower_reopens_recreated_event_path
        )

    def test_same_day_rolls_allocate_content_stating_archives_and_closed_receipts(self) -> None:
        controls = self._seed_control_state()
        selected_one = self._seed_opaque_triple()
        sentinel = self.root.resolve_relative("archive-existing-sentinel")
        sentinel.mkdir()
        sentinel_file = self._write(self.root, "archive-existing-sentinel/foreign.bin", b"keep\n")
        sentinel_before = (self._identity(sentinel), self._identity(sentinel_file))
        empty_paths = (self.root.resolve_relative("receipts/deliveries/empty-directory"),
                       self.root.resolve_relative("receipts/acks/empty-directory"))
        empty_before = {path: self._identity(path) for path in empty_paths}
        control_before = {path: self._identity(path) for path in controls}
        lock_before = {
            path: self._identity(path) for path in self.root.tenant_home.rglob("*.lock")
        }

        first = self._roll_success(key="same-day-one")
        first_archive = self._assert_receipt(self.root, first, selected_one)
        self._terminal_sequence(first)
        detached = self.base / "detached-archive"
        shutil.copytree(first_archive, detached)
        archived_files = {
            path.relative_to(detached).as_posix()
            for path in detached.rglob("*") if path.is_file()
        }
        self.assertTrue(all(
            relative == "events.jsonl"
            or relative.startswith("receipts/deliveries/")
            or relative.startswith("receipts/acks/")
            for relative in archived_files
        ), "every archive file must belong to one declared receipt plane")
        detached_before = self._path_tree_snapshot(detached)
        verified = self._verify_archive_facts(detached)
        plane_counts = verified["plane_counts"]
        assert isinstance(plane_counts, Mapping)
        self.assertEqual(len(archived_files), sum(map(int, plane_counts.values())))
        expected_verified = self._derive_archive_facts(detached)
        self.assertTrue(all(verified.get(key) == value
                            for key, value in expected_verified.items()))
        self.assertTrue(all(first[key] == value
                            for key, value in expected_verified.items()))
        self.assertEqual(detached_before, self._path_tree_snapshot(detached))
        mutated = self.base / "mutated-archive"
        shutil.copytree(first_archive, mutated)
        mutated_file = next(path for path in mutated.rglob("*.jsonl") if path.is_file())
        mutated_bytes = bytearray(mutated_file.read_bytes())
        mutated_bytes[0] ^= 1
        mutated_file.write_bytes(mutated_bytes)
        mutated_facts = self._verify_archive_facts(mutated)
        self.assertNotEqual(verified["archive_sha256"], mutated_facts["archive_sha256"])
        for field in ("archive_file_count", "plane_counts", "span"):
            self.assertEqual(verified[field], mutated_facts[field])
        extended = self.base / "extended-archive"
        shutil.copytree(first_archive, extended)
        ack_source = self._new_root(self.base / "archive-verifier-ack-source")
        ack_message = EventLog(ack_source).send(
            "actor-a", "recipient", "floati", "f" * 40,
            "docs/evidence/archive-verifier-ack.md", "valid detached ack",
            idempotency_key="archive-verifier-ack-message",
        )
        EventLog(ack_source).present("recipient")
        ack_record = SparseCursor(ack_source).ack(
            "recipient", [str(ack_message["id"])],
            acting_session_id="archive-verifier-ack-session",
        )
        added_payload = encode_frame(ack_record)
        added = extended / "receipts" / "acks" / "added.jsonl"
        added.parent.mkdir(parents=True, exist_ok=True)
        added.write_bytes(added_payload)
        extended_facts = self._verify_archive_facts(extended)
        self.assertNotEqual(verified["archive_sha256"], extended_facts["archive_sha256"])
        self.assertEqual(int(verified["archive_file_count"]) + 1,
                         extended_facts["archive_file_count"])
        expected_planes = dict(verified["plane_counts"])  # type: ignore[arg-type]
        expected_planes["acks"] += 1
        self.assertEqual(expected_planes, extended_facts["plane_counts"])
        expected_span = dict(verified["span"])  # type: ignore[arg-type]
        expected_span["byte_end"] += len(added_payload)
        self.assertEqual(expected_span, extended_facts["span"])
        for hostile_kind in (
            "root-symlink", "root-file", "symlink", "directory", "socket", "out-of-family",
        ):
            with self.subTest(archive_verify_hostile=hostile_kind):
                hostile = self.base / f"{hostile_kind}-archive"
                if hostile_kind == "root-symlink":
                    hostile.symlink_to(first_archive, target_is_directory=True)
                elif hostile_kind == "root-file":
                    hostile.write_bytes(b"not an archive directory\n")
                else:
                    shutil.copytree(first_archive, hostile)
                if hostile_kind == "symlink":
                    (hostile / "receipts" / "acks" / "linked.jsonl").symlink_to(mutated_file)
                elif hostile_kind == "directory":
                    (hostile / "receipts" / "acks" / "directory.jsonl").mkdir()
                elif hostile_kind == "socket":
                    socket_path = hostile / "receipts" / "acks" / "socket.jsonl"
                    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    listener.bind(str(socket_path))
                    self.addCleanup(listener.close)
                elif hostile_kind == "out-of-family":
                    (hostile / "foreign.jsonl").write_bytes(b"foreign\n")
                self._assert_archive_verifier_refuses(hostile)
        self.assertEqual(("actor-a", "same-day-one"),
                         (first["actor"], first["idempotency_key"]))
        self.assertEqual(empty_before, {path: self._identity(path) for path in empty_paths})
        self.assertEqual(control_before, {path: self._identity(path) for path in controls})
        self.assertEqual(lock_before, {path: self._identity(path) for path in lock_before})
        self.assertFalse(any(path.suffix == ".lock" for path in first_archive.rglob("*")))
        EventLog(self.root).send(
            "actor-a", "recipient", "floati", "a" * 40,
            "docs/evidence/second-epoch.md", "second epoch",
            idempotency_key="second-epoch-message",
        )
        selected_two = {"events.jsonl": self.root.resolve_relative("events.jsonl").read_bytes()}
        control_home = self.base / "allocator-control" / self.root.tenant_id
        control_home.parent.mkdir()
        shutil.copytree(self.root.tenant_home, control_home)
        control = FloatiRoot.open_direct_home(control_home)
        fixed_now = "2026-08-30T18:00:00.000Z"
        import floati.registry as registry_module
        import floati.records as records_module
        real_now = registry_module.utc_now
        real_uuid = records_module.uuid7_hex
        uuid_index = 0
        verifier_identity = self._archive_verifier_identity
        self.assertIsNotNone(verifier_identity)
        assert verifier_identity is not None
        verifier_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def frozen_now() -> str:
            return fixed_now

        def frozen_uuid() -> str:
            nonlocal uuid_index
            uuid_index += 1
            return f"018f7e9b3c127abc8def{uuid_index:012x}"

        def observed_verify(*args: object, **kwargs: object) -> object:
            verifier_calls.append((args, dict(kwargs)))
            return verifier_identity(*args, **kwargs)

        with ExitStack() as stack:
            self._install_callable_patch(
                stack, registry_module, "utc_now", real_now, "function", frozen_now
            )
            self._install_callable_patch(
                stack, records_module, "uuid7_hex", real_uuid, "function", frozen_uuid
            )
            self._patch_floati_callable_identity(stack, verifier_identity, observed_verify)
            uuid_index = 0
            control_second = self._roll_success_in_process(
                control, actor="actor-a", key="same-day-two"
            )
            control_archive = Path(str(control_second["archive_path"]))
            candidate_relative = control_archive.relative_to(control.tenant_home)
            reserved = self.root.resolve_relative(candidate_relative)
            reserved_file = reserved / "reserved.bin"
            reserved_before: tuple[tuple[object, ...], tuple[object, ...]] | None = None
            collision_attempted = False
            original_mkdir = os.mkdir

            def inject_collision(path: object, *args: object, **kwargs: object) -> object:
                nonlocal reserved_before, collision_attempted
                candidate = Path(os.fspath(path))  # type: ignore[arg-type]
                if not candidate.is_absolute():
                    candidate = Path.cwd() / candidate
                if candidate.resolve(strict=False) == reserved.resolve(strict=False)\
                        and not collision_attempted:
                    collision_attempted = True
                    original_mkdir(reserved)
                    reserved_file.write_bytes(b"reserved actual allocator candidate\n")
                    reserved_before = (self._identity(reserved), self._identity(reserved_file))
                return original_mkdir(path, *args, **kwargs)

            uuid_index = 0
            with ExitStack() as collision_stack:
                self._install_callable_patch(
                    collision_stack, os, "mkdir", original_mkdir, "function", inject_collision
                )
                second = self._roll_success_in_process(
                    self.root, actor="actor-a", key="same-day-two"
                )
            empty = self._new_root(self.base / "empty-epoch" / self.root.tenant_id)
            empty.resolve_relative("events.jsonl").write_bytes(b"")
            empty_receipt = self._roll_success_in_process(
                empty, actor="actor-a", key="empty-epoch"
            )
        self.assertGreaterEqual(
            len(verifier_calls), 3,
            "the same behaviorally discovered verifier must be consumed by live rolls",
        )
        second_archive = self._assert_receipt(self.root, second, selected_two)
        self.assertTrue(collision_attempted, "allocator must attempt the observed occupied candidate")
        self.assertIsNotNone(reserved_before)
        assert reserved_before is not None
        self.assertNotEqual(first_archive, second_archive)
        self.assertNotEqual(reserved, second_archive)
        self.assertTrue(first_archive.is_dir() and second_archive.is_dir())
        second_sequence = self._terminal_sequence(second)
        reserved_sequence = self._terminal_sequence(control_second)
        self.assertNotEqual(reserved_sequence, second_sequence)
        self.assertNotEqual(self._archive_files(first_archive), self._archive_files(second_archive))
        self.assertEqual(selected_two, self._archive_files(second_archive))
        self.assertEqual(reserved_before, (self._identity(reserved), self._identity(reserved_file)))
        self.assertEqual(sentinel_before, (self._identity(sentinel), self._identity(sentinel_file)))
        empty_archive = Path(str(empty_receipt["archive_path"]))
        self.assertEqual({"events.jsonl": b""}, self._archive_files(empty_archive))
        self.assertEqual(
            {"archive_file_count": 1, "plane_counts": {"events": 1, "deliveries": 0, "acks": 0},
             "span": {"byte_start": 0, "byte_end": 0}},
            {key: empty_receipt[key] for key in ("archive_file_count", "plane_counts", "span")},
        )
        empty_facts = self._derive_archive_facts(empty_archive)
        self.assertEqual(empty_facts["archive_sha256"], empty_receipt["archive_sha256"])

        fail_closed = self._new_root(self.base / "verifier-fail-closed" / self.root.tenant_id)
        self._grant_roll_authority(fail_closed, "actor-a")
        self._seed_opaque_triple(fail_closed)
        fail_before = self._snapshot(fail_closed)

        def corrupted_verify(*args: object, **kwargs: object) -> object:
            result = verifier_identity(*args, **kwargs)
            if isinstance(result, Mapping) and isinstance(result.get("facts"), Mapping):
                return dict(result, facts=dict(result["facts"], archive_file_count=-1))
            return dict(result, archive_file_count=-1)  # type: ignore[arg-type]

        with ExitStack() as stack:
            self._patch_floati_callable_identity(stack, verifier_identity, corrupted_verify)
            with self.assertRaises((ProtocolRefusal, IntegrityFailure)):
                self._public_roll_contract()[1](
                    fail_closed, actor="actor-a", idempotency_key="verifier-fail-closed",
                    fault=lambda _name: None,
                )
        self.assertEqual(fail_before, self._snapshot(fail_closed))

    def test_grant_authority_and_key_validation_are_typed_before_first_write(self) -> None:
        subject = self._roll_authority_subject()
        codes: dict[str, str] = {}
        for label, actor, direct_grant in (
            ("lexical", "bad/actor", False),
            ("unregistered-ungranted", "unknown-actor", False),
            ("unregistered-granted", "unknown-actor", True),
            ("retired-granted", "retired-actor", True),
            ("registered-ungranted", "actor-a", False),
        ):
            with self.subTest(label=label):
                root = self._new_root(self.base / f"authority-{label}")
                self._seed_opaque_triple(root)
                if direct_grant:
                    AuthorityGrantStore(root).grant_exact(
                        subject, actor, 1, datetime.now(timezone.utc)
                    )
                expected_code = {
                    "lexical": "actor_invalid",
                    "unregistered-ungranted": "unknown_node",
                    "unregistered-granted": "unknown_node",
                    "retired-granted": "unknown_node",
                    "registered-ungranted": "authority_missing",
                }[label]
                before = self._snapshot(root)
                artifact = self._assert_zero_mutation_refusal(
                    root, actor=actor, key="authority-key", code=expected_code)
                evidence = artifact["evidence"]
                assert isinstance(evidence, dict)
                codes[label] = str(evidence["code"])
                self.assertEqual(before, self._snapshot(root))
        for key_index, key in enumerate((
            "", "x" * 129, "bad\nkey", "bad\u0085key", "bad\ud800key", "bad\u202ekey",
        )):
            with self.subTest(key=repr(key)):
                root = self._new_root(self.base / f"invalid-key-{key_index}")
                self._grant_roll_authority(root, "actor-a")
                self._seed_opaque_triple(root)
                before = self._snapshot(root)
                if "\ud800" in key:
                    parsed, handler = self._public_roll_arguments(root, "actor-a", key)
                    domain_before = (
                        self._identity(root.tenant_home), self._path_tree_snapshot(root.path.parent)
                    )
                    probe = _MutationProbe(root, stack_prefix="floati")
                    with probe.installed():
                        with self.assertRaises(ProtocolRefusal) as caught:
                            handler(parsed)
                    self.assertEqual("idempotency_key_invalid", caught.exception.code)
                    self.assertEqual([], probe.events)
                    self.assertEqual(
                        domain_before,
                        (self._identity(root.tenant_home), self._path_tree_snapshot(root.path.parent)),
                    )
                else:
                    self._assert_zero_mutation_refusal(
                        root, actor="actor-a", key=key, code="idempotency_key_invalid")
                self.assertEqual(before, self._snapshot(root))

        for state in ("wrong-holder", "revoked", "expired"):
            with self.subTest(grant_state=state):
                root = self._new_root(self.base / f"authority-{state}")
                self._seed_opaque_triple(root)
                grant = self._grant_roll_authority(
                    root, "actor-b" if state == "wrong-holder" else "actor-a"
                )
                if state == "revoked":
                    AuthorityGrantStore(root).revoke_exact(
                        subject,
                        "actor-a",
                        int(grant["epoch"]),
                        datetime.now(timezone.utc),
                    )
                elif state == "expired":
                    expiry = datetime.fromisoformat(
                        str(grant["expires_at"]).replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    AuthorityGrantStore(root).expire(
                        subject,
                        "actor-a",
                        int(grant["epoch"]),
                        expiry - timedelta(microseconds=1),
                    )
                before = self._snapshot(root)
                artifact = self._assert_zero_mutation_refusal(
                    root, actor="actor-a", key=f"authority-{state}",
                    code={"wrong-holder": "holder_mismatch", "revoked": "authority_released",
                          "expired": "authority_expired"}[state],
                )
                evidence = artifact["evidence"]
                assert isinstance(evidence, dict)
                codes[state] = str(evidence["code"])
                self.assertEqual(before, self._snapshot(root))

        with self.subTest(grant_state="granted"):
            root = self._new_root(self.base / "authority-granted")
            grant = self._grant_roll_authority(root, "actor-a")
            self._seed_opaque_triple(root)
            grant_path = AuthorityGrantStore(root).path_for(subject)
            grant_before = self._identity(grant_path)
            receipt = self._roll_success(
                root=root, actor="actor-a", key="authority-granted"
            )
            self.assertEqual("actor-a", grant["holder"])
            self.assertEqual("actor-a", receipt["actor"])
            self.assertEqual(grant_before, self._identity(grant_path))
        with self.subTest(grant_state="distinct-refusal-codes"):
            self.assertEqual("unknown_node", codes.get("unregistered-granted"))
            self.assertEqual("authority_missing", codes.get("registered-ungranted"))

    def test_symlink_and_nonregular_selected_members_refuse_with_zero_mutation(self) -> None:
        self._roll_authority_subject()
        expected_codes = {
            "symlink": "epoch_selected_member_symlink",
            "directory": "epoch_selected_member_not_regular",
            "socket": "epoch_selected_member_not_regular",
            "fifo": "epoch_selected_member_not_regular",
        }
        for member_kind in ("symlink", "directory", "socket", "fifo"):
            with self.subTest(member_kind=member_kind):
                root = self._new_root(self.base / f"hostile-{member_kind}")
                self._grant_roll_authority(root, "actor-a")
                self._seed_opaque_triple(root)
                selected = root.resolve_relative("receipts/acks/hostile.jsonl")
                foreign = self.base / f"foreign-{member_kind}.jsonl"
                if member_kind == "symlink":
                    foreign.write_bytes(b"foreign target\n")
                    selected.symlink_to(foreign)
                    foreign_before = self._identity(foreign)
                else:
                    if member_kind == "directory":
                        selected.mkdir()
                    elif member_kind == "socket":
                        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        listener.bind(str(selected))
                        self.addCleanup(listener.close)
                    else:
                        os.mkfifo(selected)
                    foreign_before = None
                before = self._snapshot(root)
                artifact = self._assert_refusal(
                    self._run_roll(root=root, key=f"hostile-{member_kind}"), root=root,
                    contract=f"selected {member_kind} refusal must be root-bound")
                evidence = artifact["evidence"]
                assert isinstance(evidence, dict)
                self.assertEqual(expected_codes[member_kind], evidence["code"])
                self.assertEqual(before, self._snapshot(root))
                if foreign_before is not None:
                    self.assertEqual(foreign_before, self._identity(foreign))

    def _assert_retry_noop(
        self,
        *,
        first: Mapping[str, object],
        stable: tuple[tuple[object, ...], ...],
    ) -> dict[str, object]:
        archive_count = sum(
            child.is_dir() and child.name.startswith("archive")
            for child in self.root.tenant_home.iterdir()
        )
        frame_count = len(
            decode_frames(self.root.resolve_relative("events.jsonl").read_bytes())
        )
        result = self._run_roll(key="K1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        artifact = self._artifact(result)
        self.assertEqual(
            json.dumps(
                artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ) + "\n",
            result.stdout,
        )
        self.assertEqual({"artifact_version", "command", "status", "evidence"}, set(artifact))
        self.assertEqual(
            (0, "epoch", "ok"),
            (artifact["artifact_version"], artifact["command"], artifact["status"]),
        )
        evidence = artifact["evidence"]
        self.assertIsInstance(evidence, dict)
        assert isinstance(evidence, dict)
        self.assertEqual({"root", "tenant_id", "no_op", "original"}, set(evidence))
        self.assertEqual(str(self.root.path.resolve()), evidence["root"])
        self.assertEqual(self.root.tenant_id, evidence["tenant_id"])
        self.assertIs(True, evidence["no_op"])
        self.assertEqual(
            {
                "actor": "actor-a",
                "idempotency_key": "K1",
                "receipt_id": first["id"],
            },
            evidence["original"],
        )
        self.assertEqual(stable, self._snapshot(self.root))
        self.assertEqual(
            archive_count,
            sum(
                child.is_dir() and child.name.startswith("archive")
                for child in self.root.tenant_home.iterdir()
            ),
        )
        self.assertEqual(
            frame_count,
            len(decode_frames(self.root.resolve_relative("events.jsonl").read_bytes())),
        )
        return artifact

    def test_immediate_and_later_k1_retries_are_closed_identity_preserving_no_ops(self) -> None:
        self._seed_opaque_triple()
        first = self._roll_success(key="K1")
        immediate_stable = self._snapshot(self.root)
        immediate = self._assert_retry_noop(first=first, stable=immediate_stable)
        EventLog(self.root).send(
            "actor-a", "recipient", "floati", "b" * 40, "docs/evidence/k2.md",
            "new epoch work", idempotency_key="k2-message")
        self._roll_success(key="K2")
        later_stable = self._snapshot(self.root)
        later = self._assert_retry_noop(first=first, stable=later_stable)
        self.assertEqual(immediate, later, "immediate and later retries share one response contract")

        self._grant_roll_authority(self.root, "actor-b")
        cross_actor_stable = self._snapshot(self.root)
        self._assert_refusal(
            self._run_roll(actor="actor-b", key="K1"),
            contract="cross-actor completed-key conflict must be typed",
        )
        self.assertEqual(cross_actor_stable, self._snapshot(self.root))

    def test_roll_receipt_is_inert_only_as_unique_physical_record_one(self) -> None:
        message = EventLog(self.root).send(
            "actor-a", "recipient", "floati", "c" * 40,
            "docs/evidence/position.md", "position message",
            idempotency_key="position-message")
        message_frame = encode_frame(dict(message["message"]))
        detached_archive = self.root.resolve_relative("archives/position-seed")
        detached_archive.mkdir(parents=True)
        opaque_archive_event = detached_archive / "events.jsonl"
        opaque_archive_event.write_bytes(b"opaque archived event bytes\n")
        archive_facts = self._derive_archive_facts(detached_archive)
        detached_status = detached_archive.lstat()
        self.assertFalse(detached_archive.is_symlink())
        self.assertTrue(stat.S_ISDIR(detached_status.st_mode))
        self.assertTrue(detached_archive.is_relative_to(self.root.tenant_home))
        self.assertNotEqual(self.root.tenant_home, detached_archive.parent)
        receipt = {
            "schema_version": 1,
            "id": "bus-epoch-roll-receipt-018f7e9b3c117abc8def0123456789ab",
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-29T12:00:00.000Z",
            "kind": "bus_epoch_roll_receipt",
            "archive_path": str(detached_archive),
            "actor": "actor-a", "idempotency_key": "position-roll",
            "invalidated_followers": FOLLOWER_CLASSES, "epoch_id": "epoch-a",
            **archive_facts,
        }
        receipt_frame = encode_frame(receipt)
        second_archive = self.root.resolve_relative("archives/position-second")
        shutil.copytree(detached_archive, second_archive)
        second = dict(receipt,
                      id="bus-epoch-roll-receipt-018f7e9b3c127abc8def0123456789ac",
                      idempotency_key="position-roll-second", epoch_id="epoch-b",
                      archive_path=str(second_archive))
        ledger = self.root.resolve_relative("events.jsonl")
        ledger.write_bytes(receipt_frame + message_frame)
        try:
            rows = EventLog(self.root).event_records()
        except (IntegrityFailure, ProtocolRefusal) as exc:
            self.fail("G5 must accept one first-position roll receipt: " + exc.code)
        self.assertEqual(["bus_epoch_roll_receipt", "message_envelope"],
                         [row["kind"] for row in rows])
        self.assertEqual([message["id"]], [row["id"] for row in EventLog(self.root).records()])
        for hostile in (message_frame + receipt_frame,
                        receipt_frame + encode_frame(second) + message_frame):
            with self.subTest(hostile=hostile[:32]):
                ledger.write_bytes(hostile)
                with self.assertRaises(IntegrityFailure):
                    EventLog(self.root).event_records()

    def test_derived_writers_and_sealed_bypasses_participate_in_epoch_barrier(self) -> None:
        from floati import jsonl
        from floati.ledger_repair import LedgerRepair
        from floati.wake_hold import wake_coordination_guard

        tree = ast.parse(Path(jsonl.__file__).read_text(encoding="utf-8"))
        reaches_append = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "_append_frame" for call in ast.walk(node))
        }
        fixed_non_triple = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in reaches_append
            and {
                child.id for child in ast.walk(node) if isinstance(child, ast.Name)
            } & {"_EFFECT_RECORDS_RELATIVE", "_THREAD_OBSERVATION_RECORDS_RELATIVE"}
        }
        self.assertEqual(
            {"_transact_effect_records", "_transact_thread_observation_records"},
            fixed_non_triple,
            "only fixed non-triple sealed stores are exempt from the epoch barrier",
        )
        derived_triple = reaches_append - fixed_non_triple
        direct_external_append = set()
        bypasses = set()
        for path in sorted((REPOSITORY_ROOT / "floati").rglob("*.py")):
            if path == Path(jsonl.__file__):
                continue
            module_tree = ast.parse(path.read_text(encoding="utf-8"))
            module_constants = {
                node.targets[0].id: node.value.value
                for node in module_tree.body
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            }
            module_selected = any(
                value in {"events.jsonl", "receipts/deliveries", "receipts/acks"}
                for value in module_constants.values()
            )
            for node in ast.walk(module_tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = {
                    call.func.attr if isinstance(call.func, ast.Attribute) else
                    call.func.id if isinstance(call.func, ast.Name) else ""
                    for call in ast.walk(node) if isinstance(call, ast.Call)
                }
                if "_append_frame" in calls and module_selected:
                    direct_external_append.add(f"{path.relative_to(REPOSITORY_ROOT)}::{node.name}")
                def selected_expression(expression: ast.AST, tainted: set[str]) -> bool:
                    return any(
                        (
                            isinstance(child, ast.Name)
                            and (
                                child.id in tainted
                                or module_constants.get(child.id) in {
                                    "events.jsonl", "receipts/deliveries", "receipts/acks"
                                }
                            )
                        )
                        or (
                            isinstance(child, ast.Constant)
                            and isinstance(child.value, str)
                            and (
                                child.value == "events.jsonl"
                                or child.value.startswith("receipts/deliveries")
                                or child.value.startswith("receipts/acks")
                            )
                        )
                        for child in ast.walk(expression)
                    )

                tainted: set[str] = set()
                changed = True
                while changed:
                    changed = False
                    for child in ast.walk(node):
                        if isinstance(child, (ast.Assign, ast.AnnAssign)):
                            value = child.value
                            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                        elif isinstance(child, ast.With):
                            value = None
                            targets = [item.optional_vars for item in child.items]
                            for item in child.items:
                                if item.optional_vars is not None and selected_expression(
                                    item.context_expr, tainted
                                ):
                                    targets = [item.optional_vars]
                                    value = item.context_expr
                                    break
                        else:
                            continue
                        if value is None or not selected_expression(value, tainted):
                            continue
                        for target in targets:
                            if isinstance(target, ast.Name) and target.id not in tainted:
                                tainted.add(target.id)
                                changed = True
                mutation_calls = {
                    "write", "writelines", "write_bytes", "write_text",
                    "truncate", "ftruncate", "replace", "rename", "unlink",
                    "remove", "rmdir", "mkdir", "makedirs",
                }
                selected_mutation = False
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    called = call.func.attr if isinstance(call.func, ast.Attribute) else (
                        call.func.id if isinstance(call.func, ast.Name) else ""
                    )
                    if called not in mutation_calls:
                        continue
                    operands = list(call.args) + [keyword.value for keyword in call.keywords]
                    if isinstance(call.func, ast.Attribute):
                        operands.append(call.func.value)
                    if any(selected_expression(operand, tainted) for operand in operands):
                        selected_mutation = True
                        break
                if selected_mutation:
                    owner = next((parent.name for parent in module_tree.body
                                  if isinstance(parent, ast.ClassDef) and node in parent.body), None)
                    bypasses.add(
                        f"{path.stem}.{owner + '.' if owner else ''}{node.name}"
                    )
        self.assertFalse(
            direct_external_append,
            "a direct _append_frame route outside jsonl must be classified before GREEN",
        )
        self.assertEqual({"ledger_repair.LedgerRepair.quarantine"}, bypasses)
        exercised = derived_triple | {"ledger_repair.LedgerRepair.quarantine"}

        def pending(root: FloatiRoot, name: str) -> Callable[[], object]:
            template = self._new_root(
                self.base / ("writer-template-" + re.sub(r"\W", "-", name))
                / root.tenant_id
            )
            envelope = dict(EventLog(template).send(
                "actor-a", "recipient", "floati", "d" * 40,
                "docs/evidence/barrier.md", "pending mutation",
                idempotency_key="pending-" + re.sub(r"\W", "-", name),
            )["message"])
            if name == "append_record":
                return lambda: jsonl.append_record(
                    root, "events.jsonl", envelope, allowed_kinds={"message_envelope"})
            if name == "transact":
                return lambda: jsonl.transact(
                    root, "events.jsonl", lambda rows: ("appended", envelope),
                    allowed_kinds={"message_envelope"})
            if name == "transact_records":
                return lambda: jsonl.transact_records(
                    root, "events.jsonl", lambda rows: ("appended", (envelope,)),
                    allowed_kinds={"message_envelope"})
            if name == "transact_exact_frame":
                return lambda: jsonl.transact_exact_frame(
                    root, "events.jsonl", lambda rows, frames: ("appended", envelope),
                    allowed_kinds={"message_envelope"})
            if name == "_transact_wake_hold_records":
                EventLog(root).send(
                    "actor-a", "recipient", "floati", "e" * 40,
                    "docs/evidence/wake-pending.md", "wake pending",
                    idempotency_key="wake-pending")
                return lambda: WakeHoldController(root).evaluate(
                    "recipient", idempotency_key="barrier-hold")
            if name == "ledger_repair.LedgerRepair.quarantine":
                bad_id = "registry-018f7e9b3c127abc8def0123456789ac"
                bad = encode_frame({
                    "schema_version": 0, "id": bad_id, "tenant_id": root.tenant_id,
                    "timestamp": "2026-08-29T12:00:00.000Z", "kind": "registry_entry",
                    "node_id": "bad", "state": "active",
                })
                root.resolve_relative("events.jsonl").write_bytes(encode_frame(envelope) + bad)
                root.resolve_relative("events.jsonl.lock").touch()
                return lambda: LedgerRepair(root).quarantine(
                    "events.jsonl", bad_id, key="barrier-repair")
            raise AssertionError("unexercised discovered triple writer: " + name)

        self.assertEqual(exercised, derived_triple | bypasses)
        module, guard = self._epoch_guard_contract()
        for index, name in enumerate(sorted(exercised)):
            with self.subTest(writer=name):
                root = self._new_root(self.base / f"writer-{index}")
                call = pending(root, name)
                acquired: list[tuple[Path, bool]] = []
                shared_depth = 0

                @contextmanager
                def observed(selected_root: FloatiRoot, *, exclusive: bool = False):
                    nonlocal shared_depth
                    with guard(selected_root, exclusive=exclusive):
                        acquired.append((selected_root.path, exclusive))
                        if selected_root.path == root.path and not exclusive:
                            shared_depth += 1
                        try:
                            yield
                        finally:
                            if selected_root.path == root.path and not exclusive:
                                shared_depth -= 1

                before = self._snapshot(root)
                mutation_probe = _MutationProbe(
                    root, stack_prefix="floati", guard_active=lambda: shared_depth > 0
                )
                with ExitStack() as stack:
                    self._patch_guard_references(stack, module, guard, observed)
                    stack.enter_context(mutation_probe.installed())
                    call()
                self.assertNotEqual(before, self._snapshot(root), "probe must really mutate")
                self.assertIn((root.path, False), acquired)
                concrete = [event for event in mutation_probe.events
                            if isinstance(event, _MutationEvent)]
                self.assertTrue(concrete, "writer probe must observe a concrete mutation")
                self.assertTrue(
                    all(event.guarded for event in concrete),
                    "every concrete selected-plane mutation must occur inside the shared guard",
                )

                reverse = self._new_root(self.base / f"writer-reverse-{index}")
                reverse_call = pending(reverse, name)
                reverse_before = self._snapshot(reverse)
                with wake_coordination_guard(reverse, "recipient"):
                    with self.assertRaises(ProtocolRefusal) as caught:
                        reverse_call()
                self.assertEqual("lock_order_invalid", caught.exception.code)
                self.assertEqual(reverse_before, self._snapshot(reverse))
        self.assertEqual(
            derived_triple | {"ledger_repair.LedgerRepair.quarantine"},
            exercised,
            "every current direct triple append route plus repair needs one exercised case",
        )
        with wake_coordination_guard(self.root, "recipient"):
            result = jsonl.transact(
                self.root, "receipts/denials.jsonl", lambda rows: (len(rows), None),
                allowed_kinds={"denial_receipt"})
        self.assertEqual(0, result, "non-triple ledgers must not take the epoch barrier")

    def test_lock_order_guard_allows_forward_and_refuses_reverse_without_mutation(self) -> None:
        from floati.jsonl import _locked_path
        from floati.wake_hold import wake_coordination_guard
        module, epoch_guard = self._epoch_guard_contract()
        _roll_module, public_roll = self._public_roll_contract()
        events_lock = self.root.resolve_relative("events.jsonl.lock")
        with epoch_guard(self.root, exclusive=False):
            with wake_coordination_guard(self.root, "recipient"):
                with _locked_path(events_lock, exclusive=True):
                    pass
        for held, requested in (("wake", "epoch"), ("ledger", "wake"), ("ledger", "epoch")):
            with self.subTest(held=held, requested=requested):
                before = self._snapshot(self.root)
                manager = (wake_coordination_guard(self.root, "recipient") if held == "wake"
                           else _locked_path(events_lock, exclusive=True))
                with manager:
                    with self.assertRaises(ProtocolRefusal) as caught:
                        if requested == "epoch":
                            with epoch_guard(self.root, exclusive=False):
                                pass
                        else:
                            with wake_coordination_guard(self.root, "recipient"):
                                pass
                self.assertEqual("lock_order_invalid", caught.exception.code)
                self.assertEqual(before, self._snapshot(self.root))

        ordered = self._new_root(self.base / "public-lock-order" / self.root.tenant_id)
        self._grant_roll_authority(ordered, "actor-a")
        self._seed_opaque_triple(ordered)
        acquired: list[tuple[str, str]] = []

        @contextmanager
        def observed_epoch(*args: object, **kwargs: object):
            with epoch_guard(*args, **kwargs):
                bound = inspect.signature(epoch_guard).bind_partial(*args, **kwargs)
                selected_root = next(
                    value for value in bound.arguments.values() if isinstance(value, FloatiRoot)
                )
                self.assertEqual(ordered.path, selected_root.path)
                acquired.append((
                    "epoch", "exclusive" if any(value is True for value in bound.arguments.values())
                    else "shared",
                ))
                yield

        @contextmanager
        def observed_wake(*args: object, **kwargs: object):
            with wake_coordination_guard(*args, **kwargs):
                selected_root = next(value for value in args if isinstance(value, FloatiRoot))
                if selected_root.path == ordered.path:
                    acquired.append(("wake", str(args[1])))
                yield

        @contextmanager
        def observed_ledger(path: Path, *args: object, **kwargs: object):
            with _locked_path(path, *args, **kwargs):
                try:
                    relative = path.relative_to(ordered.tenant_home).as_posix()
                except ValueError:
                    relative = ""
                if relative == "events.jsonl.lock" or re.fullmatch(
                    r"receipts/(deliveries|acks)/[^/]+\.jsonl\.lock", relative
                ):
                    acquired.append(("ledger", relative))
                yield

        with ExitStack() as stack:
            self._patch_guard_references(stack, module, epoch_guard, observed_epoch)
            self._install_callable_patch(
                stack, sys.modules[wake_coordination_guard.__module__],
                wake_coordination_guard.__name__, wake_coordination_guard, "function", observed_wake,
            )
            self._install_callable_patch(
                stack, sys.modules[_locked_path.__module__], _locked_path.__name__,
                _locked_path, "function", observed_ledger,
            )
            public_roll(
                ordered, actor="actor-a", idempotency_key="public-lock-order",
                fault=lambda _name: None,
            )
        epoch_index = next(index for index, row in enumerate(acquired)
                           if row == ("epoch", "exclusive"))
        wake_index = next(index for index, row in enumerate(acquired) if row[0] == "wake")
        ledger_index = next(index for index, row in enumerate(acquired) if row[0] == "ledger")
        self.assertLess(epoch_index, wake_index)
        self.assertLess(wake_index, ledger_index)

        refused = self._new_root(self.base / "public-lock-reverse" / self.root.tenant_id)
        self._grant_roll_authority(refused, "actor-a")
        self._seed_opaque_triple(refused)
        before = self._snapshot(refused)
        with wake_coordination_guard(refused, "recipient"):
            with self.assertRaises(ProtocolRefusal) as caught:
                public_roll(
                    refused, actor="actor-a", idempotency_key="public-lock-reverse",
                    fault=lambda _name: None,
                )
        self.assertEqual("lock_order_invalid", caught.exception.code)
        self.assertEqual(before, self._snapshot(refused))

    def test_public_roll_is_exclusive_against_high_level_event_delivery_and_ack_operations(
        self,
    ) -> None:
        """Catches any complete public operation escaping the epoch guard."""

        module, guard = self._epoch_guard_contract()
        _boundaries, roll = self._fault_contract()
        selected_paths = {
            "events": "events.jsonl",
            "deliveries": "receipts/deliveries/recipient.jsonl",
            "acks": "receipts/acks/recipient.jsonl",
        }

        def operation_record(plane: str, result: object) -> dict[str, object]:
            if plane == "events":
                self.assertIsInstance(result, Mapping)
                assert isinstance(result, Mapping)
                record = result.get("message", result)
            elif plane == "deliveries":
                self.assertIsInstance(result, tuple)
                assert isinstance(result, tuple)
                record = result[1]
            else:
                record = result
            self.assertIsInstance(record, dict)
            assert isinstance(record, dict)
            return record

        for plane in ("events", "deliveries", "acks"):
            for placement in ("old", "new"):
                with self.subTest(plane=plane, placement=placement):
                    root = self._new_root(self.base / f"exclusive-{placement}-{plane}")
                    self._grant_roll_authority(root, "actor-a")
                    events = EventLog(root)
                    seed: Mapping[str, object] | None = None
                    if not (placement == "old" and plane == "events"):
                        seed = events.send(
                            "actor-a", "recipient", "floati", "7" * 40,
                            f"docs/evidence/exclusive-seed-{placement}-{plane}.md",
                            "pre-roll seed",
                            idempotency_key=f"exclusive-seed-{placement}-{plane}",
                        )
                    if placement == "old" and plane == "acks":
                        assert seed is not None
                        events.present("recipient")
                    if placement == "new" and plane == "acks":
                        template = self._new_root(
                            self.base / f"exclusive-ack-template-{plane}" / root.tenant_id
                        )
                        results_seed = EventLog(template).send(
                            "actor-a", "recipient", "floati", "a" * 40,
                            "docs/evidence/exclusive-new-ack-known.md",
                            "known immutable ack item", idempotency_key="known-ack-item",
                        )

                    trace: list[tuple[str, str, str]] = []
                    trace_lock = threading.Lock()
                    writer_acquired = threading.Event()
                    roller_acquired = threading.Event()
                    writer_attempted = threading.Event()
                    roller_attempted = threading.Event()
                    release_writer = threading.Event()
                    release_roller = threading.Event()
                    self.addCleanup(release_writer.set)
                    self.addCleanup(release_roller.set)
                    results: dict[str, object] = {}
                    errors: list[BaseException] = []

                    def note(thread: str, mode: str, phase: str) -> None:
                        with trace_lock:
                            trace.append((thread, mode, phase))

                    @contextmanager
                    def observed_guard(
                        selected_root: FloatiRoot, *, exclusive: bool = False
                    ):
                        thread = threading.current_thread().name
                        mode = "exclusive" if exclusive else "shared"
                        if thread not in {"writer", "roller"}:
                            with guard(selected_root, exclusive=exclusive):
                                yield
                            return
                        note(thread, mode, "attempt")
                        (roller_attempted if thread == "roller" else writer_attempted).set()
                        with guard(selected_root, exclusive=exclusive):
                            note(thread, mode, "acquired")
                            if thread == "roller":
                                roller_acquired.set()
                                if placement == "new":
                                    self._wait(release_roller, "exclusive guard release timed out")
                            elif thread == "writer":
                                writer_acquired.set()
                                self._wait(release_writer, "shared guard release timed out")
                            try:
                                yield
                            finally:
                                note(thread, mode, "exiting")

                    def high_level_operation() -> None:
                        try:
                            if plane == "events":
                                results["operation"] = EventLog(root).send(
                                    "actor-a", "recipient", "floati", "8" * 40,
                                    f"docs/evidence/exclusive-{placement}-events.md",
                                    f"{placement}-side send",
                                    idempotency_key=f"exclusive-{placement}-events-send",
                                )
                            elif plane == "deliveries":
                                results["operation"] = EventLog(root).present("recipient")
                            else:
                                item = (
                                    results_seed if placement == "new" else seed
                                )
                                assert isinstance(item, Mapping)
                                results["operation"] = SparseCursor(root).ack(
                                    "recipient",
                                    [str(item["id"])],
                                    acting_session_id=f"exclusive-{placement}-ack",
                                )
                        except BaseException as exc:
                            errors.append(exc)

                    def public_roll() -> None:
                        try:
                            results["roll"] = roll(
                                root,
                                actor="actor-a",
                                idempotency_key=f"exclusive-{placement}-{plane}-roll",
                                fault=lambda _point: None,
                            )
                        except BaseException as exc:
                            errors.append(exc)

                    writer = threading.Thread(
                        target=high_level_operation, name="writer", daemon=True
                    )
                    roller = threading.Thread(target=public_roll, name="roller", daemon=True)
                    deadline = time.monotonic() + WAIT_SECONDS
                    started: list[threading.Thread] = []

                    def wait_until(event: threading.Event, contract: str) -> None:
                        remaining = max(0.0, deadline - time.monotonic())
                        self.assertTrue(event.wait(remaining), contract)

                    def finish(thread: threading.Thread, contract: str) -> None:
                        thread.join(max(0.0, deadline - time.monotonic()))
                        self.assertFalse(thread.is_alive(), contract)

                    with ExitStack() as stack:
                        self._patch_guard_references(
                            stack, module, guard, observed_guard
                        )
                        try:
                            if placement == "old":
                                writer.start(); started.append(writer)
                                wait_until(writer_acquired, f"{plane} shared guard was not acquired")
                                roller.start(); started.append(roller)
                                wait_until(roller_attempted, "exclusive guard attempt was not observed")
                                release_writer.set()
                            else:
                                roller.start(); started.append(roller)
                                wait_until(roller_acquired, "exclusive roll guard was not acquired")
                                writer.start(); started.append(writer)
                                wait_until(writer_attempted, f"{plane} shared attempt was not observed")
                                release_roller.set()
                            finish(roller, "public roll did not finish")
                            if placement == "new":
                                wait_until(writer_acquired, f"{plane} shared guard was not acquired")
                                if plane == "deliveries":
                                    results["support_message"] = EventLog(root).send(
                                        "actor-a", "recipient", "floati", "9" * 40,
                                        "docs/evidence/exclusive-new-setup-deliveries.md",
                                        "new delivery setup",
                                        idempotency_key="exclusive-new-setup-deliveries",
                                    )
                                elif plane == "acks":
                                    from floati import jsonl
                                    support_result: dict[str, object] = {}

                                    def support() -> None:
                                        try:
                                            message = dict(results_seed["message"])
                                            jsonl.append_record(
                                                root, "events.jsonl", message,
                                                allowed_kinds={
                                                    "message_envelope", "bus_epoch_roll_receipt"
                                                },
                                            )
                                            support_result["delivery"] = EventLog(root).present(
                                                "recipient"
                                            )[1]
                                        except BaseException as exc:
                                            errors.append(exc)

                                    support_thread = threading.Thread(
                                        target=support, name="support", daemon=True
                                    )
                                    support_thread.start(); started.append(support_thread)
                                    finish(support_thread, "ack support setup did not finish")
                                    results["support_delivery"] = support_result.get("delivery")
                                release_writer.set()
                            finish(writer, f"{plane} high-level operation did not finish")
                        finally:
                            release_writer.set()
                            release_roller.set()
                            for started_thread in started:
                                if started_thread.is_alive():
                                    started_thread.join(max(0.0, deadline - time.monotonic()))
                                self.assertFalse(
                                    started_thread.is_alive(),
                                    f"{started_thread.name} escaped bounded cleanup",
                                )

                    self.assertEqual([], errors)
                    old_order = [
                        ("writer", "shared", "acquired"),
                        ("roller", "exclusive", "attempt"),
                        ("writer", "shared", "exiting"),
                        ("roller", "exclusive", "acquired"),
                    ]
                    new_order = [
                        ("roller", "exclusive", "acquired"),
                        ("writer", "shared", "attempt"),
                        ("roller", "exclusive", "exiting"),
                        ("writer", "shared", "acquired"),
                    ]
                    indexes = [trace.index(item) for item in (
                        old_order if placement == "old" else new_order
                    )]
                    self.assertEqual(sorted(indexes), indexes)
                    record = operation_record(plane, results["operation"])
                    frame = encode_frame(record)
                    receipt = self._receipt_from_roll_result(results["roll"])
                    archived = self._archive_files(Path(str(receipt["archive_path"])))
                    selected_path = selected_paths[plane]
                    live_path = root.resolve_relative(selected_path)
                    if placement == "old":
                        self.assertEqual(frame, archived[selected_path])
                        if plane == "events":
                            self.assertNotIn(str(record["id"]), live_path.read_text())
                        else:
                            self.assertFalse(live_path.exists())
                    else:
                        self.assertNotIn(frame, archived.values())
                        expected_live = (
                            encode_frame(receipt) + frame if plane == "events" else frame
                        )
                        self.assertEqual(expected_live, live_path.read_bytes())
                        if plane == "acks":
                            known_message = dict(results_seed["message"])
                            delivery = results["support_delivery"]
                            self.assertIsInstance(delivery, dict)
                            assert isinstance(delivery, dict)
                            expected_new = {
                                "events.jsonl": encode_frame(receipt) + encode_frame(known_message),
                                "receipts/deliveries/recipient.jsonl": encode_frame(delivery),
                                "receipts/acks/recipient.jsonl": frame,
                            }
                            for relative, payload in expected_new.items():
                                self.assertEqual(payload, root.resolve_relative(relative).read_bytes())
                                self.assertNotIn(payload, archived.values())
                            all_physical = list(archived.values()) + [
                                root.resolve_relative(relative).read_bytes()
                                for relative in expected_new
                            ]
                            for exact_frame in (
                                encode_frame(known_message), encode_frame(delivery), frame
                            ):
                                self.assertEqual(
                                    1, sum(part.count(exact_frame) for part in all_physical)
                                )
                    physical = list(archived.values())
                    if live_path.exists():
                        physical.append(live_path.read_bytes())
                    self.assertEqual(
                        1,
                        sum(payload.count(frame) for payload in physical),
                        "one high-level operation frame must land in exactly one epoch",
                    )

    @staticmethod
    def _marker_from_snapshot(
        snapshot: tuple[tuple[object, ...], ...],
    ) -> tuple[str, dict[str, object]] | None:
        for row in snapshot:
            if row[1] != "file" or not isinstance(row[-1], bytes):
                continue
            try:
                value = json.loads(row[-1])
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("state") in {"PREPARED", "COMMITTED"}:
                return str(row[0]), value
        return None

    def _raw_epoch_marker(
        self, root: FloatiRoot, *, actor: str, key: str,
    ) -> tuple[Path, dict[str, object]]:
        matches = []
        for path in root.tenant_home.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or value.get("state") not in {"PREPARED", "COMMITTED"}:
                continue
            request = value.get("request")
            if not isinstance(request, Mapping):
                request = value
            if request.get("actor") == actor and request.get("idempotency_key") == key:
                matches.append((path, value))
        self.assertEqual(1, len(matches), "raw roll marker must be uniquely request-bound")
        path, marker = matches[0]
        status = path.lstat()
        self.assertFalse(path.is_symlink())
        self.assertTrue(stat.S_ISREG(status.st_mode))
        request = marker.get("request")
        if not isinstance(request, Mapping):
            request = marker
        self.assertEqual(str(root.path.resolve()), marker.get("root"))
        self.assertEqual(root.tenant_id, marker.get("tenant_id"))
        self.assertEqual(actor, request.get("actor"))
        self.assertEqual(key, request.get("idempotency_key"))
        self.assertIsInstance(request.get("request_id"), str)
        self.assertTrue(request.get("request_id"))
        return path, marker

    def _durable_roll_classification(
        self, root: FloatiRoot, *, actor: str, key: str,
    ) -> tuple[str, str | None, dict[str, object] | None, dict[str, object] | None]:
        """Classify recovery from surviving disk facts, never callback position."""

        coordinates = tuple(
            path for path in root.tenant_home.iterdir()
            if path.name.startswith(".floati-epoch-roll-")
            and path.name.endswith(".v1.json")
        )
        self.assertLessEqual(len(coordinates), 1, "one roll may own one marker coordinate")
        if coordinates:
            path = coordinates[0]
            request_id = path.name[
                len(".floati-epoch-roll-"):-len(".v1.json")
            ]
            self.assertTrue(request_id)
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return "partial", request_id, None, None
            if not isinstance(value, dict) or value.get("state") not in {
                "PREPARED", "COMMITTED"
            }:
                return "partial", request_id, None, None
            _path, marker = self._raw_epoch_marker(root, actor=actor, key=key)
            request = marker.get("request")
            assert isinstance(request, Mapping)
            self.assertEqual(request_id, request.get("request_id"))
            return str(marker["state"]).lower(), request_id, marker, None

        frames = decode_frames(root.resolve_relative("events.jsonl").read_bytes())
        self.assertEqual(1, len(frames), "marker-free terminal state needs one receipt")
        receipt = frames[0]
        self.assertEqual("bus_epoch_roll_receipt", receipt.get("kind"))
        self.assertEqual((actor, key), (receipt.get("actor"), receipt.get("idempotency_key")))
        self._verify_archive_facts(Path(str(receipt["archive_path"])))
        return "complete", None, None, receipt

    def _assert_doctor_complete_noop(
        self, artifact: Mapping[str, object], before: tuple[tuple[object, ...], ...],
        root: FloatiRoot,
    ) -> None:
        findings = artifact.get("findings")
        self.assertIsInstance(findings, list)
        assert isinstance(findings, list)
        self.assertFalse(any(
            isinstance(finding, Mapping)
            and finding.get("code") == "bus_epoch_roll_reconciled"
            for finding in findings
        ), "already COMPLETE disk state must be a Doctor no-op")
        self.assertEqual(before, self._snapshot(root))

    def test_00_successful_roll_trace_is_derived_from_real_mutations_and_boundaries(self) -> None:
        boundaries, roll = self._fault_contract()
        self._assert_no_native_mutation_escape()
        root = self._new_root(self.base / "successful-fault-trace")
        self._grant_roll_authority(root, "actor-a")
        controls = self._seed_control_state(root)
        selected = self._seed_opaque_triple(root)
        controls_before = {path: self._identity(path) for path in controls}
        baseline = _MutationProbe.snapshot(root)
        probe = _MutationProbe(root)
        with probe.installed():
            result = roll(
                root,
                actor="actor-a",
                idempotency_key="successful-fault-trace",
                fault=probe.boundary,
            )
        mutation_events = [item for item in probe.events if isinstance(item, _MutationEvent)]
        boundary_events = [item for item in probe.events if isinstance(item, _BoundaryEvent)]
        self.assertTrue(mutation_events, "successful roll must expose real durable mutations")
        self.assertEqual(boundaries, tuple(item.name for item in boundary_events))
        self.assertIsInstance(probe.events[0], _MutationEvent)
        first_event = probe.events[0]
        assert isinstance(first_event, _MutationEvent)
        self.assertEqual(baseline, first_event.before)
        for left, right in zip(probe.events, probe.events[1:]):
            left_state = left.after if isinstance(left, _MutationEvent) else left.snapshot
            right_state = right.before if isinstance(right, _MutationEvent) else right.snapshot
            self.assertEqual(
                left_state, right_state,
                "whole-root delta occurred outside a captured primitive or boundary",
            )
        final_state = (
            probe.events[-1].after if isinstance(probe.events[-1], _MutationEvent)
            else probe.events[-1].snapshot
        )
        self.assertEqual(_MutationProbe.snapshot(root), final_state)
        previous = -1
        for boundary in boundary_events:
            index = probe.events.index(boundary)
            segment = [item for item in probe.events[previous + 1:index]
                       if isinstance(item, _MutationEvent)]
            self.assertLessEqual(
                sum(not item.durability for item in segment), 1,
                "two concrete byte/tree mutations cannot share one boundary",
            )
            previous = index
        self.assertFalse(any(isinstance(item, _MutationEvent)
                             for item in probe.events[previous + 1:]))
        for event in mutation_events:
            self.assertTrue(event.durability or event.before != event.after)
            after_paths = {str(row[0]) for row in event.after}
            parent = Path(event.target).parent
            self.assertTrue(
                event.target in after_paths
                or parent == Path(".")
                or str(parent) in after_paths
            )

        first_leaf = next(
            event for event in mutation_events
            if {
                str(row[0]) for row in event.after if row[1] != "directory"
            } != {
                str(row[0]) for row in event.before if row[1] != "directory"
            }
        )
        self.assertIs(
            first_event,
            first_leaf,
            "the first concrete mutation must create the durable intent marker",
        )
        marker_relative = first_leaf.target
        marker_row = next(row for row in first_leaf.after if row[0] == marker_relative)
        self.assertEqual("file", marker_row[1])
        self.assertTrue(stat.S_ISREG(int(marker_row[2])))
        self.assertEqual(b"", marker_row[-1], "mutation zero may expose only the marker coordinate")
        before_leaf = {str(row[0]) for row in first_leaf.before if row[1] != "directory"}
        after_leaf = {str(row[0]) for row in first_leaf.after if row[1] != "directory"}
        self.assertEqual({marker_relative}, after_leaf - before_leaf)
        self.assertEqual(set(), before_leaf - after_leaf)
        before_directories = {
            str(row[0]) for row in first_leaf.before if row[1] == "directory"
        }
        after_directories = {
            str(row[0]) for row in first_leaf.after if row[1] == "directory"
        }
        self.assertEqual(
            before_directories,
            after_directories,
            "the intent marker parent must already exist before mutation zero",
        )
        marker_parent = Path(marker_relative).parent
        if marker_parent != Path("."):
            self.assertIn(marker_parent.as_posix(), before_directories)
        before_rows = {str(row[0]): row for row in first_leaf.before}
        after_rows = {str(row[0]): row for row in first_leaf.after}
        for relative in before_leaf:
            self.assertEqual(
                before_rows[relative], after_rows[relative],
                f"mutation zero changed unrelated leaf {relative}",
            )
        prepared_match = next(
            match for event in mutation_events
            if (match := self._marker_from_snapshot(event.after)) is not None
            and match[0] == marker_relative
            and match[1].get("state") == "PREPARED"
        )
        _prepared_relative, prepared_marker = prepared_match
        request = prepared_marker.get("request")
        if not isinstance(request, Mapping):
            request = prepared_marker
        self.assertEqual(
            {
                "schema_version", "root", "tenant_id", "state", "request",
                "archive_path", "staging_path", "receipt", "absent_paths", "padding",
            },
            set(prepared_marker),
            "the recovery marker document has one exact closed shape",
        )
        self.assertEqual(
            {"actor", "idempotency_key", "request_id"}, set(request),
            "the marker request binding has one exact closed shape",
        )
        self.assertEqual("PREPARED", prepared_marker["state"])
        self.assertEqual((str(root.path.resolve()), root.tenant_id),
                         (prepared_marker.get("root"), prepared_marker.get("tenant_id")))
        self.assertEqual(("actor-a", "successful-fault-trace"),
                         (request.get("actor"), request.get("idempotency_key")))
        request_id = request.get("request_id")
        self.assertIsInstance(request_id, str)
        self.assertTrue(request_id)
        self.assertEqual(
            f".floati-epoch-roll-{request_id}.v1.json", Path(marker_relative).name
        )
        staging_path = Path(str(prepared_marker["staging_path"]))
        archive_path = Path(str(prepared_marker["archive_path"]))
        self.assertEqual(root.tenant_home, staging_path.parent)
        self.assertEqual(root.tenant_home, archive_path.parent)
        self.assertEqual(f".floati-epoch-staging-{request_id}", staging_path.name)
        self.assertIn(f"-request-{request_id}-", archive_path.name)
        marker_receipt = prepared_marker["receipt"]
        self.assertIsInstance(marker_receipt, Mapping)
        assert isinstance(marker_receipt, Mapping)
        self.assertEqual(str(archive_path), marker_receipt.get("archive_path"))
        self.assertEqual(
            (request.get("actor"), request.get("idempotency_key")),
            (marker_receipt.get("actor"), marker_receipt.get("idempotency_key")),
        )
        for relative, payload in selected.items():
            row = next(item for item in first_leaf.after if item[0] == relative)
            self.assertEqual(payload, row[-1])
        added_non_directories = after_leaf - {
            str(row[0]) for row in baseline if row[1] != "directory"
        }
        self.assertEqual({marker_relative}, added_non_directories)

        selected_prefixes = ("events.jsonl", "receipts/deliveries/", "receipts/acks/")
        first_selected_index = next(
            index for index, event in enumerate(probe.events)
            if isinstance(event, _MutationEvent)
            and not event.durability
            and (event.target == selected_prefixes[0]
                 or event.target.startswith(selected_prefixes[1:]))
        )
        marker_parent_relative = marker_parent.as_posix()

        def durable_before(index: int, target: str) -> bool:
            return any(
                isinstance(event, _MutationEvent)
                and event.durability
                and event.target == target
                for event in probe.events[:index]
            )

        self.assertTrue(
            durable_before(first_selected_index, marker_relative),
            "PREPARED marker bytes must be fsynced before the first plane mutation",
        )
        self.assertTrue(
            durable_before(first_selected_index, marker_parent_relative),
            "PREPARED marker directory entry must be fsynced before the first plane mutation",
        )

        intent_callbacks = [
            event for event in boundary_events
            if (match := self._marker_from_snapshot(event.snapshot)) is not None
            and match[1].get("state") == "PREPARED"
            and all(next(row for row in event.snapshot if row[0] == relative)[-1] == payload
                    for relative, payload in selected.items())
        ]
        self.assertTrue(intent_callbacks, "callback must observe intent before any plane change")
        self.assertTrue(any(
            isinstance(left, _BoundaryEvent) and isinstance(right, _BoundaryEvent)
            and left.snapshot == right.snapshot
            and self._marker_from_snapshot(left.snapshot) is not None
            for left, right in zip(probe.events, probe.events[1:])
        ), "the post-intent/pre-plane gap is a zero-mutation exported boundary")

        committed_index = None
        for index, event in enumerate(probe.events):
            if not isinstance(event, _MutationEvent):
                continue
            before_marker = self._marker_from_snapshot(event.before)
            after_marker = self._marker_from_snapshot(event.after)
            if (before_marker is not None and after_marker is not None
                    and before_marker[1].get("state") == "PREPARED"
                    and after_marker[1].get("state") == "COMMITTED"):
                committed_index = index
                committed_request = after_marker[1].get("request")
                if not isinstance(committed_request, Mapping):
                    committed_request = after_marker[1]
                self.assertEqual(request_id, committed_request.get("request_id"))
                break
        self.assertIsNotNone(committed_index, "one real mutation must commit the marker")
        assert committed_index is not None
        next_boundary_index = next(
            index for index in range(committed_index + 1, len(probe.events))
            if isinstance(probe.events[index], _BoundaryEvent)
        )
        between_commit_and_callback = probe.events[committed_index + 1:next_boundary_index]
        self.assertTrue(any(
            isinstance(event, _MutationEvent) and event.durability
            and event.target == marker_relative for event in between_commit_and_callback
        ), "COMMITTED marker bytes must be fsynced before commit classification")
        self.assertTrue(any(
            isinstance(event, _MutationEvent) and event.durability
            and event.target == marker_parent_relative for event in between_commit_and_callback
        ), "COMMITTED marker directory must be fsynced before commit classification")
        self.assertTrue(all(
            isinstance(event, _MutationEvent) and event.durability
            for event in between_commit_and_callback
        ))
        commit_boundary = probe.events[next_boundary_index].name  # type: ignore[union-attr]
        self.assertEqual(1, boundaries.count(commit_boundary))
        receipt = self._receipt_from_roll_result(result)
        self._assert_receipt(root, receipt, selected)
        self.assertEqual(encode_frame(receipt), root.resolve_relative("events.jsonl").read_bytes())
        self.assertEqual(controls_before, {path: self._identity(path) for path in controls})

    def _doctor_recover(self, root: FloatiRoot) -> tuple[dict[str, object], int]:
        artifact, return_code = Doctor(REPOSITORY_ROOT, root.path, ref="HEAD").artifact()
        validate_json_schema(artifact, Path("schemas/v1/doctor-artifact.schema.json"))
        self.assertEqual(str(root.path), artifact["root"])
        self.assertEqual(
            {"healthy": 0, "refused": 20, "unknown": 21, "cannot_speak": 22,
             "malformed_evidence": 33, "degraded": 35}[str(artifact["state"])],
            return_code,
        )
        return artifact, return_code

    def _assert_doctor_recovery(
        self,
        artifact: Mapping[str, object],
        *,
        root: FloatiRoot,
        actor: str,
        key: str,
        committed: bool,
        request_id: str,
        receipt: Mapping[str, object] | None,
    ) -> None:
        expected_class = "COMMITTED" if committed else "PREPARED"
        expected_direction = "roll_forward" if committed else "rollback"
        findings = artifact.get("findings")
        self.assertIsInstance(findings, list)
        assert isinstance(findings, list)
        matches = [
            finding
            for finding in findings
            if isinstance(finding, Mapping)
            and finding.get("code") == "bus_epoch_roll_reconciled"
            and finding.get("severity") == "ok"
            and finding.get("subject") == str(root.path.resolve())
        ]
        self.assertEqual(
            1,
            len(matches),
            "Doctor must emit one typed epoch classification and recovery direction",
        )
        payload = matches[0].get("epoch_roll")
        self.assertIsInstance(payload, Mapping)
        assert isinstance(payload, Mapping)
        self.assertEqual(
            {"root", "tenant_id", "request", "classification", "direction", "receipt"},
            set(payload),
        )
        self.assertEqual(str(root.path.resolve()), payload["root"])
        self.assertEqual(root.tenant_id, payload["tenant_id"])
        self.assertEqual(expected_class, payload["classification"])
        self.assertEqual(expected_direction, payload["direction"])
        self.assertEqual(dict(receipt) if receipt is not None else None, payload["receipt"])
        self.assertEqual(
            {"actor": actor, "idempotency_key": key, "request_id": request_id},
            payload["request"],
        )

    @staticmethod
    def _tree_paths(root: FloatiRoot) -> set[str]:
        return {
            path.relative_to(root.tenant_home).as_posix()
            for path in root.tenant_home.rglob("*")
        }

    def _assert_recovered(
        self,
        root: FloatiRoot,
        old: Mapping[str, bytes],
        baseline: tuple[tuple[object, ...], ...],
        controls_before: Mapping[Path, tuple[object, ...]],
        *,
        committed: bool,
        actor: str,
        key: str,
    ) -> None:
        if not committed:
            self.assertEqual(
                baseline,
                self._snapshot(root),
                "PREPARED rollback must restore the exact old tree without debris",
            )
            return
        frames = decode_frames(root.resolve_relative("events.jsonl").read_bytes())
        self.assertEqual(1, len(frames), "recovery must leave exactly one live receipt")
        self.assertEqual("bus_epoch_roll_receipt", frames[0]["kind"])
        self._assert_receipt(root, frames[0], old)
        self.assertEqual((actor, key), (frames[0]["actor"], frames[0]["idempotency_key"]))
        self.assertEqual(encode_frame(frames[0]), root.resolve_relative("events.jsonl").read_bytes())
        archive = Path(str(frames[0]["archive_path"]))
        self.assertEqual(dict(old), self._archive_files(archive))
        for plane in ("receipts/deliveries", "receipts/acks"):
            self.assertFalse(
                any(
                    path.is_symlink() or path.is_file()
                    for path in root.resolve_relative(plane).rglob("*.jsonl")
                )
            )
        self.assertEqual(
            controls_before, {path: self._identity(path) for path in controls_before}
        )
        for row in baseline:
            relative, identity = str(row[0]), tuple(row[1:])
            if relative in old:
                continue
            path = root.resolve_relative(relative)
            if any(Path(relative) in Path(selected).parents for selected in old):
                # Moving a selected child necessarily changes its ancestor directory
                # metadata; descendants and every other baseline leaf stay exact.
                continue
            if path in archive.parents:
                continue
            try:
                observed = self._identity(path)
            except FileNotFoundError:
                self.fail(f"COMMITTED recovery removed non-selected baseline path {relative}")
            self.assertEqual(
                identity,
                observed,
                f"COMMITTED recovery changed non-selected baseline path {relative}",
            )
        baseline_paths = {str(row[0]) for row in baseline}
        archive_relative = archive.relative_to(root.tenant_home).as_posix()
        allowed_new = {
            path.relative_to(root.tenant_home).as_posix()
            for path in archive.rglob("*")
        } | {archive_relative}
        allowed_new.update(
            parent.relative_to(root.tenant_home).as_posix()
            for parent in archive.parents
            if parent != root.tenant_home and parent.is_relative_to(root.tenant_home)
        )
        self.assertEqual(
            set(),
            self._tree_paths(root) - baseline_paths - allowed_new,
            "COMMITTED recovery must remove every staging/journal/debris path",
        )

    def test_01_exported_boundary_exception_matrix_uses_raw_marker_state(self) -> None:
        boundaries, roll = self._fault_contract()
        verifier_root = self._new_root(self.base / "doctor-verifier-contract" / self.root.tenant_id)
        self._seed_opaque_triple(verifier_root)
        verifier_receipt = self._roll_success(root=verifier_root, key="doctor-verifier-contract")
        self._verify_archive_facts(Path(str(verifier_receipt["archive_path"])))
        verifier_identity = self._archive_verifier_identity
        self.assertIsNotNone(verifier_identity)
        assert verifier_identity is not None
        verifier_doctor_calls = 0
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                root = self._new_root(self.base / f"exception-{index}")
                self._grant_roll_authority(root, "actor-a")
                controls = self._seed_control_state(root)
                selected = self._seed_opaque_triple(root)
                controls_before = {path: self._identity(path) for path in controls}
                baseline = self._snapshot(root)

                def fault(point: str, selected_boundary: str = boundary) -> None:
                    if point == selected_boundary:
                        raise RuntimeError("injected " + point)

                with self.assertRaisesRegex(RuntimeError, re.escape(boundary)):
                    roll(root, actor="actor-a", idempotency_key="fault-key", fault=fault)
                classification, request_id, marker, terminal_receipt = (
                    self._durable_roll_classification(
                        root, actor="actor-a", key="fault-key"
                    )
                )
                committed = classification in {"committed", "complete"}
                if classification == "committed":
                    incomplete = self._snapshot(root)

                    def corrupt(*args: object, **kwargs: object) -> object:
                        result = verifier_identity(*args, **kwargs)
                        if isinstance(result, Mapping) and isinstance(result.get("facts"), Mapping):
                            return dict(result, facts=dict(result["facts"], archive_sha256="0" * 64))
                        return dict(result, archive_sha256="0" * 64)  # type: ignore[arg-type]

                    with ExitStack() as stack:
                        self._patch_floati_callable_identity(stack, verifier_identity, corrupt)
                        refused_doctor, _refused_rc = self._doctor_recover(root)
                    self.assertNotEqual("healthy", refused_doctor["state"])
                    self.assertEqual(
                        incomplete, self._snapshot(root),
                        "Doctor must not roll forward from a corrupted verifier result",
                    )

                def observe(*args: object, **kwargs: object) -> object:
                    nonlocal verifier_doctor_calls
                    verifier_doctor_calls += 1
                    return verifier_identity(*args, **kwargs)

                with ExitStack() as stack:
                    self._patch_floati_callable_identity(stack, verifier_identity, observe)
                    before_doctor = self._snapshot(root)
                    doctor, doctor_rc = self._doctor_recover(root)
                self.assertIn(doctor_rc, {0, 20, 21, 22, 33, 35})
                receipt = terminal_receipt
                if classification == "complete":
                    self._assert_doctor_complete_noop(doctor, before_doctor, root)
                else:
                    if committed:
                        receipt = decode_frames(
                            root.resolve_relative("events.jsonl").read_bytes()
                        )[0]
                    self._assert_doctor_recovery(
                        doctor,
                        root=root,
                        actor="unknown" if classification == "partial" else "actor-a",
                        key="unknown" if classification == "partial" else "fault-key",
                        committed=committed,
                        request_id=str(request_id),
                        receipt=receipt,
                    )
                self._assert_recovered(
                    root, selected, baseline, controls_before, committed=committed,
                    actor="actor-a", key="fault-key",
                )
        self.assertGreater(verifier_doctor_calls, 0, "Doctor must consume the roll verifier seam")

    def test_02_abrupt_boundary_and_every_real_mutation_ordinal_converge(self) -> None:
        boundaries, roll = self._fault_contract()
        count_root = self._new_root(self.base / "abrupt-count")
        self._grant_roll_authority(count_root, "actor-a")
        self._seed_control_state(count_root)
        self._seed_opaque_triple(count_root)
        count_probe = _MutationProbe(count_root)
        with count_probe.installed():
            roll(count_root, actor="actor-a", idempotency_key="count-key",
                 fault=count_probe.boundary)
        mutation_count = sum(isinstance(item, _MutationEvent) for item in count_probe.events)
        self.assertGreater(mutation_count, 0)

        boundary_program = (
            "import os,sys\nfrom pathlib import Path\n"
            "from floati.root import FloatiRoot\n"
            "from tests.test_bus_epoch_roll import GovernedBusEpochRollTests\n"
            "r=FloatiRoot.open_direct_home(Path(sys.argv[1])); b=sys.argv[2]\n"
            "c=GovernedBusEpochRollTests(); c.base=r.path.parent/('.'+r.path.name+'-contract'); c.root=r\n"
            "c._roll_adapter=None; c._fault_boundaries=None; c._roll_target=None; c._archive_verifier_fn=None; c._discovered_authority_subject=None\n"
            "fn=c._public_roll_contract()[1]\n"
            "def f(p):\n    if p==b: os._exit(91)\n"
            "fn(r,actor='actor-a',idempotency_key='abrupt-key',fault=f)\n"
            "raise SystemExit(92)\n"
        )
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                root = self._new_root(self.base / f"abrupt-{index}")
                self._grant_roll_authority(root, "actor-a")
                controls = self._seed_control_state(root)
                selected = self._seed_opaque_triple(root)
                controls_before = {path: self._identity(path) for path in controls}
                baseline = self._snapshot(root)
                child = subprocess.run([sys.executable, "-c", boundary_program, str(root.path),
                                        boundary],
                                       cwd=REPOSITORY_ROOT, text=True,
                                       capture_output=True, check=False,
                                       timeout=SUBPROCESS_SECONDS)
                self.assertEqual(91, child.returncode, child.stderr)
                classification, request_id, _marker, terminal_receipt = (
                    self._durable_roll_classification(
                        root, actor="actor-a", key="abrupt-key"
                    )
                )
                committed = classification in {"committed", "complete"}
                before_doctor = self._snapshot(root)
                doctor, doctor_rc = self._doctor_recover(root)
                self.assertIn(doctor_rc, {0, 20, 21, 22, 33, 35})
                receipt = terminal_receipt
                if classification == "complete":
                    self._assert_doctor_complete_noop(doctor, before_doctor, root)
                else:
                    if committed:
                        receipt = decode_frames(
                            root.resolve_relative("events.jsonl").read_bytes()
                        )[0]
                    self._assert_doctor_recovery(
                        doctor,
                        root=root,
                        actor="unknown" if classification == "partial" else "actor-a",
                        key="unknown" if classification == "partial" else "abrupt-key",
                        committed=committed,
                        request_id=str(request_id),
                        receipt=receipt,
                    )
                self._assert_recovered(
                    root, selected, baseline, controls_before, committed=committed,
                    actor="actor-a", key="abrupt-key",
                )

        ordinal_program = (
            "import sys\nfrom pathlib import Path\n"
            "from floati.root import FloatiRoot\n"
            "from tests.test_bus_epoch_roll import GovernedBusEpochRollTests,_MutationProbe\n"
            "r=FloatiRoot.open_direct_home(Path(sys.argv[1])); c=GovernedBusEpochRollTests()\n"
            "c.base=r.path.parent/('.'+r.path.name+'-contract'); c.root=r\n"
            "c._roll_adapter=None; c._fault_boundaries=None; c._roll_target=None; c._archive_verifier_fn=None; c._discovered_authority_subject=None\n"
            "fn=c._public_roll_contract()[1]\n"
            "p=_MutationProbe(r,abort_after=int(sys.argv[2]))\n"
            "with p.installed(): fn(r,actor='actor-a',idempotency_key='ordinal-key',fault=p.boundary)\n"
            "raise SystemExit(92)\n"
        )
        for ordinal in range(1, mutation_count + 1):
            with self.subTest(mutation_ordinal=ordinal):
                root = self._new_root(self.base / f"ordinal-{ordinal}")
                self._grant_roll_authority(root, "actor-a")
                controls = self._seed_control_state(root)
                selected = self._seed_opaque_triple(root)
                controls_before = {path: self._identity(path) for path in controls}
                baseline = self._snapshot(root)
                child = subprocess.run(
                    [sys.executable, "-c", ordinal_program, str(root.path),
                     str(ordinal)],
                    cwd=REPOSITORY_ROOT, text=True, capture_output=True, check=False,
                    timeout=SUBPROCESS_SECONDS,
                )
                self.assertEqual(91, child.returncode, child.stderr)
                classification, request_id, _marker, terminal_receipt = (
                    self._durable_roll_classification(
                        root, actor="actor-a", key="ordinal-key"
                    )
                )
                committed = classification in {"committed", "complete"}
                before_doctor = self._snapshot(root)
                doctor, _doctor_rc = self._doctor_recover(root)
                receipt = terminal_receipt
                if classification == "complete":
                    self._assert_doctor_complete_noop(doctor, before_doctor, root)
                else:
                    if committed:
                        receipt = decode_frames(
                            root.resolve_relative("events.jsonl").read_bytes()
                        )[0]
                    self._assert_doctor_recovery(
                        doctor, root=root,
                        actor="unknown" if classification == "partial" else "actor-a",
                        key="unknown" if classification == "partial" else "ordinal-key",
                        committed=committed, request_id=str(request_id), receipt=receipt,
                    )
                self._assert_recovered(
                    root, selected, baseline, controls_before, committed=committed,
                    actor="actor-a", key="ordinal-key",
                )

    def test_doctor_never_initiates_roll_or_mutates_complete_state(self) -> None:
        untouched = self._snapshot(self.root)
        self._doctor_recover(self.root)
        self.assertEqual(untouched, self._snapshot(self.root))
        EventLog(self.root).send(
            "actor-a", "recipient", "floati", "e" * 40,
            "docs/evidence/doctor.md", "doctor state", idempotency_key="doctor-message")
        self._roll_success(key="doctor-roll")
        complete = self._snapshot(self.root)
        first, first_rc = self._doctor_recover(self.root)
        second, second_rc = self._doctor_recover(self.root)
        self.assertIn(first_rc, {0, 20, 21, 22, 33, 35})
        self.assertIn(second_rc, {0, 20, 21, 22, 33, 35})
        for artifact in (first, second):
            findings = artifact.get("findings", [])
            self.assertFalse(any(
                isinstance(finding, Mapping)
                and finding.get("code") == "bus_epoch_roll_reconciled"
                for finding in findings
            ))
        self.assertEqual(complete, self._snapshot(self.root))

    def test_test_owned_reader_inventory_dispatches_all_seven_migration_fixtures(self) -> None:
        inventory = self._reader_anchor_inventory()
        actual_counts = Counter((row.path, row.owner, row.target) for row in inventory)
        expected_counts = Counter({
            (path, owner, target): count
            for path, owner, target, count, _family in READER_ASSIGNMENTS
        })
        self.assertEqual(expected_counts, actual_counts)
        assignment = {
            (path, owner, target): family
            for path, owner, target, _count, family in READER_ASSIGNMENTS
        }
        assigned = [assignment[(row.path, row.owner, row.target)] for row in inventory]
        self.assertEqual(len(inventory), len({row.stable_coordinate for row in inventory}))
        self.assertEqual(READER_FAMILIES - {"external_path_follower"}, set(assigned))
        cases = (
            _ReaderCase("snapshot_inbox_status_board", "_fixture_snapshot_family"),
            _ReaderCase("sparse_inbox_ack_cursor", "_fixture_sparse_family"),
            _ReaderCase("wake_prefix_attempt", "_fixture_wake_prefix_family"),
            _ReaderCase("watch_board_probe", "_fixture_watch_probe_family"),
            _ReaderCase("codex_waiter", "_fixture_waiter_family"),
            _ReaderCase("wake_daemon", "_fixture_daemon_family"),
            _ReaderCase("external_path_follower", "_fixture_tail_family"),
        )
        self.assertEqual(READER_FAMILIES, {case.family for case in cases})
        self.assertEqual(len(cases), len({case.helper_name for case in cases}))
        live_counts: list[int] = []
        evidence: list[dict[str, object]] = []
        with mock.patch.object(self, "_roll_success", wraps=self._roll_success) as roll_spy:
            for case in cases:
                before = roll_spy.call_count
                helper = getattr(self, case.helper_name)
                result = helper()
                self.assertIsInstance(result, dict)
                self.assertTrue(result["receipts"])
                live_counts.append(len(result["receipts"]))
                evidence.append(result)
                self.assertEqual(len(result["receipts"]), roll_spy.call_count - before)
        self.assertTrue(all(count >= 1 for count in live_counts))
        self.assertEqual(7, len(evidence))

    def test_snapshot_cursor_and_wake_readers_detect_archive_and_rebuild(self) -> None:
        self.assertEqual(7, len(READER_FAMILIES), "the governed anchored-reader family count")
        events = EventLog(self.root)
        pre = events.send(
            "actor-a", "recipient", "floati", "f" * 40,
            "docs/evidence/pre-reader.md", "pre reader", idempotency_key="pre-reader")
        cursor, wake = SparseCursor(self.root), WakeHoldController(self.root)
        pre_hold = wake.evaluate("recipient", idempotency_key="pre-reader-hold")
        self.assertIsInstance(pre_hold.get("receipt"), dict)
        events.present("recipient")
        projection = FleetProjection(self.root)
        projection.status_artifact(datetime.now(timezone.utc))
        model_from_root(self.root)
        stores = (events._inbox_snapshot_store("recipient", 100),
                  projection._status_snapshot_store(),
                  SnapshotStore(self.root, reader="board", key="full-redraw",
                                discover_sources=projection._status_sources))
        for store in stores:
            if not store.path.is_file():
                store.refresh({"fixture_reader": store.reader})
        self.assertTrue(all(store.path.is_file() for store in stores))
        self._roll_success(key="reader-roll")
        for store in stores:
            with self.subTest(reader=store.reader):
                with self.assertRaises(SnapshotRefusal) as caught:
                    store.load()
                self.assertEqual("snapshot_source_archived", caught.exception.code)
        post_time = "2026-08-30T12:00:00.000Z"
        observation = datetime(2026, 8, 30, 12, 10, tzinfo=timezone.utc)
        with mock.patch("floati.events.utc_now", return_value=post_time):
            post = EventLog(self.root).send(
                "actor-a", "recipient", "floati", "1" * 40,
                "docs/evidence/post-reader.md", "post reader", idempotency_key="post-reader")
        self.assertEqual([post["id"]], [row["id"] for row in EventLog(self.root).records()])
        status = projection.status_artifact(observation)
        status_node = next(row for row in status["nodes"] if row["node_id"] == "recipient")
        self.assertEqual(
            {"node": "recipient", "age_minutes": 10, "observed_at": "2026-08-30T12:10:00Z"},
            status_node["oldest_unread"],
        )
        inbox, _ = events.present("recipient")
        self.assertEqual([post["id"]], [row["id"] for row in inbox])
        board = model_from_root(self.root, observation)
        board_node = next(row for row in board.nodes if row["node_id"] == "recipient")
        self.assertEqual(post["id"], board_node["visible_message_id"])
        self.assertEqual(1, board_node["inbox_depth"])
        with mock.patch(
            "floati.events.utc_now", return_value="2026-08-30T12:01:00.000Z"
        ):
            wake_post = EventLog(self.root).send(
                "actor-a", "recipient", "floati", "2" * 40,
                "docs/evidence/post-reader-wake.md", "post reader wake",
                idempotency_key="post-reader-wake",
            )
        held = wake.evaluate("recipient", idempotency_key="post-reader-hold")
        self.assertEqual(
            [wake_post["id"]], [row["id"] for row in held["fresh_messages"]]
        )
        with self.assertRaises(ProtocolRefusal) as ack_refusal:
            cursor.ack("recipient", [pre["id"]], acting_session_id="old-ack")
        self.assertIn("archiv", ack_refusal.exception.code)
        with self.assertRaises(ProtocolRefusal) as attempt_refusal:
            WakeAttemptLedger(self.root).record(
                recipient="recipient", acting_session_id="old-attempt",
                item_ids=[pre["id"]],
                decision_receipt_id=str(pre_hold["receipt"]["id"]),  # type: ignore[index]
                message_worker_session_id=None, idempotency_key="old-attempt",
                outcome="refused", reason_code="fixture")
        self.assertIn("archiv", attempt_refusal.exception.code)

    def test_watch_probe_waiter_and_daemon_loops_observe_post_roll_mail(self) -> None:
        watch_pre = EventLog(self.root).send(
            "actor-a", "recipient", "floati", "b" * 40,
            "docs/evidence/watch-pre.md", "drained watch seed",
            idempotency_key="watch-pre",
        )
        EventLog(self.root).present("recipient")
        SparseCursor(self.root).ack(
            "recipient", [str(watch_pre["id"])], acting_session_id="watch-pre-ack"
        )
        ready, released = threading.Barrier(2), threading.Barrier(2)
        rows: list[dict[str, object]] = []
        errors: list[BaseException] = []
        watch_now = datetime(2026, 8, 30, 13, 10, tzinfo=timezone.utc)

        def watch_sleep(_seconds: float) -> None:
            self._barrier(ready, "watch did not publish its initial observation")
            self._barrier(released, "watch did not resume after the post-roll write")

        def run_watch() -> None:
            try:
                rows.extend(iter_deltas(
                    FleetProjection(self.root), iterations=2,
                    now=lambda: watch_now, sleeper=watch_sleep))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_watch, daemon=True)
        thread.start()
        self._barrier(ready, "main thread did not meet the watch rendezvous")
        self._roll_success(key="watch-roll")
        with mock.patch("floati.events.utc_now", return_value="2026-08-30T13:00:00.000Z"):
            watch_post = EventLog(self.root).send(
                "actor-a", "recipient", "floati", "2" * 40,
                "docs/evidence/watch.md", "watch post", idempotency_key="watch-post")
        self._barrier(released, "main thread did not release the watch")
        self._join(thread, "watch did not leave its bounded fixture")
        self.assertEqual([], errors)
        self.assertEqual(["initial", "change"], [row["kind"] for row in rows])
        changed = rows[1]["snapshot"]
        self.assertIsInstance(changed, dict)
        assert isinstance(changed, dict)
        watch_node = next(row for row in changed["nodes"] if row["node_id"] == "recipient")
        self.assertEqual(
            {"node": "recipient", "age_minutes": 10, "observed_at": "2026-08-30T13:10:00Z"},
            watch_node["oldest_unread"],
        )
        self.assertEqual([watch_post["id"]], [row["id"] for row in EventLog(self.root).records()])

        probe_root = self._new_root(self.base / "probe-reader")
        ticks = 0

        def probe_sleep(_seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                self._roll_success(root=probe_root, key="probe-roll")
            else:
                EventLog(probe_root).present("actor-a")

        report = DoctorProbe(probe_root, budget_seconds=2, sleeper=probe_sleep,
                             poll_interval_seconds=1).run(["actor-a"])
        self.assertEqual("PASS", report.by_node["actor-a"].verdict)
        live_probes = EventLog(probe_root).records()
        self.assertEqual(1, len(live_probes), "probe must retry once in the new epoch")
        probe_id = str(live_probes[0]["id"])
        probe_deliveries = decode_frames(
            probe_root.resolve_relative("receipts/deliveries/actor-a.jsonl").read_bytes()
        )
        self.assertEqual(1, len(probe_deliveries))
        self.assertEqual([probe_id], probe_deliveries[0]["item_ids"])
        self.assertEqual("delivery_probe", report.findings_by_node["actor-a"]["code"])

        waiter_root = self._new_root(self.base / "waiter-reader")
        waiter_pre = EventLog(waiter_root).send(
            "actor-b", "actor-a", "floati", "c" * 40,
            "docs/evidence/waiter-pre.md", "drained waiter seed",
            idempotency_key="waiter-pre",
        )
        EventLog(waiter_root).present("actor-a")
        SparseCursor(waiter_root).ack(
            "actor-a", [str(waiter_pre["id"])], acting_session_id="waiter-pre-ack"
        )
        workspace = self.base / "waiter-workspace"
        workspace.mkdir()
        mapping = waiter_root.resolve_relative("codex-wait/workspaces.v0.json")
        mapping.parent.mkdir(parents=True, exist_ok=True)
        mapping.write_text(json.dumps(
            {"schema_version": 0, "tenant_id": waiter_root.tenant_id,
             "mappings": [{"workspace": str(workspace), "node_id": "actor-a"}]},
            sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        participant = resolve_participant(waiter_root.path, workspace)
        self.assertIsNotNone(participant)
        assert participant is not None
        consent = CodexWaitConsentLedger(waiter_root).arm(
            participant.binding, hook_timeout_seconds=10, wait_deadline_seconds=2,
            idempotency_key="reader-consent")
        CodexWaitSessionLedger(waiter_root).arm(
            participant.binding, consent, "reader-session",
            idempotency_key="reader-session-claim")
        rolled = False
        waiter_post: dict[str, object] = {}

        def waiter_sleep(_seconds: float) -> None:
            nonlocal rolled
            if rolled:
                return
            rolled = True
            self._roll_success(root=waiter_root, key="waiter-roll")
            send_result = EventLog(waiter_root).send(
                "actor-b", "actor-a", "floati", "3" * 40,
                "docs/evidence/waiter.md", "waiter post", idempotency_key="waiter-post")
            waiter_post.update(send_result["message"])

        output = io.StringIO()
        waiter_errors = io.StringIO()
        clock_values = iter((0.0, 0.0, 1.0, 1.0, 2.0, 2.0))
        waiter_rc = run_stop_waiter(
            bus_home=waiter_root.path,
            hook_payload={"cwd": str(workspace), "session_id": "reader-session"},
            stdout=output, stderr=waiter_errors, monotonic=lambda: next(clock_values),
            sleep=waiter_sleep, wall_time=lambda: 1000.0, poll_interval_seconds=1)
        self.assertEqual(0, waiter_rc)
        waiter_artifact = json.loads(output.getvalue())
        expected_waiter = {
            "decision": "block",
            "reason": (
                "[floati] 1 new message(s) for actor-a: " + str(waiter_post["id"])
            ),
        }
        self.assertEqual(expected_waiter, waiter_artifact)
        self.assertEqual(
            json.dumps(expected_waiter) + "\n",
            output.getvalue(),
        )
        self.assertEqual("", waiter_errors.getvalue())

        daemon_root = self._new_root(self.base / "daemon-reader")
        daemon_pre = EventLog(daemon_root).send(
            "actor-a", "actor-b", "floati", "d" * 40,
            "docs/evidence/daemon-pre.md", "drained daemon seed",
            idempotency_key="daemon-pre",
        )
        EventLog(daemon_root).present("actor-b")
        SparseCursor(daemon_root).ack(
            "actor-b", [str(daemon_pre["id"])], acting_session_id="daemon-pre-ack"
        )
        daemon_workspace = self.base / "daemon-workspace"
        daemon_workspace.mkdir()
        executable = self.base / "daemon-adapter"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        coordinate = DaemonCoordinate(daemon_root, "recipient", "cursor")
        adapter = _DaemonAdapter(daemon_root, coordinate)
        AdapterBindingStore(daemon_root).write(
            coordinate, session_id="daemon-session", workspace=daemon_workspace,
            executable=executable, adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"), binding_epoch=1)
        DaemonConsentLedger(daemon_root).consent(
            coordinate, adapter_version="1", adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1, max_poll_seconds=4, max_backoff_seconds=8,
            activation_epoch=1, idempotency_key="daemon-consent")
        daemon = WakeDaemon(coordinate, adapter)
        self.assertEqual("idle", daemon.run_cycle(100.0)["state"])
        self._roll_success(root=daemon_root, key="daemon-roll")
        daemon_post = EventLog(daemon_root).send(
            "actor-a", "recipient", "floati", "4" * 40,
            "docs/evidence/daemon.md", "daemon post", worker_session_id="daemon-session",
            idempotency_key="daemon-post")
        self.assertEqual("woke", daemon.run_cycle(101.0)["state"])
        self.assertEqual(1, len(adapter.calls))
        self.assertIn(str(daemon_post["id"]), adapter.calls[0])
        self.assertEqual(
            [daemon_post["id"]], [row["id"] for row in EventLog(daemon_root).records()]
        )

    def test_external_tail_f_follower_reopens_recreated_event_path(self) -> None:
        pre = EventLog(self.root).send(
            "actor-a", "recipient", "floati", "5" * 40,
            "docs/evidence/tail-pre.md", "tail pre", idempotency_key="tail-pre")
        follower = subprocess.Popen(
            ["tail", "-n", "+1", "-F", str(self.root.resolve_relative("events.jsonl"))],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )

        def close_follower() -> None:
            if follower.poll() is None:
                follower.terminate()
            try:
                follower.wait(timeout=WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                follower.kill()
                follower.wait(timeout=WAIT_SECONDS)
            if follower.stdout is not None:
                follower.stdout.close()
            if follower.stderr is not None:
                follower.stderr.close()

        self.addCleanup(close_follower)
        self.assertIsNotNone(follower.stdout)
        assert follower.stdout is not None
        self.assertEqual(pre["id"], self._read_json_line(follower.stdout)["id"])
        self._roll_success(key="tail-roll")
        post = EventLog(self.root).send(
            "actor-a", "recipient", "floati", "6" * 40,
            "docs/evidence/tail-post.md", "tail post", idempotency_key="tail-post")
        seen: set[str] = set()
        for _line_number in range(4):
            row = self._read_json_line(follower.stdout)
            if isinstance(row.get("id"), str):
                seen.add(row["id"])
            if post["id"] in seen:
                break
        self.assertIn(post["id"], seen, "tail -F must reopen within four bounded lines")
        close_follower()

    def test_bounded_follower_reader_skips_a_transient_blank_line(self) -> None:
        """IN+VS-F1: follower timing noise must not surface as JSONDecodeError."""
        read_descriptor, write_descriptor = os.pipe()
        stream = os.fdopen(read_descriptor, "rb", buffering=0)
        self.addCleanup(stream.close)
        try:
            os.write(write_descriptor, b'\n{"id":"post"}\n')
        finally:
            os.close(write_descriptor)

        self.assertEqual({"id": "post"}, self._read_json_line(stream))


if __name__ == "__main__":
    unittest.main()
