"""Cursor Stop waiter — the Codex waiter's twin for an IDE seat.

The Cursor ``stop`` hook calls ``floati wake wait --harness cursor`` with
an explicit root and node. Stdin is the harness payload. Five exits:

* ``error`` is never a person -> drain ``followup_message`` at once
* ``aborted`` gets ONE re-arm whose text carries a human escape
  (``if a person stopped this turn, reply exactly: stop``). A second
  consecutive abort in the same ``conversation_id``, or abort at the
  loop_limit, prints ``{}``. Consecutive-abort memory lives in this
  waiter's journal; every non-completed exit journals the payload keys.
* unread mail -> drain ``followup_message``
* the inbox cannot be read at all (root missing, not a fleet root, node
  unregistered, ledger unreadable) -> a ``followup_message`` naming the
  node, the root and the typed reason, never ``{}``
* deadline under the hook timeout -> one-line re-arm, never ``{}``
* a configuration-time refusal (bad stdin payload, bad deadline) -> a
  typed floati artifact on stdout, which Cursor reads as an empty body,
  AND one line on stderr so the person installing the hook sees the cause

Empty inbox is floati's typed silence and is not an exit; the waiter keeps
polling. Every exit is journaled under the explicit root WHEN that root is
a readable fleet root, and nowhere else: a waiter that created the root it
has just called missing would make its own second wake report a different,
wrong cause. A journal that cannot be written never suppresses the hook
body; it goes to stderr as one line instead.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, TextIO, Tuple

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .events import EventLog
from .root import FloatiRoot, validate_identifier


HOOK_TIMEOUT_SECONDS = 1800.0
DEFAULT_DEADLINE_SECONDS = 1500.0
DEFAULT_POLL_SECONDS = 3.0

# The wait loop can only re-test the clock after a sleep returns, so a
# deadline of D with a poll of P really returns at up to D + P; measured
# 6.40 s for a 5 s deadline. The margin buys the process start, the inbox
# read and the emit on top of that, so the bound states the guarantee the
# waiter exists to give: it returns BEFORE Cursor kills the hook.
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


def _fallback_session(node: str) -> str:
    """The session used when the stop payload carries no conversation id."""

    return f"cursor-stop-{node}"


def _drain_session(payload: Mapping[str, object], node: str) -> str:
    """The acting session the drain command must carry.

    Cursor's stop payload names ``conversation_id`` — "Stable ID of the
    conversation across many turns" — which is exactly what floati means by
    an acting harness session. When it is absent or is not a terminal-safe
    session id, fall back to a stable per-node value so the command still
    runs verbatim rather than handing the seat a refusal.
    """

    from .wake_control import validate_session_id

    candidate = payload.get("conversation_id")
    if isinstance(candidate, str):
        try:
            return validate_session_id(candidate)
        except ProtocolRefusal:
            pass
    return _fallback_session(node)


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


def _append_journal(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


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


def _default_peek(root: Path, node: str) -> Tuple[int, Optional[str]]:
    """Unread count, or -1 with the typed reason the inbox could not be read."""

    try:
        opened = FloatiRoot.open_direct_home(root)
        messages, _receipt = EventLog(opened).present(node)
        return len(messages), None
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
        return -1, _typed_reason(exc)


def run_cursor_stop_wait(
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
    peek_unread: Optional[Callable[[], int]] = None,
    ledger_mtime: Optional[Callable[[], float]] = None,
    now_utc: Optional[Callable[[], str]] = None,
    last_seen_root: Optional[Path] = None,
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
    seen_root = Path(last_seen_root) if last_seen_root is not None else Path.cwd()
    stamp = now_utc or (
        lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )

    def complain(line: str) -> None:
        """One line to the person watching the IDE, never an exception."""

        sink = stderr if stderr is not None else sys.stderr
        try:
            sink.write(line + "\n")
            sink.flush()
        except (OSError, ValueError):
            pass

    def emit(event: str, body: Optional[dict], **extra: object) -> int:
        record = {
            "ts": stamp(),
            "event": event,
            "node": node,
            "status": status,
            "loop_count": loop_count,
            "conversation_id": conversation_id,
            "last_seen_root": str(seen_root),
            **extra,
        }
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
                "change the reason the next wake reports. The row: "
                + json.dumps(record, sort_keys=True)
            )
        else:
            try:
                _append_journal(journal, record)
            except OSError as exc:
                complain(
                    f"[floati] {node}: could not write the cursor-wait journal at "
                    f"{journal}: {_typed_reason(exc)}; the hook body below still went out"
                )
        stdout.write(json.dumps({} if body is None else body) + "\n")
        stdout.flush()
        return 0

    def _noncompleted_extra() -> dict:
        return {"payload_keys": sorted(payload)}

    if status == "error":
        session = _drain_session(payload, node)
        return emit(
            "followup_after_error",
            {"followup_message": _drain_followup(root, node, runtime, session)},
            **_noncompleted_extra(),
        )

    if status == "aborted":
        if (
            isinstance(loop_limit, int)
            and not isinstance(loop_limit, bool)
            and isinstance(loop_count, int)
            and not isinstance(loop_count, bool)
            and loop_count >= loop_limit
        ):
            complain(
                f"[floati] {node}: Cursor aborted at loop_count {loop_count} of "
                f"loop_limit {loop_limit} — the auto-follow-up cap is reached and "
                "this seat is dormant until a human types in it"
            )
            return emit(
                "exit_empty_aborted",
                None,
                loop_limit=loop_limit,
                **_noncompleted_extra(),
            )
        previous = _last_event_for_conversation(journal, node, conversation_id)
        if previous == "followup_after_abort":
            return emit(
                "exit_empty_aborted_twice",
                None,
                **_noncompleted_extra(),
            )
        session = _drain_session(payload, node)
        return emit(
            "followup_after_abort",
            {
                "followup_message": _abort_followup(
                    root, node, runtime, session, status, loop_count
                )
            },
            **_noncompleted_extra(),
        )

    if peek_unread is None:
        peek = lambda: _default_peek(root, node)
    else:
        peek = lambda: (
            int(peek_unread()),
            "the injected peek reported the inbox unreadable",
        )
    ledger = root / "events.jsonl"

    def mtime() -> float:
        if ledger_mtime is not None:
            return float(ledger_mtime())
        try:
            return ledger.stat().st_mtime
        except OSError:
            return 0.0

    session = _drain_session(payload, node)
    unread, reason = peek()
    if unread > 0:
        return emit(
            "followup_mail_at_start",
            {"followup_message": _drain_followup(root, node, runtime, session)},
            unread=unread,
        )
    if unread < 0:
        # NOT {}. A seat pointed at a renamed, unmounted or never-initialised
        # root, or registered under a node name that was later retired, would
        # otherwise stop forever with exit 0 and nothing on any channel.
        return emit(
            "followup_inbox_unreadable",
            {
                "followup_message": _unreadable_followup(
                    root, node, reason or "the inbox read failed"
                )
            },
            reason=reason,
        )

    started = clock()
    last = mtime()
    while True:
        remaining = float(deadline_seconds) - (clock() - started)
        if remaining <= 0:
            break
        # Never sleep past the deadline: the bound above promises a return
        # before the hook timeout and a whole poll of overshoot breaks it.
        sleep(min(float(poll_seconds), remaining))
        current = mtime()
        if current != last:
            last = current
            unread, _reason = peek()
            if unread > 0:
                return emit(
                    "followup_mail",
                    {
                        "followup_message": _drain_followup(
                            root, node, runtime, session
                        )
                    },
                    unread=unread,
                    waited_seconds=round(clock() - started, 1),
                )
    return emit(
        "followup_rearm",
        {"followup_message": _rearm_followup(node, deadline_seconds)},
        waited_seconds=round(clock() - started, 1),
    )


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


def install_cursor_hook(
    *,
    workspace: Path,
    root: Path,
    node: str,
    runtime: Path,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> dict:
    """Write the seat project's ``.cursor/hooks.json``; node is never guessed."""

    workspace = Path(workspace)
    root = Path(root)
    runtime = Path(runtime)
    if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
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
    payload["version"] = 1
    encoded = json.dumps(payload, indent=2) + "\n"
    encoded_bytes = encoded.encode("utf-8")
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
    notice = SCOPE_NOTICE.format(workspace=workspace)
    if _is_readable_fleet_root(root):
        installed_path = _journal_path(root, node).parent / "installed-hook.json"
        try:
            installed_path.parent.mkdir(parents=True, exist_ok=True)
            installed_path.write_text(
                json.dumps(
                    {
                        "hooks_path": str(hooks_path),
                        "node": node,
                        "root": str(root),
                        "workspace": str(workspace),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
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
        "root": str(root),
        "node": node,
        "scope_notice": notice,
    }
