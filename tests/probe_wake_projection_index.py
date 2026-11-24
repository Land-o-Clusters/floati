"""Product-only native projector controls; never a unittest driver.

Actual native sends and sealed holds supply frames/prefixes. Read-only sequence
visit counters measure repeated input work, without a clock threshold or fake
projector results. Hostile variants exercise the existing pure replay contract.
"""
from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


class CountedSequence(Sequence):
    def __init__(self, rows):
        self.rows, self.visits = rows, 0

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self.rows[index]
        value = self.rows[index]
        self.visits += 1
        return value


class CopyRefusingDict(dict):
    def copy(self):
        raise AssertionError("dict-subclass copy must not be called")


class EnvelopeMapping(Mapping):
    def __init__(self, row, *, broken_field=None):
        self.row, self.broken_field = row, broken_field

    def __iter__(self):
        return iter(self.row)

    def __len__(self):
        return len(self.row)

    def __getitem__(self, key):
        if key == self.broken_field:
            raise ValueError("controlled Mapping read failure")
        return self.row[key]

    def copy(self):
        raise AssertionError("Mapping copy must not be called")


def run_envelope_controls(base):
    """Freeze native envelope compatibility before changing shape allocations.

    Expectations use complete native send payloads or literal refusal contracts;
    neither shape membership nor expected output calls a candidate helper.
    """
    from floati import records
    from floati.errors import IntegrityFailure, ProtocolRefusal
    from floati.events import EventLog
    from floati.ids import uuid7_hex
    from floati.registry import Registry
    from floati.root import FloatiRoot
    from floati.wake_hold import project_wake_items

    root = FloatiRoot.open_direct_home(base / "fleet", create=True)
    for node in ("sender", "seat", "other"):
        Registry(root).register(node, "Codex")
    log = EventLog(root)
    original = log.send("seat", "sender", "floati", "a" * 40, "AGENTS.md",
        "original native message", idempotency_key="envelope-original")["message"]
    binding = {"attempt_id": "attempt-" + uuid7_hex(),
        "claim_id": "claim-" + uuid7_hex(), "lease_id": "lease-" + uuid7_hex(),
        "worker_session_id": "worker-" + uuid7_hex()}
    native = log.send("sender", "seat", "floati", "b" * 40, "AGENTS.md",
        "native bound reply", idempotency_key="envelope-reply",
        reply_to=original["id"], attempt_binding=binding)["message"]
    session = binding["worker_session_id"]
    bare = dict(native)
    for field in ("reply_to", "attempt_binding", "worker_session_id"):
        bare.pop(field)
    checks, faults = {}, {}

    def project(rows, worker_session_id=None):
        return project_wake_items(events=rows, deliveries=[], acknowledgments=[],
            recipient="seat", worker_session_id=worker_session_id, tenant_id=root.tenant_id,
            event_prefix_digests=tuple("%064x" % n for n in range(len(rows) + 1)),
            delivery_prefix_digests=("d" * 64,), acknowledgment_prefix_digests=("e" * 64,))

    def complete(states):
        return [(item.message, item.state, item.presentation_count) for item in states]

    def failure_value(failure):
        if failure is None:
            return None
        return {"type": type(failure).__module__ + "." + type(failure).__name__,
            "code": getattr(failure, "code", None), "detail": getattr(failure, "detail", str(failure)),
            "remedy": getattr(failure, "remedy", None),
            "cause": failure_value(failure.__cause__)}

    def observed(call):
        try:
            call()
        except (IntegrityFailure, ProtocolRefusal, ValueError) as failure:
            return failure_value(failure)
        return None

    def expected_failure(code, detail, *, integrity=True, cause=None):
        return {"type": "floati.errors." + ("IntegrityFailure" if integrity else "ProtocolRefusal"),
            "code": code, "detail": detail,
            "remedy": None if integrity else {
                "kind": "none", "why": "no action was named for this refusal"}, "cause": cause}

    def malformed(row, code, detail, *, position=1, cause=None):
        return expected_failure("consumption_state_unavailable",
            "events.jsonl record %d id %s is malformed (%s); quarantine that one record: "
            "floati repair quarantine --root ROOT --ledger events.jsonl --record-id %s "
            "--idempotency-key KEY" % (position, row["id"], code, row["id"]),
            cause=expected_failure(code, detail) if cause is None else cause)

    checks["genuine_native_attempt_binding"] = (
        native["attempt_binding"] == binding and native["worker_session_id"] == session)
    # Historical rows may omit any optional member. A complete binding needs a
    # session; standalone binding shapes therefore carry the literal sentinel.
    variants = (
        ("bare", {}),
        ("reply", {"reply_to": original["id"]}),
        ("binding", {"attempt_binding": "absent_legacy"}),
        ("session", {"worker_session_id": session}),
        ("reply_binding", {"reply_to": original["id"], "attempt_binding": "absent_legacy"}),
        ("reply_session", {"reply_to": original["id"], "worker_session_id": session}),
        ("binding_session", {"attempt_binding": binding, "worker_session_id": session}),
        ("reply_binding_session", {"reply_to": original["id"],
            "attempt_binding": binding, "worker_session_id": session}),
    )
    for name, optional in variants:
        row = dict(bare, **optional)
        rows = [original, row] if "reply_to" in optional else [row]
        selected_session = session if "worker_session_id" in optional else None
        expected = [(deepcopy(row), "fresh", 0)]
        checks["envelope_shape_" + name] = complete(project(rows, selected_session)) == expected
        for integrity in (False, True):
            checks["validator_shape_%s_%s" % (name, integrity)] = records.validate_record(
                row, root.tenant_id, frozenset({"message_envelope"}), integrity=integrity) == row

    fields_detail = "message_envelope fields do not match the exact record contract"
    missing = dict(bare)
    missing.pop("note")
    bad_rows = (
        ("missing_required", missing, "record_fields_invalid", fields_detail),
        ("extra_field", dict(bare, unexpected=True), "record_fields_invalid", fields_detail),
        ("shape_before_value", dict(missing, sha="bad", unexpected=True), "record_fields_invalid", fields_detail),
        ("sha_before_reply", dict(bare, sha="bad", reply_to="bad"), "sha_invalid",
            "sha must be a 40- or 64-character lowercase Git object id"),
        ("reply_before_binding", dict(native, reply_to="bad", attempt_binding={}), "reply_to_invalid",
            "reply_to must use the message UUIDv7 prefix"),
        ("partial_binding", dict(bare, attempt_binding={"attempt_id": binding["attempt_id"]}),
            "attempt_binding_invalid", "attempt binding must be absent_legacy or the exact complete binding object"),
        ("binding_without_session", dict(bare, attempt_binding=binding), "attempt_binding_session_mismatch",
            "attempt binding worker session must equal the envelope worker session"),
        ("binding_session_mismatch", dict(native, worker_session_id="different-session"),
            "attempt_binding_session_mismatch", "attempt binding worker session must equal the envelope worker session"),
    )
    for name, row, code, detail in bad_rows:
        faults[name] = observed(lambda: project([row], session))
        checks[name] = faults[name] == malformed(row, code, detail)
        for integrity in (False, True):
            value = observed(lambda: records.validate_record(row, root.tenant_id,
                frozenset({"message_envelope"}), integrity=integrity))
            checks["validator_%s_%s" % (name, integrity)] = value == expected_failure(
                code, detail, integrity=integrity)

    for name, row in (("exact_dict", dict(bare)), ("dict_subclass", CopyRefusingDict(bare)),
            ("mapping", EnvelopeMapping(bare))):
        states = project([row])
        checks["projector_conversion_" + name] = (
            complete(states) == [(bare, "fresh", 0)] and type(states[0].message) is dict
            and states[0].message is not row)
    mapping = EnvelopeMapping(bare, broken_field="sha")
    mapping_cause = {"type": "builtins.ValueError", "code": None,
        "detail": "controlled Mapping read failure", "remedy": None, "cause": None}
    faults["mapping_conversion_failure"] = observed(lambda: project([mapping]))
    checks["mapping_conversion_failure"] = faults["mapping_conversion_failure"] == malformed(
        bare, "ValueError", "", cause=mapping_cause)
    checks["direct_validator_rejects_mapping"] = observed(lambda: records.validate_record(
        EnvelopeMapping(bare), root.tenant_id, frozenset({"message_envelope"}), integrity=True)) == (
            expected_failure("record_not_object", "each durable record must be an object"))

    mutable = deepcopy(native)
    projected = project([original, mutable], session)[0].message
    projected["note"] = "changed output note"
    checks["projected_top_level_mutation_isolated"] = (
        mutable == native and complete(project([original, mutable], session)) == [(native, "fresh", 0)])
    mutable["note"] = "changed input note"
    checks["input_top_level_mutation_isolated"] = projected["note"] == "changed output note"
    checks["existing_nested_binding_alias_preserved"] = projected["attempt_binding"] is mutable["attempt_binding"]
    projected["attempt_binding"]["claim_id"] = "changed-nested-claim"
    checks["existing_nested_alias_mutation_visible"] = mutable["attempt_binding"]["claim_id"] == "changed-nested-claim"

    chained = dict(bare, seq=1, prev="0" * 64)
    checks["chained_envelope_complete_output"] = complete(project([chained])) == [(chained, "fresh", 0)]
    for name, row, code, detail in (
        ("chain_pair_before_shape_value", dict(missing, seq=1, sha="bad"),
            "journal_chain_fields_invalid", "chained records must carry seq and prev together"),
        ("chain_seq_before_shape_value", dict(missing, seq=True, prev="bad", sha="bad"),
            "journal_seq_invalid", "journal seq is outside its v-next bounds"),
        ("chain_prev_before_shape_value", dict(missing, seq=1, prev="bad", sha="bad"),
            "journal_prev_invalid", "journal prev must be a lowercase SHA-256 digest"),
    ):
        faults[name] = observed(lambda: project([row]))
        checks[name] = faults[name] == malformed(row, code, detail)
    unselected = dict(bare, recipient="other", sha="bad")
    faults["malformed_unselected_envelope"] = observed(lambda: project([bare, unselected]))
    checks["malformed_unselected_envelope"] = faults["malformed_unselected_envelope"] == malformed(
        unselected, "sha_invalid", "sha must be a 40- or 64-character lowercase Git object id", position=2)

    retraction = {"schema_version": 0, "id": "ret-" + uuid7_hex(), "tenant_id": root.tenant_id,
        "timestamp": native["timestamp"], "kind": "message_retracted",
        "retracted_message_id": native["id"], "worker_session_id": session,
        "reason": "sent_in_error", "author": "sender"}
    checks["semantic_retraction_complete_output"] = complete(project(
        [original, native, retraction], session)) == [(native, "retracted", 0)]
    for name, rows, code, detail in (
        ("semantic_reply_order", [native], "message_order_invalid",
            "reply evidence must follow its referenced original message"),
        ("semantic_reply_parties", [original, dict(native, sender="other")], "message_order_invalid",
            "reply evidence must reverse the original message parties"),
        ("semantic_retraction_order", [retraction, original, native], "message_retraction_order_invalid",
            "retraction evidence must follow its original message"),
        ("semantic_retraction_session", [original, native, dict(retraction, worker_session_id="other-session")],
            "message_retraction_session_invalid", "retraction worker session must equal its original message session"),
        ("semantic_retraction_party", [original, native, dict(retraction, author="other")],
            "message_retraction_party_invalid", "retraction author must be an original message party"),
        ("semantic_retraction_duplicate", [original, native, retraction, dict(retraction, id="ret-" + uuid7_hex())],
            "message_retraction_duplicate", "a message can have one append-only retraction"),
    ):
        faults[name] = observed(lambda: project(rows, session))
        checks[name] = faults[name] == expected_failure("consumption_state_unavailable",
            "events.jsonl testimony is semantically invalid (%s)" % code,
            cause=expected_failure(code, detail))

    # The public validator consults its current native spec on every call.
    # Rebinding that entry must not silently retain the imported field shape.
    prefix, fields = records._SPECS["message_envelope"]
    extended = dict(bare, fixture_extension="current native spec")
    with patch.dict(records._SPECS, {"message_envelope": (prefix, fields | {"fixture_extension"})}):
        checks["dynamic_native_spec_extension"] = records.validate_record(extended, root.tenant_id,
            frozenset({"message_envelope"}), integrity=True) == extended
        checks["dynamic_native_spec_old_shape_refused"] = observed(lambda: records.validate_record(
            bare, root.tenant_id, frozenset({"message_envelope"}), integrity=True)) == expected_failure(
                "record_fields_invalid", fields_detail)
    return checks, faults


