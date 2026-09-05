"""Harness-neutral phase-1 adapter conformance artifact."""

from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .cursor import SparseCursor
from .adapters.acp import ACPAdapter, ACPRefusal, probe_reference_harness
from .errors import IntegrityFailure, ProtocolRefusal
from . import fixture_ids
from .events import EVENT_KINDS, EventLog
from .ids import uuid7_hex
from .jsonl import append_record, read_records_snapshot
from .planes import AuthorityGrantStore, LivenessPresenceStore, MutualExclusionHoldStore
from .registry import Registry, utc_now
from .root import FloatiRoot


CONFORMANT = 0
CONFORMANCE_FAILED = 10
CONFIGURATION_REFUSED = 20
ADAPTER_DIED = 30
INTENTIONAL_SILENCE = 31
NO_RESULT = 32
MALFORMED_EVIDENCE = 33
MANIFEST_MISMATCH = 34
DEFAULT_CALL_TIMEOUT = 2.0
_PRIMARY_NODE = fixture_ids.worker("alpha")


@dataclass(frozen=True)
class AdapterResult:
    status: str
    evidence: Dict[str, Any]


class ReferenceAdapter:
    """Reference implementation of the adapter boundary over the core."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.registry = Registry(root)
        self.events = EventLog(root, self.registry)
        self.cursor = SparseCursor(root)
        self.liveness = LivenessPresenceStore(root)
        self.authority = AuthorityGrantStore(root)
        self.exclusion = MutualExclusionHoldStore(root)

    @staticmethod
    def _result(operation: Callable[[], Dict[str, Any]]) -> AdapterResult:
        try:
            return AdapterResult("ok", operation())
        except ProtocolRefusal as exc:
            return AdapterResult("refused", {"code": exc.code, "detail": exc.detail})

    def register(self, node_id: str, role: str) -> AdapterResult:
        return self._result(lambda: self.registry.register(node_id, role))

    def send(
        self,
        sender: str,
        recipient: str,
        repo: str,
        sha: str,
        doc: str,
        note: str,
        idempotency_key: str,
    ) -> AdapterResult:
        return self._result(
            lambda: self.events.send(
                sender,
                recipient,
                repo,
                sha,
                doc,
                note,
                idempotency_key=idempotency_key,
            )
        )

    def present(self, recipient: str) -> AdapterResult:
        try:
            messages, receipt = self.events.present(recipient)
            return AdapterResult("ok", {"messages": messages, "receipt": receipt})
        except ProtocolRefusal as exc:
            return AdapterResult("refused", {"code": exc.code, "detail": exc.detail})

    def acknowledge(self, recipient: str, item_ids: Sequence[str]) -> AdapterResult:
        return self._result(
            lambda: self.cursor.ack(
                recipient, item_ids, acting_session_id="conformance-session"
            )
        )

    def observe_liveness(self, node_id: str, ttl_seconds: int, now: datetime) -> AdapterResult:
        return self._result(lambda: self.liveness.observe(node_id, ttl_seconds, now))

    def liveness_status(self, node_id: str, now: datetime) -> AdapterResult:
        try:
            return AdapterResult("ok", {"liveness_state": self.liveness.status(node_id, now)})
        except ProtocolRefusal as exc:
            return AdapterResult("refused", {"code": exc.code, "detail": exc.detail})

    def claim_authority(
        self,
        subject_id: str,
        holder: str,
        ttl_seconds: int,
        deadline_seconds: int,
        now: datetime,
    ) -> AdapterResult:
        return self._result(
            lambda: self.authority.claim(subject_id, holder, ttl_seconds, deadline_seconds, now)
        )

    def acquire_exclusion(
        self,
        resource_id: str,
        holder: str,
        ttl_seconds: int,
        deadline_seconds: int,
        now: datetime,
    ) -> AdapterResult:
        return self._result(
            lambda: self.exclusion.acquire(resource_id, holder, ttl_seconds, deadline_seconds, now)
        )

    def renew_authority(self, subject_id: str, holder: str, epoch: int, ttl_seconds: int, deadline_seconds: int, now: datetime) -> AdapterResult:
        return self._result(lambda: self.authority.renew(subject_id, holder, epoch, ttl_seconds, deadline_seconds, now))

    def release_authority(self, subject_id: str, holder: str, epoch: int, now: datetime) -> AdapterResult:
        return self._result(lambda: self.authority.release(subject_id, holder, epoch, now))

    def renew_exclusion(self, resource_id: str, holder: str, epoch: int, ttl_seconds: int, deadline_seconds: int, now: datetime) -> AdapterResult:
        return self._result(lambda: self.exclusion.renew(resource_id, holder, epoch, ttl_seconds, deadline_seconds, now))

    def release_exclusion(self, resource_id: str, holder: str, epoch: int, now: datetime) -> AdapterResult:
        return self._result(lambda: self.exclusion.release(resource_id, holder, epoch, now))


def _adapter_worker(connection: object, specification: str, root_path: str, tenant_id: str) -> None:
    try:
        root = FloatiRoot.open(Path(root_path), tenant_id)
        adapter = _load_factory(specification)(root)
        connection.send(("ready", None))
        while True:
            command = connection.recv()
            if command is None:
                return
            method_name, args = command
            try:
                result = getattr(adapter, method_name)(*args)
                connection.send(("result", result))
            except Exception as exc:
                connection.send(("raised", type(exc).__name__))
    finally:
        connection.close()


class _IsolatedAdapter:
    """Persistent adapter worker with a hard bound around every call."""

    def __init__(self, specification: str, root: FloatiRoot, call_timeout: float) -> None:
        self._timeout = call_timeout
        context = multiprocessing.get_context("fork")
        parent, child = context.Pipe()
        self._connection = parent
        self._process = context.Process(
            target=_adapter_worker,
            args=(child, specification, str(root.path), root.tenant_id),
            name="floati-conformance-adapter",
        )
        self._process.start()
        child.close()
        if not parent.poll(call_timeout):
            self.close()
            raise RuntimeError("adapter initialization timed out")
        status, detail = parent.recv()
        if status != "ready":
            self.close()
            raise RuntimeError(f"adapter initialization failed: {detail}")

    def __getattr__(self, method_name: str) -> Callable[..., object]:
        def call(*args: object) -> object:
            if not self._process.is_alive():
                raise RuntimeError("adapter process is not alive")
            self._connection.send((method_name, args))
            if not self._connection.poll(self._timeout):
                self.close()
                raise RuntimeError(f"{method_name} timed out")
            try:
                status, value = self._connection.recv()
            except EOFError as exc:
                raise RuntimeError("adapter process died") from exc
            if status == "raised":
                raise RuntimeError(f"adapter raised {value}")
            return value
        return call

    def close(self) -> None:
        if getattr(self, "_process", None) is None:
            return
        if self._process.is_alive():
            try:
                self._connection.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(0.1)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(1)
        self._connection.close()


class _Outcome(RuntimeError):
    def __init__(self, exit_code: int, status: str, detail: str) -> None:
        super().__init__(detail)
        self.exit_code = exit_code
        self.status = status
        self.detail = detail


def _invoke(adapter: object, method_name: str, *args: object) -> AdapterResult:
    method = getattr(adapter, method_name, None)
    if not callable(method):
        raise _Outcome(MALFORMED_EVIDENCE, "malformed_evidence", f"adapter has no callable {method_name}")
    try:
        result = method(*args)
    except Exception as exc:
        raise _Outcome(ADAPTER_DIED, "adapter_died", f"{method_name}: {type(exc).__name__}") from exc
    if result is None:
        raise _Outcome(NO_RESULT, "no_result", f"{method_name} returned no result")
    status = getattr(result, "status", None)
    evidence = getattr(result, "evidence", None)
    if not isinstance(status, str) or not isinstance(evidence, dict):
        raise _Outcome(MALFORMED_EVIDENCE, "malformed_evidence", f"{method_name} returned invalid evidence")
    result = AdapterResult(status, evidence)
    if result.status == "intentional_silence":
        raise _Outcome(INTENTIONAL_SILENCE, "intentional_silence", method_name)
    if result.status not in ("ok", "refused"):
        raise _Outcome(MALFORMED_EVIDENCE, "malformed_evidence", f"{method_name} returned status {result.status}")
    return result


def _expect_ok(adapter: object, method_name: str, *args: object) -> Dict[str, Any]:
    result = _invoke(adapter, method_name, *args)
    if result.status != "ok":
        raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", f"{method_name} unexpectedly refused")
    return result.evidence


def _expect_refused(
    adapter: object,
    code: str,
    method_name: str,
    *args: object,
    detail_suffix: Optional[str] = None,
) -> None:
    result = _invoke(adapter, method_name, *args)
    if result.status != "refused" or result.evidence.get("code") != code:
        raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", f"{method_name} did not refuse with {code}")
    if detail_suffix is not None:
        detail = result.evidence.get("detail")
        if not isinstance(detail, str) or not detail.endswith(detail_suffix):
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", f"{method_name} refusal detail mismatch")


def _expect_protocol_refusal(
    operation: Callable[[], object], code: str, detail_suffix: str
) -> None:
    try:
        operation()
    except ProtocolRefusal as exc:
        if exc.code == code and exc.detail.endswith(detail_suffix):
            return
        raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "refusal evidence mismatch") from exc
    raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "unrecognized party was accepted")


def _append_retired_registry_entry(root: FloatiRoot, node_id: str, role: str) -> None:
    from .bus_epoch import epoch_guard

    with epoch_guard(root, exclusive=False):
        append_record(
            root,
            "registry/entries.jsonl",
            {
                "schema_version": 0,
                "id": "registry-" + uuid7_hex(),
                "tenant_id": root.tenant_id,
                "timestamp": utc_now(),
                "kind": "registry_entry",
                "node_id": node_id,
                "role": role,
                "state": "retired",
            },
            allowed_kinds={"registry_entry"},
        )


def _remove_registry_lock(root: FloatiRoot) -> None:
    lock_path = root.resolve_relative("registry/entries.jsonl.lock")
    if lock_path.exists():
        lock_path.unlink()
    if lock_path.exists():
        raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "registry lock remained after setup")


def _root_snapshot(root: FloatiRoot) -> Tuple[Tuple[str, str, object], ...]:
    entries = []
    for entry in sorted(root.tenant_home.rglob("*"), key=lambda path: path.relative_to(root.tenant_home).as_posix()):
        relative = entry.relative_to(root.tenant_home).as_posix()
        if entry.is_symlink():
            entries.append((relative, "symlink", entry.readlink().as_posix()))
        elif entry.is_file():
            entries.append((relative, "regular", entry.read_bytes()))
        elif entry.is_dir():
            entries.append((relative, "directory", None))
        else:
            entries.append((relative, "other", None))
    return tuple(entries)


def run(adapter: object, root: FloatiRoot) -> int:
    """Exercise the fixed v0 cases and return the artifact exit code."""

    try:
        for node in ("charlie", "retired", "bob", _PRIMARY_NODE, "delta"):
            evidence = _expect_ok(adapter, "register", node, "worker")
            if evidence.get("node_id") != node or evidence.get("state") != "active":
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "registry evidence mismatch")
        _append_retired_registry_entry(root, "retired", "worker")
        _remove_registry_lock(root)

        sent = []
        run_key = "conformance-" + uuid7_hex()
        for character in ("A", "B", "C"):
            before_registered_send = _root_snapshot(root)
            before_events = read_records_snapshot(
                root,
                "events.jsonl",
                allowed_kinds=set(EVENT_KINDS),
            )
            evidence = _expect_ok(
                adapter,
                "send",
                _PRIMARY_NODE,
                "bob",
                "floati",
                character.lower() * 40,
                f"docs/evidence/{character}.md",
                f"notification {character}",
                f"{run_key}-{character}",
            )
            message = evidence.get("message")
            readiness = evidence.get("recipient_readiness")
            if (
                not isinstance(message, dict)
                or not isinstance(readiness, dict)
                or readiness.get("state") != "recipient_not_listening"
                or message.get("kind") != "message_envelope"
                or message.get("sha") != character.lower() * 40
                or message.get("doc") != f"docs/evidence/{character}.md"
            ):
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "message evidence mismatch")
            after_events = read_records_snapshot(
                root,
                "events.jsonl",
                allowed_kinds=set(EVENT_KINDS),
            )
            if (
                _root_snapshot(root) == before_registered_send
                or len(after_events) != len(before_events) + 1
                or after_events[-1].get("kind") != "message_envelope"
                or after_events[-1].get("id") != message.get("id")
                or after_events[-1].get("sender") != _PRIMARY_NODE
                or after_events[-1].get("recipient") != "bob"
                or after_events[-1].get("repo") != "floati"
                or after_events[-1].get("sha") != character.lower() * 40
                or after_events[-1].get("doc") != f"docs/evidence/{character}.md"
                or after_events[-1].get("note") != f"notification {character}"
                or after_events[-1].get("idempotency_key") != f"{run_key}-{character}"
            ):
                raise _Outcome(
                    CONFORMANCE_FAILED,
                    "conformance_failed",
                    "registered send did not append durable event",
                )
            sent.append(message)

        first = _expect_ok(adapter, "present", "bob")
        first_ids = [item.get("id") for item in first.get("messages", [])]
        if first_ids != [item["id"] for item in sent] or first.get("receipt", {}).get("kind") != "delivery_receipt":
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "delivery evidence mismatch")

        ack = _expect_ok(adapter, "acknowledge", "bob", [sent[1]["id"]])
        if ack.get("kind") != "ack_receipt" or ack.get("item_ids") != [sent[1]["id"]]:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "ack evidence mismatch")
        second = _expect_ok(adapter, "present", "bob")
        second_ids = [item.get("id") for item in second.get("messages", [])]
        if second_ids != [sent[0]["id"], sent[2]["id"]]:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "sparse acknowledgment mismatch")

        active_roster_suffix = "registered active nodes: " + ", ".join(
            sorted((_PRIMARY_NODE, "bob", "charlie", "delta"))
        )
        registered_roster_suffix = "registered nodes: " + ", ".join(
            sorted((_PRIMARY_NODE, "bob", "charlie", "delta"))
        )
        for code, sender, recipient, key, roster_suffix in (
            ("unknown_sender", "stranger", "bob", "unknown-sender", active_roster_suffix),
            ("recipient_unregistered", _PRIMARY_NODE, "stranger", "unknown-recipient", registered_roster_suffix),
            ("unknown_sender", "retired", "bob", "retired-sender", active_roster_suffix),
        ):
            before_denial = _root_snapshot(root)
            _expect_refused(
                adapter,
                code,
                "send",
                sender,
                recipient,
                "floati",
                "d" * 40,
                "docs/evidence/denied.md",
                "denied",
                key,
                detail_suffix=roster_suffix,
            )
            if _root_snapshot(root) != before_denial:
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "denial mutated root snapshot")
        denials = read_records_snapshot(root, "receipts/denials.jsonl", allowed_kinds={"denial_receipt"})
        if denials:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "denial evidence mismatch")

        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        liveness = _expect_ok(adapter, "observe_liveness", _PRIMARY_NODE, 10, now)
        if liveness.get("kind") != "liveness_presence":
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "liveness evidence mismatch")
        silent = _expect_ok(adapter, "liveness_status", _PRIMARY_NODE, now + timedelta(seconds=5))
        if silent.get("liveness_state") != "silent":
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "silent state mismatch")
        expired_presence = _expect_ok(adapter, "liveness_status", _PRIMARY_NODE, now + timedelta(seconds=10))
        if expired_presence.get("liveness_state") != "expired":
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "expired liveness state mismatch")
        authority = _expect_ok(adapter, "claim_authority", "build", _PRIMARY_NODE, 10, 10, now)
        if authority.get("kind") != "authority_grant" or authority.get("epoch") != 1:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "authority evidence mismatch")
        _expect_refused(adapter, "deadline_exceeds_ttl", "claim_authority", "invalid", _PRIMARY_NODE, 9, 10, now)
        _expect_refused(adapter, "authority_held", "claim_authority", "build", "charlie", 10, 10, now)
        renewed_authority = _expect_ok(adapter, "renew_authority", "build", _PRIMARY_NODE, 1, 10, 8, now + timedelta(seconds=1))
        if renewed_authority.get("epoch") != 1:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "authority renewal mismatch")
        _expect_refused(adapter, "epoch_mismatch", "renew_authority", "build", _PRIMARY_NODE, 2, 10, 8, now + timedelta(seconds=2))
        released_authority = _expect_ok(adapter, "release_authority", "build", _PRIMARY_NODE, 1, now + timedelta(seconds=2))
        if released_authority.get("state") != "released":
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "authority release mismatch")
        _expect_refused(adapter, "authority_released", "renew_authority", "build", _PRIMARY_NODE, 1, 10, 8, now + timedelta(seconds=3))
        next_authority = _expect_ok(adapter, "claim_authority", "build", "charlie", 10, 8, now + timedelta(seconds=3))
        if next_authority.get("epoch") != 2:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "authority epoch did not advance")
        _expect_ok(adapter, "claim_authority", "expiring", _PRIMARY_NODE, 1, 1, now)
        _expect_refused(adapter, "authority_expired", "renew_authority", "expiring", _PRIMARY_NODE, 1, 1, 1, now + timedelta(seconds=1))
        exclusion = _expect_ok(adapter, "acquire_exclusion", "workspace", _PRIMARY_NODE, 10, 8, now)
        if exclusion.get("kind") != "mutual_exclusion_hold" or exclusion.get("epoch") != 1:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "exclusion evidence mismatch")
        _expect_refused(adapter, "exclusion_held", "acquire_exclusion", "workspace", "charlie", 10, 8, now)
        renewed_exclusion = _expect_ok(adapter, "renew_exclusion", "workspace", _PRIMARY_NODE, 1, 10, 8, now + timedelta(seconds=1))
        if renewed_exclusion.get("epoch") != 1:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "exclusion renewal mismatch")
        _expect_refused(adapter, "epoch_mismatch", "renew_exclusion", "workspace", _PRIMARY_NODE, 2, 10, 8, now + timedelta(seconds=2))
        released_exclusion = _expect_ok(adapter, "release_exclusion", "workspace", _PRIMARY_NODE, 1, now + timedelta(seconds=2))
        if released_exclusion.get("state") != "released":
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "exclusion release mismatch")
        _expect_refused(adapter, "exclusion_released", "renew_exclusion", "workspace", _PRIMARY_NODE, 1, 10, 8, now + timedelta(seconds=3))
        next_exclusion = _expect_ok(adapter, "acquire_exclusion", "workspace", "charlie", 10, 8, now + timedelta(seconds=3))
        if next_exclusion.get("epoch") != 2:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "exclusion epoch did not advance")
        _expect_ok(adapter, "acquire_exclusion", "expiring", _PRIMARY_NODE, 1, 1, now)
        _expect_refused(adapter, "exclusion_expired", "renew_exclusion", "expiring", _PRIMARY_NODE, 1, 1, 1, now + timedelta(seconds=1))
        required_directories = ("liveness-presence", "authority-grants", "mutual-exclusion-holds")
        if not all((root.tenant_home / directory).is_dir() for directory in required_directories):
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "plane paths are not separated")
    except _Outcome as outcome:
        _emit(outcome.status, outcome.detail, stream=sys.stderr)
        return outcome.exit_code
    _emit("conformant", {"cases": 24}, stream=sys.stdout)
    return CONFORMANT


def run_live_root_smoke() -> int:
    """Exercise one throwaway direct-home notification round trip."""

    try:
        with TemporaryDirectory() as temporary:
            root = FloatiRoot.open_direct_home(Path(temporary) / "smoke-tenant", create=True)
            registry = Registry(root)
            events = EventLog(root, registry)
            cursor = SparseCursor(root)

            for node in ("smoke-recipient", "smoke-retired", "smoke-extra", "smoke-sender"):
                registered = registry.register(node, "smoke")
                if registered.get("node_id") != node or registered.get("state") != "active":
                    raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "registry evidence mismatch")
            _append_retired_registry_entry(root, "smoke-retired", "smoke")
            _remove_registry_lock(root)

            sent = events.send(
                "smoke-sender",
                "smoke-recipient",
                "floati",
                "a" * 40,
                "docs/evidence/live-root-smoke.md",
                "throwaway live-root smoke",
                idempotency_key="live-root-smoke",
            )
            message = sent.get("message")
            readiness = sent.get("recipient_readiness")
            if (
                not isinstance(message, dict)
                or not isinstance(readiness, dict)
                or readiness.get("state") != "recipient_not_listening"
                or message.get("kind") != "message_envelope"
                or message.get("sender") != "smoke-sender"
                or message.get("recipient") != "smoke-recipient"
                or message.get("repo") != "floati"
                or message.get("sha") != "a" * 40
                or message.get("doc") != "docs/evidence/live-root-smoke.md"
            ):
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "message evidence mismatch")

            messages, delivery = events.present("smoke-recipient")
            if (
                [message.get("id") for message in messages] != [message["id"]]
                or delivery is None
                or delivery.get("kind") != "delivery_receipt"
                or delivery.get("item_ids") != [message["id"]]
            ):
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "delivery evidence mismatch")

            acknowledgment = cursor.ack(
                "smoke-recipient",
                [message["id"]],
                acting_session_id="conformance-session",
            )
            if (
                acknowledgment.get("kind") != "ack_receipt"
                or acknowledgment.get("item_ids") != [message["id"]]
            ):
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "ack evidence mismatch")

            for sender, recipient, code, key, roster_suffix in (
                ("stranger", "smoke-recipient", "unknown_sender", "live-root-smoke-unknown-sender", "registered active nodes: smoke-extra, smoke-recipient, smoke-sender"),
                ("smoke-sender", "stranger", "recipient_unregistered", "live-root-smoke-unknown-recipient", "registered nodes: smoke-extra, smoke-recipient, smoke-sender"),
                ("smoke-retired", "smoke-recipient", "unknown_sender", "live-root-smoke-retired-sender", "registered active nodes: smoke-extra, smoke-recipient, smoke-sender"),
            ):
                before_denial = _root_snapshot(root)
                _expect_protocol_refusal(
                    lambda sender=sender, recipient=recipient, key=key: events.send(
                        sender,
                        recipient,
                        "floati",
                        "b" * 40,
                        "docs/evidence/denied.md",
                        "denied",
                        idempotency_key=key,
                    ),
                    code,
                    roster_suffix,
                )
                if _root_snapshot(root) != before_denial:
                    raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "denial mutated root snapshot")
            denials = read_records_snapshot(root, "receipts/denials.jsonl", allowed_kinds={"denial_receipt"})
            if denials:
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "denial evidence mismatch")
    except _Outcome as outcome:
        _emit(outcome.status, outcome.detail, stream=sys.stderr)
        return outcome.exit_code
    except IntegrityFailure as exc:
        _emit("malformed_evidence", exc.code, stream=sys.stderr)
        return MALFORMED_EVIDENCE
    except ProtocolRefusal as exc:
        _emit("conformance_failed", exc.code, stream=sys.stderr)
        return CONFORMANCE_FAILED
    _emit("conformant", {"cases": 5}, stream=sys.stdout)
    return CONFORMANT


def run_acp_fixture_conformance(*, executable: object = None) -> int:
    """Exercise the finite ACP codec without launching a provider harness."""

    adapter = ACPAdapter()
    request = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}'
    response = b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentCapabilities":{}}}'
    try:
        for line, category in ((request, "request"), (response, "response")):
            message = adapter.decode_line(line)
            if message.category != category or json.loads(adapter.encode_line(message)) != json.loads(line):
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "ACP fixture round-trip mismatch")
        try:
            adapter.decode_line(b'{"jsonrpc":"2.0","id":1,"id":2,"method":"initialize"}')
        except ACPRefusal as exc:
            if exc.code != "acp_duplicate_key":
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "ACP duplicate-key class mismatch") from exc
        else:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "ACP duplicate key was accepted")
        try:
            adapter.decode_line(b'{"jsonrpc":"2.0","id":1,"method":"fs/write","params":{}}')
        except ACPRefusal as exc:
            if exc.code != "acp_method_unruled":
                raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "ACP method refusal class mismatch") from exc
        else:
            raise _Outcome(CONFORMANCE_FAILED, "conformance_failed", "unruled ACP method was accepted")
    except _Outcome as outcome:
        _emit(outcome.status, outcome.detail, stream=sys.stderr)
        return outcome.exit_code
    try:
        probe = probe_reference_harness(executable=executable)
    except ProtocolRefusal as exc:
        _emit("configuration_refused", exc.code, stream=sys.stderr)
        return CONFIGURATION_REFUSED
    _emit(
        "conformant",
        {"cases": 4, "harness_status": probe["status"]},
        stream=sys.stdout,
    )
    return CONFORMANT


def _emit(status: str, detail: object, *, stream: object) -> None:
    payload = {"status": status}
    if isinstance(detail, dict):
        payload.update(detail)
    else:
        payload["detail"] = detail
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=stream)


def _load_factory(specification: str) -> Callable[[FloatiRoot], object]:
    if specification.count(":") != 1:
        raise ProtocolRefusal("adapter_spec_invalid", "adapter must be module:factory")
    module_name, factory_name = specification.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, factory_name)
    except (ImportError, AttributeError) as exc:
        raise ProtocolRefusal("adapter_spec_invalid", "adapter factory could not be loaded") from exc
    if not callable(factory):
        raise ProtocolRefusal("adapter_spec_invalid", "adapter factory is not callable")
    return factory


class _ConfigurationParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ProtocolRefusal("arguments_invalid", message)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _ConfigurationParser(prog="python3 -m floati.conformance", add_help=True)
    parser.add_argument("--adapter")
    parser.add_argument("--root")
    parser.add_argument("--tenant")
    parser.add_argument("--live-root-smoke", action="store_true")
    parser.add_argument("--acp-fixture", action="store_true")
    parser.add_argument("--acp-executable")
    parser.add_argument("--call-timeout", type=float)
    try:
        args = parser.parse_args(argv)
        if args.acp_fixture:
            if (
                args.live_root_smoke
                or args.adapter is not None
                or args.root is not None
                or args.tenant is not None
                or args.call_timeout is not None
            ):
                raise ProtocolRefusal(
                    "arguments_invalid",
                    "ACP fixture cannot be combined with live-root smoke, adapter, root, tenant, or call timeout",
                )
            return run_acp_fixture_conformance(executable=args.acp_executable)
        if args.acp_executable is not None:
            raise ProtocolRefusal(
                "arguments_invalid",
                "ACP executable declaration requires --acp-fixture",
            )
        if args.live_root_smoke:
            if (
                args.adapter is not None
                or args.root is not None
                or args.tenant is not None
                or args.call_timeout is not None
            ):
                raise ProtocolRefusal(
                    "arguments_invalid",
                    "live-root smoke cannot be combined with adapter, root, tenant, or call timeout",
                )
            return run_live_root_smoke()
        if args.adapter is None or args.root is None or args.tenant is None:
            raise ProtocolRefusal("arguments_invalid", "adapter, root, and tenant are required")
        call_timeout = DEFAULT_CALL_TIMEOUT if args.call_timeout is None else args.call_timeout
        if not 0.01 <= call_timeout <= 60:
            raise ProtocolRefusal("call_timeout_invalid", "call timeout must be 0.01 through 60 seconds")
        base = FloatiRoot.open(Path(args.root), args.tenant)
        _load_factory(args.adapter)
        run_tenant = f"{base.tenant_id}-run-{secrets.token_hex(6)}"
        root = FloatiRoot.open(base.path, run_tenant)
    except ProtocolRefusal as exc:
        _emit("configuration_refused", exc.code, stream=sys.stderr)
        return CONFIGURATION_REFUSED
    try:
        adapter = _IsolatedAdapter(args.adapter, root, call_timeout)
    except Exception as exc:
        _emit("adapter_died", type(exc).__name__, stream=sys.stderr)
        return ADAPTER_DIED
    try:
        return run(adapter, root)
    finally:
        adapter.close()


if __name__ == "__main__":
    sys.exit(main())
