"""Small native waiter memo controls, independent of every unittest driver.

This probe starts no provider or background child. Every root, consent, claim,
message, delivery, ACK and roll belongs to its private fixture bank. Observers
delegate to unchanged native functions; only explicitly named failure controls
inject a failure. Memo controls use the shipped entry points directly. This is
not a CPU benchmark.
"""
from __future__ import annotations

import argparse
import copy
from collections import Counter
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import traceback
from tests.waiter_timing import scripted_readiness


HARNESS_NAMES = ("codex", "cursor")
SESSION = "memo-conversation-a"
OTHER_SESSION = "memo-conversation-b"
IDENTITY_FIELDS = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size",
                   "st_mtime_ns", "st_ctime_ns")


def native_json(value):
    """Serialize the COMPLETE returned selection, including private testimony."""
    if is_dataclass(value):
        return {field.name: native_json(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"exact_bytes_hex": value.hex()}
    if isinstance(value, dict):
        return {key: native_json(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [native_json(child) for child in value]
    return value


def wire(value):
    return json.dumps(native_json(value), sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def observation(call):
    from floati.errors import FloatiError
    try:
        return {"bytes": wire(call()).hex()}
    except (FloatiError, OSError) as failure:
        return {"refusal": {"type": type(failure).__module__ + "." + type(failure).__qualname__,
            "code": getattr(failure, "code", None), "detail": getattr(failure, "detail", str(failure)),
            "remedy": getattr(failure, "remedy", None)}}


@contextmanager
def replaced(owner, name, replacement):
    original = getattr(owner, name)
    setattr(owner, name, replacement)
    try:
        yield original
    finally:
        setattr(owner, name, original)


class NativeWork:
    """Count real native replay calls with one stable semantic identity per run."""
    def __enter__(self):
        from floati import cursor_presentations as cp, epoch_carry, epoch_prefix, wake_hold
        self.counts = Counter()
        self.originals = []
        self.archive_context = []
        for owner, name, label in (
            (cp, "project_wake_items", "presentation_projector"),
            (wake_hold, "project_wake_items", "native_projector"),
            (cp, "_decode_path_records", "presentation_decoder"),
            (epoch_carry, "_decode", "archive_decoder"),
            (epoch_carry.ArchiveProofResolver, "_project_archived_cut", "archive_cut"),
            (epoch_prefix._ArchivePrefixProjection, "_admit", "projection_admission"),
            (wake_hold.WakeHoldController, "_read", "controller_reader"),
        ):
            original = getattr(owner, name)
            self.originals.append((owner, name, original))
            def delegate(*args, _native=original, _label=label, **kwargs):
                if _label == "archive_cut":
                    # Native _project_archived_cut(self, source, counts, home,
                    # node, worker) owns the physical archive coordinate. Keep
                    # the context through its unchanged native call only.
                    self.counts[_label] += 1
                    self.archive_context.append((args[0], args[1], args[3]))
                    try:
                        return _native(*args, **kwargs)
                    finally:
                        self.archive_context.pop()
                if _label == "projection_admission":
                    # Classify an admission only when its exact source object
                    # is in an active native archived-cut call AND that home
                    # is in the resolver's receipt-owned membership. A roll
                    # head says nothing about current versus archived owner.
                    owned = any(source is args[1] and home != resolver.root.tenant_home
                        and home in dict(resolver._paths or ())
                        for resolver, source, home in self.archive_context)
                    self.counts["archive_admission" if owned else "other_projection_admission"] += 1
                else:
                    self.counts[_label] += 1
                return _native(*args, **kwargs)
            setattr(owner, name, delegate)
        return self

    def reset(self):
        self.counts.clear()

    def snapshot(self):
        return dict(self.counts)

    def __exit__(self, *_exc):
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)


class BoundedOutput(io.StringIO):
    def __init__(self, *, fail_flush=False):
        super().__init__()
        self.fail_flush = fail_flush
        self.flush_attempts = 0
        self.flush_successes = 0

    def write(self, value):
        if self.tell() + len(value) > 1048576:
            raise RuntimeError("native fixture output exceeded 1 MiB")
        return super().write(value)

    def flush(self):
        self.flush_attempts += 1
        if self.fail_flush:
            raise OSError("fixture output flush failed")
        result = super().flush()
        self.flush_successes += 1
        return result

    def evidence(self):
        return {"stdout": self.getvalue(), "flush_attempts": self.flush_attempts,
                "flush_successes": self.flush_successes}


@contextmanager
def private_runtime(base):
    """Keep process ambient HOME/TMP and cwd bounded and restore even on failure."""
    base.mkdir(parents=True, exist_ok=True)
    before = {key: os.environ.get(key) for key in ("HOME", "TMPDIR", "TMP", "TEMP")}
    previous_tempdir, previous_cwd = tempfile.tempdir, Path.cwd()
    for name in ("home", "tmp"):
        (base / name).mkdir(mode=0o700, exist_ok=True)
    os.environ.update(HOME=str(base / "home"), TMPDIR=str(base / "tmp"),
                      TMP=str(base / "tmp"), TEMP=str(base / "tmp"))
    tempfile.tempdir = str(base / "tmp")
    try:
        yield
    finally:
        os.chdir(previous_cwd)
        tempfile.tempdir = previous_tempdir
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class NativeFixture:
    def __init__(self, base, harness, *, root_home=None):
        from floati.codex_wait_contract import (CodexWaitConsentLedger,
            resolve_participant)
        from floati.cursor_presentations import CursorPresentationLedger
        from floati.planes import AuthorityGrantStore
        from floati.registry import Registry
        from floati.root import FloatiRoot
        self.base, self.harness, self.node = base, harness, "seat"
        base.mkdir(parents=True)
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.root = FloatiRoot.open_direct_home(base / "fleet" if root_home is None else root_home, create=True)
        for node in ("sender", self.node, "other"):
            Registry(self.root).register(node, "Codex")
        self.map_path = self.root.tenant_home / "codex-wait/workspaces.v0.json"
        self.map_path.parent.mkdir(exist_ok=True)
        self.map_path.write_bytes(wire({"schema_version": 0, "tenant_id": self.root.tenant_id,
            "mappings": [{"workspace": str(self.workspace), "node_id": self.node}]}) + b"\n")
        participant = resolve_participant(self.root.tenant_home, self.workspace)
        if participant is None:
            raise RuntimeError("native fixture mapping did not resolve")
        self.binding = participant.binding
        CodexWaitConsentLedger(self.root).arm(self.binding, hook_timeout_seconds=10,
            wait_deadline_seconds=3, idempotency_key="memo-consent")
        self.arm(SESSION)
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "sender", 1,
                                                   datetime.now(timezone.utc))
        self.plane = CursorPresentationLedger(self.root)
        self.rolls = 0

    def arm(self, session):
        from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
        consent = CodexWaitConsentLedger(self.root).require_armed(self.binding)
        return CodexWaitSessionLedger(self.root).arm(self.binding, consent, session,
            idempotency_key="memo-arm-" + session, take_over=True)

    def send(self, key, *, sender="sender", recipient="seat", **kwargs):
        from floati.events import EventLog
        return EventLog(self.root).send(sender, recipient, "floati", "a" * 40,
            "docs/evidence/memo.md", "native memo envelope", idempotency_key=key, **kwargs)["message"]

    def deliveries(self):
        from floati.cursor import SparseCursor
        from floati.jsonl import read_records_snapshot
        from floati.records import WAKE_HOLD_KINDS
        return read_records_snapshot(self.root, SparseCursor(self.root)._delivery_relative_path_for(self.node),
            allowed_kinds=set(WAKE_HOLD_KINDS))

    @contextmanager
    def guarded(self):
        from floati.bus_epoch import epoch_guard
        from floati.wake_hold import wake_coordination_guard
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, self.node):
            yield

    def authority(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
        consent = CodexWaitConsentLedger(self.root).require_armed(self.binding)
        classification = CodexWaitSessionLedger(self.root)._participate_already_guarded(
            self.binding, consent, SESSION, classified=True)
        return self.binding, consent, classification

    def select(self, *, plane=None, native=False, authority=None):
        from floati.cursor_presentations import CursorPresentationLedger
        plane = (CursorPresentationLedger(self.root) if native else self.plane) if plane is None else plane
        with self.guarded():
            return self._select_guarded(plane, native=native, authority=authority)

    def _select_guarded(self, plane, *, native=False, authority=None):
        coordinates = self.authority() if authority is None else authority
        select = plane.select if native else plane._select_waiter
        return select(*coordinates, harness=self.harness)

    def publish(self, *, stage="flushed"):
        from floati.cursor_presentations import limit_selection, notification
        with self.guarded():
            output = BoundedOutput()
            self.last_publication_output = output
            selection = self._select_guarded(self.plane)
            selected, body = limit_selection(selection, lambda rows: notification(
                self.root.path, self.node, self.base, SESSION, rows))
            prepared = self.plane.prepare(selected, body)
            if stage == "prepared":
                return prepared
            delivery = self.plane.deliver(prepared, selected)
            if stage == "delivered":
                return prepared
            output.write(json.dumps(body) + "\n")
            output.flush()
            return self.plane.flushed(prepared, delivery)

    def roll(self, *, legacy=True):
        from floati import bus_epoch
        from floati.epoch_carry import build_carry_plan
        def planner(root, old):
            plan = build_carry_plan(root, old)
            return plan.live_payloads, {"trigger": "ack", "policy": {"max_bytes": 1,
                "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": next(len(raw) for name, _plane, raw in old if name == "events.jsonl"),
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None, "due_reasons": ["bytes"]}
        self.rolls += 1
        if legacy:
            # These memo controls exercise existing imported-proof consumers.
            # Keep their pre-v3 physical carry, behind a genuine native roll.
            # Current-writer qualification explicitly passes legacy=False.
            from floati.framing import encode_frame
            with bus_epoch.epoch_guard(self.root, exclusive=True):
                old = bus_epoch._read_selected_epoch_payloads(self.root,
                    bus_epoch._preflight_selected_members(self.root))
                retained, _metadata = planner(self.root, old)
            result = bus_epoch.roll_bus_epoch(self.root, actor='sender',
                idempotency_key='memo-legacy-fixture-' + str(self.rolls))
            with bus_epoch.epoch_guard(self.root, exclusive=True):
                for relative, _plane, raw in retained:
                    path = self.root.path / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes((encode_frame(result['receipt'])
                        if relative == 'events.jsonl' else b'') + raw)
            return result
        return bus_epoch.roll_automatic_epoch(self.root, actor="sender", planner=planner)

    def seed_rolled(self):
        from floati.wake_hold import WakeHoldController
        self.first, self.second = self.send("held-first"), self.send("held-second")
        WakeHoldController(self.root).evaluate(self.node, idempotency_key="native-held-pair")
        self.publish()
        self.roll()

    def adapter(self, *, on_sleep=None, on_each_sleep=None, output=None):
        from floati import codex_wait, cursor_wait
        clock, sleeps = [0.0], []
        output = BoundedOutput() if output is None else output
        errors = BoundedOutput()
        def sleep(seconds):
            clock[0] += seconds
            sleeps.append(seconds)
            if on_sleep is not None and len(sleeps) == 1:
                on_sleep()
            if on_each_sleep is not None:
                on_each_sleep(len(sleeps))
        previous = Path.cwd()
        os.chdir(self.workspace)
        status, failure = None, None
        try:
            if self.harness == "codex":
                status = codex_wait.run_stop_waiter(bus_home=self.root.tenant_home,
                    hook_payload={"cwd": str(self.workspace), "session_id": SESSION},
                    stdout=output, stderr=errors, monotonic=lambda: clock[0], wait_readable=scripted_readiness((sleep), interval=(1.0)), sleep=sleep)
            else:
                status = cursor_wait.run_cursor_stop_wait(root=self.root.path, node=self.node,
                    runtime=self.base, deadline_seconds=3, poll_seconds=1, hook_timeout_seconds=10,
                    payload={"status": "completed", "conversation_id": SESSION, "loop_count": 0},
                    stdout=output, stderr=errors, last_seen_root=self.workspace,
                    clock=lambda: clock[0], sleep=sleep,
                    wait_readable=scripted_readiness(sleep, interval=1.0),
                    ledger_mtime=lambda: 0)
        except BaseException as exc:
            failure = {"type": type(exc).__name__, "detail": str(exc)}
            raise
        finally:
            os.chdir(previous)
            self.last_adapter = {"status": status, **output.evidence(), "stderr": errors.getvalue(),
                                 "sleeps": sleeps, "exception": failure}
        return self.last_adapter


class NativeFixtureBank:
    """Restore one native seed at its original absolute paths for every case.

    The saved tree is outside the case's root, so even destructive physical
    controls cannot change the next case's bytes. Each restore gets a fresh
    presentation owner; no memo or public mutable result survives between cases.
    Unrolled suffix/publication/adapter controls keep their original unrolled
    starting state instead of inheriting the rolled-history seed.
    """
    def __init__(self, base, harness, *, rolled=True):
        self.rolled = rolled
        self.seed = NativeFixture(base / "native", harness)
        if rolled:
            self.seed.seed_rolled()
        self.saved = base / "saved-native"
        shutil.copytree(self.seed.base, self.saved)

    def restore(self):
        from floati.cursor_presentations import CursorPresentationLedger
        shutil.rmtree(self.seed.base)
        shutil.copytree(self.saved, self.seed.base)
        fixture = copy.copy(self.seed)
        if self.rolled:
            fixture.first, fixture.second = copy.deepcopy((self.seed.first, self.seed.second))
        fixture.plane = CursorPresentationLedger(fixture.root)
        return fixture


def outcome():
    return {"checks": {}, "observations": {}}


def delivery_checks(result, label, fixture, expected_batches):
    rows = fixture.deliveries()
    result["observations"][label + "_native_deliveries"] = rows
    result["checks"][label + "_exact_delivery_count_and_order"] = (
        len(rows) == len(expected_batches) and [row["item_ids"] for row in rows] == expected_batches)
    result["checks"][label + "_native_delivery_content"] = (
        len({row["id"] for row in rows}) == len(rows)
        and all(row["kind"] in {"delivery_receipt", "delivered_unpresented"}
            and row["tenant_id"] == fixture.root.tenant_id and row["recipient"] == fixture.node
            and row.get("worker_session_id") is None for row in rows))
    return rows


def emission_checks(result, label, fixture, observed, messages, *, bodies=1, flushed=1,
                    held=False):
    """Decode complete stdout bodies; exact message batches catch duplicates."""
    decoded, batches, envelopes, parse_failure = [], [], [], None
    headers_valid = True
    try:
        decoded = [json.loads(line) for line in observed["stdout"].splitlines()]
        for body in decoded:
            if not isinstance(body, dict):
                headers_valid = False
                continue
            text = body.get("reason", "") if fixture.harness == "codex" else body.get("followup_message", "")
            if not isinstance(text, str):
                headers_valid = False
                continue
            if fixture.harness == "codex":
                first_line = text.splitlines()[0] if text else ""
                match = re.fullmatch(r"\[floati\] ([0-9]+) new message\(s\) for seat: (.*)", first_line)
                if match is not None:
                    ids = match.group(2).split(", ")
                    batches.append(ids)
                    headers_valid = headers_valid and int(match.group(1)) == len(ids) and body.get("decision") == "block"
            if "Exact native envelopes:\n" in text:
                rows = json.loads(text.split("Exact native envelopes:\n", 1)[1].splitlines()[0])
                envelopes.append(rows)
                if fixture.harness == "cursor":
                    batches.append([row["id"] for row in rows])
    except (ValueError, TypeError, KeyError, IndexError) as failure:
        parse_failure = {"type": type(failure).__name__, "detail": str(failure)}
    result["observations"][label + "_output"] = {
        **observed, "decoded_bodies": decoded, "message_batches": batches,
        "native_envelope_batches": envelopes, "parse_failure": parse_failure}
    wanted_batches = [] if messages is None else [[message["id"] for message in messages]]
    wanted_envelopes = [] if messages is None or (fixture.harness == "codex" and not held) else [messages]
    result["checks"][label + "_exact_body_count"] = parse_failure is None and len(decoded) == bodies
    result["checks"][label + "_exact_selected_ids_and_order"] = headers_valid and batches == wanted_batches
    result["checks"][label + "_complete_native_envelopes"] = wire(envelopes) == wire(wanted_envelopes)
    result["checks"][label + "_observed_flush_successes"] = observed["flush_successes"] == flushed


def equivalent(result, name, fixture, *, refusal=False):
    expected = observation(lambda: fixture.select(native=True))
    actual = observation(fixture.select)
    result["observations"][name] = {"native": expected, "waiter": actual}
    result["checks"][name + "_exact_bytes_or_refusal"] = actual == expected
    if refusal:
        result["checks"][name + "_really_refuses"] = "refusal" in expected
    return actual


def unchanged(base, harness):
    from floati.cursor_presentations import CursorPresentationLedger
    f, result = NativeFixture(base, harness), outcome()
    f.seed_rolled()
    f.plane = CursorPresentationLedger(f.root)
    with NativeWork() as work:
        cold = f.select()
        cold_counts = work.snapshot()
        work.reset()
        warm = [wire(f.select()) for _ in range(3)]
        warm_counts = work.snapshot()
    result["observations"].update(cold=cold_counts, warm=warm_counts)
    result["checks"].update(
        complete_unchanged_selection_bytes=all(value == wire(cold) for value in warm),
        cold_did_native_admission=sum(cold_counts.values()) > 0,
        one_cold_archive_admission=cold_counts.get("archive_admission", 0) == 1,
        unchanged_polls_do_not_replay=sum(warm_counts.values()) == 0,
        held_ids_preserved=[row["item_id"] for row in cold["held_items"]] == [f.first["id"], f.second["id"]],
        no_second_automatic_attempt=cold["fresh_messages"] == [])
    equivalent(result, "native_replay", f)
    return result


def append_suffix(base, harness):
    from floati.cursor import SparseCursor
    from floati.events import EventLog
    result = outcome()
    fixtures = NativeFixtureBank(base, harness, rolled=False)
    for kind in ("fresh", "foreign", "reply", "worker_retraction", "delivery", "ack"):
        f = fixtures.restore()
        first = f.send("first", sender="seat", recipient="sender") if kind == "reply" else f.send("first")
        if kind == "ack":
            EventLog(f.root).present(f.node)
        if kind == "worker_retraction":
            worker = f.send("worker", worker_session_id="worker-memo")
        with NativeWork() as work:
            f.select()
            if kind == "fresh":
                added = f.send("fresh")
            elif kind == "foreign":
                f.send("foreign", recipient="other")
            elif kind == "reply":
                added = f.send("reply", reply_to=first["id"])
            elif kind == "worker_retraction":
                EventLog(f.root).retract(worker["id"], worker_session_id="worker-memo",
                    reason="sent_in_error", author="sender")
            elif kind == "delivery":
                EventLog(f.root).present(f.node)
            else:
                SparseCursor(f.root).ack(f.node, [first["id"]], acting_session_id=SESSION)
            expected = observation(lambda: f.select(native=True))
            work.reset()
            actual = observation(f.select)
            counts = work.snapshot()
        result["observations"][kind] = {"native": expected, "waiter": actual, "work": counts}
        result["checks"][kind + "_complete_bytes"] = expected == actual and "bytes" in actual
        result["checks"][kind + "_hereditary_no_full_projector"] = (
            counts.get("presentation_projector", 0) + counts.get("native_projector", 0) == 0)
        if kind in ("fresh", "reply"):
            result["checks"][kind + "_new_mail_visible"] = added["id"] in [
                row["id"] for row in f.select()["fresh_messages"]]
    return result


def _imported_hold_ids(source):
    _events, ep, deliveries, dp, _acks, ap = source
    return [row["id"] for ordinal, row in enumerate(deliveries)
        if row["kind"] == "wake_hold_receipt" and (
            row["delivery_prefix_digest"] != dp[ordinal]
            or row["event_prefix_digest"] not in ep
            or row["acknowledgment_prefix_digest"] not in ap)]


class RolledForeignWork:
    """Local delegates distinguish current admission from historical proof work.

    Installed before the first cold selection and unchanged through every
    oracle/candidate call. NativeWork remains unchanged for the old controls.
    No delegate changes a native argument, returned object or exception.
    """
    def __enter__(self):
        from floati import cursor_presentations as cp, epoch_carry
        self.counts, self.originals = Counter(), []
        self.current_depth = self.history_depth = self.proof_depth = 0

        def install(owner, name, wrapper):
            original = getattr(owner, name)
            self.originals.append((owner, name, original))
            setattr(owner, name, wrapper(original))

        def current(original):
            def delegated(*args, **kwargs):
                self.counts["current_state_entry"] += 1
                self.current_depth += 1
                try:
                    return original(*args, **kwargs)
                finally:
                    self.current_depth -= 1
            return delegated

        def historical(original):
            def delegated(index, counts):
                self.counts["historical_native"] += 1
                source = tuple(part for axis, count in zip((0, 2, 4), counts)
                    for part in (index.source[axis][:count], index.source[axis + 1][:count + 1]))
                if _imported_hold_ids(source):
                    self.counts["imported_historical_native"] += 1
                if (index.root is not None and index.home == index.root.tenant_home
                        and counts == tuple(len(index.source[axis]) for axis in (0, 2, 4))):
                    self.counts["current_full_historical_native"] += 1
                self.history_depth += 1
                try:
                    return original(index, counts)
                finally:
                    self.history_depth -= 1
            return delegated

        def proof(original):
            def delegated(*args, **kwargs):
                label = ("recursive_proof" if self.proof_depth else "historical_proof"
                    if self.history_depth else "current_proof" if self.current_depth else "other_proof")
                self.counts[label] += 1
                self.proof_depth += 1
                try:
                    return original(*args, **kwargs)
                finally:
                    self.proof_depth -= 1
            return delegated

        def origin(original):
            def delegated(*args, **kwargs):
                self.counts["hold_origin"] += 1
                self.counts["historical_origin" if self.history_depth else
                    "current_origin" if self.current_depth else "other_origin"] += 1
                return original(*args, **kwargs)
            return delegated

        def projector(original):
            def delegated(*args, **kwargs):
                if self.current_depth and not self.history_depth:
                    self.counts["full_current_projector"] += 1
                return original(*args, **kwargs)
            return delegated

        install(cp.CursorPresentationLedger, "_current_states", current)
        install(cp._HistoricalCuts, "_native", historical)
        install(epoch_carry, "verified_hold_proofs", proof)
        install(epoch_carry.ArchiveProofResolver, "_hold_origin", origin)
        install(cp, "project_wake_items", projector)
        return self

    def reset(self):
        self.counts.clear()

    def snapshot(self):
        return dict(self.counts)

    def __exit__(self, *_exc):
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)


def _rolled_input_snapshot(f):
    """Exact selected bytes/identities plus the private fixture's archive tree."""
    from floati.codex_wait_contract import CodexWaitSessionLedger, consent_ledger_relative
    from floati.cursor import SparseCursor
    import stat
    root = f.root.tenant_home
    cursor = SparseCursor(f.root)
    selected = (Path("events.jsonl"), cursor._delivery_relative_path_for(f.node),
        cursor._relative_path_for(f.node), f.plane.relative(f.node), f.plane.witness_relative(f.node),
        consent_ledger_relative(f.node), CodexWaitSessionLedger._relative(f.node),
        Path("codex-wait/workspaces.v0.json"), Path("receipts/wake-control") / (f.node + ".jsonl"))

    def entry(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        value = {"identity": [getattr(info, name) for name in IDENTITY_FIELDS]}
        if stat.S_ISREG(info.st_mode):
            value["bytes"] = path.read_bytes().hex()
        elif stat.S_ISDIR(info.st_mode):
            value["members"] = sorted(child.name for child in path.iterdir())
        else:
            value["ordinary"] = False
        return value

    coordinates = {}
    for relative in selected:
        path = root / relative
        parents = []
        for parent in path.parents:
            if parent != root and root not in parent.parents:
                break
            try:
                info = parent.lstat()
                parents.append((str(parent), info.st_dev, info.st_ino))
            except FileNotFoundError:
                parents.append((str(parent), None, None))
        coordinates[str(relative)] = {"leaf": entry(path), "parents": parents}
    archives = {}
    for archive in sorted(root.glob("archive-*")):
        for path in (archive, *sorted(archive.rglob("*"))):
            archives[str(path.relative_to(root))] = entry(path)
    return {"coordinates": coordinates, "archives": archives}


def _rolled_foreign_send(f, key):
    """Retain the ordinary writer's complete return and immutable testimony."""
    from floati.events import EventLog
    from floati.framing import encode_frame
    receipt = EventLog(f.root).send("sender", "other", "floati", "a" * 40,
        "docs/evidence/memo.md", "native memo envelope", idempotency_key=key)
    return {"receipt": native_json(receipt),
        "message_frame": encode_frame(receipt["message"]).hex(),
        "readiness_bytes": wire(receipt["recipient_readiness"]).hex()}


def _rolled_foreign_premises(before, after, native_send, *, tenant):
    from floati.events import validate_event_records
    from floati.framing import decode_frames, encode_frame
    from floati.records import validate_record
    receipt = native_send["receipt"]
    message = receipt["message"]
    old, new = (snapshot["coordinates"]["events.jsonl"] for snapshot in (before, after))
    old_leaf, leaf = old["leaf"], new["leaf"]
    raw, appended = bytes.fromhex(old_leaf["bytes"]), bytes.fromhex(leaf["bytes"])
    suffix = appended[len(raw):] if appended.startswith(raw) else b""
    frames, rows = suffix.splitlines(keepends=True), decode_frames(suffix)
    # The fresh, ordinary no-claim/no-execution send has exactly this pair.
    # Validate the real companion; do not allow arbitrary known/future rows.
    kinds = frozenset({"message_envelope", "send_readiness_observation"})
    admitted = [validate_record(dict(row), tenant, kinds, integrity=True) for row in rows]
    validate_event_records(admitted)
    readiness = rows[1] if len(rows) == 2 else {}
    old_ids = {row["id"] for row in decode_frames(raw)}
    canonical_pair = (len(rows) == len(frames) == 2
        and [row["kind"] for row in rows] == ["message_envelope", "send_readiness_observation"]
        and all(frame == encode_frame(row) for frame, row in zip(frames, admitted))
        and len({row["id"] for row in rows}) == 2
        and not old_ids.intersection(row["id"] for row in rows))
    other = lambda snapshot: {name: value for name, value in snapshot["coordinates"].items()
                              if name != "events.jsonl"}
    checks = {
        "same_inode_strict_growth_full_physical_prefix": (
            old_leaf["identity"][:4] == leaf["identity"][:4]
            and old["parents"] == new["parents"] and len(appended) > len(raw)
            and canonical_pair and appended == raw + b"".join(encode_frame(row) for row in rows)),
        "canonical_ordinary_foreign_envelope": (
            canonical_pair and rows[0] == message and frames[0].hex() == native_send["message_frame"]
            and message["tenant_id"] == tenant and message["sender"] == "sender"
            and message["recipient"] == "other" and message.get("attempt_binding") == "absent_legacy"
            and not {"worker_session_id", "reply_to"}.intersection(message)
            and set(receipt) == {"message", "recipient_readiness"}),
        "exact_native_readiness_companion": (
            canonical_pair and readiness.get("schema_version") == 1
            and readiness.get("tenant_id") == tenant and readiness.get("envelope_id") == message["id"]
            and all(readiness.get(field) == message[field] for field in (
                "sender", "recipient", "idempotency_key", "timestamp"))
            and readiness.get("observed_at") == message["timestamp"]
            and wire(readiness.get("readiness")).hex() == native_send["readiness_bytes"]
            and wire(receipt["recipient_readiness"]).hex() == native_send["readiness_bytes"]),
        "all_other_selected_and_archive_coordinates_exact": (
            other(before) == other(after) and before["archives"] == after["archives"]),
    }
    return checks, rows


def rolled_foreign_append(base, harness):
    from floati.cursor import SparseCursor
    from floati.cursor_presentations import CursorPresentationLedger
    from floati.epoch_carry import ArchiveProofResolver
    from floati.events import EventLog
    from floati.framing import encode_frame
    result = outcome()
    rolled = NativeFixtureBank(base, harness)

    def checks(label, values):
        result["checks"].update((label + "_" + name, value) for name, value in values.items())

    def no_projection_replay(label, work):
        checks(label, {name + "_not_replayed": work.get(name, 0) == 0 for name in (
            "current_proof", "full_current_projector", "imported_historical_native", "hold_origin")})

    for variant in ("archived_history", "current_history_adoption"):
        f = rolled.restore()
        if variant == "current_history_adoption":
            f.send("post-roll-presentation")
            f.publish()
        # Reset ownership, not data: cold admission must really use this new
        # plane with every stable delegating observer already installed.
        f.plane = CursorPresentationLedger(f.root)
        with NativeWork() as native, RolledForeignWork() as work:
            cold = f.select()
            cold_work, cold_native = work.snapshot(), native.snapshot()
            work.reset()
            native.reset()
            warm = f.select()
            warm_work, warm_native = work.snapshot(), native.snapshot()
            source = warm["_source"]
            current_history = [row for row in warm["_rows"] if row["phase"] == "prepared"
                               and row["epoch_anchor"] == warm["epoch_anchor"]]
            checks(variant, {
                "real_rolled_imported_history": bool(source[0]
                    and source[0][0]["kind"] == "bus_epoch_roll_receipt"
                    and _imported_hold_ids(source) and warm["_rows"]),
                "cold_native_admission": cold_native.get("presentation_projector", 0)
                    + cold_native.get("native_projector", 0) > 0,
                "same_plane_unchanged_selection": wire(cold) == wire(warm),
                "warm_no_proof_or_projection": not warm_work
                    and warm_native.get("presentation_projector", 0) + warm_native.get("native_projector", 0) == 0,
                "actual_cold_route": (
                    bool(current_history) and cold_work.get("current_full_historical_native", 0) > 0
                    and cold_work.get("imported_historical_native", 0) > 0
                    and cold_work.get("current_proof", 0) == 0
                    and cold_work.get("full_current_projector", 0) == 0
                    if variant == "current_history_adoption" else
                    not current_history and cold_work.get("current_proof", 0) > 0
                    and cold_work.get("full_current_projector", 0) > 0),
            })
            result["observations"][variant + "_cold"] = {
                "cold_work": cold_work, "cold_native": cold_native,
                "warm_work": warm_work, "warm_native": warm_native,
                "imported_ids": _imported_hold_ids(source), "current_prepared": current_history,
                "cold_selection": wire(cold).hex(), "warm_selection": wire(warm).hex()}
            for ordinal in (1, 2):
                label = variant + "_foreign_" + str(ordinal)
                before = _rolled_input_snapshot(f)
                native_send = _rolled_foreign_send(f, "foreign-" + str(ordinal))
                message = native_send["receipt"]["message"]
                appended = _rolled_input_snapshot(f)
                premises, suffix_rows = _rolled_foreign_premises(
                    before, appended, native_send, tenant=f.root.tenant_id)
                checks(label, premises)
                expected = observation(lambda: f.select(native=True))
                after_oracle = _rolled_input_snapshot(f)
                work.reset()
                native.reset()
                selected = f.select()
                actual = {"bytes": wire(selected).hex()}
                renewed, native_counts = work.snapshot(), native.snapshot()
                after = _rolled_input_snapshot(f)
                checks(label, {
                    "complete_independent_native_selection": expected == actual and "bytes" in expected,
                    "same_fresh_held_ids_and_order": (
                        selected["fresh_messages"] == warm["fresh_messages"]
                        and selected["held_items"] == warm["held_items"]),
                    "new_source_testimony_includes_exact_foreign_append": (
                        selected["_source"][0][-2:] == suffix_rows
                        and len(selected["_source"][0]) == len(source[0]) + 2 * ordinal
                        and b"".join(encode_frame(row) for row in selected["_source"][0]).hex()
                            == appended["coordinates"]["events.jsonl"]["leaf"]["bytes"]),
                    "no_oracle_or_candidate_publication_or_ack": appended == after_oracle == after,
                })
                no_projection_replay(label, renewed)
                result["observations"][label] = {"before": before, "appended": appended,
                    "after_oracle": after_oracle, "after": after, "message": message,
                    "native_send": native_send, "suffix_rows": suffix_rows,
                    "native": expected, "waiter": actual, "work": renewed, "native_work": native_counts}

    # Each invalidation starts AFTER successful rolled admission and a real
    # foreign append on the same plane. Existing physical/semantic groups keep
    # their exhaustive inode/parent/race/schema/authority checks unchanged.
    for kind in ("target", "worker", "reply", "worker_retraction", "delivery", "ack",
                 "malformed_known", "unknown_valid", "old_prefix", "archive_member",
                 "archive_identity", "history_identity", "events_identity"):
        f = rolled.restore()
        if kind == "worker_retraction":
            worker = f.send("worker-to-retract", worker_session_id="rolled-worker")
        f.plane = CursorPresentationLedger(f.root)
        with NativeWork() as native, RolledForeignWork() as work:
            admitted = f.select()
            f.send("first-foreign", recipient="other")
            foreign = f.select()
            before = _rolled_input_snapshot(f)
            events = f.root.tenant_home / "events.jsonl"
            if kind == "target":
                target = f.send("relevant-target")
            elif kind == "worker":
                f.send("worker-boundary", recipient="other", worker_session_id="rolled-worker")
            elif kind == "reply":
                f.send("reply-boundary", sender=f.node, recipient="sender", reply_to=f.first["id"])
            elif kind == "worker_retraction":
                EventLog(f.root).retract(worker["id"], worker_session_id="rolled-worker",
                    reason="sent_in_error", author="sender")
            elif kind == "delivery":
                EventLog(f.root).present(f.node)
            elif kind == "ack":
                SparseCursor(f.root).ack(f.node, [f.first["id"]], acting_session_id=SESSION)
            elif kind in ("malformed_known", "unknown_valid"):
                row = (dict(f.first, id="msg-malformed-rolled-tail", schema_version=False)
                    if kind == "malformed_known" else {
                        "kind": "memo_future_record", "schema_version": 99,
                        "tenant_id": f.root.tenant_id, "id": "unknown-rolled-tail",
                        "timestamp": f.first["timestamp"], "future_payload": "one"})
                with events.open("ab") as stream:
                    stream.write(encode_frame(row))
            elif kind == "old_prefix":
                events.write_bytes(events.read_bytes().replace(
                    b"native memo envelope", b"native memo envelopf", 1))
            elif kind == "archive_member":
                archive = ArchiveProofResolver(f.root).archives()[0]
                (archive / "extra-unowned.jsonl").write_bytes(b"{}\n")
            else:
                path = (ArchiveProofResolver(f.root).archives()[0] / "events.jsonl"
                    if kind == "archive_identity" else f.root.tenant_home / f.plane.relative(f.node)
                    if kind == "history_identity" else events)
                replacement = f.base / "replacement-input"
                replacement.write_bytes(path.read_bytes())
                replacement.replace(path)
            changed = _rolled_input_snapshot(f)
            expected = observation(lambda: f.select(native=True))
            work.reset()
            native.reset()
            actual = observation(f.select)
            renewed, native_counts = work.snapshot(), native.snapshot()
            after = _rolled_input_snapshot(f)
        label = "boundary_" + kind
        refuses = kind in {"malformed_known", "old_prefix", "archive_member"}
        checks(label, {
            "starts_with_successful_imported_foreign_admission": (
                bool(_imported_hold_ids(admitted["_source"]))
                and foreign["_source"][0][-1]["recipient"] == "other"),
            "input_really_changed": before != changed,
            "exact_independent_native_result_or_refusal": expected == actual,
            "expected_native_success_or_refusal": ("refusal" if refuses else "bytes") in expected,
            "no_candidate_or_oracle_write": changed == after,
        })
        if not refuses:
            checks(label, {"native_admission_renewed": renewed.get("current_proof", 0)
                + renewed.get("historical_native", 0) > 0})
        # Never require a later projector after the original parser/archive
        # reader already refused. Exact native error bytes are the oracle.
        if kind == "target" and "bytes" in actual:
            selected = json.loads(bytes.fromhex(actual["bytes"]))
            checks(label, {"target_visible_once": [row["id"] for row in selected["fresh_messages"]]
                == [target["id"]]})
        result["observations"][label] = {"before": before, "changed": changed, "after": after,
            "native": expected, "waiter": actual, "work": renewed, "native_work": native_counts}

    # The actual adapters own their planes/controller. Observe the interval
    # after a real foreign writer and before a later target writer; never swap
    # an adapter callback or reuse a plane-only result as publication evidence.
    f = NativeFixture(base / "adapter-foreign-then-target", harness)
    f.seed_rolled()
    output, phases = BoundedOutput(), {}
    before_deliveries = f.deliveries()
    before_history = (f.root.tenant_home / f.plane.relative(f.node)).read_bytes()
    with NativeWork() as native, RolledForeignWork() as work:
        def each_sleep(ordinal):
            if ordinal == 1:
                before = _rolled_input_snapshot(f)
                native_send = _rolled_foreign_send(f, "adapter-foreign")
                phases["foreign"] = {"before": before, "appended": _rolled_input_snapshot(f),
                    "message": native_send["receipt"]["message"], "native_send": native_send}
                native.reset()
                work.reset()
            elif ordinal == 2:
                phases["after_foreign"] = {"work": work.snapshot(), "native_work": native.snapshot(),
                    "output": output.evidence(), "snapshot": _rolled_input_snapshot(f)}
                phases["target"] = f.send("adapter-target")
        observed = f.adapter(output=output, on_each_sleep=each_sleep)
    result["observations"]["adapter_phases"] = phases
    checks("adapter", {"reached_foreign_and_later_target": set(phases) == {"foreign", "after_foreign", "target"},
        "native_adapter_completed": observed["status"] == 0 and observed["exception"] is None})
    if "after_foreign" in phases:
        interval = phases["after_foreign"]
        premises, suffix_rows = _rolled_foreign_premises(phases["foreign"]["before"],
            phases["foreign"]["appended"], phases["foreign"]["native_send"], tenant=f.root.tenant_id)
        phases["foreign"]["suffix_rows"] = suffix_rows
        checks("adapter_foreign", premises)
        checks("adapter_foreign", {
            "no_body_flush_or_publication_before_target": (
                interval["output"] == {"stdout": "", "flush_attempts": 0, "flush_successes": 0}
                and interval["snapshot"] == phases["foreign"]["appended"]),
            "distinct_native_controller_contract": (
                interval["native_work"].get("controller_reader", 0) > 0 if harness == "codex"
                else interval["native_work"].get("controller_reader", 0) == 0),
            "real_conversation_selection_observed": interval["work"].get("current_state_entry", 0) > 0,
        })
        no_projection_replay("adapter_foreign", interval["work"])
    if "target" in phases:
        target = phases["target"]
        emission_checks(result, "adapter_target", f, observed, [target], held=True)
        after_deliveries = f.deliveries()
        added_deliveries = after_deliveries[len(before_deliveries):]
        history = f.plane.read(f.node)
        prior_history_count = len(before_history.splitlines())
        added_history = history[prior_history_count:]
        checks("adapter_target", {
            "old_delivery_prefix_unchanged": after_deliveries[:len(before_deliveries)] == before_deliveries,
            "one_exact_native_delivery": len(added_deliveries) == 1
                and added_deliveries[0]["kind"] in {"delivery_receipt", "delivered_unpresented"}
                and added_deliveries[0]["item_ids"] == [target["id"]]
                and added_deliveries[0]["recipient"] == f.node
                and added_deliveries[0]["tenant_id"] == f.root.tenant_id
                and added_deliveries[0].get("worker_session_id") is None,
            "one_exact_prepare_and_terminal_flush": len(added_history) == 2
                and [row["phase"] for row in added_history] == ["prepared", "flushed"]
                and all(row["item_ids"] == [target["id"]] for row in added_history)
                and len(added_deliveries) == 1
                and added_history[1]["delivery_receipt_id"] == added_deliveries[0]["id"],
            "history_prefix_unchanged": b"".join(encode_frame(row) for row in history[:prior_history_count])
                == before_history,
        })
        result["observations"]["adapter_target_receipts"] = {"before_deliveries": before_deliveries,
            "after_deliveries": after_deliveries, "added_history": added_history}
    return result


@contextmanager
def _changed_imported_proof_contract(case, work):
    """Change one identity, preserving real native proof execution/results."""
    from floati import epoch_carry
    proof_class = epoch_carry._VerifiedHoldProofs
    if case in ("verify_hold_proof_source", "verify_refusal", "binding", "verified_hold_proofs"):
        owner = epoch_carry
        name = {"verify_refusal": "verify_hold_proof_source", "binding": "_binding"}.get(case, case)
    elif case == "proof_class":
        owner, name = epoch_carry, "_VerifiedHoldProofs"
    else:
        owner, name = proof_class, {"constructor": "__init__"}.get(case, case)
    original = getattr(owner, name)
    native = (original.__init__ if case == "proof_class" else
              original.fget if case == "origins" else original)
    counts, phases = Counter(), []
    detail = "changed imported-proof validator refuses this otherwise native cut"

    def delegate(*args, **kwargs):
        counts["entered"] += 1
        phase = "historical" if work.history_depth else "current" if work.current_depth else "other"
        counts[phase] += 1
        phases.append(phase)
        value = native(*args, **kwargs)
        counts["native_returned"] += 1
        if case == "verify_refusal":
            # Explicit changed-validator input, after the original native
            # type/source validation. observation() retains the typed refusal.
            counts["refused"] += 1
            raise epoch_carry._failure(detail)
        return value

    if case == "proof_class":
        # The native issuer and exact-type validator resolve the SAME changed
        # class. Inherit every native method/seal rule; delegate construction.
        # Replacing a class with a function would violate that native contract.
        class DelegatingProofs(original):
            __init__ = delegate
        replacement = DelegatingProofs
    elif case == "origins":
        replacement = property(delegate, original.fset, original.fdel, original.__doc__)
    else:
        replacement = delegate
    with replaced(owner, name, replacement):
        yield {"counts": counts, "phases": phases, "refusal_detail": detail,
            "identity_changed": replacement is not original,
            "identity_is_current": lambda: getattr(owner, name) is replacement,
            "directly_consumed": case not in ("original", "origins")}


def rolled_foreign_contract(base, harness):
    """A warmed imported history must not hide a changed proof validator."""
    from floati.cursor_presentations import CursorPresentationLedger
    result = outcome()
    rolled = NativeFixtureBank(base, harness)

    def checks(label, values):
        result["checks"].update((label + "_" + name, value) for name, value in values.items())

    for case in ("verify_hold_proof_source", "assert_source", "validates", "binding",
                 "constructor", "proof_class", "original", "origins",
                 "verified_hold_proofs", "verify_refusal"):
        f = rolled.restore()
        f.send("current-epoch-presentation")
        f.publish()
        f.plane = CursorPresentationLedger(f.root)
        same_plane = f.plane
        # Stable observers precede admission; only the named dependency below
        # changes afterward. No permission, proof result or projector is stubbed.
        with NativeWork() as native, RolledForeignWork() as work:
            cold = f.select()
            cold_work, cold_native = work.snapshot(), native.snapshot()
            work.reset()
            native.reset()
            warm = f.select()
            warm_work, warm_native = work.snapshot(), native.snapshot()
            prepared = [row for row in warm["_rows"] if row["phase"] == "prepared"
                        and row["epoch_anchor"] == warm["epoch_anchor"]]
            checks(case, {
                "real_rolled_imported_current_history": bool(prepared
                    and _imported_hold_ids(warm["_source"])
                    and warm["_source"][0][0]["kind"] == "bus_epoch_roll_receipt"),
                "cold_full_current_historical_adoption": (
                    cold_work.get("current_full_historical_native", 0) > 0
                    and cold_work.get("imported_historical_native", 0) > 0
                    and cold_work.get("current_proof", 0) == 0
                    and cold_work.get("full_current_projector", 0) == 0),
                "warm_complete_selection_matches_cold": wire(warm) == wire(cold),
                "warm_does_not_replay_proof_or_history": not warm_work
                    and warm_native.get("presentation_projector", 0) + warm_native.get("native_projector", 0) == 0,
            })
            before = _rolled_input_snapshot(f)
            with _changed_imported_proof_contract(case, work) as changed:
                # Change the identity first, then perform the same native
                # physical send pair used by the foreign-append regression.
                native_send = _rolled_foreign_send(f, "foreign-after-contract-change")
                appended = _rolled_input_snapshot(f)
                premises, suffix_rows = _rolled_foreign_premises(
                    before, appended, native_send, tenant=f.root.tenant_id)
                checks(case, premises)
                changed["counts"].clear()
                changed["phases"].clear()
                work.reset()
                native.reset()
                expected = observation(lambda: f.select(native=True))
                oracle_calls, oracle_phases = dict(changed["counts"]), list(changed["phases"])
                oracle_work, oracle_native = work.snapshot(), native.snapshot()
                oracle_identity = changed["identity_is_current"]()
                after_oracle = _rolled_input_snapshot(f)
                # Writer/oracle work cannot make the candidate's readmission
                # assertions pass. The replacement itself stays identical.
                changed["counts"].clear()
                changed["phases"].clear()
                work.reset()
                native.reset()
                actual = observation(f.select)
                candidate_calls, candidate_phases = dict(changed["counts"]), list(changed["phases"])
                candidate_work, candidate_native = work.snapshot(), native.snapshot()
                after = _rolled_input_snapshot(f)
                checks(case, {
                    "same_plane_with_one_stable_changed_identity": (
                        f.plane is same_plane and changed["identity_changed"]
                        and oracle_identity and changed["identity_is_current"]()),
                    "complete_independent_native_result_or_refusal": wire(actual) == wire(expected),
                    "expected_native_success_or_refusal": ("refusal" if case == "verify_refusal" else "bytes") in expected,
                    "oracle_imported_history_reentered": oracle_work.get("imported_historical_native", 0) > 0,
                    "oracle_native_proof_reissued": oracle_work.get("historical_proof", 0) > 0,
                    "candidate_imported_history_reentered": candidate_work.get("imported_historical_native", 0) > 0,
                    "candidate_native_proof_reissued": candidate_work.get("historical_proof", 0) > 0,
                    "no_oracle_or_candidate_publication_or_ack": appended == after_oracle == after,
                })
                if changed["directly_consumed"]:
                    checks(case, {
                        "oracle_changed_owner_really_delegated_in_history": (
                            oracle_calls.get("historical", 0) > 0 and oracle_calls.get("native_returned", 0) > 0),
                        "candidate_changed_owner_really_delegated_in_history": (
                            candidate_calls.get("historical", 0) > 0 and candidate_calls.get("native_returned", 0) > 0),
                    })
                # original() and origins.fget are native origin-cache contract
                # dependencies, not getters invoked by presentation selection.
                # Their identity changes must renew history/proof admission;
                # do not call them artificially or invent getter work counts.
                if case == "verify_refusal":
                    refusal = expected.get("refusal", {})
                    checks(case, {
                        "oracle_changed_validator_refusal_is_exact": (
                            refusal.get("type") == "floati.errors.IntegrityFailure"
                            and refusal.get("code") == "epoch_carry_evidence_invalid"
                            and refusal.get("detail") == changed["refusal_detail"]
                            and oracle_calls.get("refused", 0) > 0),
                        "candidate_reaches_same_original_historical_refusal_point": (
                            "refusal" in actual and candidate_calls.get("refused", 0) > 0
                            and bool(oracle_phases) and bool(candidate_phases)
                            and oracle_phases[0] == candidate_phases[0] == "historical"),
                    })
                result["observations"][case] = {
                    "role": "already_bound_positive_control" if case == "verified_hold_proofs" else
                            "changed_validator_refusal" if case == "verify_refusal" else "missing_contract_dependency",
                    "owner_directly_consumed_by_selection": changed["directly_consumed"],
                    "cold_work": cold_work, "cold_native": cold_native,
                    "warm_work": warm_work, "warm_native": warm_native,
                    "cold_selection": wire(cold).hex(), "warm_selection": wire(warm).hex(),
                    "current_prepared": prepared, "before": before, "appended": appended,
                    "after_oracle": after_oracle, "after": after,
                    "native_send": native_send, "suffix_rows": suffix_rows,
                    "native": expected, "waiter": actual,
                    "oracle_calls": oracle_calls, "oracle_phases": oracle_phases,
                    "oracle_work": oracle_work, "oracle_native": oracle_native,
                    "candidate_calls": candidate_calls, "candidate_phases": candidate_phases,
                    "candidate_work": candidate_work, "candidate_native": candidate_native,
                }
    return result


def public_aliases(base, harness):
    from floati.cursor_presentations import CursorPresentationLedger
    result = outcome()
    rolled = NativeFixtureBank(base, harness)
    for graph in ("selection", "public_source", "history"):
        f = rolled.restore()
        expected = observation(lambda: f.select(native=True))
        exposed = f.select()
        if graph == "selection":
            exposed["held_items"][0]["phase"] = "poisoned"
            exposed["_source"][0][-1]["note"] = "poisoned caller source"
            exposed["_rows"][0]["item_ids"].clear()
            exposed["_witness"]["before_digest"] = "f" * 64
        elif graph == "public_source":
            public = f.plane._sources(f.root.tenant_home, f.node)
            public[0][-1]["schema_version"] = False
        else:
            records = f.plane.read(f.node)
            records[0]["item_ids"].clear()
        actual = observation(f.select)
        result["observations"][graph] = {"native": expected, "waiter": actual}
        result["checks"][graph + "_cannot_poison_private_admission"] = actual == expected
        other = CursorPresentationLedger(f.root)
        result["checks"][graph + "_fresh_owner_isolated"] = observation(lambda: f.select(plane=other)) == expected
    f = NativeFixture(base / "unrolled-fresh", harness)
    fresh = f.send("fresh")
    worker = f.send("worker-nested", attempt_binding={"attempt_id": "memo-attempt",
        "claim_id": "memo-claim", "lease_id": "memo-lease", "worker_session_id": "memo-worker"})
    expected = observation(lambda: f.select(native=True))
    exposed = f.select()
    result["checks"]["fresh_alias_fixture_is_unrolled_and_selects_exact_envelope"] = (
        [row["id"] for row in exposed["fresh_messages"]] == [fresh["id"]]
        and exposed["_source"][0][0]["kind"] != "bus_epoch_roll_receipt")
    exposed["fresh_messages"][0]["note"] = "caller poisoned fresh message"
    # Ordinary envelopes have a scalar legacy attempt binding. A real worker
    # envelope supplies the native nested object in the SAME unrolled source;
    # never invent a nested field that native send would refuse or omit.
    nested = next(row for row in exposed["_source"][0] if row["id"] == worker["id"])
    nested["attempt_binding"]["claim_id"] = "caller-poisoned-claim"
    actual = observation(f.select)
    result["observations"]["unrolled_fresh_and_nested"] = {"native": expected, "waiter": actual}
    result["checks"]["fresh_envelope_and_nested_source_cannot_poison_memo"] = actual == expected
    # Refresh order matters: an unchanged public read can return fresh decoded
    # rows while leaving older private groups untouched. Force a native append
    # BEFORE public read so its rebuilt groups have the exposed-row alias.
    from floati.jsonl import read_records_snapshot
    f = NativeFixture(base / "refreshed-public-history", harness)
    first = f.send("attempted-before-refresh")
    f.publish()
    f.select()
    f.select()
    fresh = f.send("native-append-before-public-refresh")
    expected = observation(lambda: f.select(native=True))
    public_rows = f.plane.read(f.node)
    original_rows = native_json(public_rows)
    before_deliveries = native_json(f.deliveries())
    prepared = next(row for row in public_rows if row["phase"] == "prepared")
    prepared["item_ids"].clear()
    actual = observation(f.select)
    physical = read_records_snapshot(f.root, f.plane.relative(f.node),
        allowed_kinds={"cursor_presentation_receipt"})
    result["observations"]["refreshed_public_history"] = {
        "native": expected, "waiter": actual, "original_rows": original_rows,
        "caller_mutated_rows": public_rows, "physical_rows_after_selection": physical,
        "deliveries_before": before_deliveries}
    result["checks"]["refreshed_history_exact_native_selection"] = actual == expected and "bytes" in actual
    result["checks"]["refreshed_history_mutation_is_caller_only"] = (
        physical == original_rows and physical[0]["item_ids"] == [first["id"]]
        and public_rows[0]["item_ids"] == [])
    result["checks"]["refreshed_history_selection_has_no_publication"] = (
        len(physical) == 2 and [row["phase"] for row in physical] == ["prepared", "flushed"]
        and f.deliveries() == before_deliveries)
    # Continue on the SAME plane: a fresh adapter owner would accidentally
    # discard the poisoned cache and conceal a second automatic attempt.
    publication = observation(f.publish)
    after = read_records_snapshot(f.root, f.plane.relative(f.node),
        allowed_kinds={"cursor_presentation_receipt"})
    result["observations"]["refreshed_history_publication"] = {
        "result": publication, "physical_rows": after,
        "output": f.last_publication_output.evidence(),
        "native_after": observation(lambda: f.select(native=True))}
    result["checks"]["refreshed_history_new_mail_publication_succeeds"] = "bytes" in publication
    result["checks"]["refreshed_history_exact_two_attempts_without_retry"] = (
        len(after) == 4 and [row["phase"] for row in after] == ["prepared", "flushed", "prepared", "flushed"]
        and [row["item_ids"] for row in after if row["phase"] == "prepared"] == [[first["id"]], [fresh["id"]]])
    delivered = delivery_checks(result, "refreshed_history", f, [[first["id"]], [fresh["id"]]])
    result["checks"]["refreshed_history_exact_terminal_delivery_links"] = (
        [row["delivery_receipt_id"] for row in after if row["phase"] == "flushed"]
        == [row["id"] for row in delivered])
    return result


def physical_changes(base, harness):
    from floati.framing import encode_frame
    result = outcome()
    rolled = NativeFixtureBank(base, harness)
    for kind in ("same_size", "middle_plus_append", "shrink_regrow", "replace",
                 "absent_reappears", "hardlink", "symlink", "parent_symlink",
                 "archive_rewrite", "archive_member", "head", "transition", "witness_loss"):
        f = rolled.restore()
        f.select()
        events = f.root.tenant_home / "events.jsonl"
        original, status = events.read_bytes(), events.stat()
        if kind in ("same_size", "middle_plus_append", "shrink_regrow"):
            if kind == "middle_plus_append":
                row = f.send("new-before-middle-rewrite", recipient="other")
                # Obtain the suffix through native send BEFORE corrupting its
                # admitted prefix, so corruption is observed at select().
                with_suffix = events.read_bytes()
            else:
                with_suffix = original
            changed = with_suffix.replace(b"native memo envelope", b"native memo envelopf", 1)
            if changed == with_suffix:
                raise RuntimeError("fixture needs a carried native envelope")
            if kind == "shrink_regrow":
                events.write_bytes(b"")
            events.write_bytes(changed)
            os.utime(events, ns=(status.st_atime_ns, status.st_mtime_ns))
            if kind == "middle_plus_append":
                result["checks"][kind + "_native_suffix_appended"] = encode_frame(row) in events.read_bytes()
        elif kind == "replace":
            replacement = events.with_suffix(".replacement")
            replacement.write_bytes(original)
            replacement.replace(events)
        elif kind == "absent_reappears":
            events.unlink()
            equivalent(result, kind + "_absent", f, refusal=True)
            events.write_bytes(original)
        elif kind in ("hardlink", "symlink"):
            target = f.base / "saved-events"
            events.rename(target)
            if kind == "hardlink":
                os.link(target, events)
            else:
                events.symlink_to(target)
        elif kind == "parent_symlink":
            parent = (f.root.tenant_home / f.plane.relative(f.node)).parent
            target = parent.with_name("saved-presentations")
            parent.rename(target)
            parent.symlink_to(target, target_is_directory=True)
        elif kind in ("archive_rewrite", "archive_member"):
            from floati.epoch_carry import ArchiveProofResolver
            archive = ArchiveProofResolver(f.root).archives()[0]
            if kind == "archive_member":
                (archive / "extra-unowned.jsonl").write_bytes(b"{}\n")
            else:
                path = archive / "events.jsonl"
                path.write_bytes(path.read_bytes().replace(b"native memo envelope", b"native memo envelopf", 1))
        elif kind == "head":
            from floati.framing import decode_frames
            rows = decode_frames(original)
            rows[0]["archive_path"] += "-absent"
            events.write_bytes(b"".join(encode_frame(row) for row in rows))
        elif kind == "transition":
            (f.root.tenant_home / ".floati-epoch-roll-memo.v1.json").write_bytes(b"{}\n")
        else:
            (f.root.tenant_home / f.plane.witness_relative(f.node)).unlink()
        equivalent(result, kind, f, refusal=kind not in ("replace", "absent_reappears"))
        if events.exists() and not events.is_symlink():
            after = events.stat()
            result["observations"][kind + "_physical"] = {
                "before": [getattr(status, field) for field in IDENTITY_FIELDS],
                "after": [getattr(after, field) for field in IDENTITY_FIELDS],
                "before_digest": hashlib.sha256(original).hexdigest(),
                "after_digest": hashlib.sha256(events.read_bytes()).hexdigest()}
    # Rolled/imported bases can fall back natively for unrelated reasons. These
    # are supported UNROLLED sources, with stable delegated observer identities
    # installed before warm admission, so non-append work invalidation is real.
    for kind in ("identical_inode", "identical_parent", "identical_root", "observed_absence"):
        f = NativeFixture(base / ("readmit-" + kind), harness)
        first = f.send("supported-unrolled")
        f.publish()
        events = f.root.tenant_home / "events.jsonl"
        original = events.read_bytes()
        from floati.cursor import SparseCursor
        delivery = f.root.tenant_home / SparseCursor(f.root)._delivery_relative_path_for(f.node)
        coordinate = delivery.parent if kind == "identical_parent" else f.root.tenant_home if kind == "identical_root" else events
        before = coordinate.stat()
        before_events = events.stat()
        with NativeWork() as work:
            warmed = f.select()
            f.select()
            if kind == "identical_inode":
                replacement = events.with_name("memo-identical-events")
                replacement.write_bytes(original)
                os.chmod(replacement, before_events.st_mode & 0o777)
                os.utime(replacement, ns=(before_events.st_atime_ns, before_events.st_mtime_ns))
                replacement.replace(events)
            elif kind in ("identical_parent", "identical_root"):
                saved = f.base / (kind + "-saved")
                coordinate.rename(saved)
                coordinate.mkdir(mode=before.st_mode & 0o777)
                # Move each existing child back, preserving all leaf bytes and
                # inodes. Only the selected parent/root identity is replaced.
                for child in saved.iterdir():
                    child.rename(coordinate / child.name)
            else:
                saved = f.base / "temporarily-absent-events"
                events.rename(saved)
                absent_native = observation(lambda: f.select(native=True))
                absent_waiter = observation(f.select)
                result["observations"]["readmit_absent"] = {
                    "native": absent_native, "waiter": absent_waiter}
                result["checks"]["readmit_absence_was_observed_natively"] = (
                    absent_native == absent_waiter and "refusal" in absent_native)
                saved.rename(events)
            expected = observation(lambda: f.select(native=True))
            work.reset()
            actual = observation(f.select)
            renewed = work.snapshot()
        after = coordinate.stat()
        after_events = events.stat()
        result["observations"]["readmit_" + kind] = {
            "native": expected, "waiter": actual, "renewed_work": renewed,
            "coordinate": str(coordinate),
            "before": [getattr(before, field) for field in IDENTITY_FIELDS],
            "after": [getattr(after, field) for field in IDENTITY_FIELDS],
            "before_event_identity": [getattr(before_events, field) for field in IDENTITY_FIELDS],
            "after_event_identity": [getattr(after_events, field) for field in IDENTITY_FIELDS],
            "before_physical_digest": hashlib.sha256(original).hexdigest(),
            "after_physical_digest": hashlib.sha256(events.read_bytes()).hexdigest()}
        result["checks"]["readmit_" + kind + "_supported_unrolled_fixture"] = (
            warmed["_source"][0][0]["kind"] != "bus_epoch_roll_receipt"
            and len(warmed["_source"][2]) == 1
            and all(row["kind"] in {"delivery_receipt", "delivered_unpresented"} for row in warmed["_source"][2])
            and [row["item_id"] for row in warmed["held_items"]] == [first["id"]])
        result["checks"]["readmit_" + kind + "_byte_identical_source"] = events.read_bytes() == original
        result["checks"]["readmit_" + kind + "_exact_native_selection"] = actual == expected and "bytes" in actual
        result["checks"]["readmit_" + kind + "_renews_native_admission"] = (
            renewed.get("presentation_projector", 0) + renewed.get("native_projector", 0) > 0)
        if kind != "observed_absence":
            result["checks"]["readmit_" + kind + "_coordinate_really_replaced"] = (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino))
        if kind in ("identical_parent", "identical_root", "observed_absence"):
            result["checks"]["readmit_" + kind + "_event_inode_preserved"] = (
                (before_events.st_dev, before_events.st_ino) == (after_events.st_dev, after_events.st_ino))
    # Replace synchronously AFTER both metadata observations, immediately
    # BEFORE the candidate's protected events read. Stable delegates are in
    # place before warming; neither an oracle nor a predicate triggers this.
    from floati.cursor_presentations import CursorPresentationLedger
    f = NativeFixture(base / "readmit-after-preflight", harness)
    first = f.send("supported-before-preflight")
    f.publish()
    events = f.root.tenant_home / "events.jsonl"
    original_bytes = CursorPresentationLedger._bytes
    original_dependencies = CursorPresentationLedger._memo_dependencies
    boundary = {"armed": False, "fired": 0, "order": [], "preflights": []}

    def observe_dependencies(plane, *args, **kwargs):
        value = original_dependencies(plane, *args, **kwargs)
        if plane is f.plane and boundary["armed"] and not boundary["fired"]:
            # Retain the actual returned physical coordinates, excluding only
            # nonserializable semantic function identities. Never change them.
            boundary["preflights"].append(value[1])
            boundary["order"].append("metadata_returned")
        return value

    def observe_bytes(plane, relative, *args, **kwargs):
        selected = plane is f.plane and boundary["armed"]
        if selected and relative == f.plane.relative(f.node):
            boundary["order"].append("native_history_read")
            boundary["history_preflight_count"] = len(boundary["preflights"])
        trigger = selected and relative == Path("events.jsonl") and not boundary["fired"]
        if trigger:
            boundary["fired"] += 1
            boundary["order"].append("events_replacement")
            boundary["borrowed_read"] = kwargs.get("_borrow", False)
            boundary["replacement_preflight_count"] = len(boundary["preflights"])
            before = events.stat()
            payload = events.read_bytes()
            replacement = events.with_name("memo-after-preflight-events")
            replacement.write_bytes(payload)
            os.chmod(replacement, before.st_mode & 0o777)
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(events)
            after = events.stat()
            boundary["before"] = [getattr(before, field) for field in IDENTITY_FIELDS]
            boundary["after"] = [getattr(after, field) for field in IDENTITY_FIELDS]
            boundary["before_digest"] = hashlib.sha256(payload).hexdigest()
            boundary["after_digest"] = hashlib.sha256(events.read_bytes()).hexdigest()
            boundary["exact_replacement_bytes"] = events.read_bytes() == payload
        # The unmodified reader owns every descriptor/parent guard and result.
        value = original_bytes(plane, relative, *args, **kwargs)
        if trigger:
            boundary["order"].append("native_events_read_returned")
            boundary["native_read_digest"] = hashlib.sha256(value).hexdigest() if value is not None else None
        return value

    with NativeWork() as work, replaced(CursorPresentationLedger, "_bytes", observe_bytes), replaced(
            CursorPresentationLedger, "_memo_dependencies", observe_dependencies):
        warmed = f.select()
        f.select()
        old_bytes, old_status = events.read_bytes(), events.stat()
        added = f.send("supported-after-preflight")
        appended_bytes, appended_status = events.read_bytes(), events.stat()
        # This independent native owner sees the same appended bytes, while
        # the boundary observer remains disarmed and candidate-only.
        expected = observation(lambda: f.select(native=True))
        work.reset()
        boundary["armed"] = True
        try:
            actual = observation(f.select)
        finally:
            boundary["armed"] = False
        renewed = work.snapshot()
        native_after = observation(lambda: f.select(native=True))
    after_bytes = events.read_bytes()
    result["observations"]["readmit_after_preflight"] = {
        "native_before": expected, "waiter": actual, "native_after": native_after,
        "renewed_work": renewed, "boundary": boundary,
        "old_event_identity": [getattr(old_status, field) for field in IDENTITY_FIELDS],
        "appended_event_identity": [getattr(appended_status, field) for field in IDENTITY_FIELDS],
        "old_physical_digest": hashlib.sha256(old_bytes).hexdigest(),
        "appended_physical_digest": hashlib.sha256(appended_bytes).hexdigest(),
        "new_envelope_id": added["id"]}
    checks = {
        "supported_unrolled_fixture": (
            warmed["_source"][0][0]["kind"] != "bus_epoch_roll_receipt"
            and len(warmed["_source"][2]) == 1
            and all(row["kind"] in {"delivery_receipt", "delivered_unpresented"} for row in warmed["_source"][2])
            and [row["item_id"] for row in warmed["held_items"]] == [first["id"]]),
        "native_append_preserved_inode_and_prefix": (
            (old_status.st_dev, old_status.st_ino) == (appended_status.st_dev, appended_status.st_ino)
            and len(appended_bytes) > len(old_bytes) and appended_bytes.startswith(old_bytes)
            and encode_frame(added) in appended_bytes[len(old_bytes):]),
        "actual_preflights_saw_appended_old_inode": (
            len(boundary["preflights"]) == 2
            and all(coordinates[0][0] == "events.jsonl"
                and list(coordinates[0][1][0]) == [getattr(appended_status, field) for field in IDENTITY_FIELDS]
                for coordinates in boundary["preflights"])),
        "one_replacement_after_preflight_before_native_read": (
            boundary["fired"] == 1 and boundary.get("borrowed_read") is True
            and boundary.get("history_preflight_count") == 2
            and boundary.get("replacement_preflight_count") == 2
            and boundary["order"] == ["metadata_returned", "metadata_returned", "native_history_read",
                "events_replacement", "native_events_read_returned"]),
        "new_inode_exact_appended_bytes_and_old_prefix": (
            boundary.get("exact_replacement_bytes") is True
            and boundary.get("before", [])[:2] != boundary.get("after", [])[:2]
            and after_bytes == appended_bytes and after_bytes.startswith(old_bytes)
            and boundary.get("native_read_digest") == hashlib.sha256(appended_bytes).hexdigest()),
        "complete_independent_native_selection": actual == expected == native_after and "bytes" in actual,
        "renews_native_admission": (
            renewed.get("presentation_projector", 0) + renewed.get("native_projector", 0) > 0),
    }
    result["checks"].update(("readmit_after_preflight_" + name, passed) for name, passed in checks.items())
    return result


def protected_bytes(base, harness):
    """A hit skips bytes only after ordinary coordinates and ALL identities."""
    from floati.cursor_presentations import CursorPresentationLedger
    result, f = outcome(), NativeFixture(base, harness)
    f.send("first")
    f.select()
    relative = f.plane.relative(f.node)
    path = f.root.tenant_home / relative
    original = path.read_bytes()
    f.plane = CursorPresentationLedger(f.root)
    # This test reads a native presentation coordinate, never an external file.
    def borrowed(maximum=1048576):
        return f.plane._bytes(relative, maximum, _borrow=True)
    real_fdopen, reads = os.fdopen, []
    class ObservedFile:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *exc):
            return self.stream.__exit__(*exc)
        def read(self, *args, **kwargs):
            reads.append(True)
            return self.stream.read(*args, **kwargs)
    def opened(*args, **kwargs):
        return ObservedFile(real_fdopen(*args, **kwargs))
    with replaced(os, "fdopen", opened):
        first = borrowed()
        cold = len(reads)
        reads.clear()
        warm = borrowed()
        hot = len(reads)
        result["checks"].update(immutable_exact_bytes=type(first) is bytes and first == warm == original,
            first_physical_read=cold == 1, protected_byte_hit_has_no_reread=hot == 0)
        reads.clear()
        status = path.stat()
        os.chmod(path, (status.st_mode & 0o777) ^ 0o100)
        changed = borrowed()
        result["checks"]["changed_mode_ctime_reopens_bytes"] = changed == original and len(reads) == 1
    result["observations"]["reads"] = {"cold": cold, "unchanged": hot}
    # Public readback must use the ordinary physical read, even after a hit.
    reads.clear()
    with replaced(os, "fdopen", opened):
        public = f.plane._bytes(relative)
    result["checks"]["public_readback_always_physical"] = public == original and len(reads) == 1
    for kind in ("absent", "reappeared", "oversize", "directory", "hardlink", "symlink"):
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
        maximum = 1048576
        if kind == "reappeared":
            path.write_bytes(original)
        elif kind == "oversize":
            path.write_bytes(b"larger-than-bound")
            maximum = 1
        elif kind == "directory":
            path.mkdir()
        elif kind in ("hardlink", "symlink"):
            target = f.base / (kind + "-target")
            target.write_bytes(original)
            if kind == "hardlink":
                os.link(target, path)
            else:
                path.symlink_to(target)
        native = observation(lambda: CursorPresentationLedger(f.root)._bytes(relative, maximum))
        candidate = observation(lambda: borrowed(maximum))
        result["observations"][kind] = {"native": native, "waiter": candidate}
        result["checks"][kind + "_same_native_bytes_or_refusal"] = native == candidate
        if kind not in ("absent", "reappeared"):
            result["checks"][kind + "_refused"] = "refusal" in native
    return result


def invalid_suffix(base, harness):
    from floati.framing import decode_frames, encode_frame
    from floati.records import wake_hold_decision_digest
    from floati.wake_hold import WakeHoldController
    result = outcome()
    fixtures = NativeFixtureBank(base, harness, rolled=False)
    for kind in ("duplicate_id", "unknown_valid", "unknown_invalid", "unknown_duplicate_id",
                 "foreign_hold", "reordered_hold", "duplicate_hold_ids", "invalid_late_ack"):
        f = fixtures.restore()
        first, second = f.send("first"), f.send("second")
        foreign = f.send("foreign", recipient="other")
        hold = WakeHoldController(f.root).evaluate(f.node, idempotency_key="hold")["receipt"]
        f.publish()
        f.select()
        history_before = wire(f.plane.read(f.node))
        events = f.root.tenant_home / "events.jsonl"
        saved = {name: ((f.root.tenant_home / name).read_bytes()
            if (f.root.tenant_home / name).exists() else None)
            for name in ("events.jsonl", "receipts/deliveries/seat.jsonl", "receipts/acks/seat.jsonl")}
        if kind == "duplicate_id":
            with events.open("ab") as stream:
                stream.write(encode_frame(first))
        elif kind.startswith("unknown"):
            row = {"kind": "memo_future_record", "schema_version": 99,
                   "tenant_id": f.root.tenant_id, "id": "unknown-memo-record",
                   "timestamp": first["timestamp"], "future_payload": "one"}
            if kind == "unknown_invalid":
                row["schema_version"] = False
            if kind == "unknown_duplicate_id":
                row["id"] = first["id"]
            with events.open("ab") as stream:
                stream.write(encode_frame(row))
        elif kind == "invalid_late_ack":
            from floati.cursor import SparseCursor
            SparseCursor(f.root).ack(f.node, [first["id"]], acting_session_id=SESSION)
            path = f.root.tenant_home / "receipts/acks/seat.jsonl"
            rows = decode_frames(path.read_bytes())
            rows[-1]["item_ids"] = [foreign["id"]]
            path.write_bytes(b"".join(encode_frame(row) for row in rows))
        else:
            path = f.root.tenant_home / "receipts/deliveries/seat.jsonl"
            rows = decode_frames(path.read_bytes())
            hostile = dict(hold, item_ids={"foreign_hold": [foreign["id"]],
                "reordered_hold": [second["id"], first["id"]],
                "duplicate_hold_ids": [first["id"], first["id"]]}[kind])
            hostile["decision_digest"] = wake_hold_decision_digest(hostile)
            path.write_bytes(b"".join(encode_frame(hostile if row["id"] == hold["id"] else row) for row in rows))
        equivalent(result, kind, f, refusal=kind != "unknown_valid")
        if kind == "invalid_late_ack":
            from floati.cursor_presentations import CursorPresentationLedger
            native_history = observation(lambda: CursorPresentationLedger(f.root).read(f.node))
            memo_history = observation(lambda: f.plane.read(f.node))
            result["observations"]["valid_historical_prefix"] = {
                "native": native_history, "waiter": memo_history}
            result["checks"]["invalid_tail_does_not_invalidate_valid_historical_cut"] = (
                native_history == memo_history == {"bytes": history_before.hex()})
        if kind == "unknown_valid":
            before = events.read_bytes()
            with NativeWork() as work:
                f.select()
                events.write_bytes(before.replace(b'"future_payload":"one"', b'"future_payload":"two"'))
                work.reset()
                observed = observation(f.select)
                result["observations"]["unknown_physical_recheck"] = {"result": observed, "work": work.snapshot()}
                result["checks"]["unknown_middle_change_rechecks_physical_frames"] = work.counts["presentation_decoder"] > 0
            equivalent(result, "unknown_middle_physical_change", f)
            result["checks"]["unknown_physical_bytes_really_changed"] = events.read_bytes() != before
        for name, payload in saved.items():
            path = f.root.tenant_home / name
            if payload is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(payload)
        repaired = equivalent(result, kind + "_restored", f)
        result["checks"][kind + "_failure_is_not_cached"] = "bytes" in repaired
    return result


def authority_changes(base, harness):
    from floati.codex_wait_contract import CodexWaitConsentLedger
    from floati.wake_control import WakeController
    result = outcome()
    for kind in ("consent", "claim", "pause", "workspace"):
        if kind == "workspace" and harness == "codex":
            continue  # Codex's adapter watch, not select(), owns this check.
        f = NativeFixture(base / kind, harness)
        f.send("fresh")
        f.select()
        with f.guarded():
            authority = f.authority()
        if kind == "consent":
            CodexWaitConsentLedger(f.root).arm(f.binding, hook_timeout_seconds=10,
                wait_deadline_seconds=1, idempotency_key="new-consent")
        elif kind == "claim":
            f.arm(OTHER_SESSION)
        elif kind == "pause":
            WakeController(f.root).pause(f.node, SESSION, idempotency_key="pause")
        else:
            f.map_path.write_bytes(wire({"schema_version": 0, "tenant_id": f.root.tenant_id,
                "mappings": []}) + b"\n")
        before = (f.root.tenant_home / f.plane.relative(f.node)).read_bytes()
        expected = observation(lambda: f.select(native=True, authority=authority))
        actual = observation(lambda: f.select(authority=authority))
        result["observations"][kind] = {"native": expected, "waiter": actual}
        result["checks"][kind + "_exact_current_authority_refusal"] = (
            actual == expected and expected.get("refusal", {}).get("code") == "presentation_authority_changed")
        result["checks"][kind + "_no_publication"] = (
            (f.root.tenant_home / f.plane.relative(f.node)).read_bytes() == before)
    return result


def semantic_changes(base, harness):
    from floati import cursor_presentations as cp, records, wake_hold
    result = outcome()
    rolled = NativeFixtureBank(base, harness)
    for owner, name, label in ((cp, "_decode_path_records", "parser"),
                               (wake_hold, "project_wake_items", "archive_projector"),
                               (cp, "project_wake_items", "current_projector")):
        f = rolled.restore()
        f.select()
        original, calls = getattr(owner, name), []
        def forwarding(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)
        with replaced(owner, name, forwarding):
            actual = observation(f.select)
            observed = len(calls)
            expected = observation(lambda: f.select(native=True))
        result["observations"][label] = {"waiter": actual, "native": expected, "native_calls": observed}
        result["checks"][label + "_contract_invalidates"] = observed > 0
        result["checks"][label + "_complete_bytes"] = actual == expected and "bytes" in actual
    # Mutate the SAME grammar mapping in place while every validator callable
    # retains its identity. A cache key containing a mutable mapping alias is
    # not testimony for the grammar that admitted the previous source.
    for table, kind, label in ((records._SPECS, "message_envelope", "closed_v0"),
                                (records._V1_FIELDS, "bus_epoch_roll_receipt", "closed_v1")):
        f = rolled.restore()
        f.select()
        original = table[kind]
        try:
            table[kind] = ((original[0], original[1] | {"memo_required_field"})
                           if label == "closed_v0" else original | {"memo_required_field"})
            equivalent(result, label, f, refusal=True)
        finally:
            table[kind] = original
        equivalent(result, label + "_restored_native_contract", f)
    return result


def imported_proof_binding(base, harness):
    from floati.epoch_carry import verified_hold_proofs
    from floati.wake_hold import project_wake_items
    result, f = outcome(), NativeFixture(base, harness)
    f.seed_rolled()
    f.select()
    old_source = f.plane._sources(f.root.tenant_home, f.node)
    proof = verified_hold_proofs(f.root, old_source, f.node)
    fresh = f.send("append-after-imported-proof")
    new_source = f.plane._sources(f.root.tenant_home, f.node)
    refused = observation(lambda: project_wake_items(events=new_source[0],
        event_prefix_digests=new_source[1], deliveries=new_source[2],
        delivery_prefix_digests=new_source[3], acknowledgments=new_source[4],
        acknowledgment_prefix_digests=new_source[5], recipient=f.node,
        worker_session_id=None, tenant_id=f.root.tenant_id, _hold_proofs=proof))
    result["observations"]["old_source_proof"] = refused
    result["checks"]["old_exact_source_proof_cannot_attest_append"] = "refusal" in refused
    equivalent(result, "new_full_source", f)
    selected = f.select()
    result["checks"]["new_source_reissues_imported_proof"] = (
        [row["id"] for row in selected["fresh_messages"]] == [fresh["id"]]
        and [row["item_id"] for row in selected["held_items"]] == [f.first["id"], f.second["id"]])
    result["checks"]["old_native_hold_seal_not_translated"] = old_source[2][0] == new_source[2][0]
    return result


def publication_failures(base, harness):
    from floati.cursor_presentations import CursorPresentationLedger
    result = outcome()
    fixtures = NativeFixtureBank(base, harness, rolled=False)
    for stage in ("prepared", "delivered", "failed_flush", "failed_terminal", "flushed"):
        f = fixtures.restore()
        first = f.send("first")
        f.select()
        if stage == "failed_flush":
            output = BoundedOutput(fail_flush=True)
            try:
                observed = f.adapter(output=output)
            except OSError:
                observed = f.last_adapter
            result["checks"]["flush_failure_was_reached_once"] = output.flush_attempts == 1
            result["checks"]["flush_failure_never_succeeded"] = output.flush_successes == 0
        elif stage == "failed_terminal":
            original = CursorPresentationLedger._append_reserved
            fault_rows = []
            def fail_terminal(plane, node, data, row):
                if row["phase"] == "flushed":
                    fault_rows.append(native_json(row))
                    raise OSError("fixture terminal append failed")
                return original(plane, node, data, row)
            with replaced(CursorPresentationLedger, "_append_reserved", fail_terminal):
                observed = f.adapter()
            result["observations"]["terminal_fault_rows"] = fault_rows
            result["checks"]["terminal_failure_was_reached_once_for_exact_batch"] = (
                len(fault_rows) == 1 and fault_rows[0]["item_ids"] == [first["id"]])
        elif stage == "flushed":
            observed = f.adapter()
        else:
            f.publish(stage=stage)
            observed = {"status": None, **f.last_publication_output.evidence(),
                        "stderr": "", "exception": None}
        writes_body = stage not in ("prepared", "delivered")
        emission_checks(result, stage, f, observed, [first] if writes_body else None,
            bodies=1 if writes_body else 0, flushed=1 if stage in ("failed_terminal", "flushed") else 0)
        expected_batches = [] if stage == "prepared" else [[first["id"]]]
        deliveries = delivery_checks(result, stage, f, expected_batches)
        selection = f.select()
        result["checks"][stage + "_no_retry"] = selection["fresh_messages"] == []
        result["checks"][stage + "_exact_held_id"] = [row["item_id"] for row in selection["held_items"]] == [first["id"]]
        expected_phase = "flushed" if stage == "flushed" else "prepared"
        result["checks"][stage + "_exact_phase"] = [row["phase"] for row in selection["held_items"]] == [expected_phase]
        before = f.plane.read(f.node)
        equivalent(result, stage + "_native", f)
        f.select()
        result["checks"][stage + "_no_extra_receipt"] = f.plane.read(f.node) == before
        result["checks"][stage + "_no_extra_delivery_during_rechecks"] = wire(f.deliveries()) == wire(deliveries)
        if stage == "flushed":
            result["checks"]["flushed_row_binds_the_only_native_delivery"] = (
                len(before) == 2 and len(deliveries) == 1
                and before[-1]["delivery_receipt_id"] == deliveries[0]["id"])
        fresh = f.send("new-mail")
        result["checks"][stage + "_new_mail_independent"] = [row["id"] for row in f.select()["fresh_messages"]] == [fresh["id"]]
        new_output = f.adapter()
        emission_checks(result, stage + "_new_mail", f, new_output, [fresh], held=True)
        delivery_checks(result, stage + "_after_new_mail", f, expected_batches + [[fresh["id"]]])
    return result


def controller_admission(base, harness):
    from floati.wake_hold import WakeHoldController
    result, f = outcome(), NativeFixture(base, harness)
    f.seed_rolled()
    controller = WakeHoldController(f.root)
    with f.guarded():
        # Independent ordinary reader; neither candidate seam nor its retained
        # controller supplies the expected bytes or measured warm state.
        expected = wire(WakeHoldController(f.root)._read(f.node, None))
    def read():
        native = lambda: controller._read(f.node, None)
        return f.plane._waiter_controller_read(native, recipient=f.node, worker_session_id=None)
    with NativeWork() as work:
        with f.guarded():
            cold = wire(read())
            first = work.snapshot()
            work.reset()
            actual = [wire(read()) for _ in range(3)]
            warm = work.snapshot()
    result["observations"].update(cold=first, warm=warm, native_bytes=expected.hex(),
                                  candidate_cold_bytes=cold.hex(), candidate_warm_bytes=[value.hex() for value in actual])
    result["checks"].update(distinct_controller_cold_admission=first.get("controller_reader") == 1,
        controller_exact_bytes=cold == expected and all(item == expected for item in actual),
        controller_unchanged_no_replay=warm.get("controller_reader", 0) == 0)
    # Do not replace the controller's refusal with the presentation reader's.
    (f.root.tenant_home / "events.jsonl").write_bytes(b"malformed\n")
    with f.guarded():
        native = observation(lambda: controller._read(f.node, None))
        candidate = observation(read)
    result["checks"]["controller_native_refusal_precedence"] = (
        native == candidate and native.get("refusal", {}).get("code") == "consumption_state_unavailable")
    result["observations"]["refusal"] = {"native": native, "waiter": candidate}
    return result


def _adapter_lifecycle_cases(base, harness, changes):
    from floati.cursor_presentations import CursorPresentationLedger
    from floati.wake_control import WakeController
    result = outcome()
    fixtures = NativeFixtureBank(base, harness, rolled=False)
    for change in changes:
        f = fixtures.restore()
        original = f.send("first")
        first = f.adapter()
        emission_checks(result, change + "_first", f, first, [original])
        delivery_checks(result, change + "_first", f, [[original["id"]]])
        rows = CursorPresentationLedger(f.root).read(f.node)
        result["checks"][change + "_one_native_prepare_flush"] = (
            first["status"] == 0 and [row["phase"] for row in rows] == ["prepared", "flushed"])
        if change == "archive":
            f.roll()
        history_path = f.root.tenant_home / f.plane.relative(f.node)
        before_bytes = history_path.read_bytes()
        before_deliveries = wire(f.deliveries())
        added = []
        workspace_change = {}
        workspace_before_bytes = f.map_path.read_bytes() if change in ("workspace", "workspace_atomic") else None
        workspace_work = None
        def mutate():
            if change == "pause":
                WakeController(f.root).pause(f.node, SESSION, idempotency_key="pause-in-sleep")
            elif change == "claim":
                f.arm(OTHER_SESSION)
            elif change == "archive":
                from floati.epoch_carry import ArchiveProofResolver
                archive = ArchiveProofResolver(f.root).archives()[0] / "events.jsonl"
                before = archive.stat()
                archive.write_bytes(archive.read_bytes().replace(b"native memo envelope", b"native memo envelopf", 1))
                os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns))
            elif change == "witness":
                (f.root.tenant_home / f.plane.witness_relative(f.node)).unlink()
            elif change == "workspace":
                before = f.map_path.stat()
                f.map_path.write_bytes(wire({"schema_version": 0, "tenant_id": f.root.tenant_id,
                    "mappings": []}) + b"\n")
                after = f.map_path.stat()
                workspace_change.update(
                    before=[getattr(before, field) for field in IDENTITY_FIELDS],
                    after=[getattr(after, field) for field in IDENTITY_FIELDS])
                if workspace_work is not None:
                    workspace_change["before_mutation_work"] = workspace_work.snapshot()
                    workspace_work.reset()
            elif change == "workspace_atomic":
                from floati.codex_wait_contract import _write_workspace_map_atomically
                before = f.map_path.stat()
                encoded = wire({"schema_version": 0, "tenant_id": f.root.tenant_id,
                    "mappings": []}) + b"\n"
                _write_workspace_map_atomically(f.map_path, encoded)
                after = f.map_path.stat()
                workspace_change.update(
                    before=[getattr(before, field) for field in IDENTITY_FIELDS],
                    after=[getattr(after, field) for field in IDENTITY_FIELDS])
            else:
                if change == "epoch":
                    f.roll()
                added.append(f.send("new-in-sleep"))
        if change == "workspace":
            # Observer identities precede the candidate adapter's first poll.
            # Reset only at the real in-place mutation, then retain only that
            # candidate's subsequent native work before running any oracle.
            with NativeWork() as workspace_work:
                second = f.adapter(on_sleep=mutate)
                workspace_change["after_mutation_work"] = workspace_work.snapshot()
        else:
            second = f.adapter(on_sleep=mutate)
        result["observations"][change] = {"first": first, "second": second}
        result["checks"][change + "_really_polled"] = bool(second["sleeps"])
        if change in ("workspace", "workspace_atomic"):
            from floati.codex_wait_contract import resolve_workspace_binding
            encoded = wire({"schema_version": 0, "tenant_id": f.root.tenant_id,
                "mappings": []}) + b"\n"
            actual_bytes = f.map_path.read_bytes()
            strict_binding = observation(lambda: resolve_workspace_binding(
                f.root.tenant_home, f.workspace, _strict=True))
            workspace_change.update(path=str(f.map_path),
                before_physical_digest=hashlib.sha256(workspace_before_bytes).hexdigest(),
                after_physical_digest=hashlib.sha256(actual_bytes).hexdigest(),
                before_bytes=workspace_before_bytes.hex(), actual_bytes=actual_bytes.hex(),
                expected_bytes=encoded.hex(), strict_binding=strict_binding)
            result["observations"][change]["workspace_mutation"] = workspace_change
            observed_mutation = "before" in workspace_change
            result["checks"][change + "_physical_mutation_is_exact"] = (
                observed_mutation and workspace_change["before"] != workspace_change["after"]
                and ((workspace_change["before"][:2] == workspace_change["after"][:2])
                    if change == "workspace" else
                    (workspace_change["before"][:2] != workspace_change["after"][:2])))
            result["checks"][change + "_valid_empty_map"] = (
                observed_mutation and actual_bytes == encoded and strict_binding == {"bytes": wire(None).hex()})
        if change in ("pause", "claim", "workspace", "workspace_atomic", "archive", "witness"):
            # Seat32 s15 adopts demonstrated pre-memo native behavior: an
            # in-place Codex withdrawal emits one held deadline diagnostic.
            # Pause and atomic map withdrawal still return silently in Codex.
            body_count = 0 if harness == "codex" and change in ("pause", "workspace_atomic") else 1
            emission_checks(result, change + "_blocked", f, second, None,
                            bodies=body_count, flushed=body_count)
            delivery_checks(result, change + "_blocked", f, [[original["id"]]])
            result["checks"][change + "_retains_exact_native_delivery"] = wire(f.deliveries()) == before_deliveries
            result["checks"][change + "_blocks_publication"] = history_path.read_bytes() == before_bytes
            if change == "workspace":
                from floati.cursor import SparseCursor
                from floati.codex_wait_contract import CodexWaitSessionLedger, consent_ledger_relative
                cursor = SparseCursor(f.root)
                coordinates = (Path("events.jsonl"), cursor._delivery_relative_path_for(f.node),
                    cursor._relative_path_for(f.node), f.plane.relative(f.node), f.plane.witness_relative(f.node),
                    consent_ledger_relative(f.node), CodexWaitSessionLedger._relative(f.node))
                def native_inputs():
                    return {str(path): (None if not (f.root.tenant_home / path).exists()
                        else (f.root.tenant_home / path).read_bytes()) for path in coordinates}
                oracle_before = native_inputs()
                # Replay on the SAME root/workspace/session/envelope IDs, not
                # another fixture whose diagnostic embeds different evidence.
                # Restore only the original map bytes on its existing inode;
                # the oracle repeats the original in-place withdrawal below.
                f.map_path.write_bytes(workspace_before_bytes)
                oracle_map_before = f.map_path.stat()
                native_select = CursorPresentationLedger.select
                native_reads = {"selection": 0, "controller": 0}
                def select_native(plane, *args, **kwargs):
                    native_reads["selection"] += 1
                    # The oracle calls public select, whose contract has no
                    # adapter-private Stop-boundary hint. This fixture uses
                    # normal envelopes, so its native audience is unchanged.
                    kwargs.pop("at_stop", None)
                    # A fresh native owner cannot inherit candidate memo state.
                    return native_select(CursorPresentationLedger(plane.root), *args, **kwargs)
                def read_native(_plane, native_read, **_coordinates):
                    native_reads["controller"] += 1
                    return native_read()
                def withdraw_native():
                    f.map_path.write_bytes(encoded)
                # These delegates call original native readers and return
                # their exact results. No Boolean/predicate or output is
                # substituted. The adapter itself renders and flushes stdout;
                # its real memo predicate sees no borrowed native load.
                with replaced(CursorPresentationLedger, "_select_waiter", select_native), replaced(
                        CursorPresentationLedger, "_waiter_controller_read", read_native), NativeWork() as oracle_work:
                    native_output = f.adapter(on_sleep=withdraw_native)
                    oracle_counts = oracle_work.snapshot()
                oracle_after = native_inputs()
                oracle_map_after = f.map_path.stat()
                result["observations"][change]["independent_native_adapter"] = {
                    "output": native_output, "native_reader_calls": native_reads, "native_work": oracle_counts,
                    "root": str(f.root.tenant_home), "workspace": str(f.workspace), "session": SESSION,
                    "source_before": native_json(oracle_before), "source_after": native_json(oracle_after),
                    "map_before": [getattr(oracle_map_before, field) for field in IDENTITY_FIELDS],
                    "map_after": [getattr(oracle_map_after, field) for field in IDENTITY_FIELDS],
                    "native_stdout_bytes": native_output["stdout"].encode("utf-8").hex(),
                    "candidate_stdout_bytes": second["stdout"].encode("utf-8").hex()}
                emission_checks(result, "workspace_native_oracle", f, native_output, None, bodies=1, flushed=1)
                result["checks"]["workspace_exact_native_emitted_body_bytes"] = (
                    second["stdout"].encode("utf-8") == native_output["stdout"].encode("utf-8"))
                result["checks"]["workspace_exact_native_flush_and_status"] = (
                    second["status"] == native_output["status"] == 0
                    and second["flush_successes"] == native_output["flush_successes"] == 1)
                result["checks"]["workspace_native_oracle_same_source_coordinates"] = (
                    oracle_before == oracle_after and bool(native_output["sleeps"])
                    and (oracle_map_before.st_dev, oracle_map_before.st_ino)
                        == (oracle_map_after.st_dev, oracle_map_after.st_ino)
                        == tuple(workspace_change["after"][:2])
                    and f.map_path.read_bytes() == encoded)
                result["checks"]["workspace_native_oracle_really_read_natively"] = (
                    native_reads["selection"] > 0
                    # Only Codex routes through the controller reader. Cursor
                    # selects directly and must not invent controller work.
                    and (native_reads["controller"] > 0 if harness == "codex" else native_reads["controller"] == 0)
                    and oracle_counts.get("presentation_projector", 0) + oracle_counts.get("native_projector", 0) > 0)
                if harness == "codex":
                    # Cursor withdraws authority before further admission.
                    # Codex's retained binding continues natively, so a full
                    # post-mutation admission is mandatory for this witness.
                    renewed = workspace_change["after_mutation_work"]
                    result["checks"]["workspace_no_stale_memo_after_inplace_rewrite"] = (
                        renewed.get("presentation_projector", 0) + renewed.get("native_projector", 0) > 0)
            if change in ("archive", "witness"):
                expected = observation(lambda: f.select(native=True))
                code = expected.get("refusal", {}).get("code")
                result["observations"][change]["native"] = expected
                result["checks"][change + "_advisory_sleep_does_not_hide_refusal"] = (
                    bool(code) and code in second["stdout"] + second["stderr"])
        else:
            emission_checks(result, change + "_second", f, second, added, held=True)
            deliveries = delivery_checks(result, change + "_second", f,
                [[original["id"]], [message["id"] for message in added]])
            after = CursorPresentationLedger(f.root).read(f.node)
            result["checks"][change + "_new_envelope_published"] = bool(added) and added[0]["id"] in second["stdout"]
            result["checks"][change + "_old_envelope_not_republished"] = (
                [row["item_ids"] for row in after if row["phase"] == "prepared"] == [[original["id"]], [added[0]["id"]]])
            result["checks"][change + "_two_exact_terminal_delivery_links"] = (
                len(deliveries) == 2 and [row["delivery_receipt_id"] for row in after if row["phase"] == "flushed"]
                == [row["id"] for row in deliveries])
    return result


