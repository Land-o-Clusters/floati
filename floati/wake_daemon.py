"""Bounded, exact-session wake daemon engine with durable outcome testimony."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from .bus_epoch import shared_epoch_operation
from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot
from .records import WAKE_ATTEMPT_REFUSED_REASONS
from .registry import Registry
from .snapshot import _owned_epoch_archives
from .wake_control import WakeController, is_session_paused
from .wake_daemon_adapters import AdapterBinding, WakeAdapterResult
from .wake_daemon_contract import (
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
    DaemonLifecycleLedger,
)
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
})
_LEGACY_RUNTIME_FIELDS = _RUNTIME_FIELDS - {"bus_epoch_archive"}
_BREAKER_THRESHOLD = 3
_WAKE_BUDGET = 3
_WAKE_BUDGET_WINDOW_SECONDS = 300.0
WAKE_BREAKER_REMEDY = (
    "rebind the wake daemon to a dedicated headless session - an interactive "
    "session with a large rollout may be unresumable - then rerun doctor"
)


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
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
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
        if current_time < float(runtime["next_poll_at"]):
            artifact = self._artifact(runtime)
            artifact["state"] = "backpressure"
            artifact["reason_code"] = "wake_daemon_poll_not_due"
            return artifact

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
            self._schedule_success(runtime, consent, current_time)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="paused",
                event="paused",
                lifecycle_state="paused",
                reason_code=None,
            )

        if runtime["circuit_state"] == "open":
            self._schedule_failure(runtime, consent, current_time)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="backpressure",
                event="backpressure",
                lifecycle_state="backpressure",
                reason_code="wake_daemon_circuit_open",
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
        controller = WakeHoldController(self.root)
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
        existing = self._existing_attempt(
            runtime,
            binding,
            item_ids,
            receipt,
            message_worker_session_id,
        )
        if existing is not None:
            if existing["outcome"] == "woke":
                runtime["wake_timestamps"] = timestamps + [current_time]
                runtime["current_wake_key"] = None
                self._schedule_success(runtime, consent, current_time)
                return self._transition(
                    runtime,
                    consent,
                    binding,
                    result_state="woke",
                    event="wake_attempt",
                    lifecycle_state="running",
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
            binding, reason, min(300, int(consent["max_poll_seconds"]))
        )
        if result.outcome == "woke":
            attempt_key = self._attempt_key(runtime)
            try:
                durable = WakeAttemptLedger(self.root).record(
                    recipient=self.coordinate.node_id,
                    acting_session_id=binding.session_id,
                    item_ids=item_ids,
                    decision_receipt_id=str(receipt["id"]),
                    message_worker_session_id=message_worker_session_id,
                    idempotency_key=attempt_key,
                    outcome="woke",
                )
                if durable.get("outcome") != "woke":
                    raise IntegrityFailure(
                        "wake_evidence_unknown", "durable attempt did not confirm woke"
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
            runtime["wake_timestamps"] = timestamps + [current_time]
            runtime["current_wake_key"] = None
            self._schedule_success(runtime, consent, current_time)
            self._first_wake_verdict(binding, result)
            return self._transition(
                runtime,
                consent,
                binding,
                result_state="woke",
                event="wake_attempt",
                lifecycle_state="running",
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

    def serve(
        self,
        stop_requested: Callable[[], bool],
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.consent.require_active(self.coordinate)
        with self.owner:
            while not stop_requested():
                result = self.run_cycle(clock())
                if stop_requested():
                    break
                delay = max(0.0, float(result["next_poll_at"]) - clock())
                sleep(delay)

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
            current = self._initial_runtime(consent, binding, current_epoch)
        if current["session_digest"] not in {None, binding.session_digest}:
            current = self._initial_runtime(consent, binding, current_epoch)
        current["session_digest"] = binding.session_digest
        return current

    def _initial_runtime(
        self,
        consent: Mapping[str, object],
        binding: AdapterBinding,
        current_epoch: Optional[str],
    ) -> Dict[str, object]:
        return {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "node_id": self.coordinate.node_id,
            "harness": self.coordinate.harness,
            "coordinate_digest": self.coordinate.digest,
            "daemon_instance_id": self.daemon_instance_id,
            "activation_epoch": int(consent["activation_epoch"]),
            "cycle_index": 0,
            "current_wake_key": None,
            "consecutive_refusals": 0,
            "circuit_state": "closed",
            "next_poll_at": 0.0,
            "current_backoff": int(consent["min_poll_seconds"]),
            "wake_timestamps": [],
            "session_digest": binding.session_digest,
            "last_state": "inactive",
            "last_reason_code": None,
            "last_lifecycle_receipt_id": None,
            "bus_epoch_archive": current_epoch,
        }

    def _validate_runtime(self, value: object) -> Dict[str, object]:
        if not isinstance(value, dict) or set(value) not in {
            _RUNTIME_FIELDS, _LEGACY_RUNTIME_FIELDS
        }:
            raise IntegrityFailure(
                "wake_daemon_runtime_invalid", "daemon runtime state has an open shape"
            )
        value = dict(value)
        value.setdefault("bus_epoch_archive", None)
        if (
            value.get("schema_version") != 0
            or value.get("tenant_id") != self.root.tenant_id
            or value.get("node_id") != self.coordinate.node_id
            or value.get("harness") != self.coordinate.harness
            or value.get("coordinate_digest") != self.coordinate.digest
            or value.get("circuit_state") not in {"closed", "open"}
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
        for field in ("activation_epoch", "cycle_index", "consecutive_refusals", "current_backoff"):
            item = value.get(field)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise IntegrityFailure(
                    "wake_daemon_runtime_invalid", f"daemon runtime {field} is invalid"
                )
        for item in value["wake_timestamps"]:
            self._time(item)
        self._time(value.get("next_poll_at"))
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
    ) -> Dict[str, object]:
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
        )
        runtime["last_lifecycle_receipt_id"] = lifecycle["id"]
        self._write_runtime(runtime)
        self._maintain_breaker_notice(runtime, binding)
        return self._artifact(runtime)

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

        notice_path = self.root.resolve_relative(
            Path("state/wake-daemon/notices") / f"{self.coordinate.digest}.json"
        )
        if runtime["circuit_state"] == "closed":
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
            "remedy": WAKE_BREAKER_REMEDY,
        }
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
        self, binding: AdapterBinding, reason: str, deadline: int
    ) -> WakeAdapterResult:
        method = getattr(self.adapter, "request_wake", None)
        if not callable(method):
            raise ProtocolRefusal(
                "wake_daemon_adapter_unknown", "adapter has no wake surface"
            )
        result = method(binding, reason, deadline)
        if not isinstance(result, WakeAdapterResult) or result.outcome not in {
            "woke", "refused", "unknown"
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
        return (
            f"{runtime['current_wake_key']}-attempt-"
            f"{int(runtime['cycle_index']) + 1}"
        )

    @staticmethod
    def _schedule_success(
        runtime: Dict[str, object], consent: Mapping[str, object], now: float
    ) -> None:
        minimum = int(consent["min_poll_seconds"])
        runtime["consecutive_refusals"] = 0
        runtime["circuit_state"] = "closed"
        runtime["current_backoff"] = minimum
        runtime["next_poll_at"] = now + minimum

    @staticmethod
    def _schedule_idle(
        runtime: Dict[str, object], consent: Mapping[str, object], now: float
    ) -> None:
        maximum = int(consent["max_poll_seconds"])
        current = max(int(consent["min_poll_seconds"]), int(runtime["current_backoff"]))
        backoff = min(maximum, current * 2)
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
        runtime["current_backoff"] = backoff
        runtime["next_poll_at"] = now + backoff
        runtime["consecutive_refusals"] = refusals
        if refusals >= _BREAKER_THRESHOLD:
            runtime["circuit_state"] = "open"
