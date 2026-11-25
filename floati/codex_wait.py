"""Bounded Codex Stop waiter for explicitly participating Floati workspaces."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from functools import partial, wraps
import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, TextIO

from .codex_wait_contract import (
    CodexWaitConsentLedger,
    CodexWaitReceiptLedger,
    CodexWaitReopenLedger,
    CodexWaitSessionLedger,
    DEFAULT_WAIT_DEADLINE_SECONDS,
    WatchedLedger,
    WORKSPACE_MAP_RELATIVE,
    consent_ledger_relative,
    resolve_participant,
)
from .errors import FloatiError, IntegrityFailure, ProtocolRefusal
from .installed_reader import raise_reader_failure, reader_failure
from .ids import uuid7_hex
from .wake_control import validate_session_id
from .wake_exit import WakeExitLedger
from .wake_hold import WakeAttemptLedger, WakeHoldController
from .wake_hints import (
    ROOT_HINT_RELATIVE, SAFETY_RECHECK_SECONDS, recipient_hint_relative,
    wait_readable as native_wait_readable,
)


BREAKER_WINDOW_SECONDS = 60.0
BREAKER_MAX_INVOCATIONS = 20


class _WaiterInputsWatch:
    """Notifications invalidate an idle decision; they never grant authority.

    Register before evaluation and consume pending events only before the next
    evaluation. Writes racing with a read therefore remain dirty. Fingerprints
    also reconcile replacements/membership; they alone never enable reuse.
    """

    def __init__(self, participant, session_digest, stderr, *, workspace=None):
        from .cursor_presentations import CursorPresentationLedger
        from .jsonl import _lock_beside
        root, node = participant.root, participant.binding.node_id
        self.root = root
        self.node = node
        self._backend_observation = None
        self.workspace = Path(workspace) if workspace is not None else participant.binding.workspace
        leaves = tuple(root.tenant_home / path for path in (
            Path("events.jsonl"), Path("registry/entries.jsonl"), WORKSPACE_MAP_RELATIVE,
            Path("receipts/deliveries") / (node + ".jsonl"),
            Path("receipts/acks") / (node + ".jsonl"),
            CodexWaitSessionLedger._relative(node), consent_ledger_relative(node),
            CursorPresentationLedger.relative(node),
            CursorPresentationLedger.witness_relative(node),
            Path("receipts/wake-control") / (node + ".jsonl"),
            Path("receipts/seat-teardown") / (node + ".jsonl"),
            Path("receipts/stop-holds") / (node + ".jsonl"),
            Path("state/ledger-policy.json"),
            Path("state/wake-control") / node / (session_digest + ".json"),
            Path("state/codex-wait") / node / "holder.json",
            Path("receipts/wake-coordination") / node / "lane.lock",
            Path("tenants"),
            ROOT_HINT_RELATIVE, recipient_hint_relative(node),
        ))
        self.fixed = leaves + tuple(_lock_beside(path, path.relative_to(root.tenant_home))[0]
            for path in leaves if path.suffix == ".jsonl")
        self.paths = self.fixed
        self.observed_paths = ()
        self.complete = False
        self.metadata_unknown = False
        self.source = None
        self.stderr = stderr
        self.warned = False
        self.generation_unstable = False
        from .waiter_state import prepare_waiter_observation

        prepare_waiter_observation(root)
        self._bind_inputs()

    def _bind_inputs(self):
        from .bus_epoch import epoch_guard
        # Native rolls cannot change lineage between census and registration.
        # Other namespace changes must remain dirty across the same boundary.
        try:
            with epoch_guard(self.root):
                initial = self._generation()
                initial_unknown = self.metadata_unknown
                old_source, old_paths = self.source, self.paths
                self._refresh_inputs()
                if self.paths != old_paths or old_source is None or not self.complete:
                    self.source = None
                    self._register()
                    if old_source is not None:
                        old_source.close()
                self.before = self._fingerprints()
                baseline_unknown = self.metadata_unknown
                self.generation_unstable = (initial != self._generation()
                    or initial_unknown or baseline_unknown or self.metadata_unknown
                    or not self._census_matches())
        except (FloatiError, OSError, ValueError) as exc:
            self.complete = False
            self.generation_unstable = True
            self._unavailable(exc)
            self.before = self._fingerprints()

    def _generation(self):
        paths = set(self.fixed)
        full = set(self.fixed)
        for leaf in self.fixed:
            paths.update(leaf.parents)
            full.update(parent for parent in leaf.parents
                if parent == self.root.tenant_home or self.root.tenant_home in parent.parents)
        paths.update((self.workspace, *self.workspace.parents))
        # External ancestors establish identity, not selected membership. Their
        # unrelated siblings must not invalidate unchanged native ledger reads.
        return self._fingerprint_paths(tuple(sorted(paths)), full=full)

    def _has_epoch_receipt(self):
        """A bounded first-frame hint; native evaluation retains validation."""
        from .cursor_presentations import CursorPresentationLedger
        if CursorPresentationLedger(self.root)._cold_source_has_no_roll():
            return False
        from .framing import decode_frames
        from .jsonl import MAX_RECORD_BYTES
        from .recipient_mechanisms import _parent, _signature
        path = self.root.tenant_home / "events.jsonl"
        parent = _parent(path)
        try:
            try:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent)
            except FileNotFoundError:
                return False
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise OSError("event hint is not an ordinary single-link file")
                frame = stream.readline(MAX_RECORD_BYTES + 1)
                after = os.fstat(stream.fileno())
                named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if _signature(before) != _signature(after) or _signature(after) != _signature(named):
                    raise OSError("event hint changed")
            if len(frame) > MAX_RECORD_BYTES:
                raise OSError("event hint exceeds frame bound")
            rows = decode_frames(frame)
            if not rows:
                return False
            if not isinstance(rows[0], dict):
                raise ValueError("event hint is not an object")
            return rows[0].get("kind") == "bus_epoch_roll_receipt"
        finally:
            os.close(parent)

    def _register(self):
        from .tui import BoardFilesystemWakeup
        if not self.complete:
            return
        try:
            self.source = BoardFilesystemWakeup(self.root.tenant_home, _paths=self.paths)
        except (OSError, ProtocolRefusal) as exc:
            self._unavailable(exc)

    @staticmethod
    def _archive_paths(archive, selected, signatures=None):
        """Census only a native receipt-owned archive, through retained dirfds."""
        from .recipient_mechanisms import _parent
        def accept(path):
            if path not in selected and len(selected) >= 4096:
                raise OSError("selected archive watch budget exceeded")
            selected.add(path)
        def visit(path, descriptor, depth):
            if depth > 16 or len(selected) > 4096:
                raise OSError("selected archive watch budget exceeded")
            before = os.fstat(descriptor)
            accept(path)
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    name = entry.name
                    child = path / name
                    accept(child)
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise OSError("selected archive alias")
                    if stat.S_ISDIR(info.st_mode):
                        following = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
                        try:
                            opened = os.fstat(following)
                            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                                raise OSError("selected archive directory changed")
                            visit(child, following, depth + 1)
                        finally:
                            os.close(following)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                raise OSError("selected archive membership changed")
            if signatures is not None:
                signatures[path] = (after.st_dev, after.st_ino, after.st_mode,
                    after.st_uid, after.st_nlink, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)
        try:
            info = archive.lstat()
        except OSError as exc:
            raise OSError("selected archive is unavailable") from exc
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            accept(archive)
            if signatures is not None:
                signatures[archive] = (info.st_dev, info.st_ino, info.st_mode,
                    info.st_uid, info.st_nlink, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns)
            return
        descriptor = _parent(archive / "coordinate")
        try:
            visit(archive, descriptor, 0)
        finally:
            os.close(descriptor)

    def _refresh_inputs(self):
        from .bus_epoch import _epoch_transition_pending
        from .snapshot import _owned_epoch_archives
        selected = set(self.fixed)
        self.census_signatures = {}
        self.complete = False
        try:
            # A surviving marker quarantines selected bytes, including the
            # first-frame hint. Fixed metadata and the root namespace suffice
            # to observe recovery; marker removal rebuilds normal lineage.
            pending = _epoch_transition_pending(self.root)
            for archive in _owned_epoch_archives(self.root) if not pending and self._has_epoch_receipt() else ():
                self._archive_paths(archive, selected, self.census_signatures)
            # Include dependency namespaces themselves, so both backends see
            # pending epoch markers and parent replacement with unchanged leaves.
            for path in tuple(selected):
                while path != self.root.tenant_home:
                    path = path.parent
                    selected.add(path)
            if len(selected) > 4096:
                raise OSError("selected watch budget exceeded")
            self.complete = True
        except (FloatiError, OSError, ValueError):
            # A hint cannot hide unreadable lineage. The native evaluator must
            # run and supply its authoritative typed result instead.
            selected = set(self.fixed) | {self.root.tenant_home}
        self.paths = tuple(sorted(selected))
        ancestry = {self.workspace, *self.workspace.parents, *self.root.tenant_home.parents}
        self.observed_paths = tuple(sorted(selected | ancestry))

    def _census_matches(self):
        for path, before in self.census_signatures.items():
            try:
                after = path.lstat()
            except OSError:
                return False
            if before != (after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                    after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return False
        return True

    def _unavailable(self, exc):
        if self.source is not None:
            self.source.close()
            self.source = None
        if not self.warned:
            self.warned = True
            self.stderr.write("floati waiter change notifications unavailable: "
                              + type(exc).__name__ + "; using bounded polling\n")
            self.stderr.flush()

    def _fingerprints(self):
        from .memo_key_closure import projection_binding_snapshot
        coordinates = self._fingerprint_paths(self.observed_paths, full=self.paths)
        try:
            semantics = projection_binding_snapshot()
        except Exception:
            # An advisory observation cannot certify reuse when the native
            # projection's runtime dependencies cannot be identified.
            self.metadata_unknown = True
            semantics = None
        return coordinates, semantics

    def _fingerprint_paths(self, paths, *, full):
        result = []
        full = set(full)
        self.metadata_unknown = False
        for path in paths:
            try:
                value = path.lstat()
                identity = (value.st_dev, value.st_ino, value.st_mode, value.st_uid)
                result.append(identity if path not in full else identity +
                              (value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns))
            except FileNotFoundError:
                result.append(None)
            except OSError as exc:
                self.metadata_unknown = True
                result.append((type(exc).__name__, exc.errno))
        return tuple(result)

    def changed(self):
        import select
        uncertain = self.metadata_unknown
        current = self._fingerprints()
        dirty = (current != self.before or uncertain or self.metadata_unknown
            or not self.complete or self.generation_unstable)
        # Retain the pre-evaluation snapshot, not a post-read one that could
        # silently absorb a concurrent append the evaluator has not read.
        self.before = current
        fallback = self.source is None
        try:
            if self.source is not None and select.select([self.source], [], [], 0)[0]:
                dirty = self.source.drain() or dirty
        except (OSError, ProtocolRefusal) as exc:
            self._unavailable(exc)
            return True, True
        if dirty:
            self._bind_inputs()  # Register a new lineage before its evaluation.
        return dirty or fallback, dirty

    def close(self):
        if self.source is not None:
            self.source.close()
            self.source = None

    def wait(self, timeout_seconds, *, wait_readable, sleep, poll_seconds):
        """Block without a ledger/epoch lock, within the existing time budget."""
        timeout = min(SAFETY_RECHECK_SECONDS, max(0.0, timeout_seconds))
        if timeout <= 0:
            return False
        if wait_readable is native_wait_readable and sleep is not time.sleep:
            # Existing callers supply a clock/sleeper pair. Keep both halves
            # in that time domain unless they explicitly supply readiness too.
            self._observe_backend("polling")
            sleep(min(poll_seconds, timeout))
            return False
        if self.source is not None:
            try:
                self._observe_backend("blocking")
                return bool(wait_readable(self.source, timeout))
            except (OSError, ProtocolRefusal) as exc:
                self._unavailable(exc)
        self._observe_backend("polling")
        sleep(min(poll_seconds, timeout))
        return False

    def _observe_backend(self, mode):
        from .waiter_state import write_waiter_observation

        now = datetime.now(timezone.utc)
        previous = self._backend_observation
        # At most one renewal a minute, including the one-second lease checks.
        if previous is not None and previous[0] == mode and now < previous[1]:
            return
        if write_waiter_observation(self.root, self.node, mode,
                now + timedelta(seconds=120), observed_at=now):
            self._backend_observation = (mode, now + timedelta(seconds=60))


def _report_evidence_failure(
    stderr: Optional[TextIO], operation: str, failure: Exception
) -> None:
    """Name one failed evidence write without disclosing exception content."""

    if stderr is None:
        return
    try:
        stderr.write(
            "floati waiter evidence unavailable: "
            f"{operation}: {type(failure).__name__}\n"
        )
        stderr.flush()
    except Exception as exc:
        raise_reader_failure(exc)
        return


def _record_exit(
    participant: object,
    *,
    session_digest: str,
    reason_code: str,
    waited_seconds: int,
    invocation_id: str,
    stderr: Optional[TextIO],
    integrity_failure: Optional[dict] = None,
    timestamp: Optional[str] = None,
) -> bool:
    """Best-effort exit testimony must never widen the Stop-hook outcome."""

    try:
        WakeExitLedger(participant.root).record(
            node_id=participant.binding.node_id,
            session_digest=session_digest,
            reason_code=reason_code,
            waited_seconds=waited_seconds,
            idempotency_key=f"{invocation_id}-exit-{reason_code}",
            **({"integrity_failure": integrity_failure} if integrity_failure is not None else {}),
            **({"timestamp": timestamp} if timestamp is not None else {}),
        )
        return True
    except Exception as exc:
        raise_reader_failure(exc)
        _report_evidence_failure(stderr, "wake_exit", exc)
        return False


def _integrity_diagnostic(
    participant: object, session_digest: str, operation: str,
    failure: Exception, deadline_seconds: int,
    wall_time: Callable[[], float] = time.time,
) -> tuple[dict, float]:
    """Retain typed check identity, never arbitrary exception content."""

    check, exception_class = operation, type(failure).__name__
    identity = {"operation": operation}
    current = failure
    seen = set()
    for _ in range(16):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, FloatiError) and re.fullmatch(r"[a-z][a-z0-9_.:-]{0,127}", current.code):
            check, exception_class = current.code, type(current).__name__
        coordinate = getattr(current, "waiter_input_identity", None)
        if isinstance(coordinate, dict):
            identity = dict(coordinate, operation=operation)
        current = current.__cause__ or current.__context__
    window = deadline_seconds or DEFAULT_WAIT_DEADLINE_SECONDS
    remaining = 0.0
    diagnostic = {
        "check_id": check,
        "exception_class": exception_class,
        "input_identity": identity,
        "occurrence_count": 1,
        "hold_window_seconds": window,
    }
    try:
        from .jsonl import read_records_snapshot

        rows = read_records_snapshot(
            participant.root,
            Path("receipts/wake-waiter-exit") / f"{participant.binding.node_id}.jsonl",
            allowed_kinds={"wake_waiter_exit_receipt"},
        )
        now = datetime.fromtimestamp(wall_time(), timezone.utc)
        inherited_window = False
        for row in reversed(rows):
            if row.get("session_digest") != session_digest:
                continue
            age = max(0.0, (now - datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))).total_seconds())
            previous = row.get("integrity_failure")
            if not isinstance(previous, dict):
                continue
            proven_window = previous.get("hold_window_seconds")
            if (
                not deadline_seconds and not inherited_window
                and isinstance(proven_window, int)
                and not isinstance(proven_window, bool) and proven_window > 0
            ):
                # Consent may be unreadable now; retain the last proven wait
                # bound instead of exceeding that installation's timeout.
                window = proven_window
                diagnostic["hold_window_seconds"] = window
                inherited_window = True
            previous_identity = previous.get("input_identity")
            if not isinstance(previous_identity, dict):
                continue
            # A fault in one input can be observed at different reader stages.
            # Stage names are useful testimony but are not the input's identity.
            previous_input = {key: value for key, value in previous_identity.items() if key != "operation" or "path" not in previous_identity}
            current_input = {key: value for key, value in identity.items() if key != "operation" or "path" not in identity}
            if age >= window:
                continue
            if previous_input == current_input and all(previous.get(key) == diagnostic[key] for key in ("check_id", "exception_class")):
                diagnostic["occurrence_count"] = min(100000, int(previous["occurrence_count"]) + 1)
                diagnostic["hold_window_seconds"] = window
                remaining = window - age
                break
    except Exception as exc:
        raise_reader_failure(exc)
        # Without prior testimony the first/repeat distinction is unproven.
        # Hold conservatively rather than turning a receipt fault into a spin.
        remaining = float(window)
    return diagnostic, remaining


def _set_waiter_result(target, kind, **details):
    # Invocation-local handoff, never another ledger reader or writer. A proven
    # publication cannot be erased by later best-effort evidence failures.
    if target is not None and target.get("kind") != "fresh_work":
        # Only a selection's publication or exhaustion carries its read
        # evidence. A later authority/stop result has its own exact contract,
        # even when an earlier idle poll selected in this invocation.
        sealed_read = target.get("sealed_read") if kind in {"fresh_work", "exhausted"} else None
        target.clear()
        target.update(kind=kind, **details)
        if sealed_read is not None:
            target["sealed_read"] = sealed_read


def _epoch_maintenance_failure(failure):
    """Retain Stop's native consumption boundary and original parser cause."""
    identity = getattr(failure, "waiter_input_identity", None)
    coordinate = identity.get("path") if isinstance(identity, dict) else None
    selected_input = (coordinate == "events.jsonl" or (
        isinstance(coordinate, str)
        and coordinate.startswith(("receipts/deliveries/", "receipts/acks/"))))
    if (isinstance(failure, IntegrityFailure) and selected_input
            and failure.code != "consumption_state_unavailable"):
        from .wake_hold import _unavailable
        wrapped = _unavailable("wake consumption prefix is invalid")
        wrapped.__cause__ = failure
        return wrapped
    return failure


