"""Native car execution testimony; closed callbacks cannot select identity.

An invocation capability is an in-process object issued from a native source,
not serialized authority. Host adapters must authenticate their launch before
using a private binder. There is deliberately no public callback CLI, profile
operation, JSON 'authenticated' flag or elapsed-silence inference.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from weakref import WeakKeyDictionary
from contextlib import nullcontext

from .bus_epoch import observe_epoch_roll_state
from .car_fence import car_acceptance_guard
from .errors import IntegrityFailure, ProtocolRefusal
from .events import EVENT_KINDS, EventLog, validate_event_records
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, _transact_seat_execution_records
from .records import validate_record
from .registry import Registry, utc_now
from .snapshot import _owned_epoch_archives
from .work import WorkLog


class NativeInvocation:
    """Opaque, non-serializable invocation identity; construction grants nothing."""
    __slots__ = ("__weakref__",)


_ACTIVE = WeakKeyDictionary()
_PUBLICATIONS = WeakKeyDictionary()
_HOST_SOURCES = frozenset({"codex_host", "cursor_host", "cursor_tool", "desktop_host"})


class _ExecutionPublication:
    """Opaque companion; its constructor never grants publication authority."""
    def __init__(self, *ignored):
        pass

    def authorized(self, root):
        return self in _PUBLICATIONS and _PUBLICATIONS[self][0] == root.tenant_home

    def _row(self):
        if self not in _PUBLICATIONS:
            raise ProtocolRefusal("seat_execution_controller_only", "unissued execution companion", remedy='use the authenticated native producer; constructing a companion grants no authority')
        return _PUBLICATIONS[self][1]

    def companion(self, envelope):
        row = deepcopy(self._row())
        row.update(envelope_id=envelope["id"], timestamp=envelope["timestamp"])
        return row

    def matches(self, row):
        return all(row.get(key) == value for key, value in self._row().items()
                   if key not in {"id", "timestamp", "envelope_id"})


def _publication(root, row):
    companion = _ExecutionPublication()
    _PUBLICATIONS[companion] = (root.tenant_home, deepcopy(row))
    return companion


def _history(root, *, snapshot=False):
    """Native chronology; snapshot mode avoids lock creation under caller EPOCH."""
    # Follow verified receipt-owned lineage, never scan for arbitrary archives.
    if observe_epoch_roll_state(root)["classification"] != "absent":
        raise ProtocolRefusal("seat_execution_epoch_unknown", "epoch recovery is unresolved", remedy='ask the coordinator to reconcile the named native epoch recovery before producing execution testimony')
    batches = []
    for archive in reversed(_owned_epoch_archives(root)):
        relative = archive.relative_to(root.tenant_home) / "events.jsonl"
        if archive.is_file() and not archive.is_symlink():
            from .epoch_history import read_archived_bytes
            from .jsonl import _decode_path_records
            raw = read_archived_bytes(archive, "events.jsonl")
            batches.append([] if not raw else _decode_path_records(
                archive / "events.jsonl", root.tenant_id, frozenset(EVENT_KINDS), raw))
        else:
            batches.append(read_records_snapshot(root, relative, allowed_kinds=EVENT_KINDS))
    batches.append(EventLog(root).compatible_event_records(snapshot=True)[0]
                   if snapshot else EventLog(root).event_records())
    from .archive_execution_replay import _execution_history
    return _execution_history(batches)


def _validate_history(history):
    """Verify invocation continuity across receipt-owned archive boundaries.

    Source issuance is authenticated by native producers. This reader verifies
    the retained transcript; it does not reauthenticate historical source bytes.
    """
    from .archive_execution_replay import _validate_execution_continuity
    return _validate_execution_continuity(history)


class SeatExecutionController:
    def __init__(self, root, *, recipient=None):
        self.root = root
        self.registry = Registry(root)
        governance = self.registry.governance()
        self.recipient = recipient if recipient is not None else (
            None if governance is None else governance.coordinator)
        self._capabilities = WeakKeyDictionary()

    def _car(self, node, item_id=None):
        cars = [item for item in WorkLog(self.root).show()
                if item.get("car_binding") is not None and item["state"] == "claimed"
                and item["owner"] == item["holder"] == node]
        if item_id is not None:
            cars = [item for item in cars if item["id"] == item_id]
        if len(cars) > 1:
            raise ProtocolRefusal("seat_execution_car_ambiguous", "multiple owned claimed cars cannot select one invocation", remedy='ask the coordinator to resolve multiple owned claimed cars before starting an invocation')
        if not cars:
            raise ProtocolRefusal("seat_execution_car_absent", "one exact owned claimed car is required", remedy='claim one exact owned structured car before starting its native invocation')
        return cars[0]

    def _issue(self, *, node, session, workspace, invocation, source,
               receipt_id, source_pin, command, item_id=None, running=True, already_guarded=False,
               source_binding="absent_legacy"):
        with nullcontext() if already_guarded else car_acceptance_guard(self.root):
            self.registry.require_active(node)
            item = self._car(node, item_id)
            workspace = Path(workspace)
            if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace or not workspace.is_dir():
                raise ProtocolRefusal("seat_execution_workspace_invalid", "native workspace must be canonical and ordinary", remedy='start the native invocation in its canonical ordinary workspace')
            identity = {"node_id": node, "acting_session_id": session,
                "work_item_id": item["id"], "work_claim_id": item["claim_id"],
                "invocation_id": invocation, "source_kind": source,
                "source_receipt_id": receipt_id, "source_pin": source_pin,
                "workspace": str(workspace), "command": command,
                "source_binding": deepcopy(source_binding)}
            if source in _HOST_SOURCES:
                if running:
                    raise ProtocolRefusal("seat_execution_source_invalid", "host observations cannot mint running testimony",
                        remedy="issue the authenticated standalone host observation with running=False")
                host_event = {"codex_host": "permission_quiet", "cursor_host": "ended",
                              "cursor_tool": "tool_request_refused", "desktop_host": "ended"}[source]
                if source == "desktop_host" and isinstance(source_binding, dict) and source_binding.get("notification_type") == "permission_prompt":
                    host_event = "approval_pending"
                if source == "codex_host" and isinstance(source_binding, dict) and source_binding.get("hook_event_name") == "Interrupt":
                    host_event = "interrupted"
                probe_payload = {"event": host_event, "code": None, "detail": None}
            else:
                probe_payload = {"event": "running", "code": None, "detail": ""}
            probe = self._record(identity, probe_payload)
            validate_record(probe, self.root.tenant_id, {"seat_execution"}, integrity=False)
            capability = NativeInvocation()
            self._capabilities[capability] = deepcopy(identity)
        if running:
            self._accept(identity, {"event": "running", "code": None, "detail": ""}, already_guarded=already_guarded)
            _ACTIVE[capability] = (str(self.root.tenant_home), deepcopy(identity))
        return capability

    def bind_worker(self, receipt, workspace, *, run_id=None, attempt_id=None):
        with car_acceptance_guard(self.root):
            return self._bind_worker_already_guarded(receipt, workspace,
                run_id=run_id, attempt_id=attempt_id)

    def _bind_worker_already_guarded(self, receipt, workspace, *, run_id, attempt_id):
        from .workers import WorkerReceipts
        rows = WorkerReceipts(self.root).records()
        if not isinstance(receipt, dict) or receipt not in rows or receipt.get("transition") != "spawn":
            raise ProtocolRefusal("seat_execution_source_invalid", "worker binding needs an exact native spawn receipt", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        session_rows = [row for row in rows if row['session_id'] == receipt['session_id']]
        if not session_rows or session_rows[-1] != receipt:
            raise ProtocolRefusal("seat_execution_source_invalid", "worker binding must occur at its current native spawn boundary", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        binding = "absent_legacy"
        if run_id is not None or attempt_id is not None:
            if not isinstance(run_id, str) or not isinstance(attempt_id, str):
                raise ProtocolRefusal("seat_execution_source_invalid", "governed worker needs both native run and attempt selectors", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
            from .runtruth import RunLedger
            run = RunLedger(self.root).project().run(run_id)
            attempt = run["attempts"].get(attempt_id)
            dispatch = run["dispatches"].get(attempt_id)
            if attempt is None or dispatch is None or attempt["started"] is None or (
                attempt["terminal"] is not None
                or attempt["opened"]["item_id"] != receipt["work_item_id"]
                or dispatch["chosen_worker"] != receipt["node_id"]
                or dispatch["adapter"] != receipt["adapter"]
            ):
                raise ProtocolRefusal("seat_execution_source_invalid", "worker spawn does not match one started native attempt", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
            joins = run["harness_sessions"].get(attempt_id, [])
            if len(joins) != 1 or joins[0]["item_id"] != receipt["work_item_id"] or (
                joins[0]["worker_session_id"] != receipt["session_id"]
            ):
                raise ProtocolRefusal("seat_execution_source_invalid", "worker needs one exact native harness session join", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
            native = joins[0]
            binding = {"run_id": run_id, "attempt_id": attempt_id,
                "binding_record_id": native["id"], "attempt_binding": {
                    key: native[key] for key in ("attempt_id", "claim_id", "lease_id", "worker_session_id")}}
        return self._issue(node=receipt["node_id"], session=receipt["session_id"],
            workspace=workspace, invocation=receipt["id"], source="worker",
            receipt_id=receipt["id"], source_pin=sha256(Path(__file__).read_bytes()).hexdigest(),
            command="floati worker run " + receipt["adapter"], item_id=receipt["work_item_id"],
            source_binding=binding, already_guarded=True)

    def _bind_cursor_start_already_guarded(self, start):
        from .stop_hold import StopHoldLedger
        if not isinstance(start, dict) or start.get("harness") != "cursor" or start.get("phase") != "start":
            raise ProtocolRefusal("seat_execution_source_invalid", "Cursor failure needs its native hold start", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        rows = StopHoldLedger(self.root).read(start["node_id"])
        invocation_rows = [row for row in rows if row["invocation_id"] == start["invocation_id"]]
        if invocation_rows != [start]:
            raise ProtocolRefusal("seat_execution_source_invalid", "Cursor hold testimony is not owned by this root", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        from .codex_wait_contract import resolve_participant, CodexWaitConsentLedger, CodexWaitSessionLedger
        participant = resolve_participant(self.root.tenant_home, Path(start["workspace"]))
        if participant is None or participant.binding.node_id != start["node_id"]:
            raise ProtocolRefusal("seat_execution_source_invalid", "Cursor start has no current native workspace binding", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        claims = read_records_snapshot(self.root, CodexWaitSessionLedger._relative(start["node_id"]),
            allowed_kinds={"codex_wait_session_receipt"})
        claim = claims[-1] if claims else None
        if consent["id"] != start["consent_receipt_id"] or claim is None or (
            claim["acting_session_id"] != start["acting_session_id"]
            or claim["consent_receipt_id"] != consent["id"]
            or claim["workspace"] != start["workspace"]
        ):
            raise ProtocolRefusal("seat_execution_source_invalid", "Cursor start is not the current native session and consent", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        return self._issue(node=start["node_id"], session=start["acting_session_id"],
            workspace=Path(start["workspace"]), invocation=start["invocation_id"], source="cursor",
            receipt_id=start["id"], source_pin=sha256(Path(__file__).read_bytes()).hexdigest(),
            command="floati wake wait cursor", already_guarded=True)

    def _bind_gateway(self, node, session, workspace, *, command, source_pin, native_workspace=None):
        from .wake_hold import wake_coordination_guard
        with car_acceptance_guard(self.root), wake_coordination_guard(self.root, node):
            return self._bind_gateway_already_guarded(node, session, workspace,
                command=command, source_pin=source_pin, native_workspace=native_workspace)

    def _bind_gateway_already_guarded(self, node, session, workspace, *, command, source_pin, native_workspace=None):
        # Private adapter seam. The vendored gateway enters only after staged
        # manifest/profile/worktree authentication; runtime identity is then
        # checked against the actual native waiter binding, never ack --session.
        from .codex_wait_contract import resolve_participant, CodexWaitConsentLedger, CodexWaitSessionLedger
        mapped_workspace = Path(workspace if native_workspace is None else native_workspace)
        participant = resolve_participant(self.root.tenant_home, mapped_workspace)
        if participant is None or participant.binding.node_id != node:
            raise ProtocolRefusal("seat_execution_source_invalid", "gateway workspace has no exact native binding", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        from .wake_control import is_session_paused
        if is_session_paused(self.root, node, session):
            raise ProtocolRefusal("seat_execution_source_invalid", "gateway runtime is paused", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        rows = read_records_snapshot(self.root, CodexWaitSessionLedger._relative(node),
            allowed_kinds={"codex_wait_session_receipt"})
        claim = rows[-1] if rows else None
        if claim is None or claim["acting_session_id"] != session or claim["workspace"] != str(mapped_workspace) or claim["consent_receipt_id"] != consent["id"]:
            raise ProtocolRefusal("seat_execution_source_invalid", "gateway runtime is not the current native session", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        invocation = "gateway-" + uuid7_hex()
        return self._issue(node=node, session=session, workspace=workspace,
            invocation=invocation, source="gateway", receipt_id=invocation,
            source_pin=source_pin, command=command, already_guarded=True)

    def callback(self, capability, payload):
        return self._callback(capability, payload, already_guarded=False)

    def _callback_already_guarded(self, capability, payload):
        return self._callback(capability, payload, already_guarded=True)

    def _callback(self, capability, payload, *, already_guarded):
        if type(capability) is not NativeInvocation or capability not in self._capabilities:
            raise ProtocolRefusal("seat_execution_source_invalid", "callback has no native invocation capability", remedy='start from the current authenticated native producer boundary; do not replay historical source receipts')
        if not isinstance(payload, dict) or set(payload) != {"event", "code", "detail"}:
            raise ProtocolRefusal("seat_execution_callback_invalid", "callback may carry only event, code and detail", remedy='send only the native event, code and bounded detail through the retained invocation capability')
        identity = self._capabilities[capability]
        allowed = {"worker": {"ended", "unknown"}, "cursor": {"followup_failed", "unknown"},
                   "gateway": {"refused_action", "unknown"}, "codex_tool": {"refused_action"},
                   "codex_host": {"permission_quiet", "interrupted"}, "cursor_host": {"ended"},
                   "cursor_tool": {"tool_request_refused"}, "desktop_host": {"approval_pending", "ended"}}
        if payload["event"] not in allowed[identity["source_kind"]]:
            raise ProtocolRefusal("seat_execution_callback_unsupported", "native source cannot testify to that host outcome", remedy='retain UNKNOWN until this native source can testify to a supported outcome')
        _ACTIVE.pop(capability, None)
        result = self._accept(identity, payload, already_guarded=already_guarded)
        return result

    def _record(self, identity, payload):
        key_payload = {**identity, "event": payload["event"]}
        key = "seat-execution-" + sha256(json.dumps(key_payload, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return {"schema_version": 1, "id": "seat-execution-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": utc_now(), "kind": "seat_execution",
            **deepcopy(identity), **deepcopy(payload), "envelope_id": None, "idempotency_key": key}

    def _accept(self, identity, payload, *, already_guarded=False):
        row = self._record(identity, payload)
        validate_record(row, self.root.tenant_id, {"seat_execution"}, integrity=False)
        with nullcontext() if already_guarded else car_acceptance_guard(self.root):
            history = _history(self.root)
            for saved in history:
                if saved.get("kind") == "seat_execution" and saved["idempotency_key"] == row["idempotency_key"]:
                    if not _publication(self.root, row).matches(saved):
                        raise ProtocolRefusal("idempotency_conflict", "execution callback payload changed")
                    return self._result(saved, history)
            item = self._car(identity["node_id"], identity["work_item_id"])
            if item["claim_id"] != identity["work_claim_id"]:
                raise ProtocolRefusal("seat_execution_claim_stale", "native car claim generation changed", remedy='retain the historical result and start a fresh native invocation only for the current claimed car')
            if identity["source_kind"] in _HOST_SOURCES:
                latest = {}
                for saved in history:
                    if (saved.get("kind") == "seat_execution"
                        and saved["work_item_id"] == identity["work_item_id"]
                        and saved["work_claim_id"] == identity["work_claim_id"]):
                        latest[saved["invocation_id"]] = saved
                if any(saved["event"] == "running" for saved in latest.values()):
                    # Other producers and fork snapshots cannot share _ACTIVE.
                    # An unclosed durable start conservatively vetoes a NEW
                    # host outcome even when its publisher is unavailable.
                    raise ProtocolRefusal("seat_execution_running_unclosed", "the claimed car has unclosed native running testimony",
                        remedy="retain UNKNOWN until the native invocation durably closes; do not infer its end from publisher absence")
            prior = [r for r in history if r.get("kind") == "seat_execution"
                     and r["invocation_id"] == identity["invocation_id"]]
            if prior and any(prior[0][field] != value for field, value in identity.items()):
                raise ProtocolRefusal("idempotency_conflict", "native invocation identity changed")
            if prior and prior[-1]["event"] != "running":
                raise ProtocolRefusal("seat_execution_invocation_closed", "native invocation already ended", remedy='replay the exact accepted callback; a closed native invocation cannot accept a new outcome')
            _validate_history(history + [row])
            if payload["event"] == "running" or (
                payload["event"] == "unknown" and payload["code"] in {
                    "cursor_followup_returned", "gateway_action_returned"}
            ):
                def decide(rows):
                    validate_event_records(rows)
                    return row, (row,)
                durable = _transact_seat_execution_records(self.root, decide, allowed_kinds=EVENT_KINDS)
                return {"execution": durable, "notice": None}
            if self.recipient is None:
                raise ProtocolRefusal("seat_execution_coordinator_absent", "notification needs declared fleet coordination", remedy='have the fleet declare its coordinator before native execution notification')
            artifact = next(binding for binding in reversed(item["artifact_bindings"])
                if binding["repo"] == item["car_binding"]["repository"])
            # Event, envelope and readiness are one native EventLog batch.
            # Full refusal BODY remains in the event; mail is a safe preview.
            state = self._project_item(item, history + [row])["state"]
            if identity["source_kind"] in _HOST_SOURCES:
                binding = identity["source_binding"]
                if identity["source_kind"] == "desktop_host":
                    fact = (binding["harness"] + " " + binding["notification_type"]
                        + " observed at " + binding["observed_at"])
                    if binding["native_prompt_id"] is None:
                        fact += "; session scoped; native turn identity absent"
                elif payload["event"] == "permission_quiet":
                    tool_preview = json.dumps(binding["tool_name"], ensure_ascii=True)[1:-1][:256]
                    fact = ("permission requested at " + binding["observed_at"] + " for "
                            + tool_preview + "; no later host event recorded for this session")
                elif payload["event"] == "interrupted":
                    fact = "turn interrupted at " + binding["observed_at"]
                elif payload["event"] == "tool_request_refused":
                    fact = "tool request refused at " + binding["observed_at"]
                else:
                    fact = "Cursor stop " + binding["status"] + "; loop_count=" + str(binding["loop_count"])
                note = ("Car " + item["car_binding"]["row"] + ": FACT; " + fact)[:1024]
            else:
                preview = json.dumps(payload["detail"], ensure_ascii=True)
                command_preview = ""
                if identity["source_kind"] == "codex_tool":
                    # Reserve space for both fields regardless of escaped size.
                    command_preview = "command=" + json.dumps(identity["command"], ensure_ascii=True)[:256] + "; "
                    preview = preview[:384]
                note = ("Car " + item["car_binding"]["row"] + ": " + state + "; "
                        + payload["event"] + "; " + str(payload["code"]) + "; " + command_preview + preview)[:1024]
            notice = EventLog(self.root).send(identity["node_id"], self.recipient,
                **artifact, note=note, idempotency_key=row["idempotency_key"],
                worker_session_id=(identity["acting_session_id"] if identity["source_kind"] == "worker" and isinstance(identity["source_binding"], dict) else None),
                attempt_binding=(identity["source_binding"]["attempt_binding"] if identity["source_kind"] == "worker" and isinstance(identity["source_binding"], dict) else None),
                _execution_publication=_publication(self.root, row))
            return self._result(next(r for r in EventLog(self.root).event_records()
                if r.get("kind") == "seat_execution" and r["idempotency_key"] == row["idempotency_key"]),
                EventLog(self.root).event_records())

    def _result(self, row, history):
        from .archive_execution_replay import _execution_result
        return _execution_result(row, history)

    def _project_item(self, item, history):
        rows = [r for r in history if r.get("kind") == "seat_execution"
                and r["work_item_id"] == item["id"] and r["work_claim_id"] == item["claim_id"]]
        latest = {}
        for row in rows:
            latest[row["invocation_id"]] = row
        state, reason = "UNKNOWN", "execution_testimony_absent"
        unclosed = [r for r in latest.values() if r["event"] == "running"]
        if unclosed:
            live = {identity["invocation_id"] for root, identity in list(_ACTIVE.values())
                    if root == str(self.root.tenant_home) and identity["work_claim_id"] == item["claim_id"]}
            state = "RUNNING" if any(r["invocation_id"] in live for r in unclosed) else "UNKNOWN"
            reason = "native_running_testimony" if state == "RUNNING" else "unclosed_native_invocation"
        elif rows:
            last = rows[-1]
            state = "STALLED" if last["event"] in {"ended", "followup_failed"} else "UNKNOWN"
            reason = last["event"] if last["source_kind"] in _HOST_SOURCES else last["code"]
        return {"state": state, "reason": reason, "work_item_id": item["id"],
                "work_claim_id": item["claim_id"], "car_binding": item["car_binding"],
                "last_execution_activity": rows[-1]["timestamp"] if rows else None,
                "latest_execution": rows[-1] if rows else None}

    def project(self, node):
        from .host_observations import host_approval_projection
        from .host_callback_support import desktop_callback_support
        try:
            harness = self.registry.require_active(node)["role"]
        except (IntegrityFailure, ProtocolRefusal, OSError):
            harness = "unknown"
        return {**self._project_execution(node),
                "host_approval": host_approval_projection(self.root, node),
                "host_callbacks": desktop_callback_support(harness)}

    def _project_execution(self, node):
        empty = {"state": "NO_CAR", "reason": "no_owned_claimed_car",
                 "last_execution_activity": None, "latest_execution": None}
        try:
            with car_acceptance_guard(self.root, observe=True) as present:
                try:
                    item = self._car(node)
                except ProtocolRefusal as exc:
                    if exc.code == "seat_execution_car_absent":
                        return empty
                    raise
                if not present:
                    return {**empty, "state": "UNKNOWN", "reason": "seat_execution_fence_absent"}
                return self._project_item(item, _history(self.root))
        except (IntegrityFailure, ProtocolRefusal, OSError) as exc:
            return {**empty, "state": "UNKNOWN", "reason": getattr(exc, "code", "execution_read_unavailable")}


def _report_cursor_failure(controller, invocation, error):
    """Called after native waiter guards release; no guessed root or session."""
    if controller is None or invocation is None:
        return None
    return controller.callback(invocation, {"event": "followup_failed",
        "code": "cursor_followup_publication_failed", "detail": type(error).__name__ + ": " + str(error)})
