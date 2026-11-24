"""Correlated, content-free testimony for one bounded native Stop hold."""

from __future__ import annotations

import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from .bus_epoch import shared_epoch_operation
from .codex_wait_contract import (
    CodexWaitConsentLedger, CodexWaitSessionLedger, WorkspaceBinding,
    resolve_participant,
)
from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import MAX_LEDGER_BYTES, _decode_path_records, read_records_snapshot, transact
from .records import validate_record
from .registry import Registry
from .root import FloatiRoot, validate_identifier
from .wake_control import is_session_paused, validate_session_id
from .wake_hold import wake_coordination_guard


KIND = "stop_hold_receipt"
_KINDS = frozenset({KIND})
_IMMUTABLE = (
    "schema_version", "kind", "tenant_id", "invocation_id", "node_id",
    "acting_session_id", "consent_receipt_id", "workspace", "harness",
    "started_at", "deadline_seconds",
)
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def _timestamp(value):
    if not isinstance(value, str) or _STAMP.fullmatch(value) is None:
        raise ProtocolRefusal("stop_hold_invalid", "hold time must be UTC milliseconds",
            remedy="supply an exact UTC millisecond timestamp for this native invocation")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolRefusal("stop_hold_invalid", "hold timestamp is invalid",
            remedy="supply an exact UTC millisecond timestamp for this native invocation") from exc


