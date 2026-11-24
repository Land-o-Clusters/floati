"""Cursor Stop waiter — the Codex waiter's twin for an IDE seat.

The Cursor ``stop`` hook calls ``floati wake wait --harness cursor`` with
an explicit root and node. Stdin is the harness payload. Five exits:

* ``error`` is never a person -> drain ``followup_message`` at once
* ``aborted`` gets ONE re-arm whose text carries a human escape
  (``if a person stopped this turn, reply exactly: stop``). A second
  consecutive abort in the same ``conversation_id``, or abort at the
  loop_limit, prints ``{}``. Consecutive-abort memory lives in this
  waiter's journal; every non-completed exit journals the payload keys.
* newly selected mail -> exact-envelope ``followup_message``
* the inbox cannot be read at all (root missing, not a fleet root, node
  unregistered, ledger unreadable) -> a ``followup_message`` naming the
  node, the root and the typed reason, never ``{}``
* deadline under the hook timeout -> one-line re-arm, never ``{}``
* a configuration-time refusal (bad stdin payload, bad deadline) -> a
  typed floati artifact on stdout, which Cursor reads as an empty body,
  AND one line on stderr so the person installing the hook sees the cause

Empty inbox is floati's typed silence and is not an exit; the waiter blocks
on its hint file and the epoch marker until mail, the remaining deadline, or
the bounded safety re-check. Every exit is journaled under the explicit root
WHEN that root is a readable fleet root, and nowhere else: a waiter that created the root it
has just called missing would make its own second wake report a different,
wrong cause. A journal that cannot be written never suppresses the hook
body; it goes to stderr as one line instead.

Every normal followup additionally requires the actual working directory's
mapping, armed consent and valid native conversation identity. The shared
native claimant decision and node guard cover delivery and stdout flush.
Displaced or paused sessions print ``{}``; configuration declines continue
with a typed diagnostic and remedy, never an unauthorized drain command.
Journal publication testimony is appended only after a successful flush.
Completed polls classify unACKed mail against durable conversation-bound
preparation and flush history; held mail waits through the normal deadline.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Optional, TextIO, Tuple

from .errors import DurabilityFailure, FloatiError, IntegrityFailure, ProtocolRefusal
from .events import EventLog
from .root import FloatiRoot, validate_identifier
from .wake_hints import wait_readable as native_wait_readable


HOOK_TIMEOUT_SECONDS = 1800.0
DEFAULT_DEADLINE_SECONDS = 1500.0
DEFAULT_POLL_SECONDS = 3.0

# The idle wait blocks on the hint and epoch marker until the remaining
# deadline or the 60-second safety re-check. A missing event source still
# sleeps at the poll interval, and that fallback can return up to one poll
# after the deadline. The margin buys process start, the inbox read and the
# emit, so the hook returns BEFORE Cursor kills it.
DEADLINE_MARGIN_SECONDS = 5.0

# Cursor's hooks documentation (https://cursor.com/docs/agent/hooks):
# "The default limit is 5 auto follow-ups per script, configurable via the
# `loop_limit` option. Set `loop_limit` to `null` to remove the cap."
#
# Against that sentence there is ONE LIVE OBSERVATION AND NO CONTROL: the
# journal row of 2026-09-11T02:54:19Z, a seat running with loop_limit null,
# delivered status "aborted" at loop_count 5 with nobody at the keyboard --
# exactly the documented default cap. "Null is read as absent, not as
# uncapped" is the best explanation of that one row, NOT a measurement; the
# rival reading, that the abort came from something else that happened to
# land on 5, is not excluded, and the control -- the same seat under an
# explicit numeric limit passing 5 follow-ups -- HAS NOT BEEN RUN.
#
# The explicit integer below is written anyway because the uncertainty is
# one-sided: a number costs nothing if null would also have worked, and a
# null costs the seat at follow-up 5 if this reading is right. The docs
# state no maximum; 1000 re-arms at the 1500 s default deadline is roughly
# 17 days of continuous seat life, past any real session, and still finite
# so a genuinely runaway loop eventually stops.
CURSOR_LOOP_LIMIT = 1000


def _journal_path(root: Path, node: str) -> Path:
    return root / "state" / "cursor-wait" / node / "journal.jsonl"


def _drain_command(root: Path, node: str, runtime: Path, session: str) -> str:
    """The one line the seat is meant to paste, shell-quoted."""

    return (
        f"cd {shlex.quote(str(runtime))} && python3 -m floati inbox "
        f"--root {shlex.quote(str(root))} --as {shlex.quote(node)} "
        f"--session {shlex.quote(session)}"
    )


def _drain_followup(root: Path, node: str, runtime: Path, session: str) -> str:
    # The command is a LINE, not a clause. An agent reading this followup
    # selects a line and pastes it; the Am.1 shape put the command inside a
    # sentence with a parenthetical after it, so the paste handed the shell
    # a stray "(then ack ...)". Prose above, prose below, command alone.
    return (
        f"[floati] unread mail for {node} on {root}.\n"
        "Drain it now:\n"
        f"{_drain_command(root, node, runtime, session)}\n"
        "Then ack each envelope and act on the newest architect order first.\n"
        "When the queue is empty, say so in one line and stop."
    )


def _unreadable_followup(root: Path, node: str, reason: str) -> str:
    return (
        f"[floati] cannot read the inbox for {node} on {root}: {reason}. "
        "The root may be missing, may not be a floati fleet root, the node "
        "may not be registered there, or the ledger may be unreadable. "
        "Say that in one line, naming this root and node, and stop."
    )


def _rearm_followup(node: str, deadline_seconds: float) -> str:
    minutes = int(deadline_seconds // 60)
    return (
        f"[floati] no mail for {node} in {minutes} min. "
        "Reply exactly: armed. Then stop."
    )


HUMAN_ESCAPE = "if a person stopped this turn, reply exactly: stop"
SCOPE_NOTICE = (
    "this hook is scoped to {workspace}; re-rooting the chat leaves it behind "
    "— work other directories by absolute path"
)


def _abort_followup(
    root: Path,
    node: str,
    runtime: Path,
    session: str,
    status: object,
    loop_count: object,
) -> str:
    return (
        f"[floati] the previous turn ended with status={status} "
        f"at loop_count={loop_count}.\n"
        f"{HUMAN_ESCAPE}\n"
        "Otherwise drain:\n"
        f"{_drain_command(root, node, runtime, session)}\n"
        "Then ack each envelope and act on the newest architect order first.\n"
        "When the queue is empty, say so in one line and stop."
    )


def _last_event_for_conversation(
    journal: Path, node: str, conversation_id: object
) -> Optional[str]:
    """Last journaled event for this node and conversation, or None."""

    if not journal.is_file():
        return None
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("node") != node:
            continue
        if record.get("conversation_id") != conversation_id:
            continue
        event = record.get("event")
        return event if isinstance(event, str) else None
    return None


_STAMP_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ")


def _stamp_gap_seconds(stamp_text: Optional[str], now_text: str) -> float:
    """Wall seconds between a journaled stamp and now; 0.0 when unreadable."""

    def parse(text: str) -> Optional[datetime]:
        for pattern in _STAMP_FORMATS:
            try:
                return datetime.strptime(text, pattern)
            except ValueError:
                continue
        return None

    if not isinstance(stamp_text, str) or not isinstance(now_text, str):
        return 0.0
    stamped = parse(stamp_text)
    current = parse(now_text)
    if stamped is None or current is None:
        return 0.0
    return max(0.0, (current - stamped).total_seconds())


def _chain_wait_state(
    journal: Path, node: str, conversation_id: object
) -> Tuple[float, Optional[str]]:
    """Chain seconds already spent by earlier waits of this conversation.

    Each re-arm hands control back to Cursor, whose hook timeout keeps
    running from the turn-end invocation's start — both measured grok
    deaths (seat-13 gate §14) ended in a closed pipe just past that budget.
    A re-arm row therefore records the cumulative ``chain_wait_seconds``;
    the next invocation reads the last such row for the conversation so its
    wait can only end inside the budget that remains.
    """

    if not journal.is_file():
        return 0.0, None
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0.0, None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("node") != node:
            continue
        if record.get("conversation_id") != conversation_id:
            continue
        chain = record.get("chain_wait_seconds")
        if not isinstance(chain, (int, float)) or isinstance(chain, bool):
            continue
        stamp_text = record.get("ts")
        return float(chain), stamp_text if isinstance(stamp_text, str) else None
    return 0.0, None


def _append_journal(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _needs_you_wall() -> float:
    """Wall-clock seconds for the needs-you pull interval, which spans invocations."""
    return time.time()


def _typed_reason(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    detail = getattr(exc, "detail", None) or getattr(exc, "strerror", None)
    named = type(exc).__name__
    if isinstance(code, str) and code:
        named = f"{named} {code}"
    return f"{named}: {detail or exc}"


def _is_readable_fleet_root(root: Path) -> bool:
    """May the waiter write its journal under this root at all?

    A refusal path must not materialise the tree it has just refused to
    read. ``_append_journal`` does ``mkdir(parents=True)``, so without this
    gate a wake on a typo'd or unmounted root CREATES
    ``<root>/state/cursor-wait/<node>/`` at that path -- and the next wake
    finds a directory that opens as a fleet root, so the typed reason
    silently changes from ``direct_home_missing`` to ``unknown_node`` and
    sends the reader to registration for a path fault. The reason a seat
    reports must be a property of the operator's configuration, not of how
    many times the hook has fired against it.
    """

    try:
        FloatiRoot.open_direct_home(root)
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError):
        return False
    return True


def _default_peek(root: FloatiRoot, node: str) -> Tuple[int, Optional[BaseException]]:
    """Unread count, or -1 with the typed reason the inbox could not be read."""

    try:
        # The caller holds the shared epoch and exact node guard through
        # stdout.flush; the public method would recursively lock the node.
        messages, _receipt = EventLog(root)._present_already_guarded(node)
        return len(messages), None
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
        from .waiter_lock_retry import current_lock_window, transient_ledger_timeout, WaiterLockDeadline
        if current_lock_window() is not None and (
                transient_ledger_timeout(exc) or isinstance(exc, WaiterLockDeadline)):
            raise
        return -1, exc


def _run_cursor_stop_wait_invocation(
    *,
    root: Path,
    node: str,
    runtime: Path,
    deadline_seconds: float,
    poll_seconds: float,
    hook_timeout_seconds: float,
    payload: Mapping[str, object],
    stdout: TextIO,
    stderr: Optional[TextIO] = None,
    loop_limit: Optional[int] = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wait_readable: Callable[[object, float], bool] = native_wait_readable,
    peek_unread: Optional[Callable[[], int]] = None,
    ledger_mtime: Optional[Callable[[], float]] = None,
    now_utc: Optional[Callable[[], str]] = None,
    last_seen_root: Optional[Path] = None,
    _invocation=None,
) -> int:
    from .cursor_stop_hook import observe_cursor_stop, observe_cursor_tool_failure
    if payload.get("hook_event_name") == "postToolUseFailure":
        observe_cursor_tool_failure(root, payload)
        # The observational failure hook has no continuation or permission
        # response. It must never enter the long-lived Stop waiter.
        stdout.write("{}\n")
        stdout.flush()
        return 0
    observe_cursor_stop(root, payload)
    from .stop_hold import _HoldEvidence, _utc_milliseconds

    timestamp = now_utc or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    from .waiter_lock_retry import waiter_lock_window
    budget = deadline_seconds if peek_unread is not None and isinstance(deadline_seconds, (int, float)) else None
    hold = _HoldEvidence(stderr, lambda: _utc_milliseconds(timestamp()))
    hold.publication_failure = False
    hold.execution_binding = None
    hold.invocation_lease = _invocation
    try:
        with waiter_lock_window(budget, clock, sleep) as retry_window:
            try:
                result = _run_cursor_stop_wait(root=root, node=node, runtime=runtime,
                    deadline_seconds=deadline_seconds, poll_seconds=poll_seconds,
                    hook_timeout_seconds=hook_timeout_seconds, payload=payload,
                    stdout=hold.output(stdout), stderr=stderr, loop_limit=loop_limit,
                    clock=clock, sleep=sleep, wait_readable=wait_readable,
                    peek_unread=peek_unread,
                    ledger_mtime=ledger_mtime, now_utc=now_utc,
                    last_seen_root=last_seen_root, hold=hold, retry_window=retry_window)
            finally:
                hold.close()
    except (OSError, ValueError) as error:
        # All native node/epoch guards have exited. Publishing a diagnostic
        # must not reverse the car -> recipient lock order or replace failure.
        if hold.execution_binding is not None and hold.publication_failure:
            try:
                from .seat_execution import _report_cursor_failure
                controller, invocation = hold.execution_binding
                _report_cursor_failure(controller, invocation, error)
            except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError, ValueError):
                pass
        if isinstance(error, BrokenPipeError):
            # The harness has closed the pipe: the invocation is over, and
            # the defect this row forbids is the escaping traceback that
            # left the seat deaf. The failure is already journaled in emit;
            # end the hook quietly so the seat's next stop can re-arm it.
            try:
                sink = stderr if stderr is not None else sys.stderr
                sink.write("[floati] Cursor hook output pipe closed; ending quietly\n")
                sink.flush()
            except (OSError, ValueError):
                pass
            return 0
        raise
    if hold.execution_binding is not None:
        # Returning a followup does not prove a stopped seat. Settle only this
        # invocation's running veto, after all native guards have unwound.
        try:
            controller, invocation = hold.execution_binding
            controller.callback(invocation, {"event": "unknown",
                "code": "cursor_followup_returned", "detail": "native Cursor followup returned"})
        except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError, ValueError):
            pass
    return result


def run_cursor_stop_wait(**arguments) -> int:
    """Retain one nonqueueing invocation lease through the complete flush."""
    from .waiter_invocation import verified_invocation
    lease = None
    payload = arguments.get("payload", {})
    if payload.get("hook_event_name") != "postToolUseFailure":
        # The native adapters share workspace/session authority. Match the
        # guarded loop's native-role set, including Codex-registered seats;
        # an authorized ordinary invocation must not lose its lease here.
        lease = verified_invocation(arguments["root"], arguments["node"], Path.cwd(),
            payload.get("conversation_id"))
    if lease is not None and not lease.owned:
        output = arguments["stdout"]
        output.write("{}\n")
        output.flush()
        return 0
    try:
        return _run_cursor_stop_wait_invocation(**arguments, _invocation=lease)
    finally:
        if lease is not None:
            lease.close()


def _open_cursor_inputs_watch(participant, session_id, stderr, workspace):
    """Register the Codex hint and epoch watch. A hint grants no authority."""

    from .codex_wait import _WaiterInputsWatch
    from .wake_control import _digest

    return _WaiterInputsWatch(
        participant, _digest(session_id),
        stderr if stderr is not None else sys.stderr,
        workspace=workspace,
    )


def _run_cursor_stop_wait(
    *, root, node, runtime, deadline_seconds, poll_seconds, hook_timeout_seconds,
    payload, stdout, stderr, loop_limit, clock, sleep, wait_readable, peek_unread,
    ledger_mtime, now_utc, last_seen_root, hold, retry_window,
) -> int:
    """Block until mail or the deadline, then print one Cursor hook body."""

    if poll_seconds <= 0:
        raise ProtocolRefusal(
            "cursor_wait_poll_invalid",
            "poll_seconds must be positive",
            remedy="pass a positive --poll-seconds",
        )
    if not (
        isinstance(deadline_seconds, (int, float))
        and not isinstance(deadline_seconds, bool)
        and deadline_seconds > 0
        and deadline_seconds + float(poll_seconds) + DEADLINE_MARGIN_SECONDS
        <= float(hook_timeout_seconds)
    ):
        raise ProtocolRefusal(
            "cursor_wait_deadline_invalid",
            "deadline_seconds must be positive and leave the poll interval "
            "and the margin under the hook timeout",
            remedy="pass --deadline-seconds at most "
            f"{float(hook_timeout_seconds) - float(poll_seconds) - DEADLINE_MARGIN_SECONDS} "
            f"(timeout {hook_timeout_seconds}, poll {poll_seconds}, "
            f"margin {DEADLINE_MARGIN_SECONDS})",
        )
    root = Path(root)
    runtime = Path(runtime)
    journal = _journal_path(root, node)
    status = payload.get("status")
    loop_count = payload.get("loop_count")
    conversation_id = payload.get("conversation_id")
    workspace = Path.cwd()
    seen_root = Path(last_seen_root) if last_seen_root is not None else workspace
    from .bus_epoch import epoch_guard
    from .codex_wait_contract import (
        CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant,
    )
    from .registry import Registry
    from .seat_teardown import _marker_path, completed_teardown_for_session
    from .wake_control import WakeController, is_session_paused, validate_session_id
    from .wake_hold import _has_rolled_wake_history, _RolledWakeProjection, wake_coordination_guard

    try:
        original_session = validate_session_id(conversation_id)
    except ProtocolRefusal:
        original_session = None
    stamp = now_utc or (
        lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    node_guard_held = False
    native_guard_held = False
    pending_publication = None
    presentation_plane = None

    def complain(line: str) -> None:
        """One line to the person watching the IDE, never an exception."""

        sink = stderr if stderr is not None else sys.stderr
        try:
            sink.write(line + "\n")
            sink.flush()
        except (OSError, ValueError):
            pass

    # Chain seconds already spent before this invocation's wait began; set
    # by the chain-budget cap below, before any emit can run.
    chain_state = {"elapsed": 0.0}

    def emit(event: str, body: Optional[dict], *, confirm_wake=None, **extra: object) -> int:
        nonlocal pending_publication
        retry_window.finish()
        record = {
            "ts": stamp(),
            "event": event,
            "node": node,
            "status": status,
            "loop_count": loop_count,
            "conversation_id": conversation_id,
            "last_seen_root": str(seen_root),
            "chain_wait_seconds": round(
                chain_state["elapsed"] + float(extra.get("waited_seconds") or 0.0), 1
            ),
            **extra,
        }
        # The output wrapper otherwise takes the public, nonreentrant node
        # guard. Partial acquisition failures need that public route; normal
        # publication already owns both guards and must use the private seam.
        hold.close(already_guarded=node_guard_held)
        phase = "write"
        try:
            stdout.write(json.dumps({} if body is None else body) + "\n")
            phase = "flush"
            stdout.flush()
        except BrokenPipeError as error:
            # The harness closed the pipe. Journal the typed failure and let
            # the exception reach run_cursor_stop_wait, which reports it and
            # ends the hook quietly - never the escaping traceback that left
            # the seat deaf (CURSOR-HOOK-REARM-DEATH-1).
            failure = {**record, "publication": "failed", "failure_phase": phase,
                "reason_code": "cursor_followup_publication_failed",
                "exception_type": type(error).__name__, "detail": str(error)}
            if hold.start_row is not None:
                failure["invocation_id"] = hold.start_row["invocation_id"]
                failure["acting_session_id"] = hold.start_row["acting_session_id"]
                hold.publication_failure = True
            try:
                _append_journal(journal, failure)
            except OSError as journal_error:
                complain("[floati] Cursor publication testimony unavailable: " + type(journal_error).__name__)
            raise
        except (OSError, ValueError) as error:
            if body and body.get("followup_message") and hold.start_row is not None:
                hold.publication_failure = True
                failure = {**record, "publication": "failed", "failure_phase": phase,
                    "reason_code": "cursor_followup_publication_failed",
                    "exception_type": type(error).__name__, "detail": str(error),
                    "invocation_id": hold.start_row["invocation_id"],
                    "acting_session_id": hold.start_row["acting_session_id"]}
                try:
                    _append_journal(journal, failure)
                except OSError as journal_error:
                    complain("[floati] Cursor publication testimony unavailable: " + type(journal_error).__name__)
            raise
        if confirm_wake is not None and not confirm_wake():
            record.pop("needs_wake_id", None)
        if pending_publication is not None:
            plane, prepared, delivery = pending_publication
            pending_publication = None
            try:
                plane.flushed(prepared, delivery)
            except Exception as exc:
                # The body is already flushed. A terminal-save failure leaves
                # the durable preparation uncertain; never print it again.
                record.update(presentation_phase="presentation_uncertain",
                    presentation_item_ids=list(prepared["item_ids"]))
                complain(f"[floati] {node}: presentation_uncertain after hook flush "
                    f"for {json.dumps(prepared['item_ids'])}: {_typed_reason(exc)}; "
                    "use the native inbox and ACK recovery after the bounded hold")
        if body and body.get("followup_message") and original_session is not None:
            record.update(publication="flushed", published_at=stamp())
        # The journal is evidence, not the product. A read-only root or a
        # full disk must not turn an exit into a traceback with EMPTY stdout,
        # which Cursor reads as {} and which stops the seat exactly the way
        # the missing body would. And a root floati cannot read is not
        # journaled at all: the row goes to stderr on one line, because
        # creating it there is what corrupts the NEXT wake's diagnosis.
        if not _is_readable_fleet_root(root):
            complain(
                f"[floati] {node}: not journaling under {root}: it is not a "
                "readable floati fleet root, and creating one there would "
                "change the reason the next wake reports; no durable receipt. The row: "
                + json.dumps(record, sort_keys=True)
            )
        else:
            try:
                _append_journal(journal, record)
            except OSError as exc:
                complain(
                    f"[floati] {node}: could not write the cursor-wait journal at "
                    f"{journal}: {_typed_reason(exc)}; the hook body was flushed"
                )
        return 0

    def _noncompleted_extra() -> dict:
        return {"payload_keys": sorted(payload)}

    def unverified_pause(reason_code="wake_marker_invalid") -> int:
        # An unreadable exact marker cannot authorize a continuation. Do not
        # render its untrusted bytes, even when the stop status was an error.
        return emit("exit_empty_pause_unverified", None,
            reason_code=reason_code, calling_session_id=conversation_id,
            **(_noncompleted_extra() if status != "completed" else {}))

    def acquire_native_control(opened, guards, session):
        nonlocal native_guard_held
        # Validate the lexical session coordinate before any resolving native
        # helper can hide a symlinked marker ancestor. The same-inode native
        # lock linearizes pause/resume with every continuation's final flush.
        _marker_path(opened, node, session)
        relative = Path("state/wake-control") / (node + ".lock")
        if not native_guard_held:
            guards.enter_context(_locked_path(opened.resolve_relative(relative),
                exclusive=True, relative=relative, timeout_seconds=5.0,
                order_tracked=False))
            native_guard_held = True

    def pause_exit(opened, guards, session):
        acquire_native_control(opened, guards, session)
        try:
            completed = completed_teardown_for_session(opened, node, session)
        except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
            propagate_retry(exc)
            # Completion-ledger corruption must not revive a valid exact
            # pause. Verify its native marker independently under the same
            # lock; an unpaused chat keeps the ordinary typed diagnostic.
            controller = WakeController(opened)
            resolved_node, _session, digest = controller._identity(node, session)
            marker = controller._read_marker(_marker_path(opened, node, session), resolved_node, digest)
            if marker is not None:
                return unverified_pause("seat_teardown_unverified")
            raise
        if completed is not None:
            return emit("exit_empty_teardown", None,
                reason_code="seat_teardown_completed", calling_session_id=session,
                teardown_receipt_id=completed["id"],
                pause_receipt_id=completed["pause_receipt_id"],
                **(_noncompleted_extra() if status != "completed" else {}))
        if is_session_paused(opened, node, session):
            return emit("exit_empty_participation", None,
                reason_code="paused", calling_session_id=session,
                **(_noncompleted_extra() if status != "completed" else {}))
        return None

    def diagnostic(exc: BaseException, *, participation: bool) -> int:
        reason = _typed_reason(exc)
        reason_code = getattr(exc, "code", None) or "cursor_wait_inbox_unreadable"
        named_remedy = getattr(exc, "remedy", None)
        remedy = named_remedy if isinstance(named_remedy, str) and named_remedy else (
            "restore the declared root, active node and readable fleet ledgers"
        )
        message = (
            f"[floati] participation refused for {node} on {root}: {reason}. "
            f"Remedy: {remedy}. Report this diagnostic and stop."
        ) if participation else _unreadable_followup(root, node, reason)
        return emit(
            "followup_participation_refused" if participation else "followup_inbox_unreadable",
            {"followup_message": message}, reason=reason,
            reason_code=reason_code, remedy=remedy,
            **(_noncompleted_extra() if status != "completed" else {}),
        )

    ledger = root / "events.jsonl"

    def mtime() -> float:
        if ledger_mtime is not None:
            return float(ledger_mtime())
        try:
            return ledger.stat().st_mtime
        except OSError:
            return 0.0

    from .waiter_lock_retry import (
        WaiterAuthorityChanged, WaiterLockDeadline, transient_ledger_timeout,
    )
    from .jsonl import _locked_path, read_records_snapshot

    # Native snapshot proof establishes the original budget before guards wait.
    # Failed/unproven proof retains main's guarded named diagnostic path.
    captured = None
    try:
        participant = resolve_participant(root, workspace)
        if participant is not None and participant.binding.node_id == node:
            Registry(participant.root).require_active(node)
            consent = CodexWaitConsentLedger(participant.root).require_armed(participant.binding)
            session = validate_session_id(conversation_id)
            captured = participant, consent, session
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError):
        pass
    if captured is not None:
        participant, original_consent, session = captured
        deadline_seconds = min(float(deadline_seconds), float(original_consent["wait_deadline_seconds"]))
        retry_window.update(deadline_seconds)

        def check_authority(*, require_claim=False):
            current = resolve_participant(root, workspace, _strict=True)
            if current is None or current.root.tenant_home != participant.root.tenant_home or (
                    current.binding != participant.binding):
                raise WaiterAuthorityChanged("not_claimant")
            registry = Registry(current.root)
            registry.require_active(node)
            registry.require_protocol_lease(node, act="fresh delivery to recipient")
            try:
                latest = CodexWaitConsentLedger(current.root).require_armed(current.binding)
            except ProtocolRefusal as exc:
                if exc.code != "codex_wait_consent_missing":
                    raise
                raise WaiterAuthorityChanged("consent_withdrawn") from exc
            if latest != original_consent:
                raise WaiterAuthorityChanged("consent_withdrawn")
            ledger = CodexWaitSessionLedger(current.root)
            rows = read_records_snapshot(current.root, ledger._relative(node), allowed_kinds=ledger._KINDS)
            matching = ledger._matching(rows, current.binding)
            if require_claim and not matching:
                raise WaiterAuthorityChanged("not_claimant")
            if matching and matching[-1]["acting_session_id"] != session:
                former = any(row["acting_session_id"] == session for row in matching)
                raise WaiterAuthorityChanged("superseded_session" if former else "not_claimant",
                    matching[-1]["acting_session_id"])
            _marker_path(current.root, node, session)
            if is_session_paused(current.root, node, session):
                raise WaiterAuthorityChanged("paused")
        retry_window.check_authority = check_authority

    def propagate_retry(exc):
        if not retry_window.finishing and retry_window.deadline is not None and (
                transient_ledger_timeout(exc) or isinstance(exc, WaiterLockDeadline)):
            raise exc

    # CURSOR-HOOK-REARM-DEATH-1: the harness timeout runs from the turn-end
    # invocation's start, and a re-arm hands control back with that clock
    # still running. Cap this wait to what the chain has left, and end the
    # chain quietly when nothing is left - never a second full wait that
    # can only meet a closed pipe.
    chain_waited, chain_row_stamp = _chain_wait_state(journal, node, conversation_id)
    chain_state["elapsed"] = chain_waited + (
        _stamp_gap_seconds(chain_row_stamp, stamp())
        if chain_row_stamp is not None else 0.0
    )
    chain_budget = (
        float(hook_timeout_seconds) - float(poll_seconds)
        - DEADLINE_MARGIN_SECONDS - chain_state["elapsed"]
    )
    if chain_budget <= 0:
        return emit("exit_empty_chain_budget", None,
            reason_code="cursor_wait_chain_budget_exhausted",
            chain_wait_seconds=round(chain_state["elapsed"], 1),
            hook_timeout_seconds=float(hook_timeout_seconds),
            **(_noncompleted_extra() if status != "completed" else {}))
    if chain_budget < deadline_seconds:
        deadline_seconds = float(chain_budget)
        if retry_window.deadline is not None:
            retry_window.update(deadline_seconds)

    started = retry_window.started
    last = None
    initial_binding = None
    terminal = None
    held_signature = None
    last_native_selection = None

    def held_followup(plane, selection):
        evidence = plane.held_evidence(selection)
        uncertain = any(item.get("phase") != "flushed" for item in selection["held_items"])
        state = "presentation_uncertain" if uncertain else "held_unacked"
        message = (f"[floati] {state} for {node} on {root}: "
            + json.dumps(evidence, sort_keys=True) + ". ")
        if uncertain:
            message += ("Automatic presentation is held because a prepared attempt has no "
                "confirmed flush. Recover the exact named IDs through the native inbox; "
                "acknowledge them after seeing and handling them:\n"
                + _drain_command(root, node, runtime, original_session) + "\n")
        else:
            message += "These IDs were already notified to this conversation and remain unACKed. "
        message += "Reply exactly: armed. Then stop."
        return {"followup_message": message}, evidence, state

    def observe_held(plane, selection):
        nonlocal held_signature
        evidence = plane.held_evidence(selection)
        signature = json.dumps(evidence, sort_keys=True)
        if signature == held_signature:
            return
        held_signature = signature
        try:
            _append_journal(journal, {"ts": stamp(), "event": "held_unacked_observed",
                "node": node, "status": status, "conversation_id": conversation_id,
                "held": evidence})
        except OSError as exc:
            complain(f"[floati] {node}: could not journal held mail: {_typed_reason(exc)}")

    def terminal_output(opened=None, guards=None):
        nonlocal terminal
        if getattr(terminal, "code", None) == "wake_marker_invalid":
            return unverified_pause()
        if node_guard_held and opened is not None and original_session is not None and (
                isinstance(terminal, WaiterLockDeadline) or (
                    isinstance(terminal, WaiterAuthorityChanged) and terminal.reason == "paused")):
            # An expired retry may observe the teardown pause lock-free.
            # Reproduce its typed completion only under both exact guards,
            # retaining the native guard through any terminal re-arm flush.
            # Final acquisition gets one uncontended attempt, no new budget.
            retry_window.finish()
            try:
                result = pause_exit(opened, guards, original_session)
                if result is not None:
                    return result
            except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
                if getattr(exc, "code", None) == "wake_marker_invalid":
                    return unverified_pause()
                if isinstance(exc, WaiterLockDeadline) or transient_ledger_timeout(exc):
                    return emit("exit_empty_deadline", None,
                        reason_code="deadline_guard_unavailable", calling_session_id=conversation_id,
                        waited_seconds=round(clock() - started, 1))
                return diagnostic(exc, participation=False)
        if isinstance(terminal, WaiterAuthorityChanged):
            return emit("exit_empty_participation", None, reason_code=terminal.reason,
                claim_state=terminal.reason, calling_session_id=conversation_id,
                current_session_id=terminal.current_session)
        if not isinstance(terminal, WaiterLockDeadline):
            return diagnostic(terminal, participation=False)
        if node_guard_held and last_native_selection is not None and not _has_rolled_wake_history(opened):
            plane, previous = last_native_selection
            try:
                if retry_window.check_authority is None:
                    # A failed lock-free preflight can leave no checker even
                    # after a later authorized poll. Observe its claim tail
                    # without bootstrapping authority at the expired deadline.
                    ledger = CodexWaitSessionLedger(plane.root)
                    rows = read_records_snapshot(plane.root, ledger._relative(node),
                        allowed_kinds=ledger._KINDS)
                    matching = ledger._matching(rows, previous["_binding"])
                    session = previous["_classification"].calling_session_id
                    if not matching:
                        raise WaiterAuthorityChanged("not_claimant")
                    if matching[-1]["acting_session_id"] != session:
                        former = any(row["acting_session_id"] == session for row in matching)
                        raise WaiterAuthorityChanged("superseded_session" if former else "not_claimant",
                            matching[-1]["acting_session_id"])
                # Guards are owned here. Re-read native history and epoch
                # rather than publishing a cached held classification.
                selection = plane._select_waiter(previous["_binding"], previous["_consent"],
                    previous["_classification"], harness="cursor")
            except WaiterAuthorityChanged as exc:
                terminal = exc
                return terminal_output(opened, guards)
            except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
                return diagnostic(exc, participation=False)
            if selection["held_total"]:
                body, evidence, state = held_followup(plane, selection)
                return emit("followup_rearm", body, reason="waiter_lock_deadline",
                    reason_code=state, held=evidence, waited_seconds=round(clock() - started, 1))
        return emit("followup_rearm",
            {"followup_message": _rearm_followup(node, deadline_seconds)},
            reason="waiter_lock_deadline", waited_seconds=round(clock() - started, 1))

    # NEEDS-YOU-OBSERVED-1: the needs-you condition exists only for a seat that
    # declared an orchestrator source (and only on the native read path). The
    # loop's tick pulls at most once per interval outside every guard; poll()
    # publishes a wake for its result under the same guards as mail, after mail.
    needs_you = {"condition": None, "pending": []}
    if peek_unread is None:
        try:
            from .needs_you_condition import NeedsYouCondition
            needs_you["condition"] = NeedsYouCondition.for_seat(
                FloatiRoot.open_direct_home(root), node,
                lambda exc: complain(f"[floati] {node}: needs-you pull unavailable: {_typed_reason(exc)}"))
        except (FloatiError, OSError, ValueError):
            needs_you["condition"] = None

    def observed_item_followup(session):
        """One followup for observed items this seat was never woken for; None when there is none."""
        import hashlib
        from .ids import uuid7_hex
        condition, pending = needs_you["condition"], needs_you["pending"]
        needs_you["pending"] = []
        if condition is None or not pending:
            return None
        invocation_id = (hold.start_row["invocation_id"] if hold.start_row is not None
                         else "cursor-stop-" + uuid7_hex())
        try:
            wake = condition.prepare_wake(hashlib.sha256(session.encode("utf-8")).hexdigest(), invocation_id, pending)
        except ProtocolRefusal as exc:
            complain(f"[floati] {node}: needs-you wake not recorded: {_typed_reason(exc)}")
            return None
        if wake is None:
            return None
        return emit("followup_observed_item", {"followup_message": condition.reason(wake)},
            confirm_wake=lambda: condition.record_flushed_wake(wake),
            reason_code="observed_item", needs_wake_id=wake["id"],
            item_ids=[entry["item_id"] for entry in wake["items"]],
            waited_seconds=round(clock() - started, 1))

    def authorize_observer():
        check = retry_window.check_authority
        if check is None:
            raise WaiterAuthorityChanged("not_claimant")
        lease = hold.invocation_lease
        reason = lease.stop_reason() if lease is not None else None
        if reason is not None:
            raise WaiterAuthorityChanged(reason)
        check(require_claim=True)

    def needs_you_cap(timeout):
        condition = needs_you["condition"]
        return timeout if condition is None else condition.wait_cap(timeout, _needs_you_wall())

    def poll():
        nonlocal node_guard_held, native_guard_held, initial_binding, last, deadline_seconds, terminal, pending_publication, last_native_selection, presentation_plane
        # Authority is re-read on every poll, even when events are unchanged.
        # ExitStack also releases acquired guards after a failed open/lookup;
        # output failures propagate without retrying or claiming publication.
        node_guard_held = False
        native_guard_held = False
        if isinstance(terminal, WaiterLockDeadline) and retry_window.check_authority is not None:
            try:
                retry_window.check_authority()
            except (WaiterAuthorityChanged, ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
                terminal = exc
        with ExitStack() as guards:
            execution_controller = None
            try:
                opened = FloatiRoot.open_direct_home(root)
                guards.enter_context(epoch_guard(opened, exclusive=False))
                registration = Registry(opened).require_active(node)
                if last is None and terminal is None and registration["role"].casefold() == "cursor":
                    # A native start may bind a car only under car -> node
                    # ordering. Failure of optional testimony cannot prevent
                    # an otherwise authorized, ordinary waiter from holding.
                    try:
                        from .car_fence import car_acceptance_guard
                        from .seat_execution import SeatExecutionController
                        guards.enter_context(car_acceptance_guard(opened))
                        execution_controller = SeatExecutionController(opened)
                    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError, ValueError):
                        execution_controller = None
                lane = guards.enter_context(ExitStack())
                lane.enter_context(wake_coordination_guard(opened, node))
                node_guard_held = True
            except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
                propagate_retry(exc)
                if isinstance(terminal, WaiterAuthorityChanged):
                    # Proven loss cannot become a followup merely because
                    # the replacement still owns its publication guard.
                    return terminal_output()
                if isinstance(terminal, WaiterLockDeadline):
                    # An unchanged claim snapshot does not authorize an old
                    # followup while a replacement owns the physical guard.
                    return emit("exit_empty_deadline", None,
                        reason_code="deadline_guard_unavailable", calling_session_id=conversation_id,
                        waited_seconds=round(clock() - started, 1))
                return diagnostic(exc, participation=False)
            if terminal is not None:
                # Terminal retry paths are snapshot-only: they must never
                # bootstrap a claim or revive the already-finished budget.
                if isinstance(terminal, WaiterLockDeadline) and retry_window.check_authority is not None:
                    try:
                        retry_window.check_authority()
                    except WaiterAuthorityChanged as exc:
                        terminal = exc
                    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
                        terminal = exc
                return terminal_output(opened, guards)
            lease = hold.invocation_lease
            reason = lease.stop_reason() if lease is not None else None
            if reason is not None:
                return emit("exit_empty_invocation_retired", None, reason_code=reason,
                    calling_session_id=conversation_id)
            try:
                # A malformed map is a configuration fault even before a
                # contended acquisition has had a chance to mark the window.
                participant = resolve_participant(root, workspace, _strict=True)
                if participant is None:
                    raise ProtocolRefusal(
                        "cursor_wait_workspace_unbound",
                        "the actual working directory has no active waiter mapping",
                        remedy="restore the declared workspace mapping and its recorded waiter consent",
                    )
                binding = participant.binding
                identity = (participant.root.tenant_home, binding.workspace, binding.node_id)
                if binding.node_id != node or (initial_binding is not None and identity != initial_binding):
                    raise ProtocolRefusal(
                        "cursor_wait_workspace_mismatch",
                        "the workspace binding differs from this hook's declared seat",
                        remedy="restore the hook's declared workspace and node mapping",
                    )
                consent = CodexWaitConsentLedger(opened).require_armed(binding)
                deadline_seconds = min(float(deadline_seconds), float(consent["wait_deadline_seconds"]))
                if not retry_window.finishing:
                    retry_window.update(deadline_seconds)
                session = validate_session_id(conversation_id)
                classification = CodexWaitSessionLedger(opened)._participate_already_guarded(
                    binding, consent, session, classified=True,
                )
            except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
                propagate_retry(exc)
                if isinstance(exc, ProtocolRefusal) and exc.code == "codex_wait_consent_missing":
                    exc = ProtocolRefusal(exc.code, exc.detail,
                        remedy="restore explicit waiter consent for this mapped workspace")
                elif isinstance(exc, ProtocolRefusal) and exc.code == "wake_session_invalid":
                    exc = ProtocolRefusal(exc.code, exc.detail,
                        remedy="supply the original valid Cursor conversation_id in the stop payload")
                return diagnostic(exc, participation=True)
            initial_binding = identity
            if classification.state != "authorized":
                return emit("exit_empty_participation", None,
                    reason_code="superseded_session", claim_state=classification.state,
                    calling_session_id=classification.calling_session_id,
                    current_session_id=classification.current_session_id,
                    **(_noncompleted_extra() if status != "completed" else {}))
            if registration["role"].casefold() in {"codex", "cursor", "zcode", "claude"} and (
                    hold.invocation_lease is None or not hold.invocation_lease.owned):
                # Authority may appear after the wrapper's read-only snapshot.
                # Never turn that race into an unleased hold or publication.
                return emit("exit_empty_invocation_unowned", None,
                    reason_code="invocation_unowned", calling_session_id=conversation_id)
            try:
                result = pause_exit(opened, lane, session)
                if result is not None:
                    return result
            except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
                propagate_retry(exc)
                if getattr(exc, "code", None) == "wake_marker_invalid":
                    return unverified_pause()
                return diagnostic(exc, participation=True)
            if status == "error":
                return emit(
                    "followup_after_error",
                    {"followup_message": _drain_followup(root, node, runtime, session)},
                    **_noncompleted_extra(),
                )
            if status == "aborted":
                if (isinstance(loop_limit, int) and not isinstance(loop_limit, bool)
                        and isinstance(loop_count, int) and not isinstance(loop_count, bool)
                        and loop_count >= loop_limit):
                    complain(
                        f"[floati] {node}: Cursor aborted at loop_count {loop_count} of "
                        f"loop_limit {loop_limit} — the auto-follow-up cap is reached and "
                        "this seat is dormant until a human types in it"
                    )
                    return emit("exit_empty_aborted", None, loop_limit=loop_limit,
                        reason_code="cursor_wait_abort_cap", **_noncompleted_extra())
                if _last_event_for_conversation(journal, node, conversation_id) == "followup_after_abort":
                    return emit("exit_empty_aborted_twice", None,
                        reason_code="cursor_wait_aborted_twice", **_noncompleted_extra())
                return emit("followup_after_abort", {"followup_message": _abort_followup(
                    root, node, runtime, session, status, loop_count)}, **_noncompleted_extra())

            first = last is None
            current = mtime()
            plane = selection = None
            if peek_unread is None:
                from .cursor_presentations import CursorPresentationLedger, limit_selection, notification
                try:
                    if presentation_plane is None:
                        presentation_plane = CursorPresentationLedger(opened, sealed="bytes")
                    plane = presentation_plane
                    # Native prefix and conversation testimony is checked on
                    # every poll; mtime equality cannot hide a new envelope.
                    projection = None
                    if _has_rolled_wake_history(opened):
                        extra = plane._prepare_rolled_waiter_projection(node, session)
                        projection = _RolledWakeProjection(opened, node, None, lane, extra)
                        node_guard_held = False
                        native_guard_held = False
                    try:
                        read_options = {"materialize": False} if projection is not None else {}
                        selection = plane._select_waiter(binding, consent, classification,
                            harness="cursor", at_stop=first, **read_options)
                    finally:
                        if projection is not None:
                            try:
                                projection.revalidate(lambda: acquire_native_control(opened, lane, session))
                            finally:
                                node_guard_held = projection.lane_held
                    last_native_selection = plane, selection
                    if selection["fresh_messages"]:
                        from .fleet_presence import presence_line
                        line = presence_line(opened, node)
                        bounded, body = limit_selection(selection,
                            lambda messages: notification(root, node, runtime, session, messages, presence=line))
                        prepared = plane.prepare(bounded, body)
                        delivery = plane.deliver(prepared, bounded)
                        pending_publication = plane, prepared, delivery
                        unread = len(bounded["fresh_messages"])
                        return emit("followup_mail_at_start" if first else "followup_mail", body,
                            unread=unread, fresh_total=selection["fresh_total"],
                            **({} if first else {"waited_seconds": round(clock() - started, 1)}))
                    if selection["held_total"]:
                        observe_held(plane, selection)
                except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
                    # Output exceptions must propagate after emit has finished
                    # its window, without a second body or a publication retry.
                    if retry_window.finishing:
                        raise
                    propagate_retry(exc)
                    return diagnostic(exc, participation=False)
            elif first or current != last:
                unread, failure = (
                    int(peek_unread()), ProtocolRefusal("cursor_wait_inbox_unreadable",
                        "the injected peek reported the inbox unreadable",
                        remedy="restore the declared root, active node and readable fleet ledgers"))
                if unread > 0:
                    return emit("followup_mail_at_start" if first else "followup_mail",
                        {"followup_message": _drain_followup(root, node, runtime, session)},
                        unread=unread,
                        **({} if first else {"waited_seconds": round(clock() - started, 1)}))
                if unread < 0:
                    return diagnostic(failure, participation=False)
            if needs_you["pending"]:
                published = observed_item_followup(session)
                if published is not None:
                    return published
            remaining = float(deadline_seconds) - (clock() - started)
            if remaining <= 0:
                if selection is not None and selection["held_total"]:
                    body, evidence, state = held_followup(plane, selection)
                    return emit("followup_rearm", body, reason_code=state, held=evidence,
                        waited_seconds=round(clock() - started, 1))
                return emit("followup_rearm",
                    {"followup_message": _rearm_followup(node, deadline_seconds)},
                    waited_seconds=round(clock() - started, 1))
            if first and registration["role"].casefold() == "cursor":
                # Only an authorized empty wait is a hold. Reuse the exact
                # actual-workspace identity; do not resolve payload fields or
                # extend the invocation by time already spent checking mail.
                hold.start(participant, consent, session, remaining, already_guarded=True)
                if hold.invocation_lease is not None and hold.start_row is not None:
                    hold.invocation_lease.bind_hold(hold.start_row)
                if execution_controller is not None and hold.start_row is not None:
                    try:
                        invocation = execution_controller._bind_cursor_start_already_guarded(hold.start_row)
                        if invocation is not None:
                            hold.execution_binding = (execution_controller, invocation)
                    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError, ValueError):
                        pass
            last = current
            return None, remaining

    changes = None
    if captured is not None:
        try:
            changes = _open_cursor_inputs_watch(captured[0], captured[2], stderr, workspace)
        except (FloatiError, OSError, ValueError):
            changes = None
    skip_unchanged = False
    try:
        while True:
            # Drain the previous cycle before this native read. Events that
            # arrive during the read stay queued so the following wait returns
            # and the next read sees them; draining here would swallow that race.
            dirty = True
            if changes is not None:
                dirty, _invalidate = changes.changed()
            remaining_now = float(deadline_seconds) - (clock() - started)
            condition = needs_you["condition"]
            if (condition is not None and initial_binding is not None and status == "completed"
                    and not needs_you["pending"] and remaining_now > 0):
                needs_you["pending"] = condition.tick(_needs_you_wall(), authorize=authorize_observer)
                if needs_you["pending"]:
                    dirty = True
            if hold.invocation_lease is not None and hold.invocation_lease.stop_reason() is not None:
                dirty = True
            if skip_unchanged and not dirty and remaining_now > 0 and changes is not None:
                # Safety timeout with the same identity and hint generation.
                # Block again. A changed ledger, a delivered event, or the
                # deadline falls through to the native read.
                woke = changes.wait(
                    needs_you_cap(min(remaining_now, 1.0) if hold.invocation_lease is not None else remaining_now),
                    wait_readable=wait_readable, sleep=sleep,
                    poll_seconds=float(poll_seconds))
                skip_unchanged = (not woke) and (
                    float(deadline_seconds) - (clock() - started) > 0)
                continue
            skip_unchanged = False
            try:
                result = retry_window.run(poll)
            except (WaiterAuthorityChanged, WaiterLockDeadline) as exc:
                if retry_window.finishing:
                    raise
                # Final publication stays guarded, but cannot wait or retry.
                terminal = exc
                retry_window.finish()
                result = poll()
            except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
                if retry_window.finishing:
                    raise
                terminal = exc
                retry_window.finish()
                result = poll()
            if not isinstance(result, tuple):
                return result
            _unused, remaining = result
            # Neither node nor epoch lock is held during the blocking wait.
            # The watch caps this at the 60-second safety re-check. Its own
            # writes can make the descriptor readable once; the next changed()
            # consumes that and the following wait blocks.
            if changes is not None:
                woke = changes.wait(needs_you_cap(min(remaining, 1.0) if hold.invocation_lease is not None else remaining),
                             wait_readable=wait_readable, sleep=sleep,
                             poll_seconds=float(poll_seconds))
                skip_unchanged = not woke
            else:
                sleep(needs_you_cap(min(float(poll_seconds), remaining)))
                skip_unchanged = False
    finally:
        if changes is not None:
            changes.close()


def format_cursor_stop_command(
    *, runtime: Path, root: Path, node: str, deadline_seconds: float
) -> str:
    launcher = Path(runtime) / "scripts" / "floati"
    return " ".join(
        shlex.quote(part)
        for part in (
            str(launcher),
            "wake",
            "wait",
            "--harness",
            "cursor",
            "--root",
            str(root),
            "--as",
            node,
            "--runtime",
            str(runtime),
            "--deadline-seconds",
            str(int(deadline_seconds)),
            "--hook-timeout-seconds",
            str(int(HOOK_TIMEOUT_SECONDS)),
            "--loop-limit",
            str(int(CURSOR_LOOP_LIMIT)),
        )
    )


def _owned_cursor_failure_hook(row: object, *, root: Path, node: str) -> bool:
    """Recognize a closed generated route without executing a previous runtime."""
    if not isinstance(row, dict) or not isinstance(row.get("command"), str):
        return False
    lexer = shlex.shlex(row["command"], posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    argv = []
    malformed = False
    try:
        for token in lexer:
            argv.append(token)
    except ValueError:
        malformed = True
    def refuse():
        raise ProtocolRefusal("hook_install_failure_hook_invalid",
            "existing Floati postToolUseFailure command has uncertain or malformed ownership",
            remedy="review the named project's postToolUseFailure hooks, resolve the malformed or foreign "
                "Floati route explicitly, then re-run native hook install with exact root, node and runtime")

    # An opening quote can swallow the executable and the rest of the command
    # into one unfinished word. This anchored lexical check only refuses that
    # uncertain executable position; no coordinates are recovered from it.
    first_word = argv[0] if argv else lexer.token
    if malformed and re.match(r"/[^\x00\r\n]*?/scripts/floati(?=[ \t]|$)", first_word):
        refuse()
    if not argv or Path(argv[0]).parts[-2:] != ("scripts", "floati") or argv[1:3] != ["wake", "wait"]:
        return False

    flags = ("--root", "--as", "--runtime", "--deadline-seconds",
        "--hook-timeout-seconds", "--loop-limit")
    if malformed or len(argv) != 17 or argv[1:5] != ["wake", "wait", "--harness", "cursor"] or tuple(argv[5::2]) != flags:
        refuse()
    embedded_runtime = Path(argv[10])
    if (not embedded_runtime.is_absolute() or str(embedded_runtime) != argv[10]
            or ".." in embedded_runtime.parts
            or argv[0] != str(embedded_runtime / "scripts" / "floati")
            or argv[6] != str(root) or argv[8] != node):
        refuse()
    try:
        deadline = int(argv[12])
    except ValueError:
        refuse()
    if (str(deadline) != argv[12] or deadline <= 0
            or deadline + DEFAULT_POLL_SECONDS + DEADLINE_MARGIN_SECONDS > HOOK_TIMEOUT_SECONDS
            or argv[14] != str(int(HOOK_TIMEOUT_SECONDS)) or argv[16] != str(CURSOR_LOOP_LIMIT)
            or set(row) != {"command", "timeout"}
            or type(row["timeout"]) is not int or row["timeout"] != 10):
        refuse()
    # A removed old launcher is still a configured callback. Complete native
    # command coordinates establish the migration candidate; existence cannot
    # silently turn it into a foreign handler and permit duplicate registration.
    return True


def install_cursor_hook(
    *,
    workspace: Path,
    root: Path,
    node: str,
    runtime: Path,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    plan=None,
) -> dict:
    """Write the seat project's ``.cursor/hooks.json``; node is never guessed."""

    workspace = Path(workspace)
    root = Path(root)
    runtime = Path(runtime)
    if not workspace.is_absolute() or workspace.is_symlink() or (plan is None and not workspace.is_dir()):
        raise ProtocolRefusal(
            "hook_install_workspace_invalid",
            "workspace must be an absolute nonsymlink directory",
            remedy="pass --workspace as an absolute project directory",
        )
    if not root.is_absolute():
        raise ProtocolRefusal(
            "hook_install_root_invalid",
            "root must be an absolute path",
            remedy="pass --root as an absolute fleet directory",
        )
    if not runtime.is_absolute():
        raise ProtocolRefusal(
            "hook_install_runtime_invalid",
            "runtime must be an absolute path",
            remedy="pass --runtime as the absolute floati checkout or install",
        )
    validate_identifier(node, field="node")
    launcher = runtime / "scripts" / "floati"
    if not launcher.is_file():
        raise ProtocolRefusal(
            "hook_install_runtime_invalid",
            "runtime has no scripts/floati launcher",
            remedy="pass --runtime pointing at a floati checkout or install",
        )
    if not (
        deadline_seconds > 0
        and deadline_seconds + DEFAULT_POLL_SECONDS + DEADLINE_MARGIN_SECONDS
        <= HOOK_TIMEOUT_SECONDS
    ):
        raise ProtocolRefusal(
            "cursor_wait_deadline_invalid",
            "deadline_seconds must be positive and leave the poll interval "
            "and the margin under the hook timeout",
            remedy="pass --deadline-seconds at most "
            f"{HOOK_TIMEOUT_SECONDS - DEFAULT_POLL_SECONDS - DEADLINE_MARGIN_SECONDS}",
        )
    command = format_cursor_stop_command(
        runtime=runtime, root=root, node=node, deadline_seconds=deadline_seconds
    )
    hook = {
        "command": command,
        "timeout": int(HOOK_TIMEOUT_SECONDS),
        # An explicit number, never null: see CURSOR_LOOP_LIMIT, where the
        # evidence is one live observation without a control. On that reading
        # a null is read as absent and Cursor applies its documented default
        # of 5, which is how a seat went dormant at loop_count 5.
        "loop_limit": CURSOR_LOOP_LIMIT,
    }
    hooks_dir = workspace / ".cursor"
    hooks_path = hooks_dir / "hooks.json"
    payload: dict = {"version": 1, "hooks": {"stop": []}}
    if hooks_path.is_file():
        try:
            existing = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProtocolRefusal(
                "hook_install_hooks_invalid",
                "existing .cursor/hooks.json is not JSON",
                remedy="fix or remove the project hooks.json and re-run install",
            ) from exc
        if not isinstance(existing, dict):
            raise ProtocolRefusal(
                "hook_install_hooks_invalid",
                "existing .cursor/hooks.json is not an object",
                remedy="fix .cursor/hooks.json to an object and re-run install",
            )
        payload = existing
        hooks = payload.get("hooks")
        if hooks is None:
            payload["hooks"] = {"stop": []}
        elif not isinstance(hooks, dict):
            raise ProtocolRefusal(
                "hook_install_hooks_invalid",
                "existing hooks object is invalid",
                remedy="fix .cursor/hooks.json hooks to an object and re-run install",
            )
        stops = payload["hooks"].setdefault("stop", [])
        if not isinstance(stops, list):
            raise ProtocolRefusal(
                "hook_install_hooks_invalid",
                "existing stop hooks are not a list",
                remedy="fix .cursor/hooks.json stop to an array and re-run install",
            )
        payload["hooks"]["stop"] = [
            row
            for row in stops
            if not (
                isinstance(row, dict)
                and "wake wait" in str(row.get("command", ""))
                and "--harness cursor" in str(row.get("command", ""))
            )
        ]
    payload.setdefault("hooks", {})
    if not isinstance(payload["hooks"], dict):
        raise ProtocolRefusal(
            "hook_install_hooks_invalid",
            "existing hooks object is invalid",
            remedy="fix .cursor/hooks.json hooks to an object and re-run install",
        )
    payload["hooks"].setdefault("stop", [])
    payload["hooks"]["stop"].append(hook)
    failures = payload["hooks"].get("postToolUseFailure", [])
    if not isinstance(failures, list):
        raise ProtocolRefusal("hook_install_hooks_invalid",
            "existing postToolUseFailure hooks are not a list",
            remedy="fix .cursor/hooks.json postToolUseFailure to an array and re-run install")
    owned = [index for index, row in enumerate(failures)
        if _owned_cursor_failure_hook(row, root=root, node=node)]
    if len(owned) > 1:
        raise ProtocolRefusal("hook_install_hooks_invalid",
            "existing Floati postToolUseFailure hooks are ambiguous",
            remedy="resolve duplicate owned postToolUseFailure hooks before re-running install")
    # The same governed entrypoint dispatches the native failure payload before
    # entering any Stop wait. Only that owned executable/route is replaced;
    # foreign handlers remain in their original order.
    payload["hooks"]["postToolUseFailure"] = [row for index, row in enumerate(failures)
        if index not in owned] + [{"command": command, "timeout": 10}]
    payload["version"] = 1
    encoded = json.dumps(payload, indent=2) + "\n"
    encoded_bytes = encoded.encode("utf-8")
    installed_path = _journal_path(root, node).parent / "installed-hook.json"
    receipt = (json.dumps({"hooks_path": str(hooks_path), "node": node,
                           "root": str(root), "workspace": str(workspace)},
                          sort_keys=True) + "\n").encode("utf-8")
    if plan is not None:
        from .cursor_user_hook import marker_bytes, marker_path, plan_cursor_user_hook

        plan.file(hooks_path, encoded_bytes)
        plan.file(installed_path, receipt)
        plan.file(marker_path(workspace), marker_bytes(workspace=workspace, root=root,
                                                      node=node, runtime=runtime))
        for path, content in plan_cursor_user_hook(timeout=int(HOOK_TIMEOUT_SECONDS),
                                                   loop_limit=CURSOR_LOOP_LIMIT,
                                                   read=plan.read).items():
            plan.file(path, content, replace=path.name == "hooks.json")
        return plan.commands(workspace, "cursor")
    hooks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = hooks_path.with_name(f".{hooks_path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(descriptor, encoded_bytes) != len(encoded_bytes):
            raise OSError("short hooks.json write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, hooks_path)
    from .cursor_user_hook import install_cursor_user_hook, write_cursor_seat_marker

    marker = write_cursor_seat_marker(
        workspace=workspace, root=root, node=node, runtime=runtime,
    )
    user_hooks = install_cursor_user_hook(
        timeout=int(HOOK_TIMEOUT_SECONDS), loop_limit=CURSOR_LOOP_LIMIT,
    )
    notice = SCOPE_NOTICE.format(workspace=workspace)
    from .lifecycle_prompts import install_seat_commands

    seat_commands = install_seat_commands(workspace, "cursor")
    if _is_readable_fleet_root(root):
        try:
            installed_path.parent.mkdir(parents=True, exist_ok=True)
            installed_path.write_bytes(receipt)
        except OSError:
            pass
    return {
        "schema_version": 0,
        "harness": "cursor",
        "workspace": str(workspace),
        "hooks_path": str(hooks_path),
        "command": command,
        "timeout": int(HOOK_TIMEOUT_SECONDS),
        "loop_limit": CURSOR_LOOP_LIMIT,
        "post_tool_failure_command": command,
        "post_tool_failure_timeout": 10,
        "root": str(root),
        "node": node,
        "scope_notice": notice,
        "marker_path": str(marker),
        "user_hooks_path": str(user_hooks),
        "seat_commands": seat_commands,
    }