def _integrity_exit(
    participant: object, *, session_digest: str, operation: str,
    failure: Exception, waited_seconds: int, invocation_id: str,
    deadline_seconds: int, stdout: TextIO, stderr: TextIO,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall_time: Callable[[], float] = time.time,
    _waiter_result=None,
    _held_evidence=None,
    _publish_terminal=None,
) -> int:
    from .waiter_lock_retry import authority_change_failure, current_lock_window, lock_deadline_failure

    change = authority_change_failure(failure)
    if change is not None:
        window = current_lock_window()
        waited_seconds = max(waited_seconds, int(window.clock() - window.started))
        window.finish()
        _record_exit(participant, session_digest=session_digest,
            reason_code=change.reason, waited_seconds=waited_seconds,
            invocation_id=invocation_id, stderr=stderr)
        stdout.write("{}\n")
        stdout.flush()
        _set_waiter_result(_waiter_result, "authority_changed", reason=change.reason, decision={})
        return 0

    deadline_failure = lock_deadline_failure(failure)
    if deadline_failure is not None:
        from .waiter_lock_retry import current_lock_window
        window = current_lock_window()
        waited_seconds = max(waited_seconds, int(window.clock() - window.started))
        window.finish()
        publishing = False
        def publish_exhaustion(*, guarded):
            nonlocal publishing
            _record_exit(participant, session_digest=session_digest,
                reason_code="exhausted", waited_seconds=waited_seconds,
                invocation_id=invocation_id, stderr=stderr)
            try:
                CodexWaitReceiptLedger(participant.root).record_exhaustion(
                    node_id=participant.binding.node_id, session_digest=session_digest,
                    waited_seconds=waited_seconds, idempotency_key=invocation_id + "-exhaustion")
            except Exception as exc:
                raise_reader_failure(exc)
                _report_evidence_failure(stderr, "exhaustion", exc)
            if guarded and hasattr(stdout, "_close_hold_already_guarded"):
                stdout._close_hold_already_guarded()
            contention = " while a ledger lock remained contended" if deadline_failure.contended else ""
            publishing = True
            decision = {"decision": "block", "reason":
                "(floati: wait deadline exhausted" + contention + "; end this turn to re-arm)"}
            if guarded and _held_evidence is not None:
                try:
                    decision["reason"] += _held_evidence()
                except Exception as exc:
                    raise_reader_failure(exc)
                    _report_evidence_failure(stderr, "presentation_state_unverified", exc)
                    decision["reason"] += "\n[floati] presentation_state_unverified; preserve native inbox and ACK recovery."
            def output():
                stdout.write(json.dumps(decision) + "\n")
                stdout.flush()
                _set_waiter_result(_waiter_result, "exhausted", decision=decision)
            if _publish_terminal is None:
                output()
            else:
                _publish_terminal(output, guarded=guarded)
        from .bus_epoch import epoch_guard
        from .wake_hold import wake_coordination_guard
        try:
            with epoch_guard(participant.root), wake_coordination_guard(participant.root, participant.binding.node_id):
                if window.check_authority is not None:
                    window.check_authority(recheck_binding=window.contended)
                publish_exhaustion(guarded=True)
        except Exception as exc:
            if publishing:
                raise
            changed = authority_change_failure(exc)
            if changed is not None:
                return _integrity_exit(participant, session_digest=session_digest, operation=operation,
                    failure=exc, waited_seconds=waited_seconds, invocation_id=invocation_id,
                    deadline_seconds=deadline_seconds, stdout=stdout, stderr=stderr,
                    monotonic=monotonic, sleep=sleep, wall_time=wall_time, _waiter_result=_waiter_result,
                    _publish_terminal=_publish_terminal)
            if lock_deadline_failure(exc) is None and not (
                isinstance(exc, ProtocolRefusal) and exc.code == "epoch_lock_timeout"
            ):
                raise
            # Exhausted guards get one nonblocking attempt. Contention names
            # exhaustion without another evaluator or a second hold window.
            if window.check_authority is not None:
                try:
                    window.check_authority(recheck_binding=window.contended)
                except Exception as changed:
                    if authority_change_failure(changed) is None:
                        raise
                    return _integrity_exit(participant, session_digest=session_digest, operation=operation,
                        failure=changed, waited_seconds=waited_seconds, invocation_id=invocation_id,
                        deadline_seconds=deadline_seconds, stdout=stdout, stderr=stderr,
                        monotonic=monotonic, sleep=sleep, wall_time=wall_time, _waiter_result=_waiter_result,
                        _publish_terminal=_publish_terminal)
            publish_exhaustion(guarded=False)
        return 0
    window = current_lock_window()
    residual = None if window is None else window.remaining()
    if window is not None:
        waited_seconds = max(waited_seconds, int(window.clock() - window.started))
        window.finish()
    diagnostic, remaining = _integrity_diagnostic(
        participant, session_digest, operation, failure, deadline_seconds, wall_time,
    )
    started = monotonic()
    # A fault must fit inside the same bounded invocation as a healthy wait.
    budget = max(0.0, diagnostic["hold_window_seconds"] - waited_seconds)
    if residual is not None:
        budget = min(budget, residual)

    # Private native read-time testimony is invalidation evidence only. In
    # particular, do not sample a new baseline here: the input may already
    # have been repaired since the failed read. No version enters a receipt.
    fault_witness = None
    current, seen = failure, set()
    for _ in range(16):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        witness = getattr(current, "waiter_input_witness", None)
        if (isinstance(witness, tuple) and len(witness) == 2
                and isinstance(witness[0], Path)
                and isinstance(witness[1], tuple) and len(witness[1]) == 7
                and all(isinstance(value, int) and not isinstance(value, bool)
                        for value in witness[1])):
            try:
                relative = witness[0].relative_to(participant.root.path)
                if relative.parts and not any(part in (".", "..") for part in relative.parts):
                    fault_witness = witness
                    break
            except ValueError:
                pass
        current = current.__cause__ or current.__context__

    def fault_input_changed():
        if fault_witness is None:
            return False
        path, original_version = fault_witness
        try:
            if not stat.S_ISDIR(os.lstat(participant.root.path).st_mode):
                return True
            # A newly redirected path is another invalidation, never a reason
            # to follow it or to certify the replacement's contents.
            if path.resolve(strict=False) != path:
                return True
            value = os.lstat(path)
            version = (value.st_dev, value.st_ino, value.st_mode, value.st_size,
                       value.st_mtime_ns, value.st_ctime_ns, value.st_nlink)
            return version != original_version
        except (OSError, RuntimeError):
            return True

    input_changed = fault_input_changed()

    def hold(seconds: float) -> None:
        nonlocal input_changed
        deadline = started + min(seconds, budget)
        while not input_changed and monotonic() < deadline:
            sleep(min(1.0, deadline - monotonic()))
            input_changed = fault_input_changed()

    hold(remaining)
    recorded = _record_exit(
        participant, session_digest=session_digest, reason_code="integrity_failure",
        waited_seconds=waited_seconds + max(0, round(monotonic() - started)),
        invocation_id=invocation_id, stderr=stderr,
        integrity_failure=diagnostic,
        timestamp=datetime.fromtimestamp(wall_time(), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )
    if not recorded:
        # No durable last-alert time exists. One total hold window still caps
        # the turn rate; this never adds a second window to the hook timeout.
        hold(budget)
    refusal = getattr(failure, "code", operation)
    if not isinstance(refusal, str) or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,127}", refusal) is None:
        refusal = operation
    reason = (
        f"[floati] integrity refusal {refusal}; check {diagnostic['check_id']}; "
        "inspect the waiter exit receipt and repair the input before retrying"
    )
    if input_changed:
        reason += "; input changed since the failed read; end this turn to re-arm"
    decision = {"decision": "block", "reason": reason}
    def output():
        _report_evidence_failure(stderr, operation, failure)
        stdout.write(json.dumps(decision) + "\n")
        stdout.flush()
        _set_waiter_result(_waiter_result, "integrity_failure", code=refusal, decision=decision)
    if _publish_terminal is None:
        output()
    else:
        _publish_terminal(output, guarded=False,
            repair_input=None if fault_witness is None else fault_witness[0])
    return 0