class StopHoldLedger:
    """One append-only start/exit pair; history is independent of live consent."""

    def __init__(self, root):
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "hold testimony requires a validated root",
                remedy="open the exact declared fleet root before recording hold testimony")
        self.root = root

    @staticmethod
    def relative(node_id):
        return Path("receipts/stop-holds") / (validate_identifier(node_id, "node") + ".jsonl")

    def _path(self, relative):
        """Reject aliases and nonordinary leaves before any read or write."""
        path = self.root.tenant_home / relative
        identities = []
        for coordinate in (self.root.tenant_home, *[
            self.root.tenant_home.joinpath(*relative.parts[:index])
            for index in range(1, len(relative.parts) + 1)
        ]):
            try:
                info = coordinate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (
                coordinate == path and not stat.S_ISREG(info.st_mode)
            ) or (coordinate != path and not stat.S_ISDIR(info.st_mode)):
                raise IntegrityFailure("stop_hold_path_invalid", "hold ledger coordinate is not ordinary")
            identities.append((coordinate, info.st_dev, info.st_ino))
        return path, identities

    def _write_path(self, node):
        relative = self.relative(node)
        self._path(relative)
        self._path(relative.with_name(relative.name + ".lock"))
        return relative

    @staticmethod
    def _progress(rows, node):
        invocations = {}
        for row in rows:
            try:
                _timestamp(row["started_at"])
                _timestamp(row["timestamp"])
            except ProtocolRefusal as exc:
                raise IntegrityFailure("stop_hold_progress_invalid", exc.detail) from exc
            if row["node_id"] != node:
                raise IntegrityFailure("stop_hold_progress_invalid", "hold ledger contains another node")
            invocation = row["invocation_id"]
            prior = invocations.setdefault(invocation, [])
            if row["phase"] == "start":
                if prior:
                    raise IntegrityFailure("stop_hold_progress_invalid", "hold invocation has duplicate starts")
            elif len(prior) != 1 or prior[0]["phase"] != "start" or any(
                row[field] != prior[0][field] for field in _IMMUTABLE
            ) or any(row.get(field) != prior[0].get(field)
                     for field in ("holder_pid", "process_start_epoch")):
                raise IntegrityFailure("stop_hold_progress_invalid", "hold exit has no exact unmatched start")
            prior.append(row)
        return invocations

    def read(self, node_id):
        """Read bounded ordinary bytes without creating a path or taking a lock."""
        node = validate_identifier(node_id, "node")
        relative = self.relative(node)
        path, parents = self._path(relative)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return []
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise IntegrityFailure("stop_hold_path_invalid", "hold ledger is not an ordinary file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_LEDGER_BYTES + 1)
            current = path.lstat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise IntegrityFailure("stop_hold_path_invalid", "hold ledger changed during observation")
            for coordinate, device, inode in parents:
                current = coordinate.lstat()
                if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (device, inode):
                    raise IntegrityFailure("stop_hold_path_invalid", "hold ledger parent changed during observation")
        finally:
            os.close(descriptor)
        rows = _decode_path_records(path, self.root.tenant_id, _KINDS, data)
        self._progress(rows, node)
        return rows

    @shared_epoch_operation
    def start(self, binding, consent, acting_session_id, *, started_at, deadline_seconds,
              _timing=None, _on_started=None):
        if not isinstance(binding, WorkspaceBinding):
            raise ProtocolRefusal("stop_hold_identity_invalid", "hold binding is invalid",
                remedy="resolve this exact workspace and its armed native consent before recording a hold")
        with wake_coordination_guard(self.root, binding.node_id):
            return self._start_already_guarded(binding, consent, acting_session_id,
                started_at=started_at, deadline_seconds=deadline_seconds, _timing=_timing,
                _on_started=_on_started)

    def _start_already_guarded(self, binding, consent, acting_session_id, *, started_at,
                               deadline_seconds, _timing=None, _on_started=None):
        """Called only while the caller holds this root's epoch and node guard."""
        _timestamp(started_at)
        if not isinstance(binding, WorkspaceBinding):
            raise ProtocolRefusal("stop_hold_identity_invalid", "hold binding is invalid",
                remedy="resolve this exact workspace and its armed native consent before recording a hold")
        node, session = CodexWaitSessionLedger(self.root)._identity(binding, consent, acting_session_id)
        validate_record(consent, self.root.tenant_id, {"codex_wait_consent_receipt"}, integrity=False)
        current = resolve_participant(self.root.tenant_home, binding.workspace)
        harness = str(Registry(self.root).require_active(node)["role"]).casefold()
        if current is None or current.binding.node_id != node or current.binding.workspace != binding.workspace or (
            harness not in {"codex", "cursor", "zcode", "claude"}
        ) or CodexWaitConsentLedger(self.root).require_armed(current.binding) != consent:
            raise ProtocolRefusal("stop_hold_identity_invalid", "hold binding, harness or consent is no longer current",
                remedy="resolve this exact workspace and its armed native consent before recording a hold")
        if not isinstance(deadline_seconds, (int, float)) or isinstance(deadline_seconds, bool) or (
            not 0 < deadline_seconds <= 86400 or not math.isfinite(deadline_seconds)
            or deadline_seconds > consent["wait_deadline_seconds"]
        ):
            raise ProtocolRefusal("stop_hold_invalid", "hold budget exceeds its armed consent",
                remedy="record the finite native hold budget within its referenced consent deadline")
        claims = read_records_snapshot(self.root, CodexWaitSessionLedger._relative(node),
            allowed_kinds={"codex_wait_session_receipt"})
        matching = [row for row in claims if row["workspace"] == str(binding.workspace)]
        claim = matching[-1] if matching else None
        # A claim retains the consent under which it was armed. Native
        # participation permits its current session to use replacement
        # consent; the exact current consent was checked above under guard.
        if (claim is None and harness != "cursor") or (claim is not None and (
            claim["acting_session_id"] != session
        )) or is_session_paused(self.root, node, session):
            raise ProtocolRefusal("stop_hold_identity_invalid", "hold does not have current unpaused session authority",
                remedy="preserve the current session claim and pause evidence; only its authorized native waiter may record a hold")
        identity = "stop-hold-" + uuid7_hex()
        row = {"schema_version": 1, "id": identity, "tenant_id": self.root.tenant_id,
            "timestamp": started_at, "kind": KIND, "invocation_id": identity,
            "node_id": node, "acting_session_id": session, "consent_receipt_id": consent["id"],
            "workspace": str(binding.workspace), "harness": harness, "started_at": started_at,
            "deadline_seconds": deadline_seconds, "phase": "start"}
        if harness in {"cursor", "codex", "zcode", "claude"}:
            from .codex_wait_liveness import process_start_epoch
            birth = process_start_epoch(os.getpid())
            if birth is not None:
                row.update(holder_pid=os.getpid(), process_start_epoch=birth)
        validate_record(row, self.root.tenant_id, _KINDS, integrity=False)
        relative = self._write_path(node)
        def decide(prior):
            self._path(relative)
            self._progress(prior, node)
            if _timing is not None:
                stamp, budget = _timing()
                _timestamp(stamp)
                if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not (
                    math.isfinite(budget) and 0 < budget <= consent["wait_deadline_seconds"]
                ):
                    from .waiter_lock_retry import WaiterLockDeadline
                    raise WaiterLockDeadline("stop hold start", contended=False)
                row.update(timestamp=stamp, started_at=stamp, deadline_seconds=budget)
                validate_record(row, self.root.tenant_id, _KINDS, integrity=False)
            return row, row
        result = transact(self.root, relative, decide, allowed_kinds=_KINDS)
        if _on_started is not None:
            _on_started(result)
        return result

    @shared_epoch_operation
    def close(self, start, *, timestamp, _on_closing=None):
        validate_record(start, self.root.tenant_id, _KINDS, integrity=False)
        with wake_coordination_guard(self.root, start["node_id"]):
            if _on_closing is not None:
                _on_closing()
            return self._close_already_guarded(start, timestamp=timestamp)

    def _close_already_guarded(self, start, *, timestamp):
        """Close original testimony under the existing epoch and node guard."""
        validate_record(start, self.root.tenant_id, _KINDS, integrity=False)
        end = _timestamp(timestamp)
        if start["phase"] != "start" or end < _timestamp(start["started_at"]):
            raise ProtocolRefusal("stop_hold_close_conflict", "close requires a start and a time at or after it",
                remedy="close only the exact retained native start with its original invocation fields")
        node = start["node_id"]
        relative = self._write_path(node)
        row = dict(start, id="stop-hold-" + uuid7_hex(), phase="exit", timestamp=timestamp)
        def decide(prior):
            self._path(relative)
            groups = self._progress(prior, node)
            matching = groups.get(start["invocation_id"], [])
            if not matching or matching[0] != start:
                raise ProtocolRefusal("stop_hold_close_conflict", "close does not name the exact retained start",
                    remedy="close only the exact retained native start with its original invocation fields")
            if len(matching) == 2:
                return matching[1], None
            return row, row
        return transact(self.root, relative, decide, allowed_kinds=_KINDS)


class _HoldEvidence:
    """Native scope; acquired Codex holds require terminal proof before output."""

    def __init__(self, stderr, timestamp):
        self.stderr = stderr
        self.timestamp = timestamp
        self.ledger = None
        self.start_row = None
        self.close_attempted = False
        self.holder_row = None
        self.closure_proven = False

    def _report(self, phase, exc):
        if self.stderr is not None:
            try:
                self.stderr.write("floati waiter evidence unavailable: stop_hold_"
                    + phase + ": " + type(exc).__name__ + "\n")
                self.stderr.flush()
            except Exception:
                pass

    def start(self, participant, consent, session, deadline_seconds, *, already_guarded=False):
        try:
            harness = Registry(participant.root).require_active(participant.binding.node_id)["role"].casefold()
            if harness not in {"codex", "cursor", "zcode", "claude"}:
                # An old role label is intentional nonparticipation in this
                # new testimony contract, not a failed evidence write.
                return
            self.ledger = StopHoldLedger(participant.root)
            method = self.ledger._start_already_guarded if already_guarded else self.ledger.start
            from .waiter_lock_retry import current_lock_window
            window = current_lock_window()
            timing = {} if window is None else {"_timing": lambda: (self.timestamp(), window.remaining())}
            self.start_row = method(participant.binding, consent, session,
                started_at=self.timestamp(), deadline_seconds=deadline_seconds,
                _on_started=self._acquired, **timing)
        except Exception as exc:
            from .waiter_lock_retry import (current_lock_window, transient_ledger_timeout,
                WaiterAuthorityChanged, WaiterLockDeadline)
            if current_lock_window() is not None and (transient_ledger_timeout(exc)
                    or isinstance(exc, (WaiterAuthorityChanged, WaiterLockDeadline))):
                raise
            self._report("start", exc)

    def _acquired(self, start):
        """Run inside the actual start's epoch and recipient guard."""
        self.start_row = start
        if start["harness"] not in {"codex", "cursor"}:
            return
        try:
            from .codex_wait_liveness import write_holder_testimony
            self.holder_row = write_holder_testimony(self.ledger.root,
                start["node_id"], start["acting_session_id"],
                invocation_id=start["invocation_id"])
        except Exception as exc:
            self._report("holder", exc)

    def _retire(self):
        if self.start_row["harness"] not in {"codex", "cursor"}:
            return False
        try:
            from .codex_wait_liveness import _owned_holder_for_start, _retire_holder_invocation
            owned = self.holder_row or _owned_holder_for_start(self.ledger.root, self.start_row)
            if owned is not None:
                _retire_holder_invocation(self.ledger.root, self.start_row["node_id"], owned)
        except Exception as exc:
            self._report("holder_exit", exc)
        from .codex_wait_liveness import _holder_invocation_absent
        return _holder_invocation_absent(self.ledger.root, self.start_row["node_id"],
            self.start_row["invocation_id"])

    def _exit_proven(self):
        try:
            rows = [row for row in self.ledger.read(self.start_row["node_id"])
                if row["invocation_id"] == self.start_row["invocation_id"]]
            return len(rows) == 2 and rows[0] == self.start_row and rows[1]["phase"] == "exit"
        except Exception as exc:
            self._report("exit_read", exc)
            return False

    def close(self, *, already_guarded=False, require_proof=False):
        """Cleanup retries; acquired Codex publication raises unless closure is proved."""
        if self.start_row is None:
            return True
        codex = self.start_row["harness"] == "codex"
        if not self.close_attempted:
            retired = False
            def retire():
                nonlocal retired
                retired = self._retire()
            try:
                if already_guarded:
                    retire()
                    self.ledger._close_already_guarded(self.start_row, timestamp=self.timestamp())
                else:
                    self.ledger.close(self.start_row, timestamp=self.timestamp(), _on_closing=retire)
            except Exception as exc:
                self._report("exit", exc)
            # A callback's normal return is not proof. Reread the exact native
            # exit when retirement did not establish absence. Failed Codex
            # attempts remain retryable; other harnesses retain best effort.
            self.closure_proven = (retired or self._exit_proven()) if codex else False
            self.close_attempted = self.closure_proven if codex else True
        if codex and require_proof and not self.closure_proven:
            raise ProtocolRefusal("stop_hold_closure_unproven",
                "native Codex hold closure is unproved; output was withheld",
                remedy="retain the exact native hold and holder evidence; retry closure through its owned scope before publishing output")
        return self.closure_proven if codex else True

    def output(self, output):
        evidence = self
        class Output:
            def _close_hold_already_guarded(self):
                evidence.close(already_guarded=True, require_proof=True)
            def write(self, value):
                if value:
                    evidence.close(require_proof=True)
                return output.write(value)
            def flush(self):
                return output.flush()
        return Output()


def _utc_milliseconds(value):
    """Normalize a native clock's supplied timestamp, never infer an identity."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ProtocolRefusal("stop_hold_invalid", "native hold clock is not timezone-aware",
            remedy="supply an explicit UTC native waiter clock for hold testimony")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
