"""One owned native occurrence, one bounded local deadline process.

The child inherits an opaque capability only after the observer releases its
guards. READY is not release: publication remains impossible until the parent
explicitly releases the prepared child. No provider polling or callback CLI.
"""
from datetime import datetime, timezone
import math
import os
import select
import threading
import time

from .codex_tool_hook import _source_acceptance_guard
from .codex_wait_contract import (CodexWaitConsentLedger, CodexWaitSessionLedger,
    WORKSPACE_MAP_RELATIVE)
from .errors import FloatiError
from .host_observations import QUIET_SECONDS, _publish_due, _state_path, _still_current
from .recipient_mechanisms import _time
from .wake_control import WakeController, _digest
from .work import WorkLog


_utc_now = lambda: datetime.now(timezone.utc)
_monotonic = time.monotonic
_STARTUP_OWNERS = []


class DeadlineOwner:
    """Only this native fork's waitpid/control handles; no PID discovery."""
    def __init__(self, pid, control):
        self.pid, self._control = pid, control
        self._result = None
        self.stdio_detached = False
        self.released = False

    def poll(self):
        if self._result is not None:
            return self._result
        try:
            found, status = os.waitpid(self.pid, os.WNOHANG)
        except InterruptedError:
            return None
        if found:
            self._result = os.waitstatus_to_exitcode(status)
            self.detach()
        return self._result

    def detach(self):
        """Close parent control normally; a released child continues waiting."""
        if self._control is not None:
            descriptor, self._control = self._control, None
            os.close(descriptor)

    def cancel(self):
        if self.poll() is not None:
            return self._result
        if self._control is not None:
            try:
                os.write(self._control, b"C")
            except OSError:
                pass
            finally:
                self.detach()
        # This polls only our retained child handle during owned cleanup,
        # never a provider or another session's process. No signal fallback.
        end = time.monotonic() + 6
        while time.monotonic() < end:
            result = self.poll()
            if result is not None:
                return result
            time.sleep(0.01)
        return None  # Still owned, exit unproved; never report a reap.

    def __del__(self):
        try:
            self.detach()
        except OSError:
            pass


def _detach_descriptors(control, ready):
    os.setsid()
    descriptor = os.open(os.devnull, os.O_RDWR)
    try:
        for target in (0, 1, 2):
            os.dup2(descriptor, target)
    finally:
        if descriptor > 2:
            os.close(descriptor)
    keep = {0, 1, 2, control, ready}
    # Enumerate only our inherited descriptors, then create fresh watch
    # resources. In particular no hook output pipe or inherited lock survives.
    directory = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
    for name in os.listdir(directory):
        if name.isdecimal() and int(name) not in keep:
            try:
                os.close(int(name))
            except OSError:
                pass


def _watch_paths(occurrence):
    root, observation = occurrence.controller.root, occurrence.observation
    node = observation["node_id"]
    wake = WakeController(root)
    return (
        _state_path(root, node),
        root.tenant_home / WORKSPACE_MAP_RELATIVE,
        root.tenant_home / CodexWaitConsentLedger._relative(node),
        root.tenant_home / CodexWaitSessionLedger._relative(node),
        wake._marker(node, _digest(observation["session_id"])),
        root.tenant_home / "registry/entries.jsonl",
        WorkLog(root).path,
    )


def _current(occurrence):
    with _source_acceptance_guard(occurrence.controller.root, occurrence.observation["node_id"]):
        return _still_current(occurrence.controller, occurrence.observation)


