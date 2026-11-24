"""Invocation-local ledger acquisition retries for native Stop waiters only."""

from contextlib import contextmanager
from contextvars import ContextVar

from .errors import DurabilityFailure, ProtocolRefusal


_WINDOW = ContextVar("floati_waiter_lock_window", default=None)


def transient_ledger_timeout(failure):
    return (isinstance(failure, ProtocolRefusal)
            and failure.code == "ledger_lock_timeout"
            and isinstance(failure.remedy, dict)
            and failure.remedy.get("kind") == "transient"
            and failure.remedy.get("action") == "retry_once")


def transient_lock_timeout(failure):
    # Epoch timeout is emitted only by native flock acquisition. It does not
    # describe a policy, archive, marker, or selected-content validation fault.
    return transient_ledger_timeout(failure) or (
        isinstance(failure, ProtocolRefusal) and failure.code == "epoch_lock_timeout")


def maintenance_transition_failure(failure):
    # A fresh maintenance attempt recovers its pending transaction before
    # replanning. Never give a planner's same-code ProtocolRefusal, or corrupt
    # native evidence's IntegrityFailure, this transition meaning.
    return transient_lock_timeout(failure) or (
        isinstance(failure, DurabilityFailure)
        and failure.code == "epoch_selected_member_changed")


class WaiterLockDeadline(ProtocolRefusal):
    def __init__(self, coordinate, *, contended=True):
        self.contended = contended
        super().__init__("waiter_lock_deadline", "original waiter window exhausted while "
                         + coordinate + " remained contended",
                         remedy="end this turn to re-arm its native waiter")


class WaiterAuthorityChanged(Exception):
    def __init__(self, reason, current_session=None):
        self.reason = reason
        self.current_session = current_session


class WaiterLockWindow:
    def __init__(self, seconds, clock, sleep):
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.deadline = None if seconds is None else self.started + seconds
        self.deadline_limit = None
        self.backoff_seconds = 0.05
        self.finishing = False
        self.check_authority = None
        self.contended = False

    def update(self, seconds):
        # A validated consent replacement may change the budget, never its origin.
        self.deadline = self.started + seconds
        if self.deadline_limit is not None:
            self.deadline = min(self.deadline, self.deadline_limit)

    def remaining(self):
        if self.finishing:
            return 0.0
        return max(0.0, self.deadline - self.clock())

    def finish(self):
        # Final testimony may try an uncontended lock once, never wait again.
        self.finishing = True
        # Keep the scope active even without proven consent. The captured
        # origin is only a non-None sentinel; finishing residual is always zero.
        self.deadline = self.started

    def retry(self, failure, coordinate, *, retry_failure=transient_lock_timeout):
        if not retry_failure(failure):
            raise failure
        self.contended = True
        remaining = self.remaining()
        if remaining <= 0:
            raise WaiterLockDeadline(coordinate) from failure
        self.sleep(min(self.backoff_seconds, remaining))
        self.backoff_seconds = min(0.25, self.backoff_seconds * 2)
        if self.remaining() <= 0:
            raise WaiterLockDeadline(coordinate) from failure

    def run(self, operation, *, retry_allowed=lambda: True,
            retry_failure=transient_lock_timeout):
        """Retry one native pre-publication operation after all guards unwind."""
        if self.deadline is None:
            return operation()
        while True:
            if not self.finishing and self.remaining() <= 0:
                raise WaiterLockDeadline("native waiter operation", contended=self.contended)
            try:
                if self.contended and not self.finishing and self.check_authority is not None:
                    self.check_authority()
                return operation()
            except (ProtocolRefusal, DurabilityFailure) as failure:
                if self.finishing or not retry_allowed() or not retry_failure(failure):
                    raise
                self.retry(failure, "native waiter acquisition or epoch transition",
                    retry_failure=retry_failure)


def current_lock_window():
    window = _WINDOW.get()
    return window if window is not None and window.deadline is not None else None


def lock_deadline_failure(failure):
    seen = set()
    while failure is not None and id(failure) not in seen:
        if isinstance(failure, WaiterLockDeadline):
            return failure
        seen.add(id(failure))
        failure = failure.__cause__
    return None


def authority_change_failure(failure):
    seen = set()
    while failure is not None and id(failure) not in seen:
        if isinstance(failure, WaiterAuthorityChanged):
            return failure
        seen.add(id(failure))
        failure = failure.__cause__
    return None


@contextmanager
def waiter_lock_window(seconds, clock, sleep):
    window = WaiterLockWindow(seconds, clock, sleep)
    token = _WINDOW.set(window)
    try:
        yield window
    finally:
        _WINDOW.reset(token)
