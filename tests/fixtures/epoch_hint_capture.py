"""Bounded metadata capture for the existing native send atomicity witness."""
from contextlib import ExitStack, contextmanager
import json
import os
import sys
import threading
from unittest import mock

from floati import epoch_rotation


FIELDS = ("dev", "ino", "mode", "nlink", "size", "mtime_ns", "ctime_ns")
INNER_COMPARED_FIELDS = ("dev", "ino", "size", "mtime_ns", "ctime_ns")
MAX_OBSERVATIONS = 4
MAX_DIAGNOSTIC_BYTES = 8192


def _stat_tuple(info):
    return tuple(getattr(info, "st_" + name) for name in FIELDS)


def _sample(value):
    if value is None:
        return {"state": "absent"}
    if not isinstance(value, tuple) or len(value) != len(FIELDS):
        return {"state": "diagnostic_failure", "reason": "native_return_shape"}
    return {"state": "present", "fields": dict(zip(FIELDS, value))}


def _pair(samples, *, overflow=False, compared=FIELDS):
    result = {"before": {"state": "not_sampled"}, "after": {"state": "not_sampled"},
              "comparison": "not_sampled", "changed_fields": [], "overflow": overflow}
    for index, name in enumerate(("before", "after")):
        if len(samples) > index:
            result[name] = _sample(samples[index])
    if overflow:
        result["comparison"] = "incomplete_overflow"
    elif len(samples) == 2:
        before, after = result["before"], result["after"]
        if "diagnostic_failure" in (before["state"], after["state"]):
            result["comparison"] = "diagnostic_failure"
        else:
            result["comparison"] = "sampled"
            if before["state"] != after["state"]:
                result["changed_fields"] = ["presence"]
            elif before["state"] == "present":
                result["changed_fields"] = [name for name in compared
                    if before["fields"][name] != after["fields"][name]]
    return result