def _child(occurrence, control, ready):
    source = None
    try:
        _detach_descriptors(control, ready)
        from .tui import BoardFilesystemWakeup
        source = BoardFilesystemWakeup(occurrence.controller.root.tenant_home,
            _paths=_watch_paths(occurrence))
        # Register before rereading, so invalidation during startup is either
        # in the snapshot or queued on the already registered watch.
        if not _current(occurrence):
            return 0
        if source.coverage_lost:
            return 1
        os.write(ready, b"R")
        os.close(ready); ready = None
        if not select.select([control], [], [], 5)[0] or os.read(control, 1) != b"G":
            return 0  # Cancellation/EOF before release can never publish.
        listening = True
        while True:
            if not _current(occurrence):
                return 0
            if source.coverage_lost:
                return 1
            wall = _utc_now()
            elapsed = _monotonic() - occurrence.started_monotonic
            age = (wall - _time(occurrence.observation["observed_at"])).total_seconds()
            if not math.isfinite(elapsed) or not math.isfinite(age):
                return 1
            remaining = max(QUIET_SECONDS - elapsed, QUIET_SECONDS - age, 0)
            if remaining <= 0:
                # A due clock cannot skip already queued watch loss/cancellation.
                # Bounded draining fails closed under a notification backlog.
                for _attempt in range(8):
                    descriptors = [source.fileno()] + ([control] if listening else [])
                    changed = select.select(descriptors, [], [], 0)[0]
                    if not changed:
                        break
                    if listening and control in changed:
                        command = os.read(control, 1)
                        if command == b"C":
                            return 0
                        if not command:
                            os.close(control); control = None; listening = False
                    if source.fileno() in changed:
                        source.drain()
                        if not _current(occurrence):
                            return 0
                        if source.coverage_lost:
                            return 1
                else:
                    return 1
                if not _current(occurrence):
                    return 0
                _publish_due(occurrence.controller, occurrence.capability,
                    occurrence.observation, now=wall)
                return 0
            descriptors = [source.fileno()] + ([control] if listening else [])
            changed = select.select(descriptors, [], [], remaining)[0]
            if listening and control in changed:
                command = os.read(control, 1)
                if command == b"C":
                    return 0
                if not command:
                    # Normal native hook-parent exit is not cancellation.
                    # Stop selecting EOF so it cannot create a busy loop.
                    os.close(control); control = None; listening = False
            if source.fileno() in changed:
                source.drain()
                if not _current(occurrence):
                    return 0  # Recorded invalidation is an orderly terminal exit.
                if source.coverage_lost:
                    return 1  # A fresh snapshot cannot restore missed coverage.
    except (FloatiError, OSError, ValueError, TypeError, OverflowError):
        return 1
    finally:
        if source is not None:
            source.close()
        for descriptor in (control, ready):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def start_deadline(occurrence, *, startup_timeout_seconds=3):
    """Prepare/release once; unavailable startup leaves the occurrence UNKNOWN."""
    for pending in tuple(_STARTUP_OWNERS):
        if pending.poll() is not None:
            _STARTUP_OWNERS.remove(pending)
    if occurrence.startup_attempted:
        return occurrence.owner if occurrence.owner is not None and occurrence.owner.released else None
    occurrence.startup_attempted = True
    if (not hasattr(os, "fork") or len(threading.enumerate()) != 1
        or type(startup_timeout_seconds) not in {int, float}
        or not math.isfinite(startup_timeout_seconds) or not 0 < startup_timeout_seconds <= 5):
        return None
    owned_descriptors = []
    try:
        control_read, control_write = os.pipe()
        owned_descriptors.extend((control_read, control_write))
        ready_read, ready_write = os.pipe()
        owned_descriptors.extend((ready_read, ready_write))
        pid = os.fork()
    except OSError:
        for descriptor in owned_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None
    if pid == 0:
        status = 1
        try:
            os.close(control_write); os.close(ready_read)
            status = _child(occurrence, control_read, ready_write)
        except BaseException:
            status = 1
        finally:
            os._exit(status)
    owner = DeadlineOwner(pid, control_write)
    occurrence.owner = owner
    def failed_start():
        if owner.cancel() is None:
            occurrence.startup_cleanup_unproved = True
            _STARTUP_OWNERS.append(owner)
        return None
    try:
        os.close(control_read); os.close(ready_write)
        if not select.select([ready_read], [], [], startup_timeout_seconds)[0] or os.read(ready_read, 1) != b"R":
            return failed_start()
        owner.stdio_detached = True
        os.write(control_write, b"G")
        owner.released = True
        return owner
    except (OSError, ValueError):
        return failed_start()
    finally:
        os.close(ready_read)
