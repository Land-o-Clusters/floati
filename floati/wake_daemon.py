"""Bounded, exact-session wake daemon engine with durable outcome testimony.

The daemon's supervisor logs are bounded: see ``maintain_supervisor_logs``
(LEDGER-1 a), called once per serve cycle.
"""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import math
import os
import resource
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, Mapping, Optional

from .bus_epoch import shared_epoch_operation
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot
from .records import WAKE_ATTEMPT_REFUSED_REASONS
from .registry import Registry
from .root import FloatiRoot
from .snapshot import _owned_epoch_archives
from .wake_control import WakeController, is_session_paused
from .wake_daemon_adapters import AdapterBinding, WakeAdapterResult
from .wake_daemon_contract import (
    DAEMON_KINDS,
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
    DaemonLifecycleLedger,
)
from .wake_daemon_roll import consent_relative, lifecycle_relative
from .wake_hold import WakeAttemptLedger, WakeHoldController


_RUNTIME_FIELDS = frozenset({
    "schema_version",
    "tenant_id",
    "node_id",
    "harness",
    "coordinate_digest",
    "daemon_instance_id",
    "activation_epoch",
    "cycle_index",
    "current_wake_key",
    "consecutive_refusals",
    "circuit_state",
    "next_poll_at",
    "current_backoff",
    "wake_timestamps",
    "session_digest",
    "last_state",
    "last_reason_code",
    "last_lifecycle_receipt_id",
    "bus_epoch_archive",
    "breaker_transitions",
    "awaiting_work",
    "cpu_seconds_last_cycle",
    "cpu_seconds_total",
    "cpu_seconds_window",
    "cpu_wall_window",
})
# Fields a runtime written by an older build may not carry. The shape check
# stays CLOSED - no unknown key is ever accepted - and these are read through
# a default rather than refused, so a daemon does not have to be re-consented
# to survive an upgrade.
_OPTIONAL_RUNTIME_FIELDS = frozenset({
    "bus_epoch_archive",
    "breaker_transitions",
    "awaiting_work",
    "cpu_seconds_last_cycle",
    "cpu_seconds_total",
    "cpu_seconds_window",
    "cpu_wall_window",
})
_REQUIRED_RUNTIME_FIELDS = _RUNTIME_FIELDS - _OPTIONAL_RUNTIME_FIELDS
_BREAKER_THRESHOLD = 3
_WAKE_BUDGET = 3
_WAKE_BUDGET_WINDOW_SECONDS = 300.0
CYCLE_EXCEPTION_MESSAGE_BOUND = 128
WAKE_BREAKER_REMEDY = (
    "rebind the wake daemon to a dedicated headless session - an interactive "
    "session with a large rollout may be unresumable - then rerun doctor"
)
_CPU_WINDOW = 32
_DEFAULT_CPU_BUDGET_SECONDS = 0.25


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _bounded_exception_message(detail: object, fallback: str) -> str:
    """One bounded, terminal-safe line of exception testimony.

    SKEW-2-F1: the cycle_exception receipt names what raised, never a
    traceback. Control, surrogate, and bidirectional-control characters
    are dropped (the same vocabulary the ledger refuses), the remainder
    is truncated to CYCLE_EXCEPTION_MESSAGE_BOUND, and an empty result
    falls back to the exception type name so the field is never empty.
    """

    text = "" if detail is None else str(detail)
    printable = "".join(
        character for character in text if character.isprintable()
    )
    bounded = printable[:CYCLE_EXCEPTION_MESSAGE_BOUND]
    return bounded or fallback


def breaker_status_for_node(root: object, node_id: str) -> Dict[str, object]:
    """Read the wake-daemon breaker from the durable runtime, never the notice.

    WD-R7 writes a one-shot notice at the closed-to-open crossing and never
    refreshes it, so the notice lags the runtime (NOTICE-LAG-1). Status
    surfaces the runtime. This function does not reset the circuit. A
    successful half-open probe closes it; a consent re-grant also
    re-initializes it.
    """

    from .wake_daemon_contract import (
        AdapterBindingStore,
        DaemonCoordinate,
        SUPPORTED_HARNESSES,
    )

    store = AdapterBindingStore(root)
    coordinates: list[Dict[str, object]] = []
    for harness in sorted(SUPPORTED_HARNESSES):
        try:
            coordinate = DaemonCoordinate(root, node_id, harness)
        except ProtocolRefusal:
            continue
        try:
            store.read(coordinate)
        except ProtocolRefusal as exc:
            if exc.code == "wake_daemon_binding_absent":
                continue
            raise
        runtime_path = (
            Path(root.path) / "state" / "wake-daemon" / "runtime" / f"{coordinate.digest}.json"
        )
        if runtime_path.is_symlink():
            coordinates.append(
                {
                    "harness": harness,
                    "state": "underivable",
                    "reason": "runtime_symlink",
                    "consecutive_refusals": None,
                    "last_trip_reason": None,
                    "current_backoff": None,
                }
            )
            continue
        if not runtime_path.is_file():
            coordinates.append(
                {
                    "harness": harness,
                    "state": "underivable",
                    "reason": "runtime_missing",
                    "consecutive_refusals": None,
                    "last_trip_reason": None,
                    "current_backoff": None,
                }
            )
            continue
        try:
            raw = runtime_path.read_bytes()
            if len(raw) > 65536:
                raise ValueError("oversized")
            runtime = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            coordinates.append(
                {
                    "harness": harness,
                    "state": "underivable",
                    "reason": "runtime_malformed",
                    "consecutive_refusals": None,
                    "last_trip_reason": None,
                    "current_backoff": None,
                }
            )
            continue
        circuit_state = runtime.get("circuit_state") if isinstance(runtime, dict) else None
        refusals = runtime.get("consecutive_refusals") if isinstance(runtime, dict) else None
        backoff = runtime.get("current_backoff") if isinstance(runtime, dict) else None
        if (
            not isinstance(runtime, dict)
            or circuit_state not in {"closed", "open"}
            or not isinstance(refusals, int)
            or isinstance(refusals, bool)
            or not isinstance(backoff, int)
            or isinstance(backoff, bool)
        ):
            coordinates.append(
                {
                    "harness": harness,
                    "state": "underivable",
                    "reason": "runtime_malformed",
                    "consecutive_refusals": None,
                    "last_trip_reason": None,
                    "current_backoff": None,
                }
            )
            continue
        coordinates.append(
            {
                "harness": harness,
                "state": (
                    "half_open_awaiting_work"
                    if circuit_state == "open" and runtime.get("awaiting_work") is True
                    else circuit_state
                ),
                "consecutive_refusals": refusals,
                "last_trip_reason": runtime.get("last_reason_code"),
                "current_backoff": backoff,
            }
        )
    return {
        "source": "runtime",
        "threshold": _BREAKER_THRESHOLD,
        "coordinates": coordinates,
    }


class DaemonOwner:
    """One nonblocking kernel lock for one root/node/harness coordinate."""

    def __init__(self, coordinate: DaemonCoordinate) -> None:
        self.coordinate = coordinate
        self.path = coordinate.root.resolve_relative(
            Path("state/wake-daemon/owners") / f"{coordinate.digest}.lock"
        )
        self._descriptor: Optional[int] = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            raise ProtocolRefusal(
                "wake_daemon_owner_unknown", "daemon owner is already acquired"
            )
        if self.path.is_symlink():
            raise ProtocolRefusal(
                "wake_daemon_owner_unknown", "daemon owner path is a symlink"
            )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            current = fcntl.fcntl(descriptor, fcntl.F_GETFD)
            fcntl.fcntl(descriptor, fcntl.F_SETFD, current | fcntl.FD_CLOEXEC)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(descriptor)
            raise ProtocolRefusal(
                "wake_daemon_owner_unknown",
                "another owner may hold this daemon coordinate",
            ) from exc
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "DaemonOwner":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


# WD-2 (P4): refusals no cycle can clear are terminal, not transient.
# Polling on is the relaunch bait - under KeepAlive.SuccessfulExit=false a
# non-zero exit is relaunched every ThrottleInterval seconds (measured:
# 36,722 activation_epoch_mismatch refusals in one seat's stderr over four
# days, 2026-09-10 dispatch §2.1). Each code writes ONE typed lifecycle
# receipt and serve() returns cleanly.
_TERMINAL_SERVE_REFUSALS = frozenset(
    {
        "wake_daemon_consent_absent",
        "wake_daemon_activation_epoch_mismatch",
        "unknown_node",
        "wake_daemon_binding_absent",
    }
)
# Receipt events stay inside the lifecycle vocabulary records.py already
# ships (floati/records.py:2158-2160: event/state are closed enums, ruled
# REUSED, never widened - bus msg-01a08d33c626728e988647ea4a2ee6c5).
# reason_code on this record kind is bounded-free, not closed
# (floati/records.py:2164-2166: any 1..128 string), so the ruled reason
# names below are valid without touching records.py.
_SERVE_TERMINAL_EVENT_CONSENT = ("revoked", "revoked")
_SERVE_TERMINAL_EVENT_DEFAULT = ("stopped", "stopped")


class _ServeTerminal(Exception):
    """Internal serve-loop signal: a terminal retirement is on disk."""