@contextmanager
def capture_epoch_hint_failure(root):
    """Install only in the owned child, delegate once, restore before emission.

    No filesystem sample is taken here. The native observer's returned tuples
    and the native first-frame reader's fstat results are the only evidence.
    """
    native_observe = epoch_rotation.observe_policy
    native_signature = epoch_rotation._signature
    native_policy_signature = epoch_rotation._policy_signature
    native_first_frame = epoch_rotation._first_frame
    native_fstat, native_write = os.fstat, os.write
    local = threading.local()
    state = {"observations": 0, "overflow": False, "errors": [],
             "error_overflow": False, "failure": None, "failure_scope": "observation"}
    stack = ExitStack()

    def note(reason):
        if reason not in state["errors"]:
            if len(state["errors"]) < 8:
                state["errors"].append(reason)
            else:
                state["error_overflow"] = True

    def active():
        token = getattr(local, "token", None)
        if token is not None and token["pid"] == os.getpid() and token["thread"] == threading.get_ident():
            return token
        return None

    def retain(token, domain, value):
        values = token["samples"][domain]
        if len(values) < 2:
            values.append(value)
        else:
            token["overflow"][domain] = True

    def signature(*args, **kwargs):
        value = native_signature(*args, **kwargs)
        try:
            token = active()
            if token is not None and sys._getframe(1).f_code is native_observe.__code__:
                path = args[0] if args else kwargs.get("path")
                domain = "root" if path == root.tenant_home else "events" if path == root.tenant_home / "events.jsonl" else None
                if domain is None:
                    note("unexpected_signature_coordinate")
                else:
                    retain(token, domain, value)
        except BaseException:
            note("signature_capture_failed")
        return value

    def policy_signature(*args, **kwargs):
        value = native_policy_signature(*args, **kwargs)
        try:
            token = active()
            if token is not None and sys._getframe(1).f_code is native_observe.__code__:
                path = args[0] if args else kwargs.get("path")
                if path == root.tenant_home / "state/ledger-policy.json":
                    retain(token, "policy", value)
                else:
                    note("unexpected_policy_coordinate")
        except BaseException:
            note("policy_capture_failed")
        return value

    def fstat(*args, **kwargs):
        info = native_fstat(*args, **kwargs)
        try:
            token = active()
            if (token is not None and token["in_first_frame"]
                    and sys._getframe(1).f_code is native_first_frame.__code__):
                retain(token, "fstat", _stat_tuple(info))
                if len(token["descriptors"]) < 2:
                    token["descriptors"].append(args[0] if args else kwargs.get("fd"))
        except BaseException:
            note("fstat_capture_failed")
        return info

    def first_frame(*args, **kwargs):
        token = active()
        if token is None:
            return native_first_frame(*args, **kwargs)
        token["in_first_frame"] = True
        try:
            return native_first_frame(*args, **kwargs)
        finally:
            try:
                input_stat = args[1] if len(args) > 1 else kwargs.get("input_stat")
                if input_stat is not None and input_stat[0] is not None:
                    token["pre_read_input_stat"] = _stat_tuple(input_stat[0])
            except BaseException:
                note("pre_read_testimony_capture_failed")
            token["in_first_frame"] = False

    def observe(*args, **kwargs):
        selected = args[0] if args else kwargs.get("root")
        if selected is not root:
            return native_observe(*args, **kwargs)
        previous = getattr(local, "token", None)
        token = None
        try:
            state["observations"] += 1
            if state["observations"] <= MAX_OBSERVATIONS:
                token = {"pid": os.getpid(), "thread": threading.get_ident(),
                    "ordinal": state["observations"], "in_first_frame": False,
                    "samples": {name: [] for name in ("root", "events", "policy", "fstat")},
                    "overflow": {name: False for name in ("root", "events", "policy", "fstat")},
                    "descriptors": [], "pre_read_input_stat": None}
            else:
                state["overflow"] = True
            local.token = token
        except BaseException:
            note("observation_scope_capture_failed")
            local.token = None
        try:
            return native_observe(*args, **kwargs)
        except BaseException as failure:
            state["failure"] = (failure, token)
            raise
        finally:
            local.token = previous

    def emit():
        if state["failure"] is None:
            return
        failure, token = state["failure"]
        try:
            code = getattr(failure, "code", None)
            packet = {"capture": "process_atomicity_epoch_hint", "schema_version": 1,
                "exception": {"type": type(failure).__name__[:128],
                    "code": code[:128] if isinstance(code, str) else None,
                    "code_truncated": isinstance(code, str) and len(code) > 128},
                "observation_overflow": state["overflow"], "diagnostic_errors": state["errors"],
                "diagnostic_error_overflow": state["error_overflow"],
                "failure_scope": state["failure_scope"],
                "observation": {"state": "not_sampled"}}
            if token is not None:
                packet["observation"] = {"state": "sampled", "pid": token["pid"],
                    "thread": token["thread"], "ordinal": token["ordinal"],
                    "outer": {name: _pair(token["samples"][name], overflow=token["overflow"][name])
                              for name in ("root", "events", "policy")},
                    "first_frame": {"pair": _pair(token["samples"]["fstat"],
                        overflow=token["overflow"]["fstat"], compared=INNER_COMPARED_FIELDS),
                        "native_compared_fields": list(INNER_COMPARED_FIELDS),
                        "descriptors": token["descriptors"],
                        "pre_read_input_stat": {"state": "not_sampled"} if token["pre_read_input_stat"] is None
                            else _sample(token["pre_read_input_stat"])}}
            payload = (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            if len(payload) > MAX_DIAGNOSTIC_BYTES:
                payload = (json.dumps({"capture": "process_atomicity_epoch_hint",
                    "schema_version": 1, "exception": packet["exception"],
                    "diagnostic_failure": "output_overflow", "samples": "omitted"},
                    sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        except BaseException:
            payload = b'{"capture":"process_atomicity_epoch_hint","schema_version":1,"diagnostic_failure":"serialization_failed","samples":"omitted"}\n'
        try:
            # One bounded write, no Queue entries, flush protocol or retry.
            native_write(2, payload)
        except BaseException:
            # An unavailable diagnostic sink cannot replace the native failure.
            pass

    try:
        try:
            stack.enter_context(mock.patch.object(epoch_rotation, "observe_policy", observe))
            stack.enter_context(mock.patch.object(epoch_rotation, "_signature", signature))
            stack.enter_context(mock.patch.object(epoch_rotation, "_policy_signature", policy_signature))
            stack.enter_context(mock.patch.object(epoch_rotation, "_first_frame", first_frame))
            stack.enter_context(mock.patch.object(os, "fstat", fstat))
        except BaseException:
            note("delegator_installation_failed")
            try:
                stack.close()
            except BaseException:
                note("delegator_restoration_failed")
        try:
            yield
        except BaseException as failure:
            if state["failure"] is None and "delegator_installation_failed" in state["errors"]:
                state["failure"] = (failure, None)
                state["failure_scope"] = "worker_without_observer"
            raise
    finally:
        try:
            stack.close()
        except BaseException:
            note("delegator_restoration_failed")
        emit()
