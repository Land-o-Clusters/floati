"""Automatic live-epoch maintenance at validated native activity boundaries.

Observations are bounded hints. Only the transaction's fresh EX-owned planner
may authorize a rotation; no hint, caller clock, or manual grant is authority.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import functools
import inspect
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading

from .errors import IntegrityFailure, ProtocolRefusal
from .root import FloatiRoot

TRIGGERS = frozenset({"send", "present", "drain", "retract", "ack", "stop_poll"})
_ACTORS = {"send": "sender", "present": "recipient", "drain": "recipient",
           "retract": "author", "ack": "recipient"}
# Derived hint storage. Names end in _CACHE so the memo key does not treat
# an updated hint as a rebind of an input.
_OBSERVATIONS_CACHE = OrderedDict()
_NONRECLAIMABLE_CACHE = OrderedDict()
_CACHE_LOCK = threading.RLock()
_LOCAL = threading.local()
_CACHE_PID = os.getpid()
_MAX_OBSERVATIONS = 128


def _stamp(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _moment(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("epoch observation requires an aware datetime")
    return value.astimezone(timezone.utc)


def _signature(path, *, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or (
            not directory and info.st_nlink != 1):
        raise IntegrityFailure("epoch_rotation_path_invalid", "epoch observation path is not ordinary")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _policy_signature(path):
    """Observe cache identity; native ledger_roll_policy owns admissibility."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _first_stamp(root, payload):
    from .events import EVENT_KINDS
    from .jsonl import _decode_path_records
    if not payload:
        return None
    boundary = payload.find(b"\n")
    first = payload if boundary < 0 else payload[:boundary + 1]
    path = root.tenant_home / "events.jsonl"
    try:
        rows = _decode_path_records(path, root.tenant_id, EVENT_KINDS, first, unrecognized={})
    except Exception as failure:
        # This exact locally derived child can identify its retained parent;
        # only the transaction's original selected read can add a file witness.
        try:
            if getattr(failure, "_waiter_payload_source", None) == (path, id(first)):
                failure._waiter_payload_source = (path, id(payload))
        except Exception:
            pass
        raise
    # A compatible future kind does not establish an age anchor for this reader.
    if not rows:
        return None
    return _moment(datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00")))


def _first_frame(root, input_stat):
    from .jsonl import MAX_RECORD_BYTES
    path = root.tenant_home / "events.jsonl"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return b""
    try:
        before = os.fstat(descriptor)
        input_stat[0] = before
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise IntegrityFailure("epoch_rotation_path_invalid", "event hint is not ordinary")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            frame = stream.readline(MAX_RECORD_BYTES + 1)
        if len(frame) > MAX_RECORD_BYTES:
            raise IntegrityFailure("record_too_large", "events.jsonl line 1 exceeds its native byte bound")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise IntegrityFailure("epoch_rotation_observation_unstable", "event hint changed during observation")
        return frame
    finally:
        os.close(descriptor)


def _observed_start(root):
    from .jsonl import _failure_input_testimony
    frame = None
    try:
        with _failure_input_testimony(root.tenant_home / "events.jsonl") as input_stat:
            frame = _first_frame(root, input_stat)
            return _first_stamp(root, frame)
    except Exception as failure:
        # Preserve the original bounded read's fstat, never a post-failure
        # baseline: repair may already have replaced these exact bad bytes.
        if getattr(failure, "waiter_input_identity", None) is None:
            failure.waiter_input_identity = {"path": "events.jsonl"}
        if (isinstance(frame, bytes) and isinstance(failure, IntegrityFailure)
                and failure.code == "incomplete_jsonl_line"):
            # The bounded first physical frame supplies this offset directly;
            # no cursor cache or post-failure filesystem sample is involved.
            failure.waiter_byte_offset = frame.rfind(b"\n") + 1
            identity = getattr(failure, "waiter_input_identity", None)
            if isinstance(identity, dict) and identity.get("path") == "events.jsonl":
                identity["byte_offset"] = failure.waiter_byte_offset
        raise


def _reasons(size, started, policy, now):
    result = []
    if size >= policy["max_bytes"]:
        result.append("bytes")
    if started is not None and (now - started).total_seconds() >= policy["max_age_days"] * 86400:
        result.append("age")
    return result


def _maintenance_reasons(size, started, policy, now):
    reasons = _reasons(size, started, policy, now)
    if reasons and started is not None and started > now:
        raise ProtocolRefusal("epoch_rotation_clock_invalid", "current clock precedes the native epoch",
            remedy="preserve the native epoch and restore the system clock before automatic maintenance")
    return reasons


def _invalidate(root):
    with _CACHE_LOCK:
        _OBSERVATIONS_CACHE.pop(str(root.tenant_home), None)
        _NONRECLAIMABLE_CACHE.pop(str(root.tenant_home), None)


@contextmanager
def _observation_scope(root):
    """Exclude native append/rotation without creating observational state."""
    from .bus_epoch import EpochAcquisitionAbandoned
    lock = root.tenant_home / "events.jsonl.lock"
    while True:
        with ExitStack() as scope:
            try:
                scope.enter_context(_observation_barrier(root, lock))
            except EpochAcquisitionAbandoned:
                # A native writer created the ledger lock while this pristine
                # observation waited for EX: share that lock instead of
                # waiting behind every later shared holder.
                continue
            yield
            return


@contextmanager
def _observation_barrier(root, lock):
    from .bus_epoch import abandon_contended_acquisition, epoch_guard, without_epoch_turnstile_creation
    from .jsonl import _durability_failure, _locked_path
    with epoch_guard(root):
        try:
            descriptor = os.open(lock, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            handle = None
        except OSError as exc:
            raise _durability_failure(exc, lock) from exc
        else:
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise IntegrityFailure(
                        "epoch_rotation_path_invalid", "epoch observation lock is not ordinary")
                handle = os.fdopen(descriptor, "rb")
                descriptor = None
            except OSError as exc:
                raise _durability_failure(exc, lock) from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if handle is not None:
            with handle, _locked_path(lock, exclusive=False, relative="events.jsonl.lock",
                                      _open_handle=lambda: handle):
                yield
            return
    # A pristine or legacy lockless root has no ledger lock to share. Release
    # SH before taking EX: all native writers are excluded, no lock is created,
    # and this bounded observation never upgrades an owned shared barrier.
    with ExitStack() as scope:
        with without_epoch_turnstile_creation(), abandon_contended_acquisition(lambda: os.path.lexists(lock)):
            scope.enter_context(epoch_guard(root, exclusive=True))
        yield


def observe_policy(root: FloatiRoot, *, now: datetime) -> dict:
    """Observe current events bytes/anchor age without scanning historical planes."""
    if not isinstance(root, FloatiRoot):
        raise TypeError("epoch observation requires a FloatiRoot")
    moment = _moment(now)
    with _observation_scope(root):
        return _observe_policy_locked(root, moment)


def _observe_policy_locked(root, moment):
    from .bus_epoch import ledger_roll_policy, observe_epoch_roll_state
    global _CACHE_PID
    home = root.tenant_home
    key = str(home)
    signatures = (_signature(home, directory=True), _signature(home / "events.jsonl"),
                  _policy_signature(home / "state/ledger-policy.json"))
    with _CACHE_LOCK:
        if _CACHE_PID != os.getpid():
            _OBSERVATIONS_CACHE.clear()
            _NONRECLAIMABLE_CACHE.clear()
            _CACHE_PID = os.getpid()
        cached = _OBSERVATIONS_CACHE.get(key)
    if cached is None or cached["signatures"] != signatures:
        # Root membership changes invalidate the marker hint. Never interpret a
        # provisional event prefix when native transition state is present.
        pending = (observe_epoch_roll_state(root)["classification"] != "absent"
                   if cached is None or cached["signatures"][0] != signatures[0] else False)
        if pending:
            return {"due": True, "recovery_required": True, "due_reasons": [],
                    "observed_bytes": signatures[1][4] if signatures[1] else 0,
                    "observed_at": _stamp(moment), "epoch_started_at": None}
        policy = (ledger_roll_policy(root)
                  if cached is None or cached["signatures"][2] != signatures[2] else cached["policy"])
        started = _observed_start(root)
        final = (_signature(home, directory=True), _signature(home / "events.jsonl"),
                 _policy_signature(home / "state/ledger-policy.json"))
        if final != signatures:
            raise IntegrityFailure("epoch_rotation_observation_unstable", "epoch hint changed during observation")
        cached = {"signatures": signatures, "policy": dict(policy), "started": started,
                  "size": signatures[1][4] if signatures[1] else 0}
        with _CACHE_LOCK:
            _OBSERVATIONS_CACHE[key] = cached
            _OBSERVATIONS_CACHE.move_to_end(key)
            while len(_OBSERVATIONS_CACHE) > _MAX_OBSERVATIONS:
                _OBSERVATIONS_CACHE.popitem(last=False)
    reasons = _reasons(cached["size"], cached["started"], cached["policy"], moment)
    return {"due": bool(reasons), "recovery_required": False, "due_reasons": reasons,
            "policy": dict(cached["policy"]), "observed_bytes": cached["size"],
            "observed_at": _stamp(moment),
            "epoch_started_at": None if cached["started"] is None else _stamp(cached["started"])}


def _nested(root):
    from .bus_epoch import _epoch_holds, _order_stack, _same_lock_domain, _scope_path
    key = _scope_path(root)
    return key in _epoch_holds() or any(_same_lock_domain(key, item.path) for item in _order_stack()) or (
        getattr(_LOCAL, "pid", None) == os.getpid() and key in getattr(_LOCAL, "roots", set()))


@contextmanager
def _scope(root):
    from .bus_epoch import _scope_path
    if getattr(_LOCAL, "pid", None) != os.getpid():
        _LOCAL.pid, _LOCAL.roots = os.getpid(), set()
    key = _scope_path(root)
    _LOCAL.roots.add(key)
    try:
        yield
    finally:
        _LOCAL.roots.discard(key)


def _active_actor(root, candidate):
    from .registry import Registry
    try:
        return str(Registry(root).require_active(candidate)["node_id"])
    except ProtocolRefusal as exc:
        if exc.code in {"unknown_node", "identifier_invalid", "node_invalid"}:
            return None
        raise


def _recover(root, actor, authority_check=None):
    from .bus_epoch import epoch_guard, reconcile_epoch_roll
    from .registry import Registry
    with epoch_guard(root, exclusive=True):
        Registry(root).require_active(actor)
        if authority_check is not None:
            authority_check()
        return reconcile_epoch_roll(root)


def _without_anchors(payloads):
    from .framing import decode_frames
    result = {}
    for relative, plane, payload in payloads:
        if relative == "events.jsonl":
            frames = payload.splitlines(keepends=True)
            retained = []
            for frame in frames:
                records = decode_frames(frame)
                if not records or records[0].get("kind") not in {
                        "bus_epoch_roll_receipt", "seat_teardown_epoch_verification", "epoch_carry_verification"}:
                    retained.append(frame)
            payload = b"".join(retained)
        if payload or relative == "events.jsonl":
            result[relative] = (plane, payload)
    return result


def _nonreclaimable_hint(root):
    """Suppress only a previously proved no-op, never authorize a mutation."""
    from .epoch_carry import carry_dependency_witness
    if _CACHE_PID != os.getpid():
        return False
    key = str(root.tenant_home)
    with _CACHE_LOCK:
        cached = _NONRECLAIMABLE_CACHE.get(key)
    if cached is None:
        return False
    try:
        witness = carry_dependency_witness(root)
        if witness is None:
            _invalidate(root)
            return False
        current = (witness, _policy_signature(root.tenant_home / "state/ledger-policy.json"))
    except (OSError, IntegrityFailure, ProtocolRefusal):
        _invalidate(root)
        return False
    return current == cached


def _maintenance(root, *, actor, trigger, authority_check=None, retain=False):
    from .bus_epoch import ledger_roll_policy, roll_automatic_epoch, _require_owned_epoch_guard
    from .archive_maintenance_authority import _native_context
    from .epoch_carry import build_carry_plan, carry_dependency_witness
    from .epoch_teardown import collect_verifications
    if trigger not in TRIGGERS:
        raise ValueError("unknown native epoch trigger")
    if _nested(root):
        return {"changed": False, "action": "nested"}
    if _nonreclaimable_hint(root):
        return {"changed": False, "action": "nonreclaimable"}
    selected = _active_actor(root, actor)
    if selected is None:
        return {"changed": False, "action": "inactive"}
    outcome = {"changed": False, "action": "not_due"}
    with _native_context(root, actor=selected, trigger=trigger, authority_check=authority_check) as (admission, recovered):
        if recovered is not None:
            _invalidate(root)
            return {"changed": True, "action": "recovered"}
        def planner(current, old_payloads):
            _require_owned_epoch_guard(current, exclusive=True)
            if authority_check is not None:
                authority_check()
            policy = ledger_roll_policy(current)
            observed = datetime.now(timezone.utc)
            events = next(payload for relative, _plane, payload in old_payloads if relative == "events.jsonl")
            started = _first_stamp(current, events)
            reasons = _maintenance_reasons(len(events), started, policy, observed)
            if not reasons:
                return None
            witness = carry_dependency_witness(current)
            carry = build_carry_plan(current, old_payloads)
            teardown_seeds = collect_verifications(current, old_payloads)
            from .framing import decode_frames
            anchored = bool(events) and decode_frames(events.splitlines(keepends=True)[0])[0].get(
                "kind") == "bus_epoch_roll_receipt"
            if anchored and _without_anchors(old_payloads) == _without_anchors(carry.live_payloads):
                outcome["action"] = "nonreclaimable"
                after = carry_dependency_witness(current)
                if witness is not None and after == witness:
                    with _CACHE_LOCK:
                        _NONRECLAIMABLE_CACHE[str(current.tenant_home)] = (
                            witness, _policy_signature(current.tenant_home / "state/ledger-policy.json"))
                        _NONRECLAIMABLE_CACHE.move_to_end(str(current.tenant_home))
                        while len(_NONRECLAIMABLE_CACHE) > _MAX_OBSERVATIONS:
                            _NONRECLAIMABLE_CACHE.popitem(last=False)
                return None
            metadata = {"trigger": trigger, "policy": dict(policy), "observed_bytes": len(events),
                "observed_at": _stamp(observed), "epoch_started_at": None if started is None else _stamp(started),
                "due_reasons": reasons}
            return carry.live_payloads, metadata, teardown_seeds
        if retain:
            from .archive_retention import retain_rolled_epochs
            retain_rolled_epochs(root, admission, datetime.now(timezone.utc), actor=selected, trigger=trigger)
        if trigger == "send":
            from .send_prefix import large_pending_only_epoch
            if large_pending_only_epoch(root):
                size = (root.tenant_home / "events.jsonl").stat().st_size
                reasons = _maintenance_reasons(size, _observed_start(root), ledger_roll_policy(root), datetime.now(timezone.utc))
                return {"changed": False, "action": "nonreclaimable"} if reasons else outcome
        result = roll_automatic_epoch(root, actor=selected, planner=planner)
        if isinstance(result, dict) and isinstance(result.get("receipt"), dict):
            _invalidate(root)
            from .wake_hints import publish_root_hint
            publish_root_hint(root)
            return {"changed": True, "action": "rotated", "receipt": result["receipt"]}
        if outcome.get("action") == "nonreclaimable":
            _remember_nonreclaimable(root)
    return outcome


def _remember_nonreclaimable(root):
    """Rebind the no-op cache after retention changes archive identity."""
    from .epoch_carry import carry_dependency_witness
    try:
        witness = carry_dependency_witness(root)
    except (OSError, IntegrityFailure, ProtocolRefusal):
        _invalidate(root)
        return
    if witness is None:
        _invalidate(root)
        return
    with _CACHE_LOCK:
        _NONRECLAIMABLE_CACHE[str(root.tenant_home)] = (
            witness, _policy_signature(root.tenant_home / "state/ledger-policy.json"))
        _NONRECLAIMABLE_CACHE.move_to_end(str(root.tenant_home))
        while len(_NONRECLAIMABLE_CACHE) > _MAX_OBSERVATIONS:
            _NONRECLAIMABLE_CACHE.popitem(last=False)


def maintenance(root: FloatiRoot, *, actor: str, trigger: str) -> dict:
    """Maintain a native operation's registered self without a manual grant."""
    return _maintenance(root, actor=actor, trigger=trigger, retain=True)


def stop_maintenance(root: FloatiRoot, *, actor: str, check_authority) -> dict:
    """Only the native Stop path supplies its existing consent/session checker."""
    if _nested(root):
        return {"changed": False, "action": "nested"}
    hint = observe_policy(root, now=datetime.now(timezone.utc))
    if not hint["due"]:
        return {"changed": False, "action": "not_due"}
    if _nonreclaimable_hint(root):
        return {"changed": False, "action": "nonreclaimable"}
    check_authority()
    return _maintenance(root, actor=actor, trigger="stop_poll", authority_check=check_authority, retain=True)


def _report_maintenance_failure(trigger, result, failure):
    """A failed maintenance attempt cannot retract the native operation result."""
    try:
        code = getattr(failure, "code", None)
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            code = "epoch_maintenance_unavailable"
        ids = []
        values = result if isinstance(result, tuple) else (result,)
        for value in values:
            if not isinstance(value, dict):
                continue
            message = value.get("message")
            selected = message if isinstance(message, dict) else value
            identifier = selected.get("id")
            if isinstance(identifier, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,127}", identifier):
                ids.append(identifier)
        sys.stderr.write(json.dumps({"command": "epoch-maintenance", "status": "maintenance_failed",
            "code": code, "type": type(failure).__name__, "trigger": trigger,
            "operation_receipt_ids": ids, "operation_result_preserved": True}, sort_keys=True) + "\n")
        sys.stderr.flush()
    except Exception:
        # Neither unavailable diagnostics nor maintenance can undo a commit.
        return


def automatic_epoch_operation(trigger):
    """Release hint locks before the native act and any later EX maintenance."""
    if trigger not in _ACTORS:
        raise ValueError("unknown native activity boundary")
    def decorate(operation):
        native_operation = inspect.unwrap(operation)
        @functools.wraps(operation)
        def run(owner, *args, **kwargs):
            bound = inspect.getcallargs(native_operation, owner, *args, **kwargs)
            root = owner.root
            if _nested(root):
                return operation(owner, *args, **kwargs)
            # Invalid policy or integrity fails before the durable native act.
            hint = observe_policy(root, now=datetime.now(timezone.utc))
            try:
                result = operation(owner, *args, **kwargs)
            except ProtocolRefusal as exc:
                if exc.code != "epoch_roll_recovery_required":
                    raise
                actor = _active_actor(root, bound[_ACTORS[trigger]])
                if actor is None:
                    raise
                if trigger == "send":
                    from .registry import Registry
                    Registry(root).resolve_node_id(bound["recipient"], field="recipient")
                with _scope(root):
                    _recover(root, actor)
                _invalidate(root)
                # Repair only an existing transaction. Retry the unchanged
                # original operation once; never retry another refusal.
                result = operation(owner, *args, **kwargs)
            if hint["due"]:
                try:
                    actor = _active_actor(root, bound[_ACTORS[trigger]])
                    if actor is not None:
                        maintenance(root, actor=actor, trigger=trigger)
                except Exception as exc:
                    _invalidate(root)
                    _report_maintenance_failure(trigger, result, exc)
            return result
        return run
    return decorate


# Frozen provenance for the separate, ephemeral archive authority context.
_MAINTENANCE_BODY_CODE = _maintenance.__code__
_MAINTENANCE_GLOBALS = globals()