def run_controls(base, *, background=128, holds=8):
    from floati.cursor import SparseCursor
    from floati.errors import IntegrityFailure
    from floati.events import EventLog
    from floati.records import wake_hold_decision_digest
    from floati.registry import Registry
    from floati.root import FloatiRoot
    from floati.wake_hold import WakeHoldController, project_wake_items
    base.mkdir(parents=True)
    root = FloatiRoot.open_direct_home(base / "fleet", create=True)
    for node in ("sender", "seat", "other"):
        Registry(root).register(node, "Codex")
    def send(node, key):
        return EventLog(root).send("sender", node, "floati", "a" * 40,
            "AGENTS.md", "native projector control", idempotency_key=key)
    unrelated = [send("other", "background-%d" % index) for index in range(background)]
    pending = [send("seat", "batch-first"), send("seat", "batch-second")]
    WakeHoldController(root).evaluate("seat", idempotency_key="hold-0")
    for index in range(1, holds):
        pending.append(send("seat", "held-%d" % index))
        WakeHoldController(root).evaluate("seat", idempotency_key="hold-%d" % index)
    source = WakeHoldController(root)._read("seat", None)

    def project(value):
        return project_wake_items(events=value[0], event_prefix_digests=value[1],
            deliveries=value[2], delivery_prefix_digests=value[3],
            acknowledgments=value[4], acknowledgment_prefix_digests=value[5],
            recipient="seat", worker_session_id=None, tenant_id=root.tenant_id)

    def values(states):
        return [(item.message["id"], item.state, item.presentation_count) for item in states]

    def refused(value):
        try:
            project(value)
        except IntegrityFailure as failure:
            return {"code": failure.code, "detail": failure.detail}
        return None

    counted_events, counted_prefixes = CountedSequence(source[0]), CountedSequence(source[1])
    counted = (counted_events, counted_prefixes, *source[2:])
    began = time.process_time()
    states = project(counted)
    cpu_seconds = time.process_time() - began
    expected = [(row["id"], "held", 1) for row in pending]
    checks = {"native_exact_held_states": values(states) == expected,
        "event_work_independent_of_hold_count": counted_events.visits <= 2 * len(source[0]),
        "prefix_work_independent_of_hold_count": counted_prefixes.visits <= 3 * len(source[1]),
        "native_plain_and_counted_equivalent": states == project(source)}
    failures = {}

    def variant(name, *, receipt=None, events=None, ep=None, dp=None):
        changed = list(deepcopy(source))
        if receipt is not None:
            changed[2][0] = receipt
        if events is not None:
            changed[0] = events
        if ep is not None:
            changed[1] = ep
        if dp is not None:
            changed[3] = dp
        failures[name] = refused(changed)
        return failures[name]

    def changed_receipt(**fields):
        row = dict(source[2][0], **fields)
        row["decision_digest"] = wake_hold_decision_digest(row)
        return row

    negatives = (
        ("reordered_ids", changed_receipt(item_ids=[pending[1]["id"], pending[0]["id"]]),
            "delivery receipt item ids are not physical event order"),
        ("duplicate_ids", changed_receipt(item_ids=[pending[0]["id"], pending[0]["id"]]),
            "delivery receipt item ids are invalid"),
        ("foreign_ids", changed_receipt(item_ids=[unrelated[0]["id"]]),
            "delivery receipt names a foreign or absent message"),
        ("uncovered_event_prefix", changed_receipt(event_prefix_digest=source[1][1]),
            "hold event prefix does not cover every named event"),
        ("missing_event_prefix", changed_receipt(event_prefix_digest="f" * 64),
            "hold event prefix is not an ancestor"),
        ("immediate_delivery_precedes_event_check", changed_receipt(
            delivery_prefix_digest=source[3][1], event_prefix_digest="f" * 64),
            "hold receipt does not bind its immediate delivery prefix"),
        ("missing_ack_prefix", changed_receipt(acknowledgment_prefix_digest="f" * 64),
            "hold acknowledgment prefix is not an ancestor"),
        ("forged_decision_digest", dict(source[2][0], decision_digest="f" * 64),
            "wake hold testimony is malformed"),
    )
    for name, receipt, detail in negatives:
        observed = variant(name, receipt=receipt)
        checks[name] = observed == {"code": "consumption_state_unavailable", "detail": detail}

    # The pure prefix contract permits repeated digest occurrences; coverage
    # means ANY matching position covers the named frame, not the first match.
    one = (source[0], source[1], [source[2][0]], source[3][:2], [], source[5][:1])
    repeated = list(source[1])
    digest = source[2][0]["event_prefix_digest"]
    repeated[0] = digest
    repeated_source = (one[0], tuple(repeated), *one[2:])
    checks["duplicate_digest_later_occurrence_covers"] = values(project(repeated_source)) == (
        [(row["id"], "held" if index < 2 else "fresh", 1 if index < 2 else 0)
         for index, row in enumerate(pending)])
    repeated_early = ["e" * 64] * len(source[1])
    repeated_early[0] = repeated_early[1] = digest
    early = refused((one[0], tuple(repeated_early), *one[2:]))
    failures["duplicate_digest_only_before_named_frames"] = early
    checks["duplicate_digest_only_before_named_frames"] = early == {
        "code": "consumption_state_unavailable", "detail": "hold event prefix does not cover every named event"}

    malformed = deepcopy(source[0])
    malformed[0]["sha"] = "bad"
    first_failure = variant("malformed_event_precedes_delivery", events=malformed,
        receipt=negatives[0][1])
    checks["malformed_event_precedes_delivery"] = (first_failure is not None
        and first_failure["code"] == "consumption_state_unavailable"
        and "sha" in first_failure["detail"]
        and "physical event order" not in first_failure["detail"])

    SparseCursor(root).ack("seat", [pending[-1]["id"]], acting_session_id="native-acted")
    acknowledged = WakeHoldController(root)._read("seat", None)
    checks["native_ack_still_wins_over_hold"] = values(project(acknowledged)) == (
        [(row["id"], "acknowledged" if index == len(pending) - 1 else "held", 1)
         for index, row in enumerate(pending)])
    envelope_checks, envelope_faults = run_envelope_controls(base / "envelopes")
    checks.update(envelope_checks)
    failures.update(envelope_faults)
    return {"checks": checks, "refusals": failures, "native_events": len(source[0]),
        "native_holds": len(source[2]), "native_pending_ids": [row["id"] for row in pending],
        "event_visits": counted_events.visits, "event_visit_bound": 2 * len(source[0]),
        "prefix_visits": counted_prefixes.visits, "prefix_visit_bound": 3 * len(source[1]),
        "projector_cpu_seconds": cpu_seconds}


