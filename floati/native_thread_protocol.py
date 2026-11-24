"""Bounded native identity channel; only the parent can bank opened testimony.

Terminal testimony remains pending until the process owner proves drainage and
physical cleanup. This channel neither completes work nor releases admission.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import math
from pathlib import Path
import re
import time
import unicodedata

from .adhoc_records import NATIVE_BINDING_FIELDS, UUID
from .errors import ProtocolRefusal
from .worker_bootstrap_protocol import BootstrapChannel, validate_native_thread_context
from .worker_errors import WorkerAdapterFailure

_PROVIDERS = {"codex": "codex_local", "claude": "claude_local", "pi": "pi_local"}
_EVENT_FIELDS = {"schema_version", "event", "binding", "provider_coordinate"}
_ACK_FIELDS = _EVENT_FIELDS | {"opened_record_id"}
_FAILURES = {
    "native_output_limit": "protocol_error",
    "native_protocol_limit": "protocol_error",
    "native_session_mismatch": "protocol_error",
    "native_session_unmaterialized": "protocol_error",
    "native_permission_unsupported": "protocol_error",
    "adhoc_lease_ended": "process_timeout",
    "adhoc_spawn_unknown": "process_died",
    "adhoc_physical_cleanup_unknown": "process_died",
}


def _refuse(code, detail):
    raise ProtocolRefusal(code, detail,
        remedy="retain native references and the exact parent-owned dispatch")


def _bounded_json(value):
    """Bound hostile Python data before recursion, copying or JSON encoding.

    Count a conservative UTF-8 JSON representation, including escaped controls,
    and bound container fan-out before extending the traversal stack.
    """
    budget, remaining_nodes = 65_536, 65_536
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        remaining_nodes -= 1
        if remaining_nodes < 0 or depth > 64:
            _refuse("native_protocol_limit", "native metadata exceeds its structural bound")
        kind = type(item)
        if kind is dict:
            if len(item) > budget // 2:
                _refuse("native_protocol_limit", "native metadata object exceeds its bound")
            budget -= 2 + max(0, len(item) - 1) + len(item)
            for key, child in item.items():
                if type(key) is not str:
                    _refuse("protocol_error", "native metadata keys must be strings")
                pending.append((key, depth + 1))
                pending.append((child, depth + 1))
        elif kind is list:
            if len(item) > budget:
                _refuse("native_protocol_limit", "native metadata array exceeds its bound")
            budget -= 2 + max(0, len(item) - 1)
            pending.extend((child, depth + 1) for child in item)
        elif kind is str:
            if len(item) > budget:
                _refuse("native_protocol_limit", "native metadata string exceeds its bound")
            try:
                # JSON adds one escape byte for quotes/backslashes and up to
                # five for controls. No encoded string exceeds the fixed cap.
                size = len(item.encode("utf-8", "strict")) + 2
            except UnicodeError:
                _refuse("protocol_error", "native metadata is not valid UTF-8")
            size += sum(5 if ord(c) < 32 else 1 if c in {'"', "\\"} else 0 for c in item)
            budget -= size
        elif item is None or kind is bool:
            budget -= 5
        elif kind is int:
            if item.bit_length() > 256:
                _refuse("native_protocol_limit", "native metadata integer exceeds its bound")
            budget -= len(str(item))
        elif kind is float:
            if not math.isfinite(item):
                _refuse("protocol_error", "native metadata numbers must be finite")
            budget -= len(repr(item))
        else:
            _refuse("protocol_error", "native metadata must be closed JSON data")
        if budget < 0:
            _refuse("native_protocol_limit", "native metadata exceeds its byte bound")


def identity_mode(adapter_kind):
    if type(adapter_kind) is not str or adapter_kind not in _PROVIDERS:
        _refuse("protocol_error", "native adapter has no reviewed identity mode")
    return "terminal_only" if adapter_kind == "claude" else "pre_prompt"


def _coordinate(value, adapter_kind):
    if type(value) is not dict or set(value) != {"provider", "session_id", "session_file"}:
        _refuse("protocol_error", "provider coordinate requires its closed fields")
    if value["provider"] != _PROVIDERS[adapter_kind]:
        _refuse("native_session_mismatch", "provider identity belongs to another adapter")
    session = value["session_id"]
    if (type(session) is not str or not session or len(session) > 512
        or any(unicodedata.category(c).startswith("C") for c in session)
        or len(session.encode("utf-8")) > 512):
        _refuse("protocol_error", "native session identifier exceeds its closed bound")
    if adapter_kind == "codex":
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", session) is None or value["session_file"] is not None:
            _refuse("protocol_error", "Codex identity requires UUIDv7 without a session file")
    elif adapter_kind == "claude":
        if value["session_file"] is not None:
            _refuse("protocol_error", "Claude identity has no session-file contract")
    else:
        path = value["session_file"]
        if (type(path) is not str or not path or len(path) > 4096
            or any(unicodedata.category(c).startswith("C") for c in path)
            or len(path.encode("utf-8")) > 4096
            or not Path(path).is_absolute() or str(Path(path)) != path
            or any(part in {".", ".."} for part in path.split("/"))):
            _refuse("protocol_error", "Pi identity requires an exact absolute saved file")
    return value


def _binding(context):
    return {field: deepcopy(context[field]) for field in NATIVE_BINDING_FIELDS}


def validate_native_event(context, event, *, adapter_kind, opened_coordinate, terminal_seen):
    _bounded_json(event)
    mode = identity_mode(adapter_kind)
    context = validate_native_thread_context(context)
    if type(event) is not dict or type(event.get("event")) is not str:
        _refuse("protocol_error", "native event must be one closed object")
    phase = event["event"]
    fields = _EVENT_FIELDS
    if phase == "terminal":
        fields = fields | {"outcome"}
        if "usage" in event:
            fields = fields | {"usage"}
        if event.get("outcome") == "failed":
            fields = fields | {"reason_code", "worker_failure"}
    if set(event) != fields or type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        _refuse("protocol_error", "native event requires its exact version-one fields")
    if phase not in {"opened", "terminal"} or terminal_seen:
        _refuse("protocol_error", "native event is outside the open/terminal sequence")
    if type(event["binding"]) is not dict or set(event["binding"]) != NATIVE_BINDING_FIELDS:
        _refuse("protocol_error", "native event requires its exact parent binding")
    if event["binding"] != _binding(context):
        _refuse("native_session_mismatch", "native event belongs to another parent binding")
    coordinate = _coordinate(event["provider_coordinate"], adapter_kind)
    if "usage" in event:
        from .native_usage import validate_usage
        validate_usage(event["usage"], adapter_kind, _refuse)
    if context["operation"] == "resume" and coordinate != context["provider_coordinate"]:
        _refuse("native_session_mismatch", "native resume changed its durable provider coordinate")
    if opened_coordinate is not None and coordinate != opened_coordinate:
        _refuse("native_session_mismatch", "native terminal changed its opened provider coordinate")
    if phase == "opened":
        if mode != "pre_prompt" or opened_coordinate is not None:
            _refuse("protocol_error", "native opened identity is duplicate or unsupported")
    else:
        if mode == "pre_prompt" and opened_coordinate is None:
            _refuse("protocol_error", "native terminal preceded its durable opened identity")
        if type(event["outcome"]) is not str or event["outcome"] not in {"success", "failed"}:
            _refuse("protocol_error", "native terminal outcome is outside its closed contract")
        if event["outcome"] == "failed":
            reason = event["reason_code"]
            if type(reason) is not str or _FAILURES.get(reason) != event["worker_failure"]:
                _refuse("protocol_error", "native failure reason and worker classification differ")
    return deepcopy(event)


class NativeThreadProtocol:
    """Exact real parent owners authenticate each native event under the car guard."""
    def __init__(self, context, *, adapter_kind, journal_controller, effect_controller, now=None):
        from .adhoc_journal import AdhocJournalController
        from .effects import EffectController
        _bounded_json(context)
        self.context = validate_native_thread_context(context)
        identity_mode(adapter_kind)
        self.adapter_kind = adapter_kind
        if type(journal_controller) is not AdhocJournalController:
            _refuse("adhoc_dispatch_authentication_failed", "native protocol requires the exact journal controller")
        self.journal_controller = journal_controller
        self.journal = journal_controller.journal
        self.journal._capability_for(journal_controller)
        if type(effect_controller) is not EffectController:
            _refuse("effect_controller_invalid", "native protocol requires the exact EffectController")
        self.effects = effect_controller
        self.root = self.journal.root
        self.__journal_owner = (journal_controller, self.journal, self.root)
        self.__effect_owners = (effect_controller, effect_controller.ledger,
            effect_controller.run_ledger, effect_controller.approvals)
        self.__acceptance = None
        self.opened_coordinate = None
        self.terminal_seen = False
        admission, _current = self._authenticate(now)
        # Start before provider launch so retries never extend the dispatch's
        # requested execution interval. Lease/authority are refreshed per try.
        self.__accept_deadline = time.monotonic() + float(admission["dispatch"]["requested_duration"])

    def _validate_owners(self):
        from .adhoc_journal import AdhocJournal, AdhocJournalController
        from .adhoc_profiles import AdhocProfiles
        from .approvals import ApprovalLedger
        from .effects import EffectController, EffectLedger
        from .root import FloatiRoot
        from .runtruth import RunLedger
        writer, journal, root = self.__journal_owner
        if (type(self.journal_controller) is not AdhocJournalController
            or type(self.journal) is not AdhocJournal
            or type(self.root) is not FloatiRoot
            or self.journal_controller is not writer or self.journal is not journal
            or self.root is not root or self.journal_controller.journal is not journal
            or self.journal.root is not root
            or type(self.journal.profiles) is not AdhocProfiles
            or self.journal.profiles.root is not root):
            _refuse("adhoc_dispatch_authentication_failed", "native journal owner type, root or reciprocal binding changed")
        self.journal._capability_for(self.journal_controller)
        if (type(self.effects) is not EffectController
            or type(self.effects.ledger) is not EffectLedger
            or type(self.effects.run_ledger) is not RunLedger
            or type(self.effects.approvals) is not ApprovalLedger):
            _refuse("effect_controller_invalid", "native effect owner types changed")
        owner_roots = (self.effects.ledger.root, self.effects.run_ledger.root, self.effects.approvals.root)
        if any(type(owner) is not FloatiRoot or
               (owner.path, owner.tenant_home, owner.tenant_id) != (root.path, root.tenant_home, root.tenant_id)
               for owner in owner_roots):
            _refuse("effect_root_mismatch", "native parent owners belong to different roots")
        if any(current is not original for current, original in zip(
            (self.effects, self.effects.ledger, self.effects.run_ledger, self.effects.approvals), self.__effect_owners)):
            _refuse("effect_controller_invalid", "native effect controller ownership changed")

    @contextmanager
    def _acceptance_scope(self, now):
        from . import adhoc_journal
        from .adhoc_profiles import current_time
        from .bus_epoch import epoch_guard
        from .planes import AuthorityGrantStore, _existing_cas_read_lock
        from .runtruth import effect_acceptance_guard
        self._validate_owners()
        if self.__acceptance is not None:
            _refuse("adhoc_dispatch_authentication_failed", "native acceptance scope is already active")
        current = current_time(now)
        # Use the existing owners' fences, in their normal acquisition order.
        # The shared authority CAS blocks its exact grant/revoke writer. The
        # Effect fence blocks Run transitions through the opened append.
        with epoch_guard(self.root), adhoc_journal.car_acceptance_guard(self.root):
            admission = self.journal.show(self.context["dispatch_id"])
            with self.journal.profiles.validated(self.context["profile_id"], now=current) as bound:
                path = AuthorityGrantStore(self.root).path_for(admission["dispatch"]["authority_subject"])
                with _existing_cas_read_lock(path), effect_acceptance_guard(self.root):
                    self.__acceptance = (bound, current)
                    try:
                        yield
                    finally:
                        self.__acceptance = None

    def _refresh_acceptance(self, admission, now):
        """Check live expiry at the decision boundary without retaking its locks."""
        from .adhoc_profiles import current_time
        self._validate_owners()
        if self.__acceptance is None:
            _refuse("adhoc_dispatch_authentication_failed", "native decision requires retained acceptance guards")
        (profile, binding), _observed = self.__acceptance
        current = current_time(now)
        self.journal.profiles._lease(profile["node_id"], current, expected=profile["node_lease_id"])
        self.journal._require_work(profile, binding, admission["dispatch"], self.context["node_id"], current, allow_claimed=True)
        return current


    def _authenticate(self, now):
        self._validate_owners()
        if self.__acceptance is None:
            with self._acceptance_scope(now):
                return self._authenticate(now)
        context = self.context
        (profile, binding), _observed = self.__acceptance
        admission = self.journal.show(context["dispatch_id"])
        if (any(admission[field] != context[field] for field in
                ("profile_id", "profile_digest", "dispatch_digest"))
            or admission["actor"] != context["node_id"]):
            _refuse("adhoc_dispatch_authentication_failed", "native context differs from durable admission")
        dispatch = admission["dispatch"]
        if any(dispatch[field] != context[field] for field in
               ("work_item_id", "node_lease_id", "operation", "permission")):
            _refuse("adhoc_dispatch_authentication_failed", "native context differs from authenticated dispatch")
        expected = {"profile_id": profile["id"], "lane_opening_id": profile["lane_opening_id"],
            "node_lease_id": profile["node_lease_id"], "workspace": profile["workspace"]}
        slot = {"path": str(self.root.tenant_home / profile["provider_state"]),
                **profile["filesystem_bindings"]["provider_state"]}
        if (profile["digest"] != context["profile_digest"] or profile["adapter"] != self.adapter_kind
            or context["workspace_binding"] != expected or context["provider_state_binding"] != slot):
            _refuse("adhoc_binding_changed", "native profile, workspace or provider slot changed")
        self.journal._authenticate(profile, binding, admission["envelope_id"], dispatch, context["node_id"])
        current = self._refresh_acceptance(admission, now)
        effect = context["effect_binding"]
        projection = self.effects.run_ledger._project_under_effect_acceptance_guard()
        run = projection.run(effect["run_id"])
        attempt = run["attempts"].get(effect["attempt_id"])
        chosen = run["dispatches"].get(effect["attempt_id"])
        if (attempt is None or chosen is None or attempt["opened"].get("item_id") != effect["item_id"]
            or chosen.get("chosen_worker") != context["node_id"] or chosen.get("adapter") != self.adapter_kind):
            _refuse("effect_launch_binding_invalid", "native event differs from the exact parent dispatch")
        projection.effect_intent_context(effect["run_id"], effect["item_id"],
            effect["attempt_id"], attempt["opened"]["fence_token"])
        return admission, current

    def accept(self, event, *, now=None):
        from .adhoc_profiles import timestamp
        from .ids import uuid7_hex
        from .waiter_lock_retry import transient_lock_timeout
        # Validate before taking locks or touching durable truth.
        event = validate_native_event(self.context, event, adapter_kind=self.adapter_kind,
            opened_coordinate=self.opened_coordinate, terminal_seen=self.terminal_seen)
        # A different sender may be banking a result under the epoch barrier.
        # Retry only the parent acquisition; preserve this exact bound callback
        # and its sealed journal producer instead of wrapping/relaunching it.
        deadline = min(time.monotonic() + 30, self.__accept_deadline)
        while True:
            try:
                with self._acceptance_scope(now):
                    admission, current = self._authenticate(now)
                    if event["event"] == "terminal":
                        self.terminal_seen = True
                        return {"state": "terminal_pending_cleanup", "event": deepcopy(event)}
                    def decide(rows):
                        admissions, opened = self.journal._project_history(rows)
                        if admissions.get(self.context["dispatch_id"]) != admission:
                            _refuse("adhoc_dispatch_authentication_failed", "native admission changed before opened commit")
                        # Refresh expiry after acquisition, including a retry.
                        current = self._refresh_acceptance(admission, now)
                        existing = opened.get(self.context["dispatch_id"])
                        if existing is not None:
                            if existing["evidence"] != {"binding": event["binding"],
                                "provider_coordinate": event["provider_coordinate"], "identity_phase": "pre_prompt"}:
                                _refuse("native_session_mismatch", "a different provider coordinate already won admission")
                            record = existing
                            pending = None
                        else:
                            record = {"schema_version": 1, "kind": "adhoc_native_thread_opened_record",
                                "id": "adhoc-native-opened-" + uuid7_hex(), "tenant_id": self.root.tenant_id,
                                "timestamp": timestamp(current), "admission_id": admission["id"],
                                **{field: admission[field] for field in
                                   ("dispatch_id", "profile_id", "profile_digest", "dispatch_digest")},
                                "phase": "provider_opened", "evidence": {"binding": event["binding"],
                                    "provider_coordinate": event["provider_coordinate"], "identity_phase": "pre_prompt"}}
                            pending = record
                        ack = {"schema_version": 1, "event": "opened_ack", "binding": deepcopy(event["binding"]),
                            "provider_coordinate": deepcopy(event["provider_coordinate"]), "opened_record_id": record["id"]}
                        return ack, pending
                    ack = self.journal._transact_provider_opened(decide)
                    self.opened_coordinate = deepcopy(event["provider_coordinate"])
                    return ack
            except ProtocolRefusal as exc:
                if not transient_lock_timeout(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))


_NATIVE_PROTOCOL_ACCEPT_CODE = NativeThreadProtocol.accept.__code__


class NativeThreadEmitter:
    """Child-side ACK barrier using the owner's original absolute deadline."""
    def __init__(self, context, *, channel, deadline):
        try:
            _bounded_json(context)
            self.context = validate_native_thread_context(context)
        except ProtocolRefusal as exc:
            raise WorkerAdapterFailure(exc.code) from exc
        if type(channel) is not BootstrapChannel:
            raise WorkerAdapterFailure("protocol_error")
        try:
            valid_deadline = type(deadline) in {int, float} and math.isfinite(float(deadline))
        except (OverflowError, ValueError):
            valid_deadline = False
        if not valid_deadline:
            raise WorkerAdapterFailure("protocol_error")
        self.channel, self.deadline = channel, float(deadline)
        self.opened_coordinate, self.terminal_seen = None, False

    def emit(self, event):
        try:
            _bounded_json(event)
            coordinate = event.get("provider_coordinate") if type(event) is dict else None
            provider = coordinate.get("provider") if type(coordinate) is dict else None
            adapter = next((key for key, value in _PROVIDERS.items() if value == provider), None)
            event = validate_native_event(self.context, event, adapter_kind=adapter,
                opened_coordinate=self.opened_coordinate, terminal_seen=self.terminal_seen)
            self.channel.send(("native_thread", event), deadline=self.deadline)
            if event["event"] == "opened":
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                verb, ack = self.channel.recv(timeout=remaining)
                _bounded_json(ack)
                if (verb != "native_thread_ack" or type(ack) is not dict or set(ack) != _ACK_FIELDS
                    or type(ack["schema_version"]) is not int or ack["schema_version"] != 1
                    or ack["event"] != "opened_ack" or type(ack["opened_record_id"]) is not str
                    or re.fullmatch("adhoc-native-opened-" + UUID, ack["opened_record_id"]) is None):
                    _refuse("protocol_error", "native opened ACK has invalid fields or record identity")
                if ack["binding"] != event["binding"] or ack["provider_coordinate"] != event["provider_coordinate"]:
                    _refuse("native_session_mismatch", "native opened ACK substituted parent binding or identity")
                self.opened_coordinate = deepcopy(event["provider_coordinate"])
                return deepcopy(ack)
            self.terminal_seen = True
            return None
        except TimeoutError as exc:
            raise WorkerAdapterFailure("process_timeout") from exc
        except ProtocolRefusal as exc:
            raise WorkerAdapterFailure(exc.code) from exc
        except (OSError, EOFError) as exc:
            raise WorkerAdapterFailure("protocol_error") from exc
        except WorkerAdapterFailure as exc:
            if exc.code == "effect_worker_isolation_unavailable":
                raise WorkerAdapterFailure("protocol_error") from exc
            raise

    def opened(self, coordinate):
        return self.emit({"schema_version": 1, "event": "opened", "binding": _binding(self.context),
                          "provider_coordinate": coordinate})

    def terminal(self, coordinate, *, outcome="success", reason_code=None, worker_failure=None):
        event = {"schema_version": 1, "event": "terminal", "binding": _binding(self.context),
                 "provider_coordinate": coordinate, "outcome": outcome}
        if outcome == "failed":
            event.update(reason_code=reason_code, worker_failure=worker_failure)
        return self.emit(event)

    __call__ = emit