class WakeDaemon:
    """Evaluate and wake exactly one consented coordinate per bounded cycle."""

    def __init__(
        self,
        coordinate: DaemonCoordinate,
        adapter: object,
        *,
        daemon_instance_id: Optional[str] = None,
        tide_evaluator: Optional[object] = None,
    ) -> None:
        if not isinstance(coordinate, DaemonCoordinate):
            raise ProtocolRefusal(
                "wake_daemon_coordinate_invalid", "daemon requires one validated coordinate"
            )
        if getattr(adapter, "coordinate", None) != coordinate:
            raise ProtocolRefusal(
                "wake_daemon_adapter_coordinate_mismatch",
                "adapter belongs to another daemon coordinate",
            )
        self.coordinate = coordinate
        self.root = coordinate.root
        self.adapter = adapter
        self.consent = DaemonConsentLedger(self.root)
        self.lifecycle = DaemonLifecycleLedger(self.root)
        self.daemon_instance_id = daemon_instance_id or "daemon-" + uuid7_hex()
        if tide_evaluator is None:
            from .tide import TideEvaluator

            tide_evaluator = TideEvaluator(
                self.root,
                source_sha="f2b587634cfc6d6a52cc24bd02bfd978919c359b",
            )
        self.tide_evaluator = tide_evaluator
        self.runtime_path = self.root.resolve_relative(
            Path("state/wake-daemon/runtime") / f"{coordinate.digest}.json"
        )
        self.owner = DaemonOwner(coordinate)
        self._breaker_close_reason: Optional[str] = None
        self._hold_controller = WakeHoldController(self.root)
        self._cycle_cpu_origin: Optional[float] = None
        self._cycle_now: Optional[float] = None

    def wake_health(self, now: datetime) -> Dict[str, object]:
        """Project the same node-bound wake fact exposed by status and Doctor."""
        from .wake_health import WakeHealthProjection

        return WakeHealthProjection(self.root).fact(self.coordinate.node_id, now)

    def run_cycle(self, now: float) -> Dict[str, object]:
        current_time = self._time(now)
        consent = self.consent.require_active(self.coordinate)
        Registry(self.root).resolve_node_id(self.coordinate.node_id, field="node")
        binding = self._exact_binding()
        if (
            consent.get("adapter_version") != binding.adapter_version
            or consent.get("adapter_digest") != binding.adapter_digest
        ):
            raise ProtocolRefusal(
                "wake_daemon_adapter_digest_mismatch",
                "active consent and exact adapter binding disagree",
            )
        runtime = self._read_or_initialize(consent, binding)
        self._breaker_close_reason = None
        if current_time < float(runtime["next_poll_at"]):
            artifact = self._artifact(runtime)
            artifact["state"] = "backpressure"
            artifact["reason_code"] = "wake_daemon_poll_not_due"
            return artifact

        self._cycle_now = current_time
        self._cycle_cpu_origin = _cpu_seconds()

        if is_session_paused(
            self.root, self.coordinate.node_id, binding.session_id
        ):
            try:
                marker = WakeController(self.root).status(
                    self.coordinate.node_id, binding.session_id
                )
                if marker.get("state") != "paused":
                    raise IntegrityFailure(
                        "wake_marker_invalid", "pause marker did not project paused"
                    )
            except (IntegrityFailure, ProtocolRefusal):
                self._schedule_failure(runtime, consent, current_time)
                return self._transition(
                    runtime,
                    consent,
                    binding,
                    result_state="pause_unknown",
                    event="pause_unknown",
                    lifecycle_state="pause_unknown",
                    reason_code="wake_marker_invalid",
                )
            # Pause may still clear an open circuit (never a probe), but the
            # poll interval backs off like idle: a paused seat is cheap.
            if runtime["circuit_state"] == "open":
                self._breaker_close_reason = "operator_pause"
            runtime["consecutive_refusals"] = 0
            runtime["circuit_state"] = "closed"
            self._schedule_idle(runtime, consent, current_time)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="paused",
                event="paused",
                lifecycle_state="paused",
                reason_code=None,
            )

        timestamps = [
            float(value)
            for value in runtime["wake_timestamps"]
            if current_time - float(value) < _WAKE_BUDGET_WINDOW_SECONDS
        ]
        runtime["wake_timestamps"] = timestamps
        if len(timestamps) >= _WAKE_BUDGET:
            self._schedule_failure(runtime, consent, current_time)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="exhausted",
                event="exhausted",
                lifecycle_state="exhausted",
                reason_code="wake_daemon_budget_exhausted",
            )

        try:
            self.tide_evaluator.evaluate(self.coordinate.node_id, binding)
        except (ProtocolRefusal, IntegrityFailure) as exc:
            unknown = isinstance(exc, IntegrityFailure)
            self._schedule_failure(runtime, consent, current_time)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="adapter_unknown" if unknown else "refused",
                event="adapter_unknown" if unknown else "refused",
                lifecycle_state="unknown" if unknown else "refused",
                reason_code=exc.code,
            )
        if self.tide_evaluator.dispatch_held(self.coordinate.node_id):
            runtime["current_wake_key"] = None
            self._schedule_idle(runtime, consent, current_time)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="held",
                event="backpressure",
                lifecycle_state="backpressure",
                reason_code="tide_directive_hold",
            )

        if runtime["current_wake_key"] is None:
            runtime["current_wake_key"] = self._wake_key(runtime)
            self._write_runtime(runtime)
        controller = self._hold_controller
        decision = controller.evaluate(
            self.coordinate.node_id,
            worker_session_id=binding.session_id,
            idempotency_key=str(runtime["current_wake_key"]),
        )
        decision_state = decision.get("state")
        message_worker_session_id: Optional[str] = binding.session_id
        if decision_state != "fresh_work":
            unbound = controller.evaluate(
                self.coordinate.node_id,
                worker_session_id=None,
                idempotency_key=str(runtime["current_wake_key"]),
            )
            if unbound.get("state") == "fresh_work" or decision_state == "caught_up":
                decision = unbound
                decision_state = decision.get("state")
                message_worker_session_id = None
        if decision_state == "caught_up":
            runtime["current_wake_key"] = None
            if runtime["circuit_state"] == "open":
                # Am.1: half-open with no mail is not idle and is not a probe.
                # Hold half_open_awaiting_work until fresh work exists; the first
                # mail is the probe. Never wake a seat that has nothing to see.
                self._schedule_half_open_await(runtime, consent, current_time)
                return self._transition(
                    runtime,
                    consent,
                    binding,
                    result_state="half_open_awaiting_work",
                    event="backpressure",
                    lifecycle_state="backpressure",
                    reason_code="wake_daemon_half_open_awaiting_work",
                )
            self._schedule_idle(runtime, consent, current_time)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="idle",
                event="idle",
                lifecycle_state="idle",
                reason_code=None,
            )
        if decision_state == "held_only":
            runtime["current_wake_key"] = None
            self._schedule_idle(runtime, consent, current_time)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="held",
                event="backpressure",
                lifecycle_state="backpressure",
                reason_code="wake_daemon_work_already_held",
            )
        if decision_state != "fresh_work" or not decision.get("wake_required"):
            raise IntegrityFailure(
                "wake_daemon_decision_invalid", "wake decision has an unknown state"
            )
        messages = decision.get("fresh_messages")
        receipt = decision.get("receipt")
        if not isinstance(messages, list) or not messages or not isinstance(receipt, dict):
            raise IntegrityFailure(
                "wake_daemon_decision_invalid", "fresh decision lacks bounded testimony"
            )
        raw_item_ids = [row.get("id") for row in messages if isinstance(row, dict)]
        if len(raw_item_ids) != len(messages) or not all(isinstance(item, str) for item in raw_item_ids):
            raise IntegrityFailure(
                "wake_daemon_decision_invalid", "fresh decision message ids are malformed"
            )
        item_ids = [str(item) for item in raw_item_ids]
        envelopes = [
            {
                "id": str(row["id"]),
                "note": row["note"] if isinstance(row.get("note"), str) else "",
            }
            for row in messages
        ]
        existing = self._existing_attempt(
            runtime,
            binding,
            item_ids,
            receipt,
            message_worker_session_id,
        )
        if existing is not None:
            if existing["outcome"] in {"woke", "queued"}:
                runtime["wake_timestamps"] = timestamps + [current_time]
                runtime["current_wake_key"] = None
                self._schedule_success(runtime, consent, current_time)
                return self._transition(
                    runtime,
                    consent,
                    binding,
                    result_state=str(existing["outcome"]),
                    event="wake_attempt",
                    lifecycle_state="running" if existing["outcome"] == "woke" else "unknown",
                    reason_code=None,
                )
            reason_code = str(
                runtime.get("last_reason_code") or existing["reason_code"]
            )
            self._schedule_failure(runtime, consent, current_time)
            unknown = "unknown" in reason_code or "timeout" in reason_code
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="adapter_unknown" if unknown else "refused",
                event="adapter_unknown" if unknown else "refused",
                lifecycle_state="unknown" if unknown else "refused",
                reason_code=reason_code,
            )
        if runtime["last_state"] in {"wake_requested", "wake_evidence_unknown"}:
            self._schedule_failure(runtime, consent, current_time)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="wake_evidence_unknown",
                event="wake_evidence_unknown",
                lifecycle_state="unknown",
                reason_code="wake_evidence_unknown",
            )
        reason = (
            f"[floati] {len(item_ids)} new message(s) for "
            f"{self.coordinate.node_id}: " + ", ".join(item_ids)
        )
        runtime["last_state"] = "wake_requested"
        runtime["last_reason_code"] = "wake_evidence_pending"
        self._write_runtime(runtime)
        arm = getattr(self.adapter, "arm_timeout_forensics", None)
        if callable(arm):
            arm(self._attempt_key(runtime))
        result = self._request_wake(
            binding,
            reason,
            min(300, int(consent["max_poll_seconds"])),
            envelopes=envelopes,
        )
        if result.outcome in {"woke", "queued"}:
            attempt_key = self._attempt_key(runtime)
            try:
                durable = WakeAttemptLedger(self.root).record(
                    recipient=self.coordinate.node_id,
                    acting_session_id=binding.session_id,
                    item_ids=item_ids,
                    decision_receipt_id=str(receipt["id"]),
                    message_worker_session_id=message_worker_session_id,
                    idempotency_key=attempt_key,
                    outcome=result.outcome,
                )
                if durable.get("outcome") != result.outcome:
                    raise IntegrityFailure(
                        "wake_evidence_unknown", "durable attempt did not confirm adapter outcome"
                    )
            except Exception:
                self._schedule_failure(runtime, consent, current_time)
                return self._transition(
                    runtime,
                    consent,
                    binding,
                    result_state="wake_evidence_unknown",
                    event="wake_evidence_unknown",
                    lifecycle_state="unknown",
                    reason_code="wake_evidence_unknown",
                )
            # Accepted external requests consume budget even when execution is unknown.
            runtime["wake_timestamps"] = timestamps + [current_time]
            runtime["current_wake_key"] = None
            self._schedule_success(runtime, consent, current_time)
            self._first_wake_verdict(binding, result)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state=result.outcome,
                event="wake_attempt",
                lifecycle_state="running" if result.outcome == "woke" else "unknown",
                reason_code=None,
            )

        reason_code = result.reason_code or "wake_daemon_adapter_unknown"
        runtime["last_reason_code"] = reason_code
        self._write_runtime(runtime)
        self._record_refused_attempt(
            runtime,
            binding,
            item_ids,
            receipt,
            message_worker_session_id,
            reason_code,
        )
        self._schedule_failure(runtime, consent, current_time)
        unknown = result.outcome == "unknown"
        self._first_wake_verdict(binding, result)
        return self._transition(
            runtime,
            consent,
            binding,
            result_state="adapter_unknown" if unknown else "refused",
            event="adapter_unknown" if unknown else "refused",
            lifecycle_state="unknown" if unknown else "refused",
            reason_code=reason_code,
        )

    def _serve_refusal_record_path(self, code: str) -> Path:
        return self.root.resolve_relative(
            Path("state") / "wake-daemon" / "serve-refusal"
            / f"{self.coordinate.digest}.{code}.json"
        )

    def _hold_serve_refusal(self, exc: BaseException) -> None:
        """SERVE-PASS-1: a refusal the cycle holds quietly is recorded once.

        The swallow keeps the daemon alive, but a refusal nothing records
        is a silent failure. The first raise of a (coordinate, code)
        condition writes one typed record into the daemon's own state
        plane and the process then holds quietly for that condition --
        never per cycle. The write is best effort: when the state plane
        is unwritable too, the hold lives in process memory alone and
        nothing re-raises here.
        """

        code = str(getattr(exc, "code", type(exc).__name__))
        key = (self.coordinate.digest, code)
        if key in _SERVE_REFUSAL_HELD:
            return
        _SERVE_REFUSAL_HELD.add(key)
        record = {
            "schema_version": 0,
            "kind": "wake_daemon_serve_refusal",
            "coordinate_digest": self.coordinate.digest,
            "node_id": self.coordinate.node_id,
            "code": code,
            "detail": str(exc),
            "remedy": getattr(exc, "remedy", None),
            "recorded_at": _rotation_timestamp(),
        }
        try:
            path = self._serve_refusal_record_path(code)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _rearm_serve_refusals(self) -> None:
        """A cycle that maintained cleanly proved every held condition
        healed: the hold re-arms and the record goes with the condition
        it named, so a relapse is a second outage with its own record,
        never a replay of the first."""

        for key in [
            key for key in _SERVE_REFUSAL_HELD if key[0] == self.coordinate.digest
        ]:
            _SERVE_REFUSAL_HELD.discard(key)
            code = key[1]
            try:
                self._serve_refusal_record_path(code).unlink()
            except OSError:
                pass

    def serve(
        self,
        stop_requested: Callable[[], bool],
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        consent = self.consent.require_active(self.coordinate)
        serve_activation_epoch = int(consent["activation_epoch"])
        with self.owner:
            while not stop_requested():
                try:
                    try:
                        result = self.run_cycle(clock())
                        observed_epoch = result.get("activation_epoch")
                        if (
                            observed_epoch is not None
                            and int(observed_epoch) != serve_activation_epoch
                        ):
                            # WD-2 (c): a consent written under a NEW epoch
                            # belongs to another daemon activation. Adopting
                            # it silently would keep this process running
                            # for a coordinate it was never launched for.
                            raise ProtocolRefusal(
                                "wake_daemon_activation_epoch_mismatch",
                                "activation epoch moved under this daemon instance",
                            )
                    except Exception as exc:
                        result = self._recover_cycle_exception(
                            exc, clock, serve_activation_epoch
                        )
                except _ServeTerminal:
                    # WD-2 (P4): the retirement receipt is written; exiting
                    # cleanly is the point - under KeepAlive
                    # .SuccessfulExit=false a clean exit is final.
                    return
                try:
                    maintain_supervisor_logs(self.root, self.coordinate.digest)
                except (OSError, ProtocolRefusal) as exc:
                    self._hold_serve_refusal(exc)
                else:
                    self._rearm_serve_refusals()
                if stop_requested():
                    break
                delay = max(0.0, float(result["next_poll_at"]) - clock())
                sleep(delay)

    def _recover_cycle_exception(
        self,
        exc: BaseException,
        clock: Callable[[], float],
        serve_activation_epoch: Optional[int] = None,
    ) -> Dict[str, object]:
        """Record one typed lifecycle receipt and schedule the next poll.

        SKEW-2: a cycle fault is testimony, never silence. The daemon
        records one typed receipt naming the fault and keeps polling; the
        serve loop itself must survive whatever a cycle raises.

        WD-2 (P4): a refusal no cycle can clear is not a cycle fault. It
        writes one typed retirement receipt and ends serve() cleanly, so
        non-zero stays reserved for transient faults and the supervisor's
        KeepAlive never turns a permanent refusal into a crash loop.
        """

        if isinstance(exc, ProtocolRefusal) and exc.code in _TERMINAL_SERVE_REFUSALS:
            # FQ-9 Am.1: the split layout keeps the consent chain in its
            # own plane, so on a root that has rolled, the lifecycle path
            # alone is silent about consent - read both planes, the
            # consent plane first and the lifecycle file after it.
            #
            # Am.2 corrects what this comment used to claim. The rows are
            # concatenated in PLANE ORDER and each kind's last row is
            # taken from that concatenation - it is not a merge by
            # timestamp, so "newest wins" was never what the code did. It
            # decides nothing today because the two files hold disjoint
            # kinds once a root has migrated: the carry copies consent
            # rows into the plane and the mixed file goes whole into the
            # archive, so the fresh lifecycle file has no consent row to
            # contend with. A future edit that lets both files hold one
            # kind has to pick an order deliberately, and this is not it.
            ledger_rows: list = []
            node_planes = [lifecycle_relative(self.coordinate.node_id)]
            consent_plane = consent_relative(self.coordinate.node_id)
            if (self.root.path / consent_plane).is_file():
                node_planes.insert(0, consent_plane)
            for plane in node_planes:
                try:
                    ledger_rows.extend(
                        read_records_snapshot(
                            self.root,
                            plane,
                            allowed_kinds=DAEMON_KINDS,
                        )
                    )
                except (IntegrityFailure, ProtocolRefusal, OSError):
                    continue
            consent_rows = [
                row
                for row in ledger_rows
                if row.get("kind") == "wake_daemon_consent_receipt"
                and row.get("coordinate_digest") == self.coordinate.digest
            ]
            lifecycle_rows = [
                row
                for row in ledger_rows
                if row.get("kind") == "wake_daemon_lifecycle_receipt"
                and row.get("coordinate_digest") == self.coordinate.digest
            ]
            last_consent = consent_rows[-1] if consent_rows else None
            last_lifecycle = lifecycle_rows[-1] if lifecycle_rows else None
            reason_code = exc.code
            event, state = _SERVE_TERMINAL_EVENT_DEFAULT
            if exc.code == "wake_daemon_consent_absent":
                # Revocation is the one absent shape with its own name: the
                # last consent row for this coordinate tells revoked from
                # a ledger that never held one.
                if last_consent is not None and last_consent.get("state") == "revoked":
                    reason_code = "wake_daemon_consent_revoked"
                    event, state = _SERVE_TERMINAL_EVENT_CONSENT
            elif exc.code == "unknown_node":
                reason_code = "wake_daemon_registry_inactive"
            elif exc.code == "wake_daemon_binding_absent":
                reason_code = "wake_daemon_binding_gone"
            elif exc.code == "wake_daemon_activation_epoch_mismatch":
                reason_code = "wake_daemon_epoch_moved"
            try:
                active_consent: Optional[Mapping[str, object]] = (
                    self.consent.require_active(self.coordinate)
                )
            except (ProtocolRefusal, IntegrityFailure):
                active_consent = None
            try:
                runtime = self.read_runtime()
            except (ProtocolRefusal, IntegrityFailure):
                runtime = None
            activation_epoch = next(
                (
                    int(value)
                    for value in (
                        serve_activation_epoch,
                        None if runtime is None else runtime.get("activation_epoch"),
                        None
                        if active_consent is None
                        else active_consent.get("activation_epoch"),
                        None if last_consent is None else last_consent.get("activation_epoch"),
                    )
                    if value is not None
                ),
                None,
            )
            adapter_digest = next(
                (
                    str(value)
                    for value in (
                        None
                        if active_consent is None
                        else active_consent.get("adapter_digest"),
                        None if last_consent is None else last_consent.get("adapter_digest"),
                        None
                        if last_lifecycle is None
                        else last_lifecycle.get("adapter_digest"),
                    )
                    if value
                ),
                None,
            )
            predecessor = next(
                (
                    str(value)
                    for value in (
                        None
                        if runtime is None
                        else runtime.get("last_lifecycle_receipt_id"),
                        None if last_lifecycle is None else last_lifecycle.get("id"),
                    )
                    if value
                ),
                None,
            )
            receipt = None
            if activation_epoch is not None and adapter_digest is not None:
                instance = (
                    str(runtime["daemon_instance_id"])
                    if runtime is not None and runtime.get("daemon_instance_id")
                    else self.daemon_instance_id
                )
                try:
                    receipt = self.lifecycle.record(
                        self.coordinate,
                        daemon_instance_id=instance,
                        activation_epoch=activation_epoch,
                        event=event,
                        state=state,
                        reason_code=reason_code,
                        adapter_digest=adapter_digest,
                        plist_digest=None,
                        session_digest=(
                            None if runtime is None else runtime.get("session_digest")
                        ),
                        predecessor_receipt_id=predecessor,
                        idempotency_key=f"{instance}-serve-terminal-{reason_code}"[
                            :128
                        ],
                    )
                except (ProtocolRefusal, IntegrityFailure) as receipt_failure:
                    # The retirement must stay clean even when its receipt
                    # cannot be written; the held serve-refusal record is
                    # the typed trace of why.
                    self._hold_serve_refusal(receipt_failure)
            else:
                self._hold_serve_refusal(exc)
            if runtime is not None and receipt is not None:
                try:
                    runtime["last_lifecycle_receipt_id"] = receipt["id"]
                    runtime["last_state"] = state
                    runtime["last_reason_code"] = reason_code
                    self._write_runtime(runtime)
                except (ProtocolRefusal, IntegrityFailure, OSError, ValueError, KeyError):
                    pass
            raise _ServeTerminal(exc)

        current_time = self._time(clock())
        consent = self.consent.require_active(self.coordinate)
        binding = self._exact_binding()
        runtime = self._read_or_initialize(consent, binding)
        self._schedule_failure(runtime, consent, current_time)
        if isinstance(exc, (IntegrityFailure, ProtocolRefusal)):
            reason_code = exc.code
            detail: object = exc.detail
        else:
            reason_code = "wake_daemon_cycle_exception"
            # SKEW-2-F1 Am.1: str(exc) runs user-controlled __str__ and can
            # itself raise; a fault inside the handler would kill serve(),
            # the silence this recovery exists to prevent. None falls back
            # to the type name in the bounded message.
            try:
                detail = str(exc)
            except Exception:
                detail = None
        # SKEW-2-F1 Am.2: the type name is user-chosen too (type("x\x01", …))
        # and flows through the same field validator, so it takes the same
        # bounded-printable filter as the message.
        exception_type = _bounded_exception_message(
            type(exc).__name__, "unprintable_exception_type"
        )
        try:
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="adapter_unknown",
                event="cycle_exception",
                lifecycle_state="unknown",
                reason_code=reason_code,
                exception_type=exception_type,
                exception_message=_bounded_exception_message(detail, exception_type),
            )
        except Exception as receipt_failure:
            # The recovery path may never raise. When the full receipt
            # refuses, write a minimal typed receipt naming the refusal;
            # when even that fails, continue the loop with the runtime's
            # scheduled backoff — silence with a schedule beats silence.
            fallback_reason = (
                receipt_failure.code
                if isinstance(receipt_failure, ProtocolRefusal)
                else "wake_daemon_cycle_exception"
            )
            try:
                lifecycle = self.lifecycle.record(
                    self.coordinate,
                    daemon_instance_id=str(runtime["daemon_instance_id"]),
                    activation_epoch=int(consent["activation_epoch"]),
                    event="cycle_exception",
                    state="unknown",
                    reason_code=fallback_reason,
                    adapter_digest=binding.adapter_digest,
                    plist_digest=None,
                    session_digest=binding.session_digest,
                    predecessor_receipt_id=runtime["last_lifecycle_receipt_id"],
                    idempotency_key=(
                        f"{runtime['daemon_instance_id']}"
                        f"-{runtime['cycle_index']}-cycle_exception"
                    ),
                )
                runtime["last_lifecycle_receipt_id"] = lifecycle["id"]
                self._write_runtime(runtime)
            except Exception:
                pass
            return self._artifact(runtime)

    def read_runtime(self) -> Dict[str, object]:
        if self.runtime_path.is_symlink() or not self.runtime_path.is_file():
            raise ProtocolRefusal(
                "wake_daemon_runtime_absent", "daemon runtime state is absent"
            )
        try:
            raw = self.runtime_path.read_bytes()
            if len(raw) > 65536:
                raise ValueError("oversized")
            value = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise IntegrityFailure(
                "wake_daemon_runtime_invalid", "daemon runtime state is unreadable"
            ) from exc
        return self._validate_runtime(value)

    @shared_epoch_operation
    def _read_or_initialize(
        self, consent: Mapping[str, object], binding: AdapterBinding
    ) -> Dict[str, object]:
        current_epoch = self._epoch_archive_token()
        try:
            current = self.read_runtime()
        except ProtocolRefusal as exc:
            if exc.code != "wake_daemon_runtime_absent":
                raise
            current = self._initial_runtime(consent, binding, current_epoch)
        if current["bus_epoch_archive"] != current_epoch:
            current["bus_epoch_archive"] = current_epoch
            current["next_poll_at"] = 0.0
        if current["activation_epoch"] != consent["activation_epoch"]:
            current = self._initial_runtime(
                consent,
                binding,
                current_epoch,
                breaker_transitions=int(current["breaker_transitions"]),
            )
        if current["session_digest"] not in {None, binding.session_digest}:
            current = self._initial_runtime(
                consent,
                binding,
                current_epoch,
                breaker_transitions=int(current["breaker_transitions"]),
            )
        current["session_digest"] = binding.session_digest
        return current

    def _initial_runtime(
        self,
        consent: Mapping[str, object],
        binding: AdapterBinding,
        current_epoch: Optional[str],
        *,
        breaker_transitions: int = 0,
    ) -> Dict[str, object]:
        """Reactivation resets the cycle counters and CARRIES THE BREAKER'S.

        Everything else here is per-activation state and must start over.
        ``breaker_transitions`` is not: it names WHICH outage this daemon is
        on, and an outage that heals and returns is a second outage. Reset it
        and the announcement key repeats, and the bus - correctly - treats the
        relapse as a replay of the first.
        """
        return {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "node_id": self.coordinate.node_id,
            "harness": self.coordinate.harness,
            "coordinate_digest": self.coordinate.digest,
            "daemon_instance_id": self.daemon_instance_id,
            "activation_epoch": int(consent["activation_epoch"]),
            "cycle_index": 0,
            "breaker_transitions": int(breaker_transitions),
            "current_wake_key": None,
            "consecutive_refusals": 0,
            "circuit_state": "closed",
            "awaiting_work": False,
            "next_poll_at": 0.0,
            "current_backoff": int(consent["min_poll_seconds"]),
            "wake_timestamps": [],
            "session_digest": binding.session_digest,
            "last_state": "inactive",
            "last_reason_code": None,
            "last_lifecycle_receipt_id": None,
            "bus_epoch_archive": current_epoch,
            "cpu_seconds_last_cycle": 0.0,
            "cpu_seconds_total": 0.0,
            "cpu_seconds_window": [],
            "cpu_wall_window": [],
        }

    def _validate_runtime(self, value: object) -> Dict[str, object]:
        if not isinstance(value, dict) or not (
            _REQUIRED_RUNTIME_FIELDS <= set(value) <= _RUNTIME_FIELDS
        ):
            raise IntegrityFailure(
                "wake_daemon_runtime_invalid", "daemon runtime state has an open shape"
            )
        value = dict(value)
        value.setdefault("bus_epoch_archive", None)
        value.setdefault("breaker_transitions", 0)
        value.setdefault("awaiting_work", False)
        value.setdefault("cpu_seconds_last_cycle", 0.0)
        value.setdefault("cpu_seconds_total", 0.0)
        value.setdefault("cpu_seconds_window", [])
        value.setdefault("cpu_wall_window", [])
        if (
            value.get("schema_version") != 0
            or value.get("tenant_id") != self.root.tenant_id
            or value.get("node_id") != self.coordinate.node_id
            or value.get("harness") != self.coordinate.harness
            or value.get("coordinate_digest") != self.coordinate.digest
            or value.get("circuit_state") not in {"closed", "open"}
            or value.get("awaiting_work") not in {True, False}
            or not isinstance(value.get("wake_timestamps"), list)
            or (
                value.get("bus_epoch_archive") is not None
                and (
                    not isinstance(value.get("bus_epoch_archive"), str)
                    or Path(str(value["bus_epoch_archive"])).name
                    != value["bus_epoch_archive"]
                    or not str(value["bus_epoch_archive"]).startswith("archive-")
                )
            )
        ):
            raise IntegrityFailure(
                "wake_daemon_runtime_invalid", "daemon runtime identity is invalid"
            )
        for field in (
            "activation_epoch",
            "cycle_index",
            "consecutive_refusals",
            "current_backoff",
            "breaker_transitions",
        ):
            item = value.get(field)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise IntegrityFailure(
                    "wake_daemon_runtime_invalid", f"daemon runtime {field} is invalid"
                )
        for item in value["wake_timestamps"]:
            self._time(item)
        self._time(value.get("next_poll_at"))
        for field in ("cpu_seconds_last_cycle", "cpu_seconds_total"):
            item = value.get(field)
            if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
                raise IntegrityFailure(
                    "wake_daemon_runtime_invalid", f"daemon runtime {field} is invalid"
                )
            value[field] = float(item)
        cpu_window = value.get("cpu_seconds_window")
        wall_window = value.get("cpu_wall_window")
        if not isinstance(cpu_window, list) or not isinstance(wall_window, list):
            raise IntegrityFailure(
                "wake_daemon_runtime_invalid", "daemon runtime cpu windows are invalid"
            )
        if len(cpu_window) != len(wall_window):
            raise IntegrityFailure(
                "wake_daemon_runtime_invalid", "daemon runtime cpu windows disagree"
            )
        cleaned_cpu: list[float] = []
        cleaned_wall: list[float] = []
        for cpu_item, wall_item in zip(cpu_window, wall_window):
            if (
                isinstance(cpu_item, bool)
                or isinstance(wall_item, bool)
                or not isinstance(cpu_item, (int, float))
                or not isinstance(wall_item, (int, float))
                or cpu_item < 0
                or wall_item < 0
            ):
                raise IntegrityFailure(
                    "wake_daemon_runtime_invalid", "daemon runtime cpu sample is invalid"
                )
            cleaned_cpu.append(float(cpu_item))
            cleaned_wall.append(float(wall_item))
        value["cpu_seconds_window"] = cleaned_cpu
        value["cpu_wall_window"] = cleaned_wall
        return value

    def _epoch_archive_token(self) -> Optional[str]:
        """Name the live receipt's owned predecessor without touching its members."""

        if not self.root.resolve_relative("events.jsonl").exists():
            return None
        archives = _owned_epoch_archives(self.root)
        return archives[0].name if archives else None

    def _write_runtime(self, runtime: Mapping[str, object]) -> None:
        checked = self._validate_runtime(dict(runtime))
        encoded = (
            json.dumps(checked, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.runtime_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.runtime_path.with_name(
            f".{self.runtime_path.name}.{os.getpid()}.{uuid7_hex()}.tmp"
        )
        descriptor = -1
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short runtime write")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.runtime_path)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise ProtocolRefusal(
                "wake_daemon_runtime_unavailable",
                "daemon runtime state could not be committed",
            ) from exc

    def _transition(
        self,
        runtime: Dict[str, object],
        consent: Mapping[str, object],
        binding: AdapterBinding,
        *,
        result_state: str,
        event: str,
        lifecycle_state: str,
        reason_code: Optional[str],
        exception_type: Optional[str] = None,
        exception_message: Optional[str] = None,
    ) -> Dict[str, object]:
        reason_code = self._apply_cycle_cpu(runtime, consent, reason_code)
        runtime["cycle_index"] = int(runtime["cycle_index"]) + 1
        runtime["last_state"] = result_state
        runtime["last_reason_code"] = reason_code
        lifecycle = self.lifecycle.record(
            self.coordinate,
            daemon_instance_id=str(runtime["daemon_instance_id"]),
            activation_epoch=int(consent["activation_epoch"]),
            event=event,
            state=lifecycle_state,
            reason_code=reason_code,
            adapter_digest=binding.adapter_digest,
            plist_digest=None,
            session_digest=binding.session_digest,
            predecessor_receipt_id=runtime["last_lifecycle_receipt_id"],
            idempotency_key=(
                f"{runtime['daemon_instance_id']}-{runtime['cycle_index']}-{event}"
            ),
            exception_type=exception_type,
            exception_message=exception_message,
        )
        runtime["last_lifecycle_receipt_id"] = lifecycle["id"]
        self._write_runtime(runtime)
        self._maintain_breaker_notice(runtime, binding)
        return self._artifact(runtime)

    def _apply_cycle_cpu(
        self,
        runtime: Dict[str, object],
        consent: Mapping[str, object],
        reason_code: Optional[str],
    ) -> Optional[str]:
        origin = self._cycle_cpu_origin
        self._cycle_cpu_origin = None
        if origin is None:
            return reason_code
        delta = max(0.0, _cpu_seconds() - origin)
        runtime["cpu_seconds_last_cycle"] = delta
        runtime["cpu_seconds_total"] = float(runtime.get("cpu_seconds_total") or 0.0) + delta
        budget = consent.get("max_cpu_seconds_per_cycle", _DEFAULT_CPU_BUDGET_SECONDS)
        try:
            budget_value = float(budget)
        except (TypeError, ValueError):
            budget_value = _DEFAULT_CPU_BUDGET_SECONDS
        if budget_value <= 0:
            budget_value = _DEFAULT_CPU_BUDGET_SECONDS
        if delta > budget_value:
            maximum = int(consent["max_poll_seconds"])
            now = float(self._cycle_now if self._cycle_now is not None else 0.0)
            runtime["current_backoff"] = maximum
            runtime["next_poll_at"] = now + maximum
            reason_code = "wake_daemon_cycle_over_budget"
        cpu_window = list(runtime.get("cpu_seconds_window") or [])
        wall_window = list(runtime.get("cpu_wall_window") or [])
        cpu_window.append(delta)
        wall_window.append(float(runtime["current_backoff"]))
        runtime["cpu_seconds_window"] = cpu_window[-_CPU_WINDOW:]
        runtime["cpu_wall_window"] = wall_window[-_CPU_WINDOW:]
        return reason_code

    def _first_wake_verdict(self, binding: AdapterBinding, result: WakeAdapterResult) -> None:
        """WD-R5c-F1: flip unproven→suspect only on typed bound exhaustion
        (`wake_daemon_adapter_timeout`), never on the unknown outcome class.
        A subsequent woke clears resume_suspect to resume_proven. A verdict
        that cannot be recorded must not kill the cycle."""
        state = binding.resume_state
        if result.outcome == "woke" and state in {"resume_unproven", "resume_suspect"}:
            flipped = "resume_proven"
        elif (
            result.reason_code == "wake_daemon_adapter_timeout"
            and state == "resume_unproven"
        ):
            flipped = "resume_suspect"
        else:
            return
        try:
            AdapterBindingStore(self.root).write(
                self.coordinate,
                session_id=binding.session_id,
                workspace=binding.workspace,
                executable=binding.executable,
                adapter_version=binding.adapter_version,
                adapter_digest=binding.adapter_digest,
                binding_epoch=binding.binding_epoch,
                resume_state=flipped,
            )
        except (ProtocolRefusal, IntegrityFailure, OSError):
            return

    def _maintain_breaker_notice(
        self, runtime: Mapping[str, object], binding: AdapterBinding
    ) -> None:
        """WD-R7: the breaker says so ONCE, locally. One notice file per
        coordinate, present for exactly as long as the circuit stays open:
        written when open and absent - presence, never a count standing in
        for presence - so a lost notice re-derives from the durable runtime
        and a restart cannot leave it gone. Cleared when the circuit closes
        again. Local only - no network, no telemetry, ever."""

        notice_relative = Path("state/wake-daemon/notices") / f"{self.coordinate.digest}.json"
        notice_path = self.root.resolve_relative(notice_relative)
        if runtime["circuit_state"] == "closed":
            if self._breaker_close_reason in {"probe", "operator_pause"}:
                self._announce_breaker_closed(
                    runtime,
                    notice_relative.as_posix(),
                    close_reason=str(self._breaker_close_reason),
                )
            notice_path.unlink(missing_ok=True)
            return
        if runtime["circuit_state"] != "open" or notice_path.exists():
            return
        notice = {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "node_id": self.coordinate.node_id,
            "harness": self.coordinate.harness,
            "coordinate_digest": self.coordinate.digest,
            "session_id": binding.session_id,
            "session_digest": binding.session_digest,
            "consecutive_refusals": int(runtime["consecutive_refusals"]),
            "current_backoff": int(runtime["current_backoff"]),
            "last_reason_code": runtime["last_reason_code"],
            "cycle_index": int(runtime["cycle_index"]),
            "half_open_awaiting_work": bool(runtime.get("awaiting_work")),
            "remedy": WAKE_BREAKER_REMEDY,
        }
        notice["announcement"] = self._announce_breaker(
            runtime, notice_relative.as_posix()
        )
        encoded = (
            json.dumps(notice, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        notice_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = notice_path.with_name(
            f".{notice_path.name}.{os.getpid()}.{uuid7_hex()}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short breaker notice write")
            os.fsync(descriptor)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
        os.replace(temporary, notice_path)

    def _announce_breaker(
        self, runtime: Mapping[str, object], notice_relative: str
    ) -> Dict[str, object]:
        """WAKE-NOTICE-1: the crossing into an open circuit also reaches the bus.

        WD-R7's notice says so once, LOCALLY, and that is where it stopped: on
        2026-09-05 grok/cursor had been open for 381 refusals and six days and
        the fleet's architect had no way to learn it except by running doctor.
        This posts ONE envelope to the root's one declared architect naming the
        reason code and the notice path.

        Once per TRANSITION, never per cycle: the caller only reaches this on
        the crossing that writes the notice, so the 378 backpressure cycles
        one live seat has run since produce nothing.

        Am.1 - THE KEY IS THE TRANSITION COUNT, NEVER cycle_index. cycle_index
        counts cycles and is reset to 0 by every consent re-grant, while the
        breaker trips at a fixed threshold, so a genuine close-then-reopen
        relapse composed a byte-identical key and the bus deduped the second
        outage as a replay of the first: measured, two outages, one envelope,
        and a notice naming the healed outage's message id.
        ``breaker_transitions`` is incremented at the one closed-to-open flip
        in this engine and carried across reactivation, so it is monotonic per
        outage and idempotent within one.

        Fail-soft, and never silently: this runs inside every daemon on the
        machine, so a root that cannot take the envelope must back off rather
        than kill a fleet - and the reason it could not is written into the
        same notice, so the operator sees the announcement's fate beside the
        breaker's.
        """

        try:
            from .admin_registry import RegistryAdminBackend
            from .events import EventLog

            architect = str(
                RegistryAdminBackend(self.root).current_architect()["node_id"]
            )
            binding = self._exact_binding()
            note = (
                f"WAKE BREAKER OPEN - {self.coordinate.node_id}/"
                f"{self.coordinate.harness} has stopped waking its seat: "
                f"{runtime['last_reason_code']} after "
                f"{int(runtime['consecutive_refusals'])} consecutive refusals, "
                f"backoff {int(runtime['current_backoff'])}s. "
                f"Notice: {notice_relative}. Remedy: {WAKE_BREAKER_REMEDY}"
            )
            receipt = EventLog(self.root).send(
                self.coordinate.node_id,
                architect,
                Path(binding.workspace).name,
                self.coordinate.digest,
                notice_relative,
                note,
                idempotency_key=(
                    f"wake-breaker-{self.coordinate.digest}-"
                    f"{int(runtime['breaker_transitions'])}"
                ),
            )
            envelope = receipt.get("message", receipt)
            return {
                "recipient": architect,
                "message_id": str(envelope["id"]),
            }
        except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
            return {"refused": getattr(exc, "code", type(exc).__name__)}

    def _announce_breaker_closed(
        self,
        runtime: Mapping[str, object],
        notice_relative: str,
        *,
        close_reason: str = "probe",
    ) -> Dict[str, object]:
        """WAKE-NOTICE-1: closing an open circuit reaches the bus.

        Keyed on the same transition count as the open announcement, with a
        closed suffix, so recovery of outage N cannot replay as outage N's
        opening and cannot collide with a later relapse.

        Am.1: only a real half-open probe may say "probe succeeded". An
        operator pause that clears the circuit says cleared_by_operator_pause.
        """

        try:
            from .admin_registry import RegistryAdminBackend
            from .events import EventLog

            architect = str(
                RegistryAdminBackend(self.root).current_architect()["node_id"]
            )
            binding = self._exact_binding()
            if close_reason == "operator_pause":
                how = "cleared_by_operator_pause"
            else:
                how = "probe succeeded"
            note = (
                f"WAKE BREAKER CLOSED - {self.coordinate.node_id}/"
                f"{self.coordinate.harness} {how} after "
                f"{int(runtime.get('breaker_transitions', 0))} breaker "
                f"transition(s); refusals reset, notice cleared. "
                f"Notice: {notice_relative}."
            )
            receipt = EventLog(self.root).send(
                self.coordinate.node_id,
                architect,
                Path(binding.workspace).name,
                self.coordinate.digest,
                notice_relative,
                note,
                idempotency_key=(
                    f"wake-breaker-{self.coordinate.digest}-"
                    f"{int(runtime['breaker_transitions'])}-closed"
                ),
            )
            envelope = receipt.get("message", receipt)
            return {
                "recipient": architect,
                "message_id": str(envelope["id"]),
            }
        except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
            return {"refused": getattr(exc, "code", type(exc).__name__)}

    @staticmethod
    def _artifact(runtime: Mapping[str, object]) -> Dict[str, object]:
        artifact = dict(runtime)
        artifact["state"] = runtime["last_state"]
        artifact["reason_code"] = runtime["last_reason_code"]
        return artifact

    def _record_refused_attempt(
        self,
        runtime: Mapping[str, object],
        binding: AdapterBinding,
        item_ids: list[str],
        receipt: Mapping[str, object],
        message_worker_session_id: Optional[str],
        reason_code: str,
    ) -> None:
        recorded = reason_code if reason_code in WAKE_ATTEMPT_REFUSED_REASONS else "wake_prompt_failed"
        try:
            WakeAttemptLedger(self.root).record(
                recipient=self.coordinate.node_id,
                acting_session_id=binding.session_id,
                item_ids=item_ids,
                decision_receipt_id=str(receipt["id"]),
                message_worker_session_id=message_worker_session_id,
                idempotency_key=self._attempt_key(runtime),
                outcome="refused",
                reason_code=recorded,
            )
        except ProtocolRefusal as exc:
            if exc.code not in WAKE_ATTEMPT_REFUSED_REASONS:
                raise

    def _existing_attempt(
        self,
        runtime: Mapping[str, object],
        binding: AdapterBinding,
        item_ids: list[str],
        receipt: Mapping[str, object],
        message_worker_session_id: Optional[str],
    ) -> Optional[Dict[str, object]]:
        key = self._attempt_key(runtime)
        rows = read_records_snapshot(
            self.root,
            Path("receipts/wakes") / f"{self.coordinate.node_id}.jsonl",
            allowed_kinds={"wake_attempt_receipt"},
        )
        matching = [row for row in rows if row.get("idempotency_key") == key]
        if len(matching) > 1:
            raise IntegrityFailure(
                "wake_evidence_unknown", "wake attempt key has duplicate durable rows"
            )
        if not matching:
            return None
        existing = matching[0]
        if (
            existing.get("node_id") != self.coordinate.node_id
            or existing.get("acting_session_id") != binding.session_id
            or existing.get("message_worker_session_id") != message_worker_session_id
            or existing.get("item_ids") != item_ids
            or existing.get("decision_receipt_id") != receipt.get("id")
        ):
            raise IntegrityFailure(
                "wake_evidence_unknown", "wake attempt replay differs from its durable row"
            )
        return existing

    def _exact_binding(self) -> AdapterBinding:
        method = getattr(self.adapter, "exact_binding", None)
        if not callable(method):
            raise ProtocolRefusal(
                "wake_daemon_adapter_unknown", "adapter has no exact binding surface"
            )
        binding = method()
        if not isinstance(binding, AdapterBinding):
            raise IntegrityFailure(
                "wake_daemon_binding_invalid", "adapter returned no exact binding"
            )
        return binding

    def _request_wake(
        self,
        binding: AdapterBinding,
        reason: str,
        deadline: int,
        envelopes: Optional[list] = None,
    ) -> WakeAdapterResult:
        method = getattr(self.adapter, "request_wake", None)
        if not callable(method):
            raise ProtocolRefusal(
                "wake_daemon_adapter_unknown", "adapter has no wake surface"
            )
        kwargs = {}
        if envelopes is not None:
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "envelopes" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                kwargs["envelopes"] = envelopes
        result = method(binding, reason, deadline, **kwargs)
        if not isinstance(result, WakeAdapterResult) or result.outcome not in {
            "woke", "queued", "refused", "unknown"
        }:
            raise IntegrityFailure(
                "wake_daemon_adapter_unknown", "adapter returned an unknown outcome"
            )
        return result

    @staticmethod
    def _time(value: object) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) < 0.0
        ):
            raise ProtocolRefusal(
                "wake_daemon_time_invalid", "daemon clock testimony is invalid"
            )
        return float(value)

    @staticmethod
    def _wake_key(runtime: Mapping[str, object]) -> str:
        return (
            f"daemon-{str(runtime['coordinate_digest'])[:12]}-"
            f"{runtime['activation_epoch']}-{int(runtime['cycle_index']) + 1}"
        )

    @staticmethod
    def _attempt_key(runtime: Mapping[str, object]) -> str:
        # FQ-8: the attempt slot is scoped by the acting session digest. A seat
        # rebind reinitializes the runtime while the consent's activation epoch
        # persists, so an unscoped key recomputes the previous session's attempt
        # slot and the replay check throws at legitimate work. The comparison in
        # _existing_attempt is unchanged: acting_session_id still moves it, and
        # only a same-session replay may return a durable row.
        return (
            f"{runtime['current_wake_key']}-attempt-"
            f"{int(runtime['cycle_index']) + 1}-"
            f"{str(runtime['session_digest'])[:12]}"
        )

    def _schedule_success(
        self,
        runtime: Dict[str, object],
        consent: Mapping[str, object],
        now: float,
        *,
        close_reason: str = "probe",
    ) -> None:
        if runtime["circuit_state"] == "open":
            self._breaker_close_reason = close_reason
        minimum = int(consent["min_poll_seconds"])
        runtime["consecutive_refusals"] = 0
        runtime["circuit_state"] = "closed"
        runtime["awaiting_work"] = False
        runtime["current_backoff"] = minimum
        runtime["next_poll_at"] = now + minimum

    @staticmethod
    def _schedule_half_open_await(
        runtime: Dict[str, object], consent: Mapping[str, object], now: float
    ) -> None:
        """Keep the open circuit; re-check soon for the mail that is the probe."""

        minimum = int(consent["min_poll_seconds"])
        runtime["awaiting_work"] = True
        runtime["current_backoff"] = minimum
        runtime["next_poll_at"] = now + minimum

    @staticmethod
    def _schedule_idle(
        runtime: Dict[str, object], consent: Mapping[str, object], now: float
    ) -> None:
        maximum = int(consent["max_poll_seconds"])
        current = max(int(consent["min_poll_seconds"]), int(runtime["current_backoff"]))
        backoff = min(maximum, current * 2)
        runtime["awaiting_work"] = False
        runtime["current_backoff"] = backoff
        runtime["next_poll_at"] = now + backoff

    @staticmethod
    def _schedule_failure(
        runtime: Dict[str, object], consent: Mapping[str, object], now: float
    ) -> None:
        maximum = int(consent["max_backoff_seconds"])
        current = max(int(consent["min_poll_seconds"]), int(runtime["current_backoff"]))
        backoff = min(maximum, current * 2)
        refusals = int(runtime["consecutive_refusals"]) + 1
        runtime["awaiting_work"] = False
        runtime["current_backoff"] = backoff
        runtime["next_poll_at"] = now + backoff
        runtime["consecutive_refusals"] = refusals
        if refusals >= _BREAKER_THRESHOLD and runtime["circuit_state"] != "open":
            # The ONE line in this engine where the circuit crosses into open.
            # Counting here, rather than where the notice is written, is what
            # makes the count a count of TRANSITIONS: a notice deleted while
            # the circuit is still open is re-derived without advancing it, and
            # every later refusal in the same outage re-assigns "open" without
            # passing this guard.
            runtime["breaker_transitions"] = (
                int(runtime.get("breaker_transitions", 0)) + 1
            )
            runtime["circuit_state"] = "open"


#: LEDGER-1 (a): the supervisor (launchd/systemd) appends the daemon's
#: stderr/stdout to ``state/wake-daemon/logs/<digest>.<stream>.log``
#: forever — measured on the live root at 7.1 MB of one refusal line per
#: cycle from a circuit-open daemon. The daemon maintains its own logs
#: each serve cycle: copytruncate rotation past a size threshold (the
#: supervisor's append descriptor stays valid across an in-place
#: truncate), a bounded number of retained rotations, and one typed
#: receipt per rotation under the root's ``receipts/`` plane.
_LOG_ROTATION_MAX_BYTES = 1024 * 1024
_LOG_ROTATION_RETAIN = 3
_LOG_ROTATION_STREAMS = ("stderr", "stdout")


def _rotation_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


_ROTATION_COUNTER: Dict[str, Iterator[int]] = {}


def _rotation_counter(stream: str) -> Iterator[int]:
    """LEDGER-1a Am.2: a monotonic per-stream counter so two rotations
    inside one wall-clock second can never share a rotated name."""

    import itertools

    if stream not in _ROTATION_COUNTER:
        _ROTATION_COUNTER[stream] = itertools.count(1)
    return _ROTATION_COUNTER[stream]


def maintain_supervisor_logs(
    root: FloatiRoot,
    coordinate_digest: str,
    *,
    max_bytes: int = _LOG_ROTATION_MAX_BYTES,
    retain: int = _LOG_ROTATION_RETAIN,
    id_factory: Callable[[], str] = uuid7_hex,
    stamp: Optional[str] = None,
) -> list[Dict[str, object]]:
    """Rotate oversized supervisor logs; one receipt per rotation.

    Copytruncate: the rotated file carries the exact pre-rotation bytes
    and the active file is truncated in place, so the supervisor's open
    append descriptor keeps working. Paths in receipts are relative to
    the root, never ambient. A rotation never names an absolute path.
    """

    logs_dir = root.resolve_relative(Path("state/wake-daemon/logs"))
    receipts_dir = root.resolve_relative(
        Path("receipts") / "wake-daemon-log-rotation" / coordinate_digest
    )
    millisecond_stamp = stamp if stamp is not None else _rotation_timestamp()
    receipts: list[Dict[str, object]] = []
    for stream, live_fd in (
        ("stderr", 2),
        ("stdout", 1),
    ):
        active = logs_dir / f"{coordinate_digest}.{stream}.log"
        if active.is_symlink():
            raise ProtocolRefusal(
                "wake_daemon_log_symlink",
                "supervisor log must not be a symlink",
                remedy="remove the symlink and let the daemon recreate a "
                "real log file, then let the next cycle rotate",
            )
        if not active.is_file():
            continue
        identity = active.stat()
        if identity.st_size <= max_bytes:
            continue
        # LEDGER-1a Am.2: the rotated name carries the SAME millisecond
        # stamp the receipt uses plus a monotonic per-stream counter, so
        # same-second rotations cannot collide; an existing target
        # REFUSES instead of overwriting a rotated file.
        name_stamp = millisecond_stamp.replace("-", "").replace(":", "")
        rotated = logs_dir / (
            f"{coordinate_digest}.{stream}.{name_stamp}-"
            f"{next(_rotation_counter(stream)):04d}.log"
        )
        if rotated.exists():
            raise ProtocolRefusal(
                "wake_daemon_log_rotation_exists",
                f"the rotation target already exists: {rotated.name}",
                remedy="inspect the logs directory; a rotation never "
                "overwrites a rotated file",
            )
        digest_of_bytes = hashlib.sha256()
        with active.open("rb") as live:
            for chunk in iter(lambda: live.read(1024 * 1024), b""):
                digest_of_bytes.update(chunk)
        receipt = {
            "schema_version": 0,
            "id": f"wake-daemon-log-rotation-{id_factory()}",
            "timestamp": millisecond_stamp,
            "tenant_id": root.tenant_id,
            "kind": "wake_daemon_log_rotation",
            "coordinate_digest": coordinate_digest,
            "stream": stream,
            "rotated_bytes": identity.st_size,
            "rotated_sha256": digest_of_bytes.hexdigest(),
            "rotated_to": rotated.relative_to(root.path).as_posix(),
            "active_bytes_after": 0,
            "takeover": "daemon_fd",
        }
        receipt_path = receipts_dir / f"{receipt['id']}.json"
        receipt["receipt_path"] = receipt_path.relative_to(root.path).as_posix()
        # LEDGER-1a Am.1: the receipt is written BEFORE any takeover — a
        # rotation without its receipt must be impossible, so an
        # unwritable receipts plane aborts with the log untouched.
        try:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise ProtocolRefusal(
                "wake_daemon_log_receipt_unwritable",
                "the rotation receipt could not be written; the live log "
                "is untouched and will keep growing until the receipts "
                "plane is writable",
                remedy="make receipts/wake-daemon-log-rotation writable, then let the next cycle rotate",
            ) from exc
        # RENAME, never truncate: every O_APPEND descriptor the supervisor
        # or an in-flight child holds follows the inode, so the window
        # that copytruncate lost lines in does not exist. The fresh live
        # file is opened and taken over by the daemon's own descriptor.
        pre_takeover = os.fstat(live_fd)
        takeover = (
            pre_takeover.st_ino == identity.st_ino
            and pre_takeover.st_dev == identity.st_dev
        )
        receipt["takeover"] = "daemon_fd" if takeover else "rename_only"
        os.replace(active, rotated)
        fresh = os.open(active, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            if takeover:
                os.dup2(fresh, live_fd)
        finally:
            os.close(fresh)
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        receipts.append(receipt)
        _prune_rotations(
            root, coordinate_digest, stream, retain,
            id_factory=id_factory, stamp=millisecond_stamp,
        )
    return receipts


def _prune_receipts_dir(root: FloatiRoot, coordinate_digest: str) -> Path:
    return root.resolve_relative(
        Path("receipts") / "wake-daemon-log-prune" / coordinate_digest
    )


def _rotation_receipt_naming(
    root: FloatiRoot, coordinate_digest: str, rotated_relative: str
) -> Optional[Dict[str, object]]:
    """The rotation receipt whose rotated_to names the file being pruned,
    so the prune receipt carries the id it retires. None when the rotation
    receipt is absent or unreadable - the prune receipt still records the
    bytes it destroyed."""

    receipts_dir = root.resolve_relative(
        Path("receipts") / "wake-daemon-log-rotation" / coordinate_digest
    )
    if not receipts_dir.is_dir() or receipts_dir.is_symlink():
        return None
    for path in sorted(receipts_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(record, dict) and record.get("rotated_to") == rotated_relative:
            return record
    return None


def _prune_block_marker_path(
    root: FloatiRoot, coordinate_digest: str, stream: str
) -> Path:
    return root.resolve_relative(
        Path("state") / "wake-daemon" / "log-prune-blocked"
        / f"{coordinate_digest}.{stream}.json"
    )


def _record_prune_block(
    root: FloatiRoot,
    coordinate_digest: str,
    stream: str,
    pending_prunes: int,
) -> None:
    """LEDGER-1a-F1: the state plane is the daemon's own writable home,
    so the marker survives exactly while the receipts plane cannot."""

    _write_prune_block_marker(
        _prune_block_marker_path(root, coordinate_digest, stream),
        coordinate_digest,
        stream,
        pending_prunes,
        recorded_at=_rotation_timestamp(),
    )


def _refresh_prune_block(
    root: FloatiRoot,
    coordinate_digest: str,
    stream: str,
    pending_prunes: int,
) -> None:
    """LEDGER-1a-F1 Am.1 FINDING 1: every blocked cycle refreshes the
    marker's pending_prunes to the backlog actually measured now - the
    first cycle's count is never allowed to go stale. recorded_at keeps
    naming the first block; last_blocked_at names this one."""

    path = _prune_block_marker_path(root, coordinate_digest, stream)
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        recorded_at = str(previous.get("recorded_at") or "")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        recorded_at = ""
    _write_prune_block_marker(
        path,
        coordinate_digest,
        stream,
        pending_prunes,
        recorded_at=recorded_at or _rotation_timestamp(),
    )


#: LEDGER-1a-F1 Am.2: when the marker plane itself is unwritable the
#: typed refusal can only raise ONCE per (digest, stream) - the marker
#: can never be written, so the recorded-already check is always false
#: and a refusal every cycle is the stderr-wall disease again. The flag
#: lives in process memory and re-arms whenever the condition changes:
#: the marker becomes recorded, or a prune proves a plane healed.
_PRUNE_BLOCK_HELD: set = set()

#: SERVE-PASS-1: the conditions serve() is currently holding quietly,
#: keyed (coordinate digest, refusal code). Process memory, like the
#: prune-block flag: the durable half lives in the state plane, and the
#: flag re-arms the moment a cycle maintains cleanly.
_SERVE_REFUSAL_HELD: set = set()


def _write_prune_block_marker(
    path: Path,
    coordinate_digest: str,
    stream: str,
    pending_prunes: int,
    *,
    recorded_at: str,
) -> None:
    """LEDGER-1a-F1 Am.1 FINDING 2: the marker plane can be unwritable
    too; that failure is its own typed refusal - a raw PermissionError
    every cycle is the same disease one layer down."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "schema_version": 0,
                "coordinate_digest": coordinate_digest,
                "stream": stream,
                "code": "wake_daemon_log_prune_receipt_unwritable",
                "pending_prunes": pending_prunes,
                "recorded_at": recorded_at,
                "last_blocked_at": _rotation_timestamp(),
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ProtocolRefusal(
            "wake_daemon_log_prune_marker_unwritable",
            "the prune-block marker could not be written either; the "
            "block cannot be recorded where it can be read, and the "
            "rotated file keeps its bytes",
            remedy="make state/wake-daemon and "
            "receipts/wake-daemon-log-prune writable, then let the next "
            "cycle prune",
        ) from exc


def _clear_prune_block(
    root: FloatiRoot, coordinate_digest: str, stream: str
) -> None:
    """Best effort: a prune that receipted proved a plane healed, so the
    in-process flag re-arms even when the stale marker cannot be
    unlinked right now."""

    _PRUNE_BLOCK_HELD.discard((coordinate_digest, stream))
    try:
        _prune_block_marker_path(root, coordinate_digest, stream).unlink()
    except OSError:
        pass


def _prune_rotations(
    root: FloatiRoot,
    coordinate_digest: str,
    stream: str,
    retain: int,
    *,
    id_factory: Callable[[], str] = uuid7_hex,
    stamp: Optional[str] = None,
) -> list[Dict[str, object]]:
    """LEDGER-1a Am.3: retention is receipted, never silent.

    The pruned file's name lives on its rotation receipt; unlinking it
    without testimony left that receipt naming bytes that no longer
    exist. Each prune writes one typed receipt (kind
    wake_daemon_log_prune: the pruned path, the digest of the bytes
    actually destroyed, and the retaining rule) BEFORE the unlink - a
    prune without its receipt must be impossible, exactly like a
    rotation without its receipt.

    LEDGER-1a-F1: while the receipts plane stays unwritable the refusal
    is recorded ONCE (one raise plus the state-plane block marker) and
    every further blocked cycle holds quietly - an unhealable condition
    must not re-announce itself each cycle. The first prune that writes
    its receipt proves the plane healed and clears the marker.
    """

    logs_dir = root.resolve_relative(Path("state/wake-daemon/logs"))
    receipts_dir = _prune_receipts_dir(root, coordinate_digest)
    prune_stamp = stamp if stamp is not None else _rotation_timestamp()
    receipts: list[Dict[str, object]] = []
    rotations = sorted(
        logs_dir.glob(f"{coordinate_digest}.{stream}.*.log"),
        key=lambda path: path.name,
    )
    stale_rotations = rotations[: max(0, len(rotations) - retain)]
    for stale in stale_rotations:
        if stale.is_symlink():
            raise ProtocolRefusal(
                "wake_daemon_log_symlink",
                f"a rotated log must not be a symlink: {stale.name}",
                remedy="inspect the logs directory and replace the symlink "
                "with a real file; a rotated log is never a symlink",
            )
        digest_of_bytes = hashlib.sha256()
        with stale.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest_of_bytes.update(chunk)
        pruned_relative = stale.relative_to(root.path).as_posix()
        rotation = _rotation_receipt_naming(
            root, coordinate_digest, pruned_relative
        )
        receipt: Dict[str, object] = {
            "schema_version": 0,
            "id": f"wake-daemon-log-prune-{id_factory()}",
            "timestamp": prune_stamp,
            "tenant_id": root.tenant_id,
            "kind": "wake_daemon_log_prune",
            "coordinate_digest": coordinate_digest,
            "stream": stream,
            "pruned_path": pruned_relative,
            "pruned_bytes": stale.stat().st_size,
            "pruned_sha256": digest_of_bytes.hexdigest(),
            "retaining_rule": {"kind": "retain_newest", "retain": retain},
        }
        if rotation is not None:
            receipt["rotated_receipt"] = rotation.get("id")
            receipt["rotated_receipt_path"] = rotation.get("receipt_path")
        receipt_path = receipts_dir / f"{receipt['id']}.json"
        receipt["receipt_path"] = receipt_path.relative_to(root.path).as_posix()
        try:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            marker = _prune_block_marker_path(root, coordinate_digest, stream)
            if marker.exists():
                # Recorded already; hold quietly, but refresh the marker's
                # backlog first - the first cycle's count must never go
                # stale (LEDGER-1a-F1 Am.1 FINDING 1). The condition changed
                # (the marker plane healed), so the in-process flag re-arms.
                _PRUNE_BLOCK_HELD.discard((coordinate_digest, stream))
                _refresh_prune_block(
                    root, coordinate_digest, stream, len(stale_rotations)
                )
                return receipts
            key = (coordinate_digest, stream)
            try:
                _record_prune_block(
                    root, coordinate_digest, stream, len(stale_rotations)
                )
            except ProtocolRefusal:
                # LEDGER-1a-F1 Am.2: the marker plane is unwritable too -
                # the typed refusal raises once per process, then the
                # condition holds quietly until it changes.
                if key in _PRUNE_BLOCK_HELD:
                    return receipts
                _PRUNE_BLOCK_HELD.add(key)
                raise
            raise ProtocolRefusal(
                "wake_daemon_log_prune_receipt_unwritable",
                "the prune receipt could not be written; the rotated file "
                "survives and keeps its bytes until the receipts plane is "
                "writable (further blocked cycles hold quietly until it "
                "heals)",
                remedy="make receipts/wake-daemon-log-prune writable, then "
                "let the next cycle prune",
            ) from exc
        stale.unlink()
        receipts.append(receipt)
    if receipts:
        # At least one prune receipt landed: the plane is writable again.
        _clear_prune_block(root, coordinate_digest, stream)
    return receipts
