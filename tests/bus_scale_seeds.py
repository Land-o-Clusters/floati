"""Private diagnostic histories materialized from native schemas and factories.

Bulk fixture publication is deliberately not evidence that 10,000 commands ran.
One native registration prelude and bounded native producer templates establish
the row shapes; native switch plans establish each registry transition. Complete
published histories then pass native readers and wake projection. No waiter,
hook, mapping, consent, claim or live harness is installed here.
"""
from __future__ import annotations

from tests import HOME_PIN_ROOT
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import stable_fixture_sources

from datetime import datetime, timezone
import hashlib
import itertools
import os
from pathlib import Path
import shutil
import stat

from floati.admin_registry import RegistryAdminBackend
from floati.codex_wait_contract import CodexWaitReceiptLedger
from floati.events import EVENT_KINDS, EventLog, validate_event_records
from floati.framing import encode_frame
from floati.jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES, read_records
from floati.provider_switch import ProviderSwitchWizard
from floati.records import validate_record
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from floati.wake_hold import WakeHoldController, project_wake_items


_TENANT = "scale-fleet"
_WORKER = "scale-worker"
_ARCHITECT = "architect"
_NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
_STAMP = "2026-10-09T00:00:00.000Z"
_FAMILIES = {
    "events": ("events.jsonl", frozenset(EVENT_KINDS)),
    "registry": ("registry/entries.jsonl", frozenset(REGISTRY_KINDS)),
    "exhaustion": (
        "receipts/codex-wait-exhaustion/scale-worker.jsonl",
        frozenset({"codex_wait_exhaustion_receipt"}),
    ),
    "deliveries": (
        "receipts/deliveries/architect.jsonl", frozenset({"delivered_unpresented"}),
    ),
}


def _suffix(index):
    """Deterministic fixed-width UUIDv7-shaped fixture identity."""
    if not 0 <= index < 2 ** 48:
        raise ValueError("fixture UUID index is out of bounds")
    return f"{index:012x}70008000000000000000"


def _session_digest(index):
    return hashlib.sha256(f"scale-history-session-{index:08d}".encode("utf-8")).hexdigest()


def _base_path(base):
    path = Path(base)
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or Path(REAL_TEMP_ROOT) not in path.parents):
        raise ValueError("seed base must be canonical and below the native private test temp root")
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("seed base must be an owned private ordinary directory")
    if Path.home() != Path(HOME_PIN_ROOT):
        raise ValueError("native test HOME must be installed before seed creation")
    if any(path.iterdir()):
        raise ValueError("seed base must be empty; existing fixtures are never replaced")
    return path


def _native_prelude(home):
    home.mkdir(parents=True, mode=0o700)
    root = FloatiRoot.open_direct_home(home)
    registry = Registry(root)
    registry.register(_WORKER, "codex")
    registry.register(_ARCHITECT, "codex")
    return root


def _native_envelope(root, *, key):
    receipt = EventLog(root).send(
        _WORKER, _ARCHITECT, "floati", "a" * 40,
        "docs/evidence/bus-scale-seed.md", "bounded scale fixture",
        idempotency_key=key, now=_NOW,
    )
    return dict(receipt["message"])


class _PlanAssignment:
    """O(1) assignment state for native plan generation, without ledger replay."""
    def __init__(self, active):
        self.active = dict(active, model=None)

    def active_assignment(self, node):
        if node != self.active["node_id"]:
            raise ValueError("switch planner crossed its fixture node")
        return dict(self.active)

    def accept(self, plan):
        replacement, receipt = plan.records
        if (receipt["previous_registry_entry_id"] != self.active["id"]
                or receipt["previous_model"] != self.active["model"]
                or receipt["previous_harness"] != self.active["role"]
                or receipt["registry_entry_id"] != replacement["id"]
                or receipt["node_id"] != self.active["node_id"]
                or replacement["node_id"] != self.active["node_id"]):
            raise ValueError("native switch plan does not continue its predecessor")
        self.active = dict(replacement, model=receipt["model"])