def _reopen_consent(
    participant: object,
    replacement: tuple,
    *,
    consent_relative: Path,
    session_digest: str,
    invocation_id: str,
    waited_seconds: int,
    stderr: TextIO,
) -> Optional[tuple]:
    """Reopen the consent ledger at the file its PATH names now.

    The waiter decides from a consent receipt it read once.  When a repair,
    rotation or restore replaces that file the snapshot is stale, so the ledger
    is read again at the new inode and the reopen is recorded with the position
    the wait continues from.  ``None`` means the waiter may no longer hold the
    turn: the exit is already recorded.
    """

    before, after = replacement
    try:
        reopened = CodexWaitConsentLedger(participant.root).require_armed(
            participant.binding
        )
    except Exception as exc:
        raise_reader_failure(exc)
        if isinstance(exc, ProtocolRefusal) and exc.code == "codex_wait_consent_missing":
            reopened = None
        else:
            exc.waiter_input_identity = {"path": consent_relative.as_posix()}
            raise
    deadline_seconds = None if reopened is None else reopened.get("wait_deadline_seconds")
    usable = isinstance(deadline_seconds, int) and not isinstance(deadline_seconds, bool)
    try:
        CodexWaitReopenLedger(participant.root).record(
            node_id=participant.binding.node_id,
            session_digest=session_digest,
            ledger=consent_relative.as_posix(),
            before=before,
            after=after,
            waited_seconds=waited_seconds,
            outcome="consent_reopened" if usable else "consent_withdrawn",
            invocation_id=invocation_id,
        )
    except Exception as exc:
        raise_reader_failure(exc)
        _report_evidence_failure(stderr, "consent_reopen", exc)
        # Reopen testimony is evidence. A failed write does not withdraw consent.
    if not usable:
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="consent_withdrawn", waited_seconds=waited_seconds,
            invocation_id=invocation_id,
            stderr=stderr,
        )
        return None
    return reopened, deadline_seconds


