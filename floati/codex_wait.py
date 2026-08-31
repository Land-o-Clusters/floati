"""Bounded Codex Stop waiter for explicitly participating Floati workspaces."""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, TextIO

from .codex_wait_contract import (
    CodexWaitConsentLedger,
    CodexWaitReceiptLedger,
    CodexWaitSessionLedger,
    resolve_participant,
)
from .ids import uuid7_hex
from .wake_control import validate_session_id
from .wake_exit import WakeExitLedger
from .wake_hold import WakeAttemptLedger, WakeHoldController


BREAKER_WINDOW_SECONDS = 60.0
BREAKER_MAX_INVOCATIONS = 20


def _record_exit(
    participant: object,
    *,
    session_digest: str,
    reason_code: str,
    waited_seconds: int,
    invocation_id: str,
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
    except Exception:
        pass


def _breaker_tripped(root: object, node_id: str, *, now: float) -> bool:
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
                return True
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return True
    return len(hits) > BREAKER_MAX_INVOCATIONS


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
    except Exception:
        return 0
    try:
        session_id = validate_session_id(hook_payload.get("session_id"))
    except Exception:
        return 0
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    invocation_id = "codex-stop-" + uuid7_hex()
    try:
        session_authority = CodexWaitSessionLedger(participant.root).participate(
            participant.binding,
            consent,
            session_id,
        )
    except Exception:
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="integrity_failure", waited_seconds=0,
            invocation_id=invocation_id,
        )
        return 0
    if session_authority is None:
        return 0
    try:
        from .wake_daemon_adapters import record_codex_daemon_binding

        record_codex_daemon_binding(participant, session_id)
    except Exception:
        pass
    try:
        from .wake_control import is_session_paused

        if is_session_paused(participant.root, participant.binding.node_id, session_id):
            _record_exit(
                participant, session_digest=session_digest,
                reason_code="paused", waited_seconds=0,
                invocation_id=invocation_id,
            )
            return 0
    except Exception:
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="integrity_failure", waited_seconds=0,
            invocation_id=invocation_id,
        )
        return 0
    if _breaker_tripped(
        participant.root,
        participant.binding.node_id,
        now=wall_time(),
    ):
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="breaker", waited_seconds=0,
            invocation_id=invocation_id,
        )
        return 0
    deadline_seconds = consent.get("wait_deadline_seconds")
    if not isinstance(deadline_seconds, int) or isinstance(deadline_seconds, bool):
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="integrity_failure", waited_seconds=0,
            invocation_id=invocation_id,
        )
        return 0
    if not isinstance(poll_interval_seconds, (int, float)) or poll_interval_seconds <= 0:
        _record_exit(
            participant, session_digest=session_digest,
            reason_code="integrity_failure", waited_seconds=0,
            invocation_id=invocation_id,
        )
        return 0
    started = monotonic()
    deadline = started + deadline_seconds
    controller = WakeHoldController(participant.root)
    while True:
        try:
            current_authority = CodexWaitSessionLedger(
                participant.root
            ).participate(participant.binding, consent, session_id)
        except Exception:
            _record_exit(
                participant, session_digest=session_digest,
                reason_code="integrity_failure",
                waited_seconds=max(0, int(monotonic() - started)),
                invocation_id=invocation_id,
            )
            return 0
        if current_authority is None:
            _record_exit(
                participant, session_digest=session_digest,
                reason_code="not_claimant",
                waited_seconds=max(0, int(monotonic() - started)),
                invocation_id=invocation_id,
            )
            return 0
        invocation_key = "codex-stop-" + uuid7_hex()
        try:
            artifact = controller.evaluate(
                participant.binding.node_id,
                idempotency_key=invocation_key,
            )
        except Exception:
            _record_exit(
                participant, session_digest=session_digest,
                reason_code="integrity_failure",
                waited_seconds=max(0, int(monotonic() - started)),
                invocation_id=invocation_id,
            )
            return 0
        state = artifact.get("state")
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
                stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
                stdout.flush()
            except Exception:
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
                try:
                    stderr.write(f"wake evidence unavailable: {type(exc).__name__}\n")
                    stderr.flush()
                except Exception:
                    pass
            return 0
        now = monotonic()
        if now >= deadline:
            waited = max(0, int(now - started))
            _record_exit(
                participant, session_digest=session_digest,
                reason_code="exhausted", waited_seconds=waited,
                invocation_id=invocation_id,
            )
            try:
                CodexWaitReceiptLedger(participant.root).record_exhaustion(
                    node_id=participant.binding.node_id,
                    session_digest=session_digest,
                    waited_seconds=waited,
                    idempotency_key=invocation_key + "-exhaustion",
                )
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
            except Exception:
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
