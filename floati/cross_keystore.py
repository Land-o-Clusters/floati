"""Independent cross-store observations and ordered replacement.

The fixture can model a helper whose result is unresolved across invocations.
Platform helpers use private pipes. R8.2 qualification is required before
either installed helper can be used to issue a production replacement.
"""
from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from threading import Lock, RLock
from typing import Protocol

from .cross_anchor import OutstandingWrite
from .errors import ProtocolRefusal


class ReadOutcome(Enum):
    PRESENT = "present"
    CONFIRMED_ABSENT = "confirmed_absent"
    LOCKED = "locked"
    UNREACHABLE = "unreachable"
    OUTSTANDING = "outstanding"
    UNKNOWN = "unknown"


class WriteOutcome(Enum):
    APPLIED = "applied"
    UNAPPLIED = "unapplied"
    OUTSTANDING = "outstanding"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Observation:
    outcome: ReadOutcome
    value: object = field(default=None, repr=False)


class KeyStore(Protocol):
    def observe(self, slot: str) -> Observation:
        """Read a fixed root-scoped item, with no secret in diagnostics."""

    def replace(self, slot: str, expected: object, successor: object) -> WriteOutcome:
        """Issue an ordered, exact expected-value replacement."""


_SLOT = re.compile(r"(?:anchor|sentinel|quota-migration|pair/[0-9a-f]{32})\Z")
_SERVICE = "floati-cross"


def _verify_slot(slot):
    if not isinstance(slot, str) or not _SLOT.fullmatch(slot):
        raise ValueError("invalid cross store slot")


class _JournalSlot:
    def __init__(self, store: KeyStore, slot: str):
        self.store = store
        self.slot = slot

    def read(self):
        current = self.store.observe(self.slot)
        if current.outcome is ReadOutcome.PRESENT:
            return current.value
        if current.outcome is ReadOutcome.CONFIRMED_ABSENT:
            return None
        if current.outcome is ReadOutcome.OUTSTANDING:
            return OutstandingWrite()
        raise OSError("independent store evidence is unavailable")

    def compare_and_set(self, expected, successor):
        return self.store.replace(self.slot, expected, successor) is WriteOutcome.APPLIED


class FixtureStore:
    """Root-scoped fixture with one outstanding operation across all slots."""

    def __init__(self):
        self._items = {}
        self._pending = None
        self._next = WriteOutcome.APPLIED
        self._anchor_issued = False
        self._lock = Lock()
        self._provenance_lock = RLock()
        self._journal_established = False
        self.anchor = _JournalSlot(self, "anchor")
        self.quota = _JournalSlot(self, "quota-migration")

    def observe(self, slot):
        _verify_slot(slot)
        with self._lock:
            if self._pending is not None:
                outcome = self._pending[0]
                return Observation(ReadOutcome(outcome.value))
            if slot in self._items:
                return Observation(ReadOutcome.PRESENT, self._items[slot])
            return Observation(ReadOutcome.CONFIRMED_ABSENT)

    def interrupt_next(self, outcome):
        with self._lock:
            self._next = outcome

    def exclude(self, root):
        return self._provenance_lock

    def read(self, root):
        """Fixture provenance lives outside the restored journal image."""
        with self._provenance_lock, self._lock:
            return "established" if self._journal_established or self._anchor_issued else "never_established"

    def mark_established(self, root):
        with self._provenance_lock:
            self._journal_established = True
            return True

    def prove_unbound(self):
        """A provisional journal can be adopted only before store authority was issued."""
        with self._lock:
            return not self._anchor_issued and self._pending is None and not self._items

    def _apply(self, slot, successor):
        if successor is None:
            self._items.pop(slot, None)
        else:
            self._items[slot] = successor

    def replace(self, slot, expected, successor, *, outcome=None):
        _verify_slot(slot)
        with self._lock:
            if self._pending is not None:
                return self._pending[0]
            if self._items.get(slot) != expected:
                return WriteOutcome.UNAPPLIED
            result = self._next if outcome is None else outcome
            self._next = WriteOutcome.APPLIED
            if slot == "anchor" and successor is not None:
                self._anchor_issued = True
            if result is WriteOutcome.APPLIED:
                self._apply(slot, successor)
            elif result in {WriteOutcome.OUTSTANDING, WriteOutcome.UNKNOWN}:
                self._pending = result, slot, successor
            return result

    def settle(self, *, applied):
        with self._lock:
            if self._pending is None:
                raise ValueError("there is no outstanding store operation")
            _, slot, successor = self._pending
            if applied:
                self._apply(slot, successor)
            self._pending = None