def _breaker_tripped(
    root: object,
    node_id: str,
    *,
    now: float,
    stderr: Optional[TextIO] = None,
) -> bool:
    """Persist one bounded invocation window after participation is proven."""

    path = root.resolve_relative(Path("state/codex-wait") / node_id / "breaker.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        prior = raw.get("hits", []) if isinstance(raw, dict) else []
    except (OSError, json.JSONDecodeError):
        prior = []
    hits = [
        float(value)
        for value in prior
        if isinstance(value, (int, float)) and 0.0 <= now - float(value) < BREAKER_WINDOW_SECONDS
    ]
    hits.append(float(now))
    encoded = (json.dumps({"hits": hits}, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                _report_evidence_failure(
                    stderr, "breaker", OSError("short breaker write")
                )
                return True
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except OSError as exc:
        _report_evidence_failure(stderr, "breaker", exc)
        try:
            temporary.unlink()
        except OSError:
            pass
        return True
    return len(hits) > BREAKER_MAX_INVOCATIONS


def _forward_reader_refusal(operation):
    @wraps(operation)
    def run(**kwargs):
        output = kwargs['stdout']
        published = False

        class Output:
            def write(self, value):
                nonlocal published
                if value:
                    published = True
                return output.write(value)

            def flush(self):
                return output.flush()

        kwargs['stdout'] = Output()
        try:
            return operation(**kwargs)
        except Exception as exc:
            refusal = reader_failure(exc)
            if refusal is None:
                raise
            # A completed block cannot be retracted. A schema change racing
            # publication is a typed stderr diagnostic, never a second stdout.
            stdout = kwargs['stderr'] if published else output
            artifact = {
                'artifact_version': 0, 'command': 'codex-wait', 'status': 'refused',
                'evidence': {'code': refusal.code, 'detail': refusal.detail,
                             'remedy': refusal.remedy},
            }
            stdout.write(json.dumps(artifact, sort_keys=True) + '\n')
            stdout.flush()
            if not published:
                _set_waiter_result(kwargs.get('_waiter_result'), 'refused', code=refusal.code, decision=artifact)
            return 20
    return run


@_forward_reader_refusal
def _run_stop_waiter_invocation(
    *,
    bus_home: Path,
    hook_payload: Mapping[str, object],
    stdout: TextIO,
    stderr: TextIO,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall_time: Callable[[], float] = time.time,
    poll_interval_seconds: float = 1.0,
    wait_readable: Callable[[object, float], bool] = native_wait_readable,
    _waiter_window=None,
    _waiter_result=None,
    _invocation=None,
) -> int:
    from .stop_hold import _HoldEvidence

    from contextlib import ExitStack, nullcontext
    from .waiter_lock_retry import current_lock_window, waiter_lock_window

    if _waiter_result is not None:
        if not isinstance(_waiter_result, dict):
            raise TypeError("_waiter_result must be an invocation-local dictionary")
        _waiter_result.clear()

    if _waiter_window is not None:
        if current_lock_window() is not _waiter_window or _waiter_window.finishing:
            raise ValueError("provided waiter window must be the active unfinished native scope")
        monotonic, sleep = _waiter_window.clock, _waiter_window.sleep
        _waiter_window.deadline_limit = _waiter_window.deadline

    from .host_observations import observe_codex_host
    observe_codex_host(bus_home, hook_payload)

    # Initial registry/consent reads are native lock-free snapshots. Enable
    # retries only after that consent proves the budget, retaining this origin.
    scope = waiter_lock_window(None, monotonic, sleep) if _waiter_window is None else nullcontext(_waiter_window)
    with scope as retry_window, ExitStack() as resources:
        inherited_checker = retry_window.check_authority
        hold = _HoldEvidence(stderr, lambda: datetime.fromtimestamp(
            wall_time(), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        hold.invocation_lease = _invocation
        try:
            return _run_stop_waiter(bus_home=bus_home, hook_payload=hook_payload,
                stdout=hold.output(stdout), stderr=stderr, monotonic=monotonic,
                sleep=sleep, wall_time=wall_time,
                poll_interval_seconds=poll_interval_seconds, hold=hold, retry_window=retry_window,
                wait_readable=wait_readable,
                _waiter_result=_waiter_result, resources=resources)
        finally:
            try:
                hold.close()
            finally:
                if _waiter_window is not None:
                    retry_window.check_authority = inherited_checker


@_forward_reader_refusal
def run_stop_waiter(**arguments) -> int:
    """One kernel owner for each verified native session through output flush."""
    from .waiter_invocation import verified_invocation
    lease = None
    payload = arguments.get("hook_payload", {})
    workspace = payload.get("cwd")
    if isinstance(workspace, str):
        lease = verified_invocation(arguments["bus_home"], None,
            Path(workspace), payload.get("session_id"), harnesses=("codex", "zcode", "claude"))
    if lease is not None and not lease.owned:
        output = arguments["stdout"]
        output.write("{}\n")
        output.flush()
        _set_waiter_result(arguments.get("_waiter_result"), "stopped", reason="duplicate_invocation", decision={})
        return 0
    try:
        return _run_stop_waiter_invocation(**arguments, _invocation=lease)
    finally:
        if lease is not None:
            lease.close()


def _run_stop_waiter(
    *, bus_home, hook_payload, stdout, stderr, monotonic, sleep, wall_time,
    poll_interval_seconds, hold, retry_window, wait_readable,
    _waiter_result=None, resources=None,
) -> int:
    """Run one hook invocation; unbound workspaces are silent non-participants."""

    raw_workspace = hook_payload.get("cwd")
    if not isinstance(raw_workspace, str) or not raw_workspace:
        _set_waiter_result(_waiter_result, "nonparticipant")
        return 0
    participant = resolve_participant(Path(bus_home), Path(raw_workspace))
    if participant is None:
        _set_waiter_result(_waiter_result, "nonparticipant")
        return 0
    try:
        session_id = validate_session_id(hook_payload.get("session_id"))
    except Exception as exc:
        raise_reader_failure(exc)
        _set_waiter_result(_waiter_result, "nonparticipant")
        return 0
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    invocation_id = "codex-stop-" + uuid7_hex()
    terminal_authority = None
    terminal_pause = None
    def terminal_publication(publication, *, guarded, repair_input=None):
        from .jsonl import _locked_path, read_records_snapshot
        from .seat_teardown import _marker_path
        from .wake_control import WakeController
        from .waiter_lock_retry import authority_change_failure, lock_deadline_failure, transient_ledger_timeout
        relative = Path("state/wake-control") / (participant.binding.node_id + ".lock")
        # Preserve the caller's existing EPOCH/NODE behavior, including its
        # exhausted fallback. No terminal write obtains a fresh wait window.
        retry_window.finish()
        publication_started = False
        try:
            with _locked_path(participant.root.resolve_relative(relative), exclusive=True,
                    relative=relative, timeout_seconds=5, order_tracked=False):
                if terminal_authority is not None:
                    try:
                        terminal_authority(recheck_binding=retry_window.contended,
                            _repair_input=repair_input)
                    except ProtocolRefusal as exc:
                        if exc.code != "codex_wait_workspace_map_invalid":
                            raise
                        # A malformed map is the diagnostic's repair target.
                        # Retain only the invocation's originally proven seat;
                        # consent, current claimant and exact pause still bind.
                        terminal_authority(recheck_binding=False, _repair_input=repair_input)
                else:
                    # Initial consent faults precede participation. Snapshot
                    # identity can authorize a repair diagnostic, never mail.
                    ledger = CodexWaitSessionLedger(participant.root)
                    matching = ledger._matching(read_records_snapshot(participant.root,
                        ledger._relative(participant.binding.node_id), allowed_kinds=ledger._KINDS),
                        participant.binding)
                    if matching and matching[-1]["acting_session_id"] != session_id:
                        _set_waiter_result(_waiter_result, "authority_changed", reason="not_claimant", decision={})
                        return
                    if WakeController(participant.root)._read_marker(
                            _marker_path(participant.root, participant.binding.node_id, session_id),
                            participant.binding.node_id, session_digest) is not None:
                        _set_waiter_result(_waiter_result, "authority_changed", reason="pause_unverified", decision={})
                        return
                publication_started = True
                publication()
        except Exception as exc:
            raise_reader_failure(exc)
            if publication_started:
                raise
            change = authority_change_failure(exc)
            malformed = getattr(exc, "code", None) == "wake_marker_invalid"
            if terminal_pause is not None and (malformed or (change is not None and change.reason == "paused")):
                if repair_input in {
                        participant.root.resolve_relative(consent_ledger_relative(participant.binding.node_id)),
                        participant.root.resolve_relative(CodexWaitSessionLedger._relative(participant.binding.node_id))}:
                    # The exact pause is terminal, but this diagnostic already
                    # names unreadable native authority. Do not certify a
                    # completion by reading that same broken proof again.
                    _set_waiter_result(_waiter_result, "authority_changed", reason="pause_unverified", decision={})
                    return
                if terminal_pause(max(0, int(monotonic() - retry_window.started)), unknown=malformed):
                    return
            if change is not None:
                _record_exit(participant, session_digest=session_digest, reason_code=change.reason,
                    waited_seconds=max(0, int(monotonic() - retry_window.started)),
                    invocation_id=invocation_id, stderr=stderr)
                _set_waiter_result(_waiter_result, "authority_changed", reason=change.reason, decision={})
                return
            if malformed or lock_deadline_failure(exc) is not None or transient_ledger_timeout(exc):
                _set_waiter_result(_waiter_result, "authority_changed",
                    reason="pause_unverified" if malformed else "terminal_guard_unavailable", decision={})
                return
            raise
    last_presentation_context = None
    def deadline_presentation_evidence():
        from .bus_epoch import _epoch_transition_pending, epoch_guard
        from .wake_hold import _has_rolled_wake_history
        with epoch_guard(participant.root):
            if _epoch_transition_pending(participant.root):
                return "\n[floati] epoch recovery pending; presentation history quarantined."
            if last_presentation_context is None:
                return ""
            plane, binding, original_consent, original_classification = last_presentation_context
            if not plane._cold_source_has_no_roll() and _has_rolled_wake_history(participant.root):
                # The deadline has passed. Let the next invocation project a
                # new rolled cut instead of reading history in the final guard.
                return "\n[floati] presentation_state_unverified; preserve native inbox and ACK recovery."
        selection = plane._select_waiter_empty(binding, original_consent, original_classification, harness="codex")
        if selection is None:
            selection = plane._select_waiter(binding, original_consent, original_classification, harness="codex")
        if not selection["held_total"]:
            return ""
        evidence = plane.held_evidence(selection)
        reason = "\n[floati] " + evidence["state"] + ": " + json.dumps(evidence, sort_keys=True)
        if evidence["state"] == "presentation_uncertain":
            reason += "\nRecover through this seat's declared native inbox; act on each exact envelope before ACK."
        return reason
    decline = partial(_integrity_exit, monotonic=monotonic, sleep=sleep, wall_time=wall_time,
        _waiter_result=_waiter_result, _held_evidence=deadline_presentation_evidence,
        _publish_terminal=terminal_publication)
    try:
        consent = CodexWaitConsentLedger(participant.root).require_armed(participant.binding)
        if retry_window.check_authority is not None:
            retry_window.check_authority()
    except Exception as exc:
        raise_reader_failure(exc)
        if isinstance(exc, ProtocolRefusal) and exc.code == "codex_wait_consent_missing":
            _set_waiter_result(_waiter_result, "nonparticipant", reason="consent_missing")
            return 0
        exc.waiter_input_identity = {"path": consent_ledger_relative(participant.binding.node_id).as_posix()}
        return decline(
            participant, session_digest=session_digest, operation="consent_read",
            failure=exc, waited_seconds=0, invocation_id=invocation_id,
            deadline_seconds=0, stdout=stdout, stderr=stderr,
        )

    proven_budget = consent.get("wait_deadline_seconds")
    if isinstance(proven_budget, int) and not isinstance(proven_budget, bool):
        retry_window.update(proven_budget)

    def exact_pause_marker():
        from .seat_teardown import _marker_path
        from .wake_control import WakeController
        return WakeController(participant.root)._read_marker(
            _marker_path(participant.root, participant.binding.node_id, session_id),
            participant.binding.node_id, session_digest)

    def check_authority(*, recheck_binding=True, _repair_input=None, require_claim=False):
        # Native snapshot readers take no ledger lock and cannot recurse into
        # the acquisition being checked. Completed callbacks are never replayed.
        from .jsonl import read_records_snapshot
        from .waiter_lock_retry import WaiterAuthorityChanged
        current = participant
        if recheck_binding:
            current = (resolve_participant(Path(bus_home), Path(raw_workspace), _strict=True)
                       if retry_window.contended else resolve_participant(Path(bus_home), Path(raw_workspace)))
        if current is None or current.root.tenant_home != participant.root.tenant_home or (
            current.binding.workspace != participant.binding.workspace
            or current.binding.node_id != participant.binding.node_id
        ):
            raise WaiterAuthorityChanged("not_claimant")
        try:
            latest_consent = CodexWaitConsentLedger(participant.root).require_armed(current.binding)
        except IntegrityFailure:
            if _repair_input != participant.root.resolve_relative(
                    consent_ledger_relative(participant.binding.node_id)):
                raise
            # Only a terminal repair diagnostic can name its already failed
            # input here. Mail selection and delivery never pass this seam.
            latest_consent = consent
        except ProtocolRefusal as exc:
            if exc.code != "codex_wait_consent_missing":
                raise
            raise WaiterAuthorityChanged("consent_withdrawn") from exc
        if latest_consent != consent:
            raise WaiterAuthorityChanged("consent_withdrawn")
        ledger = CodexWaitSessionLedger(participant.root)
        try:
            rows = read_records_snapshot(participant.root, ledger._relative(participant.binding.node_id),
                                        allowed_kinds=ledger._KINDS)
        except IntegrityFailure:
            if _repair_input != participant.root.resolve_relative(
                    ledger._relative(participant.binding.node_id)):
                raise
            rows = []
        matching = ledger._matching(rows, participant.binding)
        if require_claim and not matching:
            raise WaiterAuthorityChanged("not_claimant")
        if matching and matching[-1]["acting_session_id"] != session_id:
            former = any(row["acting_session_id"] == session_id for row in matching)
            raise WaiterAuthorityChanged("superseded_session" if former else "not_claimant",
                                         matching[-1]["acting_session_id"])
        # Read the lexical marker even when a resolving existence check would
        # hide a dangling ancestor. Native completion grants no new claimant.
        if exact_pause_marker() is not None:
            raise WaiterAuthorityChanged("paused")

    retry_window.check_authority = check_authority
    terminal_authority = check_authority

    def superseded(classification, waited):
        retry_window.finish()
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="superseded_session", waited_seconds=waited,
            invocation_id=invocation_id, stderr=stderr,
        )
        # Both identifiers came from the validated claim transaction. This is
        # transient snapshot evidence, not a new field in the durable v1 row.
        try:
            stderr.write(json.dumps({
                "code": "superseded_session",
                "calling_session_id": classification.calling_session_id,
                "current_session_id": classification.current_session_id,
            }, sort_keys=True) + "\n")
            stderr.flush()
        except Exception as exc:
            raise_reader_failure(exc)
        try:
            stdout.write("{}\n")
            stdout.flush()
            _set_waiter_result(_waiter_result, "authority_changed", reason="superseded_session", decision={})
        except Exception as exc:
            raise_reader_failure(exc)

    def startup_testimony():
        from .registry import Registry
        if Registry(participant.root).require_active(participant.binding.node_id)["role"].casefold() in {
                "codex", "cursor", "zcode", "claude"} and hold.invocation_lease is None:
            return
        try:
            from .codex_wait_liveness import write_holder_testimony

            write_holder_testimony(participant.root, participant.binding.node_id, session_id)
        except Exception as exc:
            raise_reader_failure(exc)
            _report_evidence_failure(stderr, "holder_testimony", exc)
            # Testimony remains best effort; the physical guard still covers
            # the following adapter write when this first writer fails.
        try:
            from .wake_daemon_adapters import record_codex_daemon_binding

            record_codex_daemon_binding(participant, session_id)
        except ProtocolRefusal as exc:
            raise_reader_failure(exc)
            if exc.code != "wake_daemon_codex_executable_absent":
                _report_evidence_failure(stderr, "daemon_binding", exc)
        except Exception as exc:
            raise_reader_failure(exc)
            _report_evidence_failure(stderr, "daemon_binding", exc)

    def pause_exit(waited, *, unknown=False):
        from .seat_teardown import completed_teardown_for_session
        try:
            marker = None if unknown else exact_pause_marker()
        except Exception as exc:
            raise_reader_failure(exc)
            if getattr(exc, "code", None) != "wake_marker_invalid":
                raise
            marker, unknown = None, True
        if marker is not None or unknown:
            from .jsonl import read_records_snapshot
            ledger = CodexWaitSessionLedger(participant.root)
            matching = ledger._matching(read_records_snapshot(participant.root,
                ledger._relative(participant.binding.node_id), allowed_kinds=ledger._KINDS),
                participant.binding)
            if not matching or matching[-1]["acting_session_id"] != session_id:
                return False
            try:
                latest = CodexWaitConsentLedger(participant.root).require_armed(participant.binding)
            except ProtocolRefusal as exc:
                if exc.code == "codex_wait_consent_missing":
                    return False
                raise
            if latest != consent:
                return False
            # An exact pause is already terminal. Its proof may try each
            # uncontended native read once, never renew the hold's deadline.
            retry_window.finish()
        checker = retry_window.check_authority
        try:
            # The verifier reads native ledgers. Its locks retain the original
            # window, but must not recursively run this same terminal check.
            retry_window.check_authority = None
            completed = None if unknown else completed_teardown_for_session(
                participant.root, participant.binding.node_id, session_id)
            paused = unknown or completed is not None or marker is not None
        except Exception as exc:
            raise_reader_failure(exc)
            if marker is None and getattr(exc, "code", None) != "wake_marker_invalid":
                raise
            unknown, paused, completed = True, True, None
        finally:
            retry_window.check_authority = checker
        if not paused:
            return False
        waited = max(waited, max(0, int(monotonic() - retry_window.started)))
        retry_window.finish()
        _record_exit(participant, session_digest=session_digest,
            reason_code="integrity_failure" if unknown else "paused",
            waited_seconds=waited, invocation_id=invocation_id, stderr=stderr)
        if completed is not None:
            _set_waiter_result(_waiter_result, "completed_teardown",
                teardown_receipt_id=completed["id"], decision={})
        else:
            _set_waiter_result(_waiter_result, "authority_changed",
                reason="pause_unverified" if unknown else "paused", decision={})
        return True

    original_decline = decline
    terminal_pause = pause_exit
    def decline(*args, **kwargs):
        from .waiter_lock_retry import authority_change_failure
        failure = kwargs["failure"]
        change = authority_change_failure(failure)
        if change is None or change.reason == "paused":
            paused = False
            try:
                paused = exact_pause_marker() is not None
            except Exception as exc:
                raise_reader_failure(exc)
                if getattr(exc, "code", None) != "wake_marker_invalid":
                    raise
                if pause_exit(kwargs["waited_seconds"], unknown=True):
                    return 0
            if paused and pause_exit(kwargs["waited_seconds"]):
                return 0
        return original_decline(*args, **kwargs)

    try:
        participation = retry_window.run(lambda: CodexWaitSessionLedger(participant.root).participate_classified(
            participant.binding,
            consent,
            session_id,
            on_authorized=startup_testimony,
        ))
    except Exception as exc:
        raise_reader_failure(exc)
        return decline(
            participant, session_digest=session_digest,
            operation="session_authority", failure=exc, waited_seconds=0,
            invocation_id=invocation_id,
            deadline_seconds=int(consent["wait_deadline_seconds"]), stdout=stdout, stderr=stderr,
        )
    if participation.state == "superseded_session":
        superseded(participation, 0)
        return 0
    if participation.state != "authorized":
        _set_waiter_result(_waiter_result, "authority_changed", reason=participation.state, decision={})
        return 0
    try:
        if pause_exit(0):
            return 0
    except Exception as exc:
        raise_reader_failure(exc)
        return decline(
            participant, session_digest=session_digest,
            operation="pause_state", failure=exc, waited_seconds=0,
            invocation_id=invocation_id,
            deadline_seconds=int(consent["wait_deadline_seconds"]), stdout=stdout, stderr=stderr,
        )
    from .registry import Registry
    if Registry(participant.root).require_active(participant.binding.node_id)["role"].casefold() in {
            "codex", "cursor", "zcode", "claude"} and (
            hold.invocation_lease is None or not hold.invocation_lease.owned):
        stdout.write("{}\n")
        stdout.flush()
        _set_waiter_result(_waiter_result, "stopped", reason="invocation_unowned", decision={})
        return 0
    from .epoch_rotation import stop_maintenance
    from .waiter_lock_retry import maintenance_transition_failure
    try:
        retry_window.run(lambda: stop_maintenance(participant.root,
            actor=participant.binding.node_id, check_authority=check_authority),
            retry_failure=maintenance_transition_failure)
    except Exception as exc:
        raise_reader_failure(exc)
        return decline(participant, session_digest=session_digest, operation="epoch_maintenance",
            failure=_epoch_maintenance_failure(exc), waited_seconds=max(0, int(monotonic() - retry_window.started)),
            invocation_id=invocation_id, deadline_seconds=int(consent["wait_deadline_seconds"]),
            stdout=stdout, stderr=stderr)
    if _breaker_tripped(
        participant.root,
        participant.binding.node_id,
        now=wall_time(),
        stderr=stderr,
    ):
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="breaker", waited_seconds=0,
            invocation_id=invocation_id,
            stderr=stderr,
        )
        _set_waiter_result(_waiter_result, "stopped", reason="breaker", decision={})
        return 0
    deadline_seconds = consent.get("wait_deadline_seconds")
    if not isinstance(deadline_seconds, int) or isinstance(deadline_seconds, bool):
        return decline(
            participant, session_digest=session_digest,
            operation="wait_deadline", failure=ValueError(), waited_seconds=0,
            invocation_id=invocation_id,
            deadline_seconds=0, stdout=stdout, stderr=stderr,
        )
    if not isinstance(poll_interval_seconds, (int, float)) or poll_interval_seconds <= 0:
        return decline(
            participant, session_digest=session_digest,
            operation="poll_interval", failure=ValueError(), waited_seconds=0,
            invocation_id=invocation_id,
            deadline_seconds=deadline_seconds, stdout=stdout, stderr=stderr,
        )
    started = retry_window.started
    deadline = retry_window.deadline
    try:
        retry_window.run(lambda: hold.start(participant, consent, session_id, retry_window.remaining()))
        if hold.invocation_lease is not None and hold.start_row is not None:
            hold.invocation_lease.bind_hold(hold.start_row)
    except Exception as exc:
        return decline(participant, session_digest=session_digest, operation="stop_hold_start",
            failure=exc, waited_seconds=max(0, int(monotonic() - started)),
            invocation_id=invocation_id, deadline_seconds=deadline_seconds, stdout=stdout, stderr=stderr)
    controller = WakeHoldController(participant.root)
    from .cursor_presentations import CursorPresentationLedger
    presentation_plane = CursorPresentationLedger(participant.root, sealed="bytes")
    consent_relative = consent_ledger_relative(participant.binding.node_id)
    consent_watch = WatchedLedger(participant.root.resolve_relative(consent_relative))
    workspace_map_watch = WatchedLedger(Path(bus_home) / WORKSPACE_MAP_RELATIVE)
    changes = _WaiterInputsWatch(participant, session_digest, stderr, workspace=Path(raw_workspace))
    resources.callback(changes.close)
    idle_artifact = None
    at_stop = True
    # NEEDS-YOU-OBSERVED-1: the needs-you condition exists only for a seat that
    # declared an orchestrator source. Without one this is None and the loop
    # below is unchanged; with one, the loop's own tick runs one bounded pull
    # per interval and decides on its result alone.
    from .needs_you_condition import NeedsYouCondition
    needs_you = NeedsYouCondition.for_seat(participant.root, participant.binding.node_id,
        lambda exc: _report_evidence_failure(stderr, "needs_you", exc))

    def needs_you_wait(timeout):
        return timeout if needs_you is None else needs_you.wait_cap(timeout, wall_time())

    def authorize_observer():
        from .waiter_lock_retry import WaiterAuthorityChanged

        lease = hold.invocation_lease
        reason = lease.stop_reason() if lease is not None else None
        if reason is not None:
            raise WaiterAuthorityChanged(reason)
        check_authority(require_claim=True)
        registry = Registry(participant.root)
        registry.require_active(participant.binding.node_id)
        registry.require_protocol_lease(participant.binding.node_id, act="observe needs for recipient")

    def observed_item_exit(pending):
        """Wake for observed items through the exhaustion exit's steps, receipted observed_item."""
        from .bus_epoch import epoch_guard
        from .wake_hold import wake_coordination_guard
        waited = max(0, int(monotonic() - started))
        publishing = False
        try:
            with epoch_guard(participant.root), wake_coordination_guard(participant.root, participant.binding.node_id):
                authorize_observer()
                wake = needs_you.prepare_wake(session_digest, invocation_id, pending)
                if wake is None:
                    return None
                decision = {"decision": "block", "reason": needs_you.reason(wake)}

                def output():
                    stdout._close_hold_already_guarded()
                    stdout.write(json.dumps(decision) + "\n")
                    stdout.flush()
                    recorded = needs_you.record_flushed_wake(wake)
                    _record_exit(participant, session_digest=session_digest, reason_code="observed_item",
                        waited_seconds=waited, invocation_id=invocation_id, stderr=stderr)
                    _set_waiter_result(_waiter_result, "observed_item", decision=decision,
                        item_ids=[entry["item_id"] for entry in wake["items"]],
                        event_keys=[entry["event_key"] for entry in wake["items"]],
                        receipt_id=wake["id"] if recorded else None)
                publishing = True
                terminal_publication(output, guarded=True)
            return 0
        except Exception as exc:
            raise_reader_failure(exc)
            if publishing:
                raise
            return decline(participant, session_digest=session_digest, operation="observed_item",
                failure=exc, waited_seconds=waited, invocation_id=invocation_id,
                deadline_seconds=deadline_seconds, stdout=stdout, stderr=stderr)

    while True:
        reason = hold.invocation_lease.stop_reason() if hold.invocation_lease is not None else None
        if reason is not None:
            stdout.write("{}\n")
            stdout.flush()
            _set_waiter_result(_waiter_result, "stopped", reason=reason, decision={})
            return 0
        try:
            if exact_pause_marker() is not None:
                check_authority(recheck_binding=False)
        except Exception as exc:
            raise_reader_failure(exc)
            return decline(participant, session_digest=session_digest,
                operation="pause_state", failure=exc,
                waited_seconds=max(0, int(monotonic() - started)),
                invocation_id=invocation_id, deadline_seconds=deadline_seconds,
                stdout=stdout, stderr=stderr)
        try:
            rotated = retry_window.run(lambda: stop_maintenance(participant.root,
                actor=participant.binding.node_id, check_authority=check_authority),
                retry_failure=maintenance_transition_failure)
            if rotated["changed"]:
                idle_artifact = None
                controller = WakeHoldController(participant.root)
                presentation_plane = CursorPresentationLedger(participant.root, sealed="bytes")
                last_presentation_context = None
                changes.close()
                changes = _WaiterInputsWatch(participant, session_digest, stderr, workspace=Path(raw_workspace))
                resources.callback(changes.close)
                consent_watch = WatchedLedger(participant.root.resolve_relative(consent_relative))
                workspace_map_watch = WatchedLedger(Path(bus_home) / WORKSPACE_MAP_RELATIVE)
        except Exception as exc:
            raise_reader_failure(exc)
            return decline(participant, session_digest=session_digest, operation="epoch_maintenance",
                failure=_epoch_maintenance_failure(exc), waited_seconds=max(0, int(monotonic() - started)),
                invocation_id=invocation_id, deadline_seconds=deadline_seconds, stdout=stdout, stderr=stderr)
        if needs_you is not None and monotonic() < deadline:
            pending = needs_you.tick(wall_time(), authorize=authorize_observer)
            if pending:
                result = observed_item_exit(pending)
                if result is not None:
                    return result
        dirty, invalidate = changes.changed()
        current_time = monotonic()
        if idle_artifact is not None and not dirty and current_time < deadline:
            from .wake_hold import _claim_holder_released
            held = idle_artifact.get("held_total", 0)
            if not held or not _claim_holder_released(participant.root, participant.binding.node_id):
                # The watch already compared ledger identity and hint
                # generation. An unchanged admission is not projected again.
                # A changed identity, a delivered event, or a source that fell
                # back to polling still takes the full read.
                changes.wait(needs_you_wait(min(deadline - current_time, 1.0) if hold.invocation_lease is not None else deadline - current_time),
                             wait_readable=wait_readable,
                             sleep=sleep, poll_seconds=float(poll_interval_seconds))
                continue
        if idle_artifact is not None and invalidate:
            # Notifications include in-place rewrites. Do not retain a cursor
            # that only samples its first frame across a dirty generation.
            controller = WakeHoldController(participant.root)
        map_replacement = workspace_map_watch.poll()
        if map_replacement is not None:
            refreshed = resolve_participant(Path(bus_home), Path(raw_workspace))
            if (
                refreshed is None
                or refreshed.root.tenant_home != participant.root.tenant_home
                or refreshed.binding.workspace != participant.binding.workspace
                or refreshed.binding.node_id != participant.binding.node_id
            ):
                _record_exit(
                    participant,
                    session_digest=session_digest,
                    reason_code="not_claimant",
                    waited_seconds=max(0, int(monotonic() - started)),
                    invocation_id=invocation_id,
                    stderr=stderr,
                )
                _set_waiter_result(_waiter_result, "authority_changed", reason="not_claimant", decision={})
                return 0
            participant = refreshed
        replacement = consent_watch.poll()
        if replacement is not None:
            try:
                reopened = _reopen_consent(
                    participant,
                    replacement,
                    consent_relative=consent_relative,
                    session_digest=session_digest,
                    invocation_id=invocation_id,
                    waited_seconds=max(0, int(monotonic() - started)),
                    stderr=stderr,
                )
            except Exception as exc:
                raise_reader_failure(exc)
                return decline(
                    participant, session_digest=session_digest, operation="consent_reopen",
                    failure=exc, waited_seconds=max(0, int(monotonic() - started)),
                    invocation_id=invocation_id, deadline_seconds=deadline_seconds,
                    stdout=stdout, stderr=stderr,
                )
            if reopened is None:
                _set_waiter_result(_waiter_result, "authority_changed", reason="consent_withdrawn", decision={})
                return 0
            # Continue the same wait from where it stood: the start instant is
            # never reset, so a reopen neither restarts nor extends the clock.
            consent, deadline_seconds = reopened
            retry_window.update(deadline_seconds)
            deadline = retry_window.deadline
        invocation_key = "codex-stop-" + uuid7_hex()
        classification = None
        operation = "session_authority"
        finished = False
        published_items = None
        published_receipt = None
        publication_started = False
        now = None
        projection_unlocked = False

        def before_rolled_projection():
            nonlocal projection_unlocked
            projection_unlocked = True
            return presentation_plane._prepare_rolled_waiter_projection(
                participant.binding.node_id, session_id)

        def conversation_selection(selection=None):
            plane = presentation_plane
            if selection is None:
                read_options = {"materialize": False} if projection_unlocked else {}
                selection = plane._select_waiter(participant.binding, consent, classification,
                    harness="codex", at_stop=at_stop, **read_options)
            return {"state": "fresh_work" if selection["fresh_messages"] else (
                "held_only" if selection["held_total"] else "caught_up"),
                "wake_required": bool(selection["fresh_messages"]),
                "fresh_messages": selection["fresh_messages"], "receipt": None,
                "_plane": plane, "_selection": selection}

        def before_selected_read():
            nonlocal last_presentation_context
            from .bus_epoch import _epoch_transition_pending
            check_authority(recheck_binding=False)
            if not _epoch_transition_pending(participant.root):
                selection = presentation_plane._select_waiter_empty(
                    participant.binding, consent, classification, harness="codex")
                if selection is not None:
                    return conversation_selection(selection)
                return None
            # Session participation has already run inside EPOCH -> WAKE.
            # Retain the consent/pause/claim checks normally made by select,
            # without initializing presentation/witness or reading history.
            check_authority(recheck_binding=False)
            last_presentation_context = None
            return {
                "state": "epoch_recovery_pending", "wake_required": False,
                "fresh_messages": [], "held_total": 0, "receipt": None,
            }

        def authorize():
            nonlocal classification, operation
            if retry_window.remaining() <= 0:
                from .waiter_lock_retry import WaiterLockDeadline
                raise WaiterLockDeadline("wake evaluation", contended=False)
            # The evaluator owns the physical node guard already. Reuse the
            # same native transaction; the public wrapper would reacquire it.
            classification = CodexWaitSessionLedger(participant.root)._participate_already_guarded(
                participant.binding, consent, session_id, classified=True,
            )
            operation = "wake_evaluation"
            return classification.state == "authorized"

        def publish(artifact):
            from .jsonl import _locked_path
            relative = Path("state/wake-control") / (participant.binding.node_id + ".lock")
            # The evaluator already owns EPOCH -> CAR -> NODE. This is the
            # identical inode used by native pause, retained through flush.
            with _locked_path(participant.root.resolve_relative(relative), exclusive=True,
                    relative=relative, timeout_seconds=5, order_tracked=False):
                check_authority(recheck_binding=False)
                publish_guarded(artifact)

        def publish_guarded(artifact):
            nonlocal finished, published_items, published_receipt, now, publication_started
            nonlocal last_presentation_context, at_stop
            selection = artifact.get("_selection")
            if selection is not None:
                if _waiter_result is not None:
                    _waiter_result["sealed_read"] = sorted(set(_waiter_result.get("sealed_read", ()))
                        | set(selection["sealed_read"]))
                last_presentation_context = artifact["_plane"], participant.binding, consent, classification
            state = artifact.get("state")
            if state == "fresh_work" and artifact.get("wake_required"):
                messages = artifact.get("fresh_messages")
                if not isinstance(messages, list):
                    return
                item_ids = [row.get("id") for row in messages if isinstance(row, dict)]
                if len(item_ids) != len(messages) or not all(isinstance(item, str) for item in item_ids):
                    return
                from .cursor_presentations import limit_selection
                from .fleet_presence import presence_status, with_presence
                plane, selection = artifact["_plane"], artifact["_selection"]
                presence = presence_status(participant.root)
                def body_for(selected):
                    from .colleague_frame import frame_clause
                    reason = (
                        f"[floati] {len(selected)} new message(s) for {participant.binding.node_id}: "
                        + ", ".join(row["id"] for row in selected))
                    # Ordinary fresh mail is already the native inbox prefix.
                    # Keep its established ID-only artifact. When this same
                    # conversation holds attempted unACKed mail, a generic
                    # drain can return that older prefix instead of this tail;
                    # only then publish the exact selected envelope context.
                    if selection["held_total"]:
                        reason += (
                            "\nExact native envelopes:\n" + json.dumps(selected, sort_keys=True)
                            + "\nAct on these exact envelopes, then acknowledge their IDs with acting session "
                            + session_id + ". Flushed notification is not acknowledgment.")
                    reason += "\n" + frame_clause(selected)
                    return {"decision": "block", "reason": with_presence(
                        participant.root, participant.binding.node_id, reason, presence=presence)}
                selection, decision = limit_selection(selection, body_for)
                messages = selection["fresh_messages"]
                item_ids = [row["id"] for row in messages]
                try:
                    from .jsonl import read_records_snapshot

                    read_records_snapshot(
                        participant.root,
                        Path('receipts/wakes') / (participant.binding.node_id + '.jsonl'),
                        allowed_kinds={'wake_attempt_receipt'},
                    )
                except Exception as exc:
                    # Detect an older installed reader before the hook block.
                    # Ordinary receipt observation remains best effort.
                    raise_reader_failure(exc)
                # A busy delivery writer can retry the guarded selection
                # until the original deadline. Once its lock is held, freeze
                # retries before writing the durable presentation reservation.
                prepared, receipt = plane._prepare_and_deliver(
                    selection, decision, begin_publication=retry_window.finish)
                finished = True
                # Close inside the evaluator's node guard before Output.write
                # can attempt to acquire the same non-reentrant guard again.
                hold.close(already_guarded=True, require_proof=True)
                try:
                    publication_started = True
                    stdout.write(json.dumps(decision) + "\n")
                    stdout.flush()
                    _set_waiter_result(_waiter_result, "fresh_work", decision=decision,
                        item_ids=list(item_ids), receipt=dict(receipt))
                except Exception as exc:
                    raise_reader_failure(exc)
                    return
                try:
                    terminal = plane.flushed(prepared, receipt)
                except Exception as exc:
                    raise_reader_failure(exc)
                    _report_evidence_failure(stderr, "presentation_uncertain", exc)
                    return
                published_items, published_receipt = item_ids, terminal
                return
            if selection is not None:
                # Epoch maintenance does not create another Stop boundary.
                at_stop = False
            now = monotonic()
            if now >= deadline:
                finished = True
                retry_window.finish()
                waited = max(0, int(now - started))
                _record_exit(
                    participant, session_digest=session_digest,
                    reason_code="exhausted", waited_seconds=waited,
                    invocation_id=invocation_id, stderr=stderr,
                )
                try:
                    CodexWaitReceiptLedger(participant.root).record_exhaustion(
                        node_id=participant.binding.node_id,
                        session_digest=session_digest, waited_seconds=waited,
                        idempotency_key=invocation_key + "-exhaustion",
                    )
                except Exception as exc:
                    raise_reader_failure(exc)
                    _report_evidence_failure(stderr, "exhaustion", exc)
                hold.close(already_guarded=True, require_proof=True)
                try:
                    publication_started = True
                    selection = artifact.get("_selection")
                    held_reason = ""
                    if selection and selection["held_total"]:
                        evidence = artifact["_plane"].held_evidence(selection)
                        held_reason = "\n[floati] " + evidence["state"] + ": " + json.dumps(evidence, sort_keys=True)
                        if evidence["state"] == "presentation_uncertain":
                            held_reason += "\nRecover through the declared native inbox; act on each exact envelope before ACK."
                    decision = {
                        "decision": "block",
                        "reason": "(floati: wait deadline exhausted; end this turn to re-arm)" + held_reason,
                    }
                    stdout.write(json.dumps(decision) + "\n")
                    stdout.flush()
                    _set_waiter_result(_waiter_result, "exhausted", decision=decision)
                except Exception as exc:
                    raise_reader_failure(exc)

        try:
            artifact = retry_window.run(lambda: controller.evaluate(
                participant.binding.node_id, idempotency_key=invocation_key,
                _authorize=authorize, _publish=publish,
                _conversation=conversation_selection,
                _before_read=before_selected_read,
                _before_rolled_projection=before_rolled_projection,
                _waiter_read=lambda native_read: presentation_plane._waiter_controller_read(native_read,
                    recipient=participant.binding.node_id, worker_session_id=None),
            ), retry_allowed=lambda: not publication_started)
        except Exception as exc:
            raise_reader_failure(exc)
            return decline(
                participant, session_digest=session_digest,
                operation=operation, failure=exc,
                waited_seconds=max(0, int(monotonic() - started)),
                invocation_id=invocation_id,
                deadline_seconds=deadline_seconds, stdout=stdout, stderr=stderr,
            )
        if artifact is None:
            waited = max(0, int(monotonic() - started))
            if classification.state == "superseded_session":
                superseded(classification, waited)
            else:
                _record_exit(
                    participant, session_digest=session_digest,
                    reason_code="not_claimant", waited_seconds=waited,
                    invocation_id=invocation_id, stderr=stderr,
                )
                _set_waiter_result(_waiter_result, "authority_changed", reason="not_claimant", decision={})
            return 0
        if published_items is not None:
            # Historical testimony for a completed flush. This writer takes
            # the node guard itself, so it runs after evaluation releases it.
            try:
                WakeAttemptLedger(participant.root).record(
                    recipient=participant.binding.node_id,
                    acting_session_id=session_id, item_ids=published_items,
                    decision_receipt_id=str(published_receipt["id"]),
                    message_worker_session_id=None,
                    idempotency_key=invocation_key + "-prompt", outcome="woke",
                )
            except Exception as exc:
                raise_reader_failure(exc)
                _report_evidence_failure(stderr, "wake_attempt", exc)
                _record_exit(
                    participant, session_digest=session_digest,
                    reason_code="integrity_failure",
                    waited_seconds=max(0, int(monotonic() - started)),
                    invocation_id=invocation_id,
                    stderr=stderr,
                    integrity_failure=_integrity_diagnostic(
                        participant, session_digest, "wake_attempt", exc, deadline_seconds, wall_time,
                    )[0],
                )
        if finished:
            return 0
        idle_artifact = artifact
        # Keep this admission across a safety timeout. The loop compares
        # identity and hint generation before projecting again.
        changes.wait(needs_you_wait(min(deadline - monotonic(), 1.0) if hold.invocation_lease is not None else deadline - monotonic()),
                     wait_readable=wait_readable,
                     sleep=sleep, poll_seconds=float(poll_interval_seconds))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    try:
        args = parser.parse_args(argv)
        payload = json.loads(sys.stdin.read() or "{}")
    except (SystemExit, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("hook_event_name") == "PostToolUse":
        from .codex_tool_hook import run_tool_hook
        return run_tool_hook(bus_home=Path(args.root), hook_payload=payload)
    if payload.get("hook_event_name") is not None and payload.get("hook_event_name") != "Stop":
        from .host_observations import observe_codex_host
        observe_codex_host(Path(args.root), payload)
        return 0
    return run_stop_waiter(
        bus_home=Path(args.root),
        hook_payload=payload,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
