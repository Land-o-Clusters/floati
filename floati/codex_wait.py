"""Bounded Codex Stop waiter for explicitly participating Floati workspaces."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack
from functools import wraps
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
    WatchedLedger,
    WORKSPACE_MAP_RELATIVE,
    consent_ledger_relative,
    resolve_participant,
)
from .errors import FloatiError, ProtocolRefusal
from .installed_reader import raise_reader_failure, reader_failure
from .ids import uuid7_hex
from .wake_control import validate_session_id
from .wake_exit import WakeExitLedger
from .wake_hold import WakeAttemptLedger, WakeHoldController


BREAKER_WINDOW_SECONDS = 60.0
BREAKER_MAX_INVOCATIONS = 20


def _waiter_parent(path: Path) -> int:
    """Open a fixed absolute parent without following namespace aliases."""

    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("watch coordinate must be absolute and contained")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _waiter_signature(info: os.stat_result) -> tuple:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


class _WaiterInputsWatch:
    """An idle-result invalidator, never an authority or ledger reader.

    Registration precedes native evaluation. Its pre-evaluation fingerprint
    is retained, so a write racing with evaluation stays dirty next poll.
    Metadata alone never enables reuse without complete native notifications.
    """

    def __init__(self, participant, session_digest, *, workspace=None):
        from .jsonl import _lock_beside

        self.root = participant.root
        home, node = self.root.tenant_home, participant.binding.node_id
        leaves = tuple(home / relative for relative in (
            Path("events.jsonl"), Path("registry/entries.jsonl"), WORKSPACE_MAP_RELATIVE,
            Path("receipts/deliveries") / (node + ".jsonl"),
            Path("receipts/acks") / (node + ".jsonl"),
            CodexWaitSessionLedger._relative(node), consent_ledger_relative(node),
            Path("state/wake-control") / node / (session_digest + ".json"),
            Path("state/codex-wait") / node / "holder.json",
            Path("receipts/wake-coordination") / node / "lane.lock",
            Path("tenants"),
        ))
        self.fixed = leaves + tuple(
            _lock_beside(path, path.relative_to(home))[0]
            for path in leaves if path.suffix == ".jsonl"
        )
        selected = set(self.fixed)
        for leaf in self.fixed:
            parent = leaf.parent
            while parent != home:
                selected.add(parent)
                parent = parent.parent
        selected.add(home)
        self.paths = tuple(sorted(selected))
        workspace = Path(workspace) if workspace is not None else participant.binding.workspace
        ancestry = {
            workspace, *workspace.parents,
            participant.binding.workspace, *participant.binding.workspace.parents,
            *home.parents,
        }
        self.observed_paths = tuple(sorted(selected | ancestry))
        self.source = None
        self.complete = False
        self.metadata_unknown = False
        self.generation_unstable = False
        self.before = ()
        self._bind_inputs()

    def _fingerprint_paths(self, paths, *, full):
        full, result = set(full), []
        self.metadata_unknown = False
        for path in paths:
            parent = None
            try:
                parent = _waiter_parent(path)
                info = (os.fstat(parent) if path == Path("/") else
                        os.stat(path.name, dir_fd=parent, follow_symlinks=False))
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise OSError("watch coordinate is not ordinary")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise OSError("watch coordinate is multiply linked")
                signature = _waiter_signature(info)
                # Unrelated entries in an outer workspace/home directory do
                # not change the selected bus inputs. Its identity still does.
                result.append(signature if path in full else signature[:4])
            except FileNotFoundError:
                result.append(None)
            except (OSError, ValueError) as exc:
                self.metadata_unknown = True
                result.append((type(exc).__name__, getattr(exc, "errno", None)))
            finally:
                if parent is not None:
                    os.close(parent)
        return tuple(result)

    def _fingerprints(self):
        return self._fingerprint_paths(self.observed_paths, full=self.paths)

    def _has_epoch_receipt(self):
        """A bounded hint only: rolled roots keep their full native polling.

        Archive closure is intentionally outside this shipped-version
        backport. A malformed or unstable hint also prevents idle reuse.
        """
        from .framing import decode_frames
        from .jsonl import MAX_RECORD_BYTES

        path = self.root.tenant_home / "events.jsonl"
        parent = _waiter_parent(path)
        try:
            try:
                descriptor = os.open(
                    path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent,
                )
            except FileNotFoundError:
                return False
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise OSError("event hint is not an ordinary single-link file")
                frame = stream.readline(MAX_RECORD_BYTES + 1)
                after = os.fstat(stream.fileno())
                named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if (_waiter_signature(before) != _waiter_signature(after)
                        or _waiter_signature(after) != _waiter_signature(named)):
                    raise OSError("event hint changed")
            if len(frame) > MAX_RECORD_BYTES:
                raise OSError("event hint exceeds frame bound")
            rows = decode_frames(frame)
            if not rows:
                return False
            if len(rows) != 1 or not isinstance(rows[0], dict):
                raise ValueError("event hint is not one object")
            return rows[0].get("kind") == "bus_epoch_roll_receipt"
        finally:
            os.close(parent)

    def _coverage_complete(self, fingerprints):
        """Kqueue registers leaves/dirs; inotify registers containing dirs."""
        import select

        if self.source is None:
            return False
        registered = set(self.source.registered_paths())
        existing = {
            path: value for path, value in zip(self.observed_paths, fingerprints)
            if path in self.paths and value is not None
        }
        # Every existing selected directory must be registered. File coverage
        # can be its own vnode or its registered parent directory.
        for path, value in existing.items():
            if len(value) != 8:
                return False
            if stat.S_ISDIR(value[2]):
                if path not in registered:
                    return False
            elif path not in registered:
                # A kqueue directory vnode does not report in-place writes
                # to its children. Inotify directory watches do.
                if hasattr(select, "kqueue") or path.parent not in registered:
                    return False
        return self.root.tenant_home in registered

    def _bind_inputs(self):
        from .bus_epoch import epoch_guard
        from .tui import BoardFilesystemWakeup

        self.close()
        self.complete = False
        self.generation_unstable = False
        try:
            with epoch_guard(self.root, exclusive=False):
                initial = self._fingerprints()
                initial_unknown = self.metadata_unknown
                hint_unknown = False
                try:
                    rolled = self._has_epoch_receipt()
                except (FloatiError, OSError, ValueError):
                    rolled, hint_unknown = True, True
                if not initial_unknown and not rolled:
                    try:
                        self.source = BoardFilesystemWakeup(
                            self.root.tenant_home, _paths=self.paths,
                        )
                    except (FloatiError, OSError, ValueError):
                        self.close()
                self.before = self._fingerprints()
                self.generation_unstable = (
                    initial != self.before or initial_unknown
                    or self.metadata_unknown or hint_unknown
                )
                self.complete = (
                    not self.generation_unstable
                    and self._coverage_complete(self.before)
                )
        except (FloatiError, OSError, ValueError):
            # Notification evidence is optional. The unchanged native
            # evaluator supplies the authoritative result and diagnostics.
            self.close()
            self.before = self._fingerprints()
            self.generation_unstable = self.metadata_unknown
        if not self.complete:
            self.close()

    def changed(self):
        import select

        uncertain = self.metadata_unknown
        current = self._fingerprints()
        dirty = (
            current != self.before or uncertain or self.metadata_unknown
            or self.generation_unstable
        )
        self.before = current
        try:
            if self.source is not None:
                if not self._coverage_complete(current):
                    dirty = True
                if select.select([self.source], [], [], 0)[0]:
                    # Readiness is itself invalidation, including a backend
                    # retaining the legacy None-returning drain contract.
                    dirty = True
                    self.source.drain()
        except (FloatiError, OSError, ValueError):
            self.close()
            dirty = True
        if dirty:
            self._bind_inputs()
        return dirty or not self.complete or self.source is None, dirty

    def close(self):
        if self.source is not None:
            source, self.source = self.source, None
            try:
                source.close()
            except (FloatiError, OSError, ValueError):
                pass


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
) -> None:
    """Best-effort exit testimony must never widen the Stop-hook outcome."""

    try:
        WakeExitLedger(participant.root).record(
            node_id=participant.binding.node_id,
            session_digest=session_digest,
            reason_code=reason_code,
            waited_seconds=waited_seconds,
            idempotency_key=f"{invocation_id}-exit-{reason_code}",
        )
    except Exception as exc:
        raise_reader_failure(exc)
        _report_evidence_failure(stderr, "wake_exit", exc)


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
        reopened = None
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
            stdout.write(json.dumps({
                'artifact_version': 0, 'command': 'codex-wait', 'status': 'refused',
                'evidence': {'code': refusal.code, 'detail': refusal.detail,
                             'remedy': refusal.remedy},
            }, sort_keys=True) + '\n')
            stdout.flush()
            return 20
    return run


@_forward_reader_refusal
def run_stop_waiter(
    *,
    bus_home: Path,
    hook_payload: Mapping[str, object],
    stdout: TextIO,
    stderr: TextIO,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall_time: Callable[[], float] = time.time,
    poll_interval_seconds: float = 1.0,
) -> int:
    """Run one hook invocation; unbound workspaces are silent non-participants."""

    raw_workspace = hook_payload.get("cwd")
    if not isinstance(raw_workspace, str) or not raw_workspace:
        return 0
    participant = resolve_participant(Path(bus_home), Path(raw_workspace))
    if participant is None:
        return 0
    try:
        consent = CodexWaitConsentLedger(participant.root).require_armed(participant.binding)
    except Exception as exc:
        raise_reader_failure(exc)
        return 0
    try:
        session_id = validate_session_id(hook_payload.get("session_id"))
    except Exception as exc:
        raise_reader_failure(exc)
        return 0
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    invocation_id = "codex-stop-" + uuid7_hex()
    try:
        session_authority = CodexWaitSessionLedger(participant.root).participate(
            participant.binding,
            consent,
            session_id,
        )
    except Exception as exc:
        raise_reader_failure(exc)
        _report_evidence_failure(stderr, "session_authority", exc)
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="integrity_failure", waited_seconds=0,
            invocation_id=invocation_id,
            stderr=stderr,
        )
        return 0
    if session_authority is None:
        return 0
    try:
        from .codex_wait_liveness import write_holder_testimony

        write_holder_testimony(participant.root, participant.binding.node_id, session_id)
    except Exception as exc:
        raise_reader_failure(exc)
        _report_evidence_failure(stderr, "holder_testimony", exc)
        # Testimony is evidence, not a participation gate: a failed write
        # reads as unproven later, which holds conservatively, and the hold
        # itself must survive.
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
        # Binding is testimony, not a participation gate. A failed write may
        # name the op on stderr but must not delete the waiter's later decision.
    try:
        from .wake_control import is_session_paused

        if is_session_paused(participant.root, participant.binding.node_id, session_id):
            _record_exit(
                participant, session_digest=session_digest,
                reason_code="paused", waited_seconds=0,
                invocation_id=invocation_id,
                stderr=stderr,
            )
            return 0
    except Exception as exc:
        raise_reader_failure(exc)
        _report_evidence_failure(stderr, "pause_state", exc)
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="integrity_failure", waited_seconds=0,
            invocation_id=invocation_id,
            stderr=stderr,
        )
        return 0
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
        return 0
    deadline_seconds = consent.get("wait_deadline_seconds")
    if not isinstance(deadline_seconds, int) or isinstance(deadline_seconds, bool):
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="integrity_failure", waited_seconds=0,
            invocation_id=invocation_id,
            stderr=stderr,
        )
        return 0
    if not isinstance(poll_interval_seconds, (int, float)) or poll_interval_seconds <= 0:
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="integrity_failure", waited_seconds=0,
            invocation_id=invocation_id,
            stderr=stderr,
        )
        return 0
    started = monotonic()
    deadline = started + deadline_seconds
    controller = WakeHoldController(participant.root)
    consent_relative = consent_ledger_relative(participant.binding.node_id)
    consent_watch = WatchedLedger(participant.root.resolve_relative(consent_relative))
    workspace_map_watch = WatchedLedger(Path(bus_home) / WORKSPACE_MAP_RELATIVE)
    with ExitStack() as resources:
        changes = _WaiterInputsWatch(
            participant, session_digest, workspace=Path(raw_workspace),
        )
        resources.callback(changes.close)
        idle_artifact = None
        while True:
            dirty, invalidate = changes.changed()
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
                    return 0
                participant = refreshed
            replacement = consent_watch.poll()
            if replacement is not None:
                reopened = _reopen_consent(
                    participant,
                    replacement,
                    consent_relative=consent_relative,
                    session_digest=session_digest,
                    invocation_id=invocation_id,
                    waited_seconds=max(0, int(monotonic() - started)),
                    stderr=stderr,
                )
                if reopened is None:
                    return 0
                # Continue the same wait from where it stood: the start instant is
                # never reset, so a reopen neither restarts nor extends the clock.
                consent, deadline_seconds = reopened
                deadline = started + deadline_seconds
            try:
                current_authority = CodexWaitSessionLedger(
                    participant.root
                ).participate(participant.binding, consent, session_id)
            except Exception as exc:
                raise_reader_failure(exc)
                _report_evidence_failure(stderr, "session_authority", exc)
                _record_exit(
                    participant, session_digest=session_digest,
                    reason_code="integrity_failure",
                    waited_seconds=max(0, int(monotonic() - started)),
                    invocation_id=invocation_id,
                    stderr=stderr,
                )
                return 0
            if current_authority is None:
                _record_exit(
                    participant, session_digest=session_digest,
                    reason_code="not_claimant",
                    waited_seconds=max(0, int(monotonic() - started)),
                    invocation_id=invocation_id,
                    stderr=stderr,
                )
                return 0
            current_time = monotonic()
            if idle_artifact is not None and not dirty and current_time < deadline:
                from .wake_hold import _claim_holder_released

                try:
                    released = bool(idle_artifact.get("held_total", 0)) and _claim_holder_released(
                        participant.root, participant.binding.node_id,
                    )
                except Exception as exc:
                    raise_reader_failure(exc)
                    # Let the native evaluator supply its existing failure outcome.
                    released = True
                if not released:
                    sleep(min(float(poll_interval_seconds), max(0.0, deadline - current_time)))
                    continue
            if invalidate:
                # A changed middle frame must not survive the prefix cursor's
                # first-frame sample, even when inode/size/mtime were restored.
                controller = WakeHoldController(participant.root)
            invocation_key = "codex-stop-" + uuid7_hex()
            try:
                artifact = controller.evaluate(
                    participant.binding.node_id,
                    idempotency_key=invocation_key,
                )
            except Exception as exc:
                raise_reader_failure(exc)
                _report_evidence_failure(stderr, "wake_evaluation", exc)
                _record_exit(
                    participant, session_digest=session_digest,
                    reason_code="integrity_failure",
                    waited_seconds=max(0, int(monotonic() - started)),
                    invocation_id=invocation_id,
                    stderr=stderr,
                )
                return 0
            state = artifact.get("state")
            idle_artifact = artifact if (
                state in {"caught_up", "held_only"} and not artifact.get("wake_required")
            ) else None
            if state == "fresh_work" and artifact.get("wake_required"):
                messages = artifact.get("fresh_messages")
                receipt = artifact.get("receipt")
                if not isinstance(messages, list) or not isinstance(receipt, dict):
                    return 0
                item_ids = [row.get("id") for row in messages if isinstance(row, dict)]
                if len(item_ids) != len(messages) or not all(isinstance(item, str) for item in item_ids):
                    return 0
                reason = (
                    f"[floati] {len(item_ids)} new message(s) for "
                    f"{participant.binding.node_id}: " + ", ".join(item_ids)
                )
                try:
                    from .jsonl import read_records_snapshot

                    read_records_snapshot(
                        participant.root,
                        Path('receipts/wakes') / (participant.binding.node_id + '.jsonl'),
                        allowed_kinds={'wake_attempt_receipt'},
                    )
                except Exception as exc:
                    # Keep ordinary best-effort receipt behavior, but detect an
                    # older installed reader before publishing the hook decision.
                    raise_reader_failure(exc)
                try:
                    stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
                    stdout.flush()
                except Exception as exc:
                    raise_reader_failure(exc)
                    return 0
                try:
                    WakeAttemptLedger(participant.root).record(
                        recipient=participant.binding.node_id,
                        acting_session_id=session_id,
                        item_ids=item_ids,
                        decision_receipt_id=str(receipt["id"]),
                        message_worker_session_id=None,
                        idempotency_key=invocation_key + "-prompt",
                        outcome="woke",
                    )
                except Exception as exc:
                    raise_reader_failure(exc)
                    _report_evidence_failure(stderr, "wake_attempt", exc)
                    _record_exit(
                        participant,
                        session_digest=session_digest,
                        reason_code="integrity_failure",
                        waited_seconds=max(0, int(monotonic() - started)),
                        invocation_id=invocation_id,
                        stderr=stderr,
                    )
                return 0
            now = monotonic()
            if now >= deadline:
                waited = max(0, int(now - started))
                _record_exit(
                    participant, session_digest=session_digest,
                    reason_code="exhausted", waited_seconds=waited,
                    invocation_id=invocation_id,
                    stderr=stderr,
                )
                try:
                    CodexWaitReceiptLedger(participant.root).record_exhaustion(
                        node_id=participant.binding.node_id,
                        session_digest=session_digest,
                        waited_seconds=waited,
                        idempotency_key=invocation_key + "-exhaustion",
                    )
                except Exception as exc:
                    raise_reader_failure(exc)
                    _report_evidence_failure(stderr, "exhaustion", exc)
                try:
                    stdout.write(
                        json.dumps(
                            {
                                "decision": "block",
                                "reason": "(floati: wait deadline exhausted; end this turn to re-arm)",
                            }
                        )
                        + "\n"
                    )
                    stdout.flush()
                except Exception as exc:
                    raise_reader_failure(exc)
                    return 0
                return 0
            sleep(min(float(poll_interval_seconds), max(0.0, deadline - now)))


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
    return run_stop_waiter(
        bus_home=Path(args.root),
        hook_payload=payload,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
