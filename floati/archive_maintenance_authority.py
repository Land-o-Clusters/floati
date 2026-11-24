"""Ephemeral admission from the original native maintenance call path.

This module publishes nothing. The later native writer must pass the exact
admission to require_admission immediately before each durable mutation. Data
plans, EPOCH exclusion, and knowledge of this private API cannot issue a token.
Like the existing effect/wake seals, this is an in-process provenance boundary,
not isolation from code that rewrites Python module internals or the interpreter.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import os
import sys
import threading

from .bus_epoch import epoch_guard, _require_owned_epoch_guard
from .errors import ProtocolRefusal
from . import epoch_rotation as _rotation
from .registry import Registry


class _Admission:
    """Instances carry no authority unless identity-registered by the context."""
    __slots__ = ()


@dataclass(frozen=True)
class _Context:
    root: object
    home: object
    actor: str
    trigger: str
    checker: object
    frame: object
    hold: object
    process: int
    thread: object


_CONTEXTS = {}
_CONTEXTS_LOCK = threading.RLock()


def _refuse():
    return ProtocolRefusal("archive_maintenance_required",
        "archive mutation requires its original active native maintenance context")


def _original_frame(frame, root, actor, trigger, checker):
    return (frame is not None
        and frame.f_code is _rotation._MAINTENANCE_BODY_CODE
        and frame.f_globals is _rotation._MAINTENANCE_GLOBALS
        and getattr(_rotation._maintenance, "__code__", None) is _rotation._MAINTENANCE_BODY_CODE
        and frame.f_locals.get("root") is root
        and frame.f_locals.get("selected") is actor
        and frame.f_locals.get("trigger") is trigger
        and frame.f_locals.get("authority_check") is checker)


@contextmanager
def _native_context(root, *, actor, trigger, authority_check=None):
    # contextlib.__enter__ resumes this generator from the original body.
    try:
        owner = sys._getframe(2)
    except ValueError:
        owner = None
    if (not _original_frame(owner, root, actor, trigger, authority_check)
            or trigger not in _rotation.TRIGGERS or _rotation._nested(root)):
        raise _refuse()
    with _rotation._scope(root), epoch_guard(root, exclusive=True):
        # Preserve native recovery and Stop-check ordering before any issuance.
        recovered = _rotation._recover(root, actor, authority_check)
        if recovered is not None:
            yield None, recovered
            return
        admission = _Admission()
        context = _Context(root, root.tenant_home, actor, trigger, authority_check,
            owner, _require_owned_epoch_guard(root, exclusive=True),
            os.getpid(), threading.current_thread())
        # A legacy direct stop_poll call without its checker still preserves
        # native roll behavior, but cannot confer new archive-write authority.
        eligible = trigger != "stop_poll" or callable(authority_check)
        if eligible:
            with _CONTEXTS_LOCK:
                _CONTEXTS[admission] = context
        try:
            yield admission, None
        finally:
            with _CONTEXTS_LOCK:
                _CONTEXTS.pop(admission, None)


def _require_context(root, admission, context):
    if (root is not context.root or root.tenant_home != context.home
            or context.process != os.getpid() or context.thread is not threading.current_thread()
            or not _original_frame(context.frame, root, context.actor, context.trigger, context.checker)
            or context.frame.f_locals.get("admission") is not admission):
        raise _refuse()
    frame = sys._getframe(1)
    try:
        while frame is not None and frame is not context.frame:
            frame = frame.f_back
        if frame is None:
            raise _refuse()
    finally:
        del frame
    if _require_owned_epoch_guard(root, exclusive=True) is not context.hold:
        raise _refuse()


def require_admission(root, admission):
    """Recheck exact native authority immediately before a durable mutation.

    Returns no transferable data or new capability. Native registration,
    consent/session and lock failures propagate with their original types.
    """
    if type(admission) is not _Admission:
        raise _refuse()
    with _CONTEXTS_LOCK:
        context = _CONTEXTS.get(admission)
    if context is None:
        raise _refuse()
    _require_context(root, admission, context)
    Registry(root).require_active(context.actor)
    if context.checker is not None:
        context.checker()
    # The retained checker cannot silently replace the context, root or guard.
    _require_context(root, admission, context)
    Registry(root).require_active(context.actor)


def _admission_identity(root, admission):
    """Return immutable context metadata; these values confer no authority."""
    require_admission(root, admission)
    with _CONTEXTS_LOCK:
        context = _CONTEXTS.get(admission)
        if context is None:
            raise _refuse()
        _require_context(root, admission, context)
        return context.actor, context.trigger