def _trusted_helper(path):
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0 and not metadata.st_mode & 0o022


def _wire(value):
    return b64encode(value).decode("ascii")


def _decode(stdout):
    wire = stdout.removesuffix(b"\n")
    try:
        value = b64decode(wire, validate=True)
    except ValueError:
        return Observation(ReadOutcome.UNKNOWN)
    if not value or _wire(value).encode("ascii") != wire:
        return Observation(ReadOutcome.UNKNOWN)
    return Observation(ReadOutcome.PRESENT, value)


def _unavailable():
    return ProtocolRefusal("cross_key_store_unavailable", "The cross-machine key store is unavailable or unqualified.")


class _PlatformStore:
    def __init__(self, root, helper):
        self.helper = helper
        self.namespace = sha256(os.fsencode(root.tenant_home)).hexdigest()
        self.environment = {"HOME": str(Path.home()), "LC_ALL": "C", "PATH": "/usr/bin:/bin"}

    def _run(self, args, payload=b""):
        try:
            return subprocess.run(
                (str(self.helper), *args), input=payload, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self.environment, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def observe(self, slot):
        _verify_slot(slot)
        return self._lookup(slot)

    def replace(self, slot, expected, successor):
        _verify_slot(slot)
        # A synchronous CLI return alone cannot exclude a backend write after
        # caller death. Qualification must supply this property before issue.
        return WriteOutcome.UNKNOWN


class KeychainStore(_PlatformStore):
    """Login keychain helper with a fixed service and explicit keychain path."""

    @classmethod
    def probe(cls, root):
        helper = Path("/usr/bin/security")
        keychain = Path.home() / "Library/Keychains/login.keychain-db"
        if sys.platform != "darwin" or not _trusted_helper(helper):
            raise _unavailable()
        try:
            metadata = keychain.lstat()
        except OSError:
            raise _unavailable() from None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise _unavailable()
        return cls(root, helper, keychain)

    def __init__(self, root, helper, keychain):
        super().__init__(root, helper)
        self.keychain = str(keychain)

    def _selector(self, slot):
        return "-s", _SERVICE, "-a", f"{self.namespace}/{slot}"

    def _lookup(self, slot):
        result = self._run(("find-generic-password", "-w", *self._selector(slot), self.keychain))
        if result is None:
            return Observation(ReadOutcome.UNREACHABLE)
        if result.returncode == 0:
            return _decode(result.stdout)
        # No absence classification on an unqualified CLI return code.
        return Observation(ReadOutcome.UNKNOWN)

    def _issue(self, slot, successor):
        selector = self._selector(slot)
        if successor is None:
            return self._run(("delete-generic-password", *selector, self.keychain))
        # security -i interprets stdin as commands. All variable words other
        # than the quoted path are drawn from fixed safe alphabets.
        path = '"' + self.keychain.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if "\n" in path or "\r" in path:
            return None
        words = ("add-generic-password", "-U", *selector, "-w", _wire(successor), path)
        return self._run(("-i",), (" ".join(words) + "\n").encode())


class SecretServiceStore(_PlatformStore):
    """Root-scoped Secret Service helper on the local user session bus."""

    @classmethod
    def probe(cls, root):
        helper = Path("/usr/bin/secret-tool")
        if sys.platform != "linux" or not _trusted_helper(helper):
            raise _unavailable()
        bus = Path("/run/user") / str(os.getuid()) / "bus"
        try:
            identity = bus.lstat()
        except OSError:
            raise _unavailable() from None
        if not stat.S_ISSOCK(identity.st_mode) or identity.st_uid != os.getuid():
            raise _unavailable()
        adapter = cls(root, helper)
        adapter.environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        return adapter

    def _selector(self, slot):
        return "service", _SERVICE, "tenant", self.namespace, "slot", slot

    def _lookup(self, slot):
        result = self._run(("lookup", *self._selector(slot)))
        if result is None:
            return Observation(ReadOutcome.UNREACHABLE)
        if result.returncode == 0:
            return _decode(result.stdout)
        return Observation(ReadOutcome.UNKNOWN)

    def _issue(self, slot, successor):
        selector = self._selector(slot)
        if successor is None:
            return self._run(("clear", *selector))
        return self._run(("store", f"--label={_SERVICE}", *selector), _wire(successor).encode())