def workspace_inplace_lifecycle(base, harness):
    """Keep the original in-place witness with ruled exact native output."""
    return _adapter_lifecycle_cases(base, harness, ("workspace",))


def workspace_atomic_lifecycle(base, harness):
    """Additional native-publisher case; never substitutes for in-place edits."""
    return _adapter_lifecycle_cases(base, harness, ("workspace_atomic",))


def workspace_map_admission(base, harness):
    from floati.codex_wait_contract import resolve_participant
    result = outcome()
    for kind in ("inplace", "same_size_restored_mtime"):
        f = NativeFixture(base / kind, harness)
        first = f.send("workspace-admission-held")
        f.publish()
        original = f.map_path.read_bytes()
        document = json.loads(original)
        before = f.map_path.stat()
        with NativeWork() as work:
            warmed = f.select()
            cold = work.snapshot()
            work.reset()
            f.select()
            warm = work.snapshot()
            if kind == "inplace":
                changed = json.dumps(document, sort_keys=True, indent=2).encode("utf-8") + b"\n"
            else:
                # JSON accepts a trailing space just as it accepts a newline.
                # This changes one physical byte without changing the map.
                changed = original[:-1] + b" "
            f.map_path.write_bytes(changed)
            if kind == "same_size_restored_mtime":
                os.utime(f.map_path, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = f.map_path.stat()
            actual_bytes = f.map_path.read_bytes()
            current = resolve_participant(f.root.tenant_home, f.workspace, _strict=True)
            original_binding = f.binding
            # Cursor natively resolves its participant each poll; Codex keeps
            # its binding until the native device/inode watcher reports a
            # replacement. Preserve that difference while exercising the same
            # memo owner against changed physical map bytes in BOTH adapters.
            if harness == "cursor" and current is not None:
                f.binding = current.binding
            expected = observation(lambda: f.select(native=True))
            work.reset()
            actual = observation(f.select)
            renewed = work.snapshot()
        before_identity = [getattr(before, field) for field in IDENTITY_FIELDS]
        after_identity = [getattr(after, field) for field in IDENTITY_FIELDS]
        result["observations"][kind] = {
            "path": str(f.map_path), "before": before_identity, "after": after_identity,
            "before_physical_digest": hashlib.sha256(original).hexdigest(),
            "after_physical_digest": hashlib.sha256(actual_bytes).hexdigest(),
            "before_bytes": original.hex(), "after_bytes": actual_bytes.hex(),
            "original_binding": native_json(original_binding),
            "current_binding": native_json(None if current is None else current.binding),
            "current_root": None if current is None else str(current.root.tenant_home),
            "candidate_binding": native_json(f.binding),
            "cold_work": cold, "warm_work": warm, "renewed_work": renewed,
            "native": expected, "waiter": actual}
        checks = {
            "held_unrolled_native_fixture": (
                warmed["_source"][0][0]["kind"] != "bus_epoch_roll_receipt"
                and [row["item_id"] for row in warmed["held_items"]] == [first["id"]]),
            "cold_native_admission": cold.get("presentation_projector", 0) + cold.get("native_projector", 0) > 0,
            "warm_admission_was_reused": (
                warm.get("presentation_projector", 0) + warm.get("native_projector", 0) == 0
                and warm.get("presentation_decoder", 0) == 0),
            "same_inode_changed_seven_field_identity": (
                before_identity[:2] == after_identity[:2] and before_identity != after_identity),
            "exact_physical_rewrite_same_valid_map": (
                original != changed and actual_bytes == changed and json.loads(actual_bytes) == document
                and current is not None and current.binding.node_id == original_binding.node_id
                and current.binding.workspace == original_binding.workspace
                and current.binding.map_digest != original_binding.map_digest),
            "binding_reopen_policy_preserved": (
                (f.binding == current.binding if harness == "cursor" and current is not None
                 else f.binding == original_binding)),
            "complete_independent_native_result": actual == expected and bool({"bytes", "refusal"} & actual.keys()),
            "renews_full_native_admission": (
                renewed.get("presentation_projector", 0) + renewed.get("native_projector", 0) > 0),
        }
        if kind == "same_size_restored_mtime":
            checks["same_size_and_mtime_with_changed_ctime"] = (
                len(original) == len(actual_bytes) and before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns and before.st_ctime_ns != after.st_ctime_ns
                and original.endswith(b"\n") and actual_bytes == original[:-1] + b" ")
        else:
            checks["ordinary_rewrite_changed_length"] = len(original) != len(actual_bytes)
        result["checks"].update((kind + "_" + name, passed) for name, passed in checks.items())
    return result


def adapter_lifecycle(base, harness):
    result = _adapter_lifecycle_cases(base, harness,
        ("fresh", "pause", "claim", "epoch", "archive", "witness", "workspace"))
    # New owner/process lifetime cannot inherit an old root's private admission.
    f = NativeFixture(base / "root-owner", harness)
    old = f.send("old-root")
    old_output = f.adapter()
    emission_checks(result, "root_owner_before", f, old_output, [old])
    delivery_checks(result, "root_owner_before", f, [[old["id"]]])
    old_home = f.root.tenant_home
    old_identity = old_home.stat()
    old_home.rename(f.base / "archived-root")
    # The same root pathname now belongs to a new physical root/adapter owner.
    replacement = NativeFixture(base / "replacement-owner", harness, root_home=old_home)
    fresh = replacement.send("replacement-root")
    emitted = replacement.adapter()
    emission_checks(result, "root_owner_after", replacement, emitted, [fresh])
    delivery_checks(result, "root_owner_after", replacement, [[fresh["id"]]])
    result["checks"]["root_owner_replacement_keeps_exact_new_mail"] = (
        fresh["id"] in emitted["stdout"] and old["id"] not in emitted["stdout"])
    new_identity = old_home.stat()
    result["checks"]["root_owner_replacement_is_physical"] = (
        (old_identity.st_dev, old_identity.st_ino) != (new_identity.st_dev, new_identity.st_ino))
    return result


CONTROL_GROUPS = {
    "unchanged": unchanged,
    "append_suffix": append_suffix,
    "rolled_foreign_append": rolled_foreign_append,
    "rolled_foreign_contract": rolled_foreign_contract,
    "public_aliases": public_aliases,
    "physical_changes": physical_changes,
    "protected_bytes": protected_bytes,
    "invalid_suffix": invalid_suffix,
    "authority_changes": authority_changes,
    "semantic_changes": semantic_changes,
    "imported_proof_binding": imported_proof_binding,
    "publication_failures": publication_failures,
    "controller_admission": controller_admission,
    "adapter_lifecycle": adapter_lifecycle,
    "workspace_inplace_lifecycle": workspace_inplace_lifecycle,
    "workspace_atomic_lifecycle": workspace_atomic_lifecycle,
    "workspace_map_admission": workspace_map_admission,
}


def run_controls(base, *, groups=None):
    """Retain every group's failure and continue through the shared adapter set."""
    results = {}
    with private_runtime(base / "ambient"):
        for harness in HARNESS_NAMES:
            for name in CONTROL_GROUPS if groups is None else groups:
                key = harness + "/" + name
                try:
                    results[key] = CONTROL_GROUPS[name](base / harness / name, harness)
                except Exception as failure:
                    results[key] = {"checks": {}, "harness_failure": {
                        "type": type(failure).__name__, "detail": str(failure),
                        "traceback": traceback.format_exc(limit=12)}}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True,
        help="literal clean commit verified externally by the caller; the probe checks source hashes only")
    parser.add_argument("--group", action="append", choices=tuple(CONTROL_GROUPS))
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != source:
        parser.error("run from the exact source worktree root")
    if len(args.source_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.source_sha):
        parser.error("source-sha must be 40 literal lowercase hex characters")
    if args.group is not None and len(args.group) != len(set(args.group)):
        parser.error("each bounded control group may be selected only once")
    bank = Path(tempfile.mkdtemp(prefix="waiter-projection-memo-", dir="\x2fprivate/tmp"))
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    names = sorted(path.relative_to(source).as_posix() for path in (source / "floati").rglob("*.py"))
    names += ["tests/probe_waiter_projection_memo.py", "tests/test_waiter_projection_memo.py"]
    def pins():
        return {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}
    receipt = {"source_sha": args.source_sha, "source_identity": "caller-verified literal SHA; not verified by probe",
               "before": pins(), "child_collection": "not measured; probe invokes no child-process API"}
    exit_code = 1
    try:
        with private_runtime(bank / "ambient"):
            receipt["results"] = run_controls(bank / "native", groups=args.group)
        checks = [value for group in receipt["results"].values() for value in group["checks"].values()]
        exit_code = 0 if checks and all(checks) and all(
            "harness_failure" not in group for group in receipt["results"].values()) else 1
    except BaseException as failure:
        receipt["harness_failure"] = {"type": type(failure).__name__, "detail": str(failure),
                                      "traceback": traceback.format_exc(limit=12)}
        exit_code = 130 if isinstance(failure, KeyboardInterrupt) else 1
    finally:
        receipt["after"] = pins()
        receipt["source_unchanged"] = receipt["before"] == receipt["after"]
        if not receipt["source_unchanged"]:
            exit_code = 1
        receipt["exit_code"] = exit_code
        (bank / "result.private.json").write_text(json.dumps(receipt, indent=2) + "\n")
    checks = [value for group in receipt.get("results", {}).values() for value in group["checks"].values()]
    print(json.dumps({"bank": str(bank), "source_sha": args.source_sha, "checks": len(checks),
        "passed": sum(checks), "exit_code": exit_code, "source_unchanged": receipt["source_unchanged"],
        "harness_failures": [key for key, value in receipt.get("results", {}).items() if "harness_failure" in value],
        "harness_failure": receipt.get("harness_failure"), "source_identity": receipt["source_identity"],
        "child_collection": receipt["child_collection"]}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