def main():
    root = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != root:
        raise SystemExit("exact source worktree required")
    bank = Path(tempfile.mkdtemp(prefix="roll-wake-projector-", dir="\x2fprivate/tmp"))
    for name in ("home", "tmp"):
        (bank / name).mkdir(mode=0o700)
    os.environ.update(HOME=str(bank / "home"), TMPDIR=str(bank / "tmp"),
        PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    paths = ("floati/wake_hold.py", "floati/records.py", "tests/probe_wake_projection_index.py")
    pins = lambda: {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths}
    before = pins()
    receipt = {"source_sha": subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=root, text=True).strip(), "before": before, "children_collected": True}
    try:
        receipt["result"] = run_controls(bank / "native")
    except Exception as failure:
        receipt["harness_failure"] = {"type": type(failure).__name__, "detail": str(failure)}
    finally:
        receipt["after"] = pins()
        receipt["source_unchanged"] = receipt["after"] == before
        (bank / "result.private.json").write_text(json.dumps(receipt, indent=2) + "\n")
    checks = receipt.get("result", {}).get("checks", {})
    print(json.dumps({"bank": str(bank), "passed": sum(checks.values()), "checks": len(checks),
        "source_unchanged": receipt["source_unchanged"], "harness_failure": receipt.get("harness_failure")}))
    return 0 if checks and all(checks.values()) and receipt["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
