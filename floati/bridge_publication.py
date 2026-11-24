"""Private, invocation-bound delivery after the bridge's actual stdout flush.

Only lexical bridge queries defer.  A pending batch is not a receipt, and
neither a firing log nor an unfinished invocation is presentation evidence.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .root import FloatiRoot, validate_identifier
from .wake_control import validate_session_id


_CURRENT = ContextVar("floati_bridge_publication", default=None)
_REMEDY = "preserve the exact envelope IDs and recover through the declared native inbox; do not reuse bridge publication testimony"


def _refuse():
    raise ProtocolRefusal("bridge_publication_invalid",
        "bridge publication owner, authority, batch or output changed", remedy=_REMEDY)


class BridgePublication:
    """One process/thread's exact bridge invocation; never public authority."""

    def __init__(self, root, node, workspace, session):
        self.coordinate = Path(root)
        self.node = node
        self.workspace = Path(workspace).resolve()
        self.session = session
        self.owner = (os.getpid(), threading.get_ident())
        self.invocation = "bridge-publication-" + uuid7_hex()
        self.pending = None
        self.root = None
        self.root_identity = None
        self.authority = None
        self.finished = False
        self._sealed_proof = None
        self.output_started = False
        self.wake_only = False

    def _owned(self):
        if self.finished or self.owner != (os.getpid(), threading.get_ident()):
            _refuse()
        if Path.cwd().resolve() != self.workspace:
            _refuse()

    @contextmanager
    def capture(self, *, authority=None, wake_only=False):
        """Only the synchronous inbox/fast peek, never a delegated waiter."""
        self._owned()
        if self.pending is not None or _CURRENT.get() is not None:
            _refuse()
        self.root = FloatiRoot.open_direct_home(self.coordinate)
        info = self.root.tenant_home.lstat()
        self.root_identity = (info.st_dev, info.st_ino)
        self.authority = authority
        self.wake_only = wake_only
        token = _CURRENT.set(self)
        try:
            yield
        finally:
            _CURRENT.reset(token)

    def defer(self, log, recipient, messages, worker_session_id):
        self._owned()
        if _CURRENT.get() is not self or self.pending is not None or worker_session_id is not None:
            _refuse()
        if validate_identifier(self.node, "node") != recipient:
            _refuse()
        validate_session_id(self.session)
        from .records import _terminal_unsafe
        if len(str(self.workspace)) > 4096 or _terminal_unsafe(str(self.workspace)):
            _refuse()
        opened = FloatiRoot.open_direct_home(self.coordinate)
        if (opened.tenant_home, opened.tenant_id) != (log.root.tenant_home, log.root.tenant_id):
            _refuse()
        info = opened.tenant_home.lstat()
        if (info.st_dev, info.st_ino) != self.root_identity:
            _refuse()
        self.pending = deepcopy(list(messages))

    def publish(self, messages, output, stream):
        """Revalidate, write, flush, THEN receipt under the same native guards."""
        self._owned()
        if _CURRENT.get() is not None or self.pending is None or list(messages) != self.pending:
            _refuse()
        try:
            decision = json.loads(output)
        except (ValueError, TypeError):
            _refuse()
        if not isinstance(decision, dict) or set(decision) != {"decision", "reason"} or (
            decision["decision"] != "block" or not isinstance(decision["reason"], str)
            or any(row["id"] not in decision["reason"] for row in self.pending)
        ):
            _refuse()
        from .bus_epoch import epoch_guard, _require_epoch_writer_admission
        from .events import EventLog
        from .registry import Registry
        from .seat_declaration import require_declared_coordinate
        from .wake_hold import wake_coordination_guard

        try:
            with epoch_guard(self.root), wake_coordination_guard(self.root, self.node):
                current = FloatiRoot.open_direct_home(self.coordinate)
                info = current.tenant_home.lstat()
                if current.tenant_home != self.root.tenant_home or (
                    info.st_dev, info.st_ino
                ) != self.root_identity:
                    _refuse()
                _require_epoch_writer_admission(current)
                registry = Registry(current)
                registry.require_active(self.node)
                registry.require_protocol_lease(self.node, act="fresh delivery to recipient")
                require_declared_coordinate(self.workspace, self.node, current)
                if self.authority is not None:
                    self.authority()
                log = EventLog(current)
                selected, _payload = log._present_full_scan(self.node, len(self.pending),
                    _wake_only=self.wake_only)
                if selected != self.pending:
                    _refuse()
                self.output_started = True
                written = stream.write(output)
                if type(written) is not int or written != len(output):
                    _refuse()
                stream.flush()
                proof = {"invocation_id": self.invocation,
                    "acting_session_id": self.session,
                    "workspace": str(self.workspace),
                    "output_digest": hashlib.sha256(output.encode("utf-8")).hexdigest()}
                self._sealed_proof = proof
                return log._write_delivery_receipt(self.node, self.pending,
                    _bridge_publication=(self, proof))
        finally:
            self.finished = True
            self.pending = None
            self._sealed_proof = None

    def receipt_proof(self, root, recipient, messages, proof):
        """Called solely after flush within this object's guarded publication."""
        self._owned()
        if proof is not self._sealed_proof or proof is None or _CURRENT.get() is not None:
            _refuse()
        current = FloatiRoot.open_direct_home(self.coordinate)
        info = current.tenant_home.lstat()
        if current.tenant_home != self.root.tenant_home or (info.st_dev, info.st_ino) != self.root_identity:
            _refuse()
        if root.tenant_home != self.root.tenant_home or recipient != self.node or list(messages) != self.pending:
            _refuse()
        if proof.get("invocation_id") != self.invocation or proof.get("acting_session_id") != self.session:
            _refuse()
        return proof

    def _publish_context_already_guarded(self, messages, body, stream):
        """Flush one prepared native hook selection before its delivery receipt.

        The relay retains epoch/car/node/pause guards and the presentation
        reservation throughout. Capture has bound this invocation's exact
        selected envelopes; this is not an independent inbox or an ACK.
        """
        self._owned()
        if _CURRENT.get() is not None or self.pending is None or list(messages) != self.pending:
            _refuse()
        if not isinstance(body, dict) or set(body) != {"hookSpecificOutput"}:
            _refuse()
        specific = body["hookSpecificOutput"]
        if not isinstance(specific, dict) or set(specific) != {"hookEventName", "additionalContext"} or (
            specific["hookEventName"] not in {"SessionStart", "PostToolUse", "UserPromptSubmit"}
            or not isinstance(specific["additionalContext"], str)
            or len(specific["additionalContext"]) > 10000
            or json.dumps(self.pending, sort_keys=True) not in specific["additionalContext"]
        ):
            _refuse()
        from .events import EventLog
        output = json.dumps(body) + "\n"
        try:
            if self.authority is not None:
                self.authority()
            self.output_started = True
            written = stream.write(output)
            if type(written) is not int or written != len(output):
                _refuse()
            stream.flush()
            proof = {"invocation_id": self.invocation, "acting_session_id": self.session,
                "workspace": str(self.workspace),
                "output_digest": hashlib.sha256(output.encode("utf-8")).hexdigest()}
            self._sealed_proof = proof
            return EventLog(self.root)._write_delivery_receipt(self.node, self.pending,
                _bridge_publication=(self, proof))
        finally:
            self.finished = True
            self.pending = None
            self._sealed_proof = None


def deferred_delivery(log, recipient, messages, worker_session_id):
    scope = _CURRENT.get()
    if scope is None:
        return False
    if type(scope) is not BridgePublication:
        _refuse()
    scope.defer(log, recipient, messages, worker_session_id)
    return True