def _history(root, family, total):
    if family == "events":
        template = _native_envelope(root, key="scale-event-template")
        # A legacy-compatible envelope-only history is intentional. Native
        # current sends also emit readiness observations; this fixture does
        # not pretend those absent observations were produced historically.
        return [dict(template, id="msg-" + _suffix(index), timestamp=_STAMP,
                     idempotency_key=f"scale-event-{index:08d}")
                for index in range(1, total + 1)]
    if family == "registry":
        registry = Registry(root)
        # Aggregate construction validates one full view. Selected lookups
        # must not create a disposable bank alongside the sole scaled input.
        with registry.read_once():
            rows = [dict(row) for row in registry.known_records()]
            assignment = _PlanAssignment(registry.require_active(_ARCHITECT))
        identifiers = itertools.count(100000)
        wizard = ProviderSwitchWizard(
            root, assignment, id_factory=lambda: _suffix(next(identifiers)), now=lambda: _NOW,
        )
        for index in range((total - 2) // 2):
            model = "scale-model-a" if index % 2 == 0 else "scale-model-b"
            plan = wizard._plan((_ARCHITECT, "codex", model))
            assignment.accept(plan)
            rows.extend(dict(row) for row in plan.records)
        return rows
    if family == "exhaustion":
        template = CodexWaitReceiptLedger(root).record_exhaustion(
            node_id=_WORKER, session_digest=_session_digest(0),
            waited_seconds=90, idempotency_key="scale-exhaustion-template",
        )
        return [dict(template, id="codex-wait-exhaustion-" + _suffix(index),
                     timestamp=_STAMP, session_digest=_session_digest(index),
                     idempotency_key=f"scale-exhaustion-{index:08d}")
                for index in range(1, total + 1)]
    if family == "deliveries":
        envelope = _native_envelope(root, key="scale-delivery-base")
        template = EventLog(root)._write_delivery_receipt(_ARCHITECT, [envelope])
        return [dict(template, id="delivery-" + _suffix(index), timestamp=_STAMP,
                     presentation_count=index, item_ids=list(template["item_ids"]))
                for index in range(1, total + 1)]
    raise ValueError("unknown closed seed family")


def _encoded_history(root, family, rows, counts):
    _relative, kinds = _FAMILIES[family]
    if len(rows) != counts[1] or len(rows) > MAX_LEDGER_RECORDS:
        raise ValueError("fixture history has the wrong bounded record count")
    identifiers = set()
    frames = []
    for row in rows:
        validate_record(row, root.tenant_id, kinds, integrity=True)
        if row["id"] in identifiers:
            raise ValueError("fixture history repeats a native record identity")
        identifiers.add(row["id"])
        frame = encode_frame(row)
        if len(frame) - 1 > MAX_RECORD_BYTES:
            raise ValueError("fixture frame exceeds the native record byte bound")
        frames.append(frame)
    if family == "events":
        validate_event_records(rows)
    if sum(map(len, frames)) > MAX_LEDGER_BYTES:
        raise ValueError("fixture history exceeds the native ledger byte bound")
    return b"".join(frames[:counts[0]]), b"".join(frames)


def _projection(root, recipient):
    controller = WakeHoldController(root)
    events, event_prefixes, deliveries, delivery_prefixes, acks, ack_prefixes = controller._read(recipient, None)
    return project_wake_items(
        events=events, deliveries=deliveries, acknowledgments=acks,
        recipient=recipient, worker_session_id=None, tenant_id=root.tenant_id,
        event_prefix_digests=event_prefixes, delivery_prefix_digests=delivery_prefixes,
        acknowledgment_prefix_digests=ack_prefixes,
    )


def _validate_home(root, family, count):
    relative, kinds = _FAMILIES[family]
    rows = read_records(root, relative, allowed_kinds=kinds)
    if len(rows) != count:
        raise ValueError("native reader did not retain the complete fixture history")
    log = EventLog(root)
    with log.registry.read_once():
        events = log.event_records()
        validate_event_records(events)
        if log.unacked_ids(_WORKER) or _projection(root, _WORKER):
            raise ValueError("fixture introduced selected builder work")
    if family == "deliveries":
        projected = _projection(root, _ARCHITECT)
        if (len(projected) != 1 or projected[0].state != "fresh"
                or projected[0].presentation_count != count):
            raise ValueError("native delivery projection did not retain the unpresented history")
    backend = RegistryAdminBackend(root)
    assignments = {}
    # The two copied arms retain identical ancillary bytes. Complete native
    # aggregate readers still validate their respective overwritten ledger;
    # a selected cache rebuild is not part of this immutable fixture proof.
    with backend.registry.read_once():
        if backend.registry.active_node_ids() != (_ARCHITECT, _WORKER):
            raise ValueError("fixture changed its two active node identities")
        for node in (_WORKER, _ARCHITECT):
            active = backend.active_assignment(node)
            assignments[node] = {field: active[field] for field in ("role", "model", "state")}
    expected_model = "scale-model-a" if family == "registry" else None
    expected = {
        _WORKER: {"role": "codex", "model": None, "state": "active"},
        _ARCHITECT: {"role": "codex", "model": expected_model, "state": "active"},
    }
    if assignments != expected:
        raise ValueError("fixture history changed the required final provider state")
    return {"tenant_id": root.tenant_id, "node_id": _WORKER,
            "architect_id": _ARCHITECT, "assignments": assignments}


def _files(home):
    return {path.relative_to(home).as_posix(): path.read_bytes()
            for path in home.rglob("*") if path.is_file()}


@stable_fixture_sources()
def make_seed_pair(base: Path, *, counts=(1000, 10000)):
    """Return four isolated 1,000/10,000-record direct-home seed pairs.

    ``base`` must be empty, owned and private under the native test temp root.
    Each family maps ``'1'``/``'10'`` to canonical direct-home Paths, plus
    ``ledger_relative`` and ``selected_state``. Failures retain partial fixture
    evidence; the caller owns and removes the enclosing private directory.
    """
    if (not isinstance(counts, tuple) or len(counts) != 2
            or any(type(value) is not int for value in counts)
            or not 0 < counts[0] < counts[1] <= MAX_LEDGER_RECORDS
            or any(value % 4 for value in counts)):
        raise ValueError("seed counts require increasing multiples of four within the native bound")
    base = _base_path(base)
    result = {}
    for family, (relative, _kinds) in _FAMILIES.items():
        small = base / family / "1" / _TENANT
        large = base / family / "10" / _TENANT
        root = _native_prelude(small)
        small_raw, large_raw = _encoded_history(root, family, _history(root, family, counts[1]), counts)
        ledger = root.resolve_relative(relative)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_bytes(small_raw)
        selected = _validate_home(root, family, counts[0])
        # Copy the complete native prelude and validation side effects before
        # changing the sole scaled input. copytree creates independent files.
        shutil.copytree(small, large)
        large_root = FloatiRoot.open_direct_home(large)
        large_root.resolve_relative(relative).write_bytes(large_raw)
        if _validate_home(large_root, family, counts[1]) != selected:
            raise ValueError("fixture arms disagree on selected state")
        before, after = _files(small), _files(large)
        del before[relative]
        del after[relative]
        if before != after:
            raise ValueError("a non-scaled fixture file changed between arms")
        result[family] = {"1": small, "10": large, "ledger_relative": relative,
                          "selected_state": selected}
    return result
