"""Native hereditary-prefix controls; this module is not a unittest driver.

The first RED is actual archived replay work in verified_hold_proofs, without
requiring the future private index to import. All observers delegate unchanged.
Direct index controls activate when its implementation exists; absent controls
are explicit pending evidence, never a successful optimization claim.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import sys
import tempfile


def require(value, detail):
    if not value:
        raise AssertionError(detail)


def cut(source, counts):
    result = []
    for plane, count in zip((0, 2, 4), counts):
        result.extend((source[plane][:count], source[plane + 1][:count + 1]))
    return tuple(result)


def state_bytes(states):
    from floati.framing import encode_frame
    return encode_frame({"states": [{"message": row.message, "state": row.state,
        "presentation_count": row.presentation_count} for row in states]})


def roll_imported_hold_fixture(root, messages, holds):
    """Check the R3 writer, then install the pre-R3 byte-exact carry fixture.

    The real transaction owns the archive and receipt. Only successor placement
    is synthetic, as in EpochCarryTests.install_fixture_carry: old fleets kept
    the original hold frames and did not have a carry-verification event.
    """
    from unittest import mock
    from floati import bus_epoch, epoch_carry, epoch_rotation, wake_hold
    from floati.framing import encode_frame

    with bus_epoch.epoch_guard(root, exclusive=True):
        old = bus_epoch._read_selected_epoch_payloads(root,
            bus_epoch._preflight_selected_members(root))
        retained = epoch_carry.build_carry_plan(root, old).live_payloads
    rolled = epoch_rotation.maintenance(root, actor="sender", trigger="send")
    require(rolled.get("changed") is True, "genuine automatic epoch roll absent")
    with bus_epoch.epoch_guard(root, exclusive=True):
        with wake_hold.wake_coordination_guard(root, "seat"):
            current = wake_hold.WakeHoldController(root)._read("seat", None)
            with mock.patch.object(epoch_carry.ArchiveProofResolver, "_hold_origin",
                    side_effect=AssertionError("R3 native holds must not resolve sealed origins")):
                proofs = epoch_carry.verified_hold_proofs(root, current, "seat")
                states = wake_hold.project_wake_items(events=current[0], event_prefix_digests=current[1],
                    deliveries=current[2], delivery_prefix_digests=current[3],
                    acknowledgments=current[4], acknowledgment_prefix_digests=current[5],
                    recipient="seat", worker_session_id=None, tenant_id=root.tenant_id,
                    _hold_proofs=proofs)
            native = (proofs is None and len(current[2]) == len(holds)
                and all(row["schema_version"] == 3 and row["item_ids"] == original["item_ids"]
                    and row["carried_from"] == {"hold_id": original["id"],
                        "roll_receipt_id": rolled["receipt"]["id"]}
                    for row, original in zip(current[2], holds))
                and [(row.message["id"], row.state, row.presentation_count) for row in states]
                    == [(message["id"], "held" if index < len(holds) else "fresh",
                        1 if index < len(holds) else 0) for index, message in enumerate(messages)])
        for relative, _plane, raw in retained:
            path = root.path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((encode_frame(rolled["receipt"])
                if relative == "events.jsonl" else b"") + raw)
    return rolled, native


def run_controls(base):
    from floati import bus_epoch, epoch_carry, epoch_rotation, wake_hold
    from floati.cursor import SparseCursor
    from floati.errors import IntegrityFailure
    from floati.events import EVENT_KINDS, EventLog
    from floati.framing import decode_frames, encode_frame
    from floati.jsonl import MAX_LEDGER_BYTES
    from floati.records import WAKE_HOLD_KINDS, validate_record, wake_hold_decision_digest
    from floati.registry import Registry
    from floati.root import FloatiRoot

    base.mkdir(parents=True)
    checks, failures, observations, coverage = {}, {}, {}, {}
    phase = ["fixture"]
    native_project = wake_hold.project_wake_items

    def observe_project(**arguments):
        observations.setdefault(phase[0], Counter())["native_projector_calls"] += 1
        return native_project(**arguments)

    # Keep one function identity for preparation and both outer invocations.
    # Replacing it only for measurement would invalidate semantic cache keys.
    wake_hold.project_wake_items = observe_project
    try:
        root = FloatiRoot.open_direct_home(base / "fleet", create=True)
        for node in ("sender", "seat", "other"):
            Registry(root).register(node, "Codex")
        policy = root.tenant_home / "state/ledger-policy.json"
        policy.parent.mkdir(parents=True, exist_ok=True)

        def quiet_policy():
            policy.write_text(json.dumps({"schema_version": 0,
                "max_bytes": MAX_LEDGER_BYTES + 1, "max_age_days": 7}) + "\n")

        def send(key, recipient="seat", **arguments):
            return EventLog(root).send("sender", recipient, "floati", "a" * 40,
                "AGENTS.md", "native hereditary prefix", idempotency_key=key, **arguments)

        quiet_policy()
        messages = [send("mail-%d" % index) for index in range(18)]
        controller = wake_hold.WakeHoldController(root)
        held = [controller.evaluate("seat", idempotency_key="hold-%d" % index,
            limit=1)["receipt"] for index in range(6)]
        require([row["item_ids"] for row in held] == [[row["id"]] for row in messages[:6]],
            "genuine native hold order differs from fixture")
        policy.write_text(json.dumps({"schema_version": 0,
            "max_bytes": 1, "max_age_days": 7}) + "\n")
        rolled, checks["r3_roll_uses_native_v3_holds"] = roll_imported_hold_fixture(root, messages, held)
        require(rolled.get("changed") is True, "genuine automatic epoch roll absent")
        archive = Path(rolled["receipt"]["archive_path"])
        bus_epoch.verify_epoch_archive(archive)
        quiet_policy()

        def native(source, *, tenant=root.tenant_id, worker=None):
            return native_project(events=source[0], event_prefix_digests=source[1],
                deliveries=source[2], delivery_prefix_digests=source[3],
                acknowledgments=source[4], acknowledgment_prefix_digests=source[5],
                recipient="seat", worker_session_id=worker, tenant_id=tenant)

        def outcome(operation):
            try:
                return {"bytes": state_bytes(operation()).decode("utf-8")}
            except IntegrityFailure as failure:
                return {"type": type(failure).__name__, "code": failure.code,
                    "detail": failure.detail}

        def validate_fixture_records(source, tenant):
            # The pure projector does not schema-validate generic delivery/ACK
            # records. Prove that derived controls have valid native row shapes
            # before testing their deliberately changed semantic dependencies.
            for plane, kinds in ((0, EVENT_KINDS), (2, WAKE_HOLD_KINDS), (4, {"ack_receipt"})):
                for row in source[plane]:
                    validate_record(dict(row), tenant, frozenset(kinds), integrity=True)

        with bus_epoch.epoch_guard(root), wake_hold.wake_coordination_guard(root, "seat"):
            current = controller._read("seat", None)
            require(current[2] == held and all(row["schema_version"] in (1, 2) for row in held),
                "pre-R3 imported-hold fixture precondition: exact v1/v2 holds must survive the carry")
            resolver = epoch_carry.ArchiveProofResolver(root, head=current[0][0])
            resolver.archives()
            first = cut(current, (len(current[0]), 3, len(current[4])))
            phase[0] = "first_outer"
            first_proofs = epoch_carry.verified_hold_proofs(root, first, "seat", resolver=resolver)
            require(first_proofs is not None,
                "pre-R3 imported-hold fixture precondition: first cut must require sealed proofs")
            first_calls = observations.get(phase[0], Counter())["native_projector_calls"]
            checks["first_outer_replays_at_most_one_complete_archive"] = first_calls == 1
            checks["first_outer_has_native_imported_origins"] = (
                first_proofs is not None and len(first_proofs.origins) == 3)
            phase[0] = "second_outer"
            proofs = epoch_carry.verified_hold_proofs(root, current, "seat", resolver=resolver)
            require(proofs is not None,
                "pre-R3 imported-hold fixture precondition: complete cut must require sealed proofs")
            second_calls = observations.get(phase[0], Counter())["native_projector_calls"]
            # Three previously unseen origins prevent the old per-origin cache
            # from making the second lifetime/admission check vacuous.
            checks["second_outer_replays_at_most_one_complete_archive"] = second_calls == 1
            checks["second_outer_admits_new_origins"] = proofs is not None and len(proofs.origins) == 6
            phase[0] = "reference"
            projected = native_project(events=current[0], event_prefix_digests=current[1],
                deliveries=current[2], delivery_prefix_digests=current[3],
                acknowledgments=current[4], acknowledgment_prefix_digests=current[5],
                recipient="seat", worker_session_id=None, tenant_id=root.tenant_id,
                _hold_proofs=proofs)
            checks["native_roll_states_and_counts"] = [
                (row.message["id"], row.state, row.presentation_count) for row in projected] == [
                (message["id"], "held" if ordinal < 6 else "fresh", 1 if ordinal < 6 else 0)
                for ordinal, message in enumerate(messages)]
            original_source, original_states = proofs.original(held[0])
            expected = encode_frame({"source": original_source,
                "states": state_bytes(original_states).decode("utf-8")})
            original_states[0].message["note"] = "caller mutation"
            original_source[2].append(deepcopy(held[-1]))
            again_source, again_states = proofs.original(held[0])
            checks["public_origin_mutation_cannot_poison_later_testimony"] = expected == encode_frame({
                "source": again_source, "states": state_bytes(again_states).decode("utf-8")})
            archived = resolver.source(archive, "seat")
            checks["outer_verification_scope_is_closed"] = resolver._verification_depth == 0

        # Missing future API is reported rather than hiding the real native work
        # RED behind an import error. Once implemented, every direct control runs.
        try:
            from floati.epoch_prefix import _ArchivePrefixProjection
        except ModuleNotFoundError as failure:
            if failure.name != "floati.epoch_prefix":
                raise
            _ArchivePrefixProjection = None
        checks["private_prefix_index_available"] = _ArchivePrefixProjection is not None
        pending = []
        if _ArchivePrefixProjection is None:
            pending.append("direct index equivalence/dependency/fallback controls await implementation")
        else:
            # One small source with native foreign envelopes, backward replies,
            # readiness/claim metadata, unpresented delivery, ACK and retraction.
            small = FloatiRoot.open_direct_home(base / "small", create=True)
            for node in ("sender", "seat", "other"):
                Registry(small).register(node, "Codex")
            log = EventLog(small)
            small_worker = "small-worker"
            claim = {"kind": "delivery_claim", "schema_version": 0, "sha": "a" * 40,
                "repo_path": str(base), "bank": ["native_prefix"],
                "declared": {"ran": 1, "result": "OK"}, "artifacts": [], "deadline_seconds": 10}
            def mail(sender, recipient, key, **arguments):
                return log.send(sender, recipient, "floati", "a" * 40, "AGENTS.md",
                    "native small prefix", idempotency_key=key,
                    worker_session_id=small_worker, **arguments)
            m0 = mail("sender", "seat", "small-0", claim=claim)
            foreign = mail("sender", "other", "small-foreign")
            outgoing = mail("seat", "sender", "small-outgoing", reply_to=m0["id"])
            m1 = mail("sender", "seat", "small-reply", reply_to=outgoing["id"])
            _items, unpresented = log.present("seat", limit=1, worker_session_id=small_worker)
            require(unpresented["kind"] == "delivered_unpresented", "native unpresented delivery absent")
            SparseCursor(small).ack("seat", [m0["id"]], acting_session_id="small-acted",
                worker_session_id=small_worker)
            log.retract(m0["id"], worker_session_id=small_worker, author="sender", reason="sent_in_error")
            small_controller = wake_hold.WakeHoldController(small)
            small_controller.evaluate("seat", idempotency_key="small-held-1", limit=1,
                worker_session_id=small_worker)
            SparseCursor(small).ack("seat", [m1["id"]], acting_session_id="small-acted-1",
                worker_session_id=small_worker)
            m2 = mail("sender", "seat", "small-2")
            small_controller.evaluate("seat", idempotency_key="small-held-2", limit=1,
                worker_session_id=small_worker)
            complete = small_controller._read("seat", small_worker)

            def reference(source):
                return native(source, tenant=small.tenant_id, worker=small_worker)

            def index(source, counter=None):
                def counted(**arguments):
                    if counter is not None:
                        counter["admissions"] += 1
                    return native_project(**arguments)
                return _ArchivePrefixProjection(source, "seat", small_worker, small.tenant_id,
                    native_projector=counted)

            admissions = Counter()
            candidate = index(complete, admissions)
            cuts = list(itertools.product(*(range(len(complete[plane]) + 1) for plane in (0, 2, 4))))
            valid, invalid, accelerated, mismatches = 0, 0, 0, []
            for coordinates in cuts:
                requested = cut(complete, coordinates)
                want = outcome(lambda: reference(requested))
                got_states = candidate.project(coordinates)
                got = outcome(lambda: reference(requested)) if got_states is None else outcome(lambda: got_states)
                if "bytes" in want:
                    valid += 1
                    accelerated += got_states is not None
                else:
                    invalid += 1
                    require(got_states is None, "index accepted a native refused dependency cut")
                if got != want:
                    mismatches.append({"counts": coordinates, "candidate": got, "native": want})
            coverage["small"] = {"cuts": len(cuts), "valid": valid, "refused": invalid,
                "accelerated": accelerated, "mismatches": mismatches}
            checks["every_small_cut_matches_native_bytes_or_exact_refusal"] = not mismatches
            checks["small_controls_include_valid_and_inconsistent_cuts"] = valid > 0 and invalid > 0
            checks["supported_valid_cuts_use_one_native_admission"] = (
                admissions["admissions"] == 1 and accelerated == valid)
            checks["small_source_has_physical_foreign_and_metadata_gaps"] = (
                foreign["id"] not in [row.message["id"] for row in reference(complete)]
                and {row["kind"] for row in complete[0]} >= {
                    "message_envelope", "message_retracted", "delivery_claim", "send_readiness_observation"})
            checks["retraction_ack_hold_and_counts_preserve_precedence"] = [
                (row.message["id"], row.state, row.presentation_count) for row in reference(complete)] == [
                    (m0["id"], "retracted", 1), (m1["id"], "acknowledged", 1), (m2["id"], "held", 1)]
            retraction_at = next(i for i, row in enumerate(complete[0])
                if row["kind"] == "message_retracted")
            unpresented_cut = cut(complete, (retraction_at, 1, 0))
            # Canonical native receipt was unpresented; its delivery count still
            # increases while state remains fresh. Exact reference is independent.
            unpresented_states = reference(unpresented_cut)
            checks["unpresented_delivery_counts_without_hold"] = (
                next(row for row in unpresented_states if row.message["id"] == m0["id"]).state == "fresh"
                and next(row for row in unpresented_states if row.message["id"] == m0["id"]).presentation_count == 1)

            # A normal delivery frame uses the native unpresented receipt's
            # exact shape minus its extra evidence, exercising the pure contract.
            presented = list(deepcopy(unpresented_cut))
            presented[2][0]["kind"] = "delivery_receipt"
            presented[2][0].pop("presenter_evidence")
            presented = tuple(presented)
            validate_fixture_records(presented, small.tenant_id)
            presented_index = index(presented)
            presented_counts = tuple(len(presented[p]) for p in (0, 2, 4))
            presented_states = presented_index.project(presented_counts)
            checks["presented_delivery_counts_and_holds"] = (
                presented_states is not None and state_bytes(presented_states) == state_bytes(reference(presented))
                and next(row for row in presented_states if row.message["id"] == m0["id"]).state == "held")

            # Earliest SUFFICIENT matching digest, rather than a last-only bound.
            repeated = list(deepcopy(complete))
            repeated[2] = repeated[2][:2]
            repeated[3] = repeated[3][:3]
            repeated[4] = repeated[4][:1]
            repeated[5] = repeated[5][:2]
            hold = repeated[2][1]
            needed = max(i + 1 for i, row in enumerate(repeated[0]) if row.get("id") in hold["item_ids"])
            native_at = repeated[1].index(hold["event_prefix_digest"])
            ep = list(repeated[1])
            ep[0] = ep[-1] = hold["event_prefix_digest"]
            repeated[1] = tuple(ep)
            repeated = tuple(repeated)
            validate_fixture_records(repeated, small.tenant_id)
            repeated_counter = Counter()
            repeated_index = _ArchivePrefixProjection(repeated, "seat", small_worker, small.tenant_id,
                native_projector=lambda **args: (repeated_counter.update(["admissions"]), native_project(**args))[1])
            repeat_counts = (native_at, 2, 1)
            repeated_got = repeated_index.project(repeat_counts)
            checks["repeated_digest_uses_earliest_sufficient_occurrence"] = (
                needed <= native_at < len(ep) - 1 and repeated_got is not None
                and state_bytes(repeated_got) == state_bytes(reference(cut(repeated, repeat_counts)))
                and repeated_counter["admissions"] == 1)

            def late_failure(name, plane, row):
                damaged = list(deepcopy(complete))
                damaged[plane].append(row)
                damaged[plane + 1] = (*damaged[plane + 1], "f" * 64)
                damaged = tuple(damaged)
                validate_fixture_records(damaged, small.tenant_id)
                late = index(damaged)
                before_counts = tuple(len(complete[p]) for p in (0, 2, 4))
                full_counts = tuple(len(damaged[p]) for p in (0, 2, 4))
                before_states = late.project(before_counts)
                before = outcome(lambda: reference(cut(damaged, before_counts))) if before_states is None else outcome(lambda: before_states)
                after_states = late.project(full_counts)
                after = outcome(lambda: reference(damaged)) if after_states is None else outcome(lambda: after_states)
                wanted_failure = outcome(lambda: reference(damaged))
                failures[name] = wanted_failure
                checks[name + "_earlier_valid_cut_survives"] = before == outcome(lambda: reference(complete))
                checks[name + "_exact_native_fallback"] = (
                    after_states is None and after == wanted_failure and "code" in wanted_failure)

            bad_delivery = deepcopy(complete[2][-1])
            bad_delivery["id"] = "wake-hold-00000000000070008000000000000009"
            bad_delivery["item_ids"] = [foreign["id"]]
            bad_delivery["delivery_prefix_digest"] = complete[3][-1]
            bad_delivery["decision_digest"] = wake_hold_decision_digest(bad_delivery)
            late_failure("late_semantically_bad_delivery", 2, bad_delivery)
            bad_ack = deepcopy(complete[4][-1])
            bad_ack["id"] = "ack-00000000000070008000000000000009"
            bad_ack["item_ids"] = [foreign["id"]]
            late_failure("late_semantically_bad_ack", 4, bad_ack)

            ordinary_events = [deepcopy(current[0][0]), *deepcopy(complete[0])]
            ordinary = (ordinary_events, tuple("ordinary-%d" % i for i in range(len(ordinary_events) + 1)),
                [], ("ordinary-delivery",), [], ("ordinary-ack",))
            # This is a derived pure-projector input, not a receipt-owned archive.
            # Bind its copied head to the small root's distinct tenant explicitly.
            ordinary_events[0]["tenant_id"] = small.tenant_id
            validate_fixture_records(ordinary, small.tenant_id)
            ordinary_counter = Counter()
            ordinary_index = index(ordinary, ordinary_counter)
            ordinary_states = ordinary_index.project((len(ordinary_events), 0, 0))
            checks["ordinary_roll_head_without_teardown_is_supported"] = (
                ordinary_states is not None and ordinary_counter["admissions"] == 1
                and state_bytes(ordinary_states) == state_bytes(reference(ordinary)))

            # An unchanged pending closure is intentionally nonreclaimable.
            # Close a separate native foreign batch while policy is quiet so
            # the next automatic roll has actual reclaimable evidence. Pending
            # send-readiness metadata alone would be retained with its envelope.
            reclaimable = send("reroll-reclaimable", recipient="other")
            foreign_items, _foreign_receipt = EventLog(root).present("other", limit=1)
            require([row["id"] for row in foreign_items] == [reclaimable["id"]],
                "native reclaimable foreign batch absent")
            SparseCursor(root).ack("other", [reclaimable["id"]], acting_session_id="reroll-acted")
            policy.write_text(json.dumps({"schema_version": 0,
                "max_bytes": 1, "max_age_days": 7}) + "\n")
            rerolled = epoch_rotation.maintenance(root, actor="sender", trigger="send")
            require(rerolled.get("changed") is True, "second genuine automatic roll absent")
            quiet_policy()
            with bus_epoch.epoch_guard(root), wake_hold.wake_coordination_guard(root, "seat"):
                second_current = controller._read("seat", None)
                second_resolver = epoch_carry.ArchiveProofResolver(root, head=second_current[0][0])
                second_archive = Path(rerolled["receipt"]["archive_path"])
                imported_source = second_resolver.source(second_archive, "seat")
                imported_counts = tuple(len(imported_source[p]) for p in (0, 2, 4))
                imported_index = _ArchivePrefixProjection(imported_source, "seat", None, root.tenant_id,
                    native_projector=native_project)
                checks["imported_hold_physical_hashes_require_native_fallback"] = (
                    imported_index.project(imported_counts) is None)
                # The resolver seam must perform native sealed recursion; no
                # translated digest or synthetic proof can replace it.
                helper = getattr(second_resolver, "_project_archived_cut", None)
                with second_resolver._verification_scope():
                    sealed = helper(imported_source, imported_counts, second_archive, "seat", None) if helper else None
                checks["imported_hold_fallback_preserves_sealed_native_states"] = (
                    sealed is not None and state_bytes(sealed) == state_bytes(projected))

            # Native helpers bind a derived verification seed to copied roll
            # metadata for a pure-projector control, not completed teardown proof.
            # Omitting its final verification is a non-hereditary native refusal.
            from floati.epoch_teardown import finalize_verifications, verification_frames
            anchor = deepcopy(current[0][0])
            seeds, commitment = finalize_verifications([{
                "teardown_receipt_id": "seat-teardown-00000000000070008000000000000001",
                "teardown_context_sha256": "a" * 64, "report_envelope_id": None,
                "report_envelope_sha256": None,
                "verification": {"state": "verified", "failure_code": None, "remedy": None},
            }], anchor["epoch_id"])
            anchor["automatic"]["teardown_carry"] = commitment
            verifications = decode_frames(verification_frames(root, seeds, anchor))
            unsupported = ([anchor, *verifications], ("a" * 64, "b" * 64, "c" * 64),
                [], ("d" * 64,), [], ("e" * 64,))
            validate_fixture_records(unsupported, root.tenant_id)
            fallback = _ArchivePrefixProjection(unsupported, "seat", None, root.tenant_id,
                native_projector=native_project)
            for name, coordinates in (("complete", (2, 0, 0)), ("incomplete", (1, 0, 0))):
                states = fallback.project(coordinates)
                wanted = outcome(lambda: native(cut(unsupported, coordinates)))
                failures["teardown_" + name] = wanted
                checks["unsupported_teardown_" + name + "_uses_native_fallback"] = (
                    states is None and (("bytes" in wanted) if name == "complete" else ("code" in wanted)))

        return {"checks": checks, "pending": pending, "refusals": failures,
            "coverage": coverage, "observations": {key: dict(value) for key, value in observations.items()},
            "native_messages": len(messages), "native_holds": len(held),
            "scope": "small native hereditary equivalence and replay work; not large/cold/idle qualification"}
    finally:
        wake_hold.project_wake_items = native_project


def main():
    repository = Path(__file__).resolve().parents[1]
    require(Path.cwd().resolve() == repository, "exact source worktree required")
    require(len(sys.argv) == 3 and sys.argv[1] == "--source-sha"
        and re.fullmatch("[0-9a-f]{40}", sys.argv[2]), "literal caller-verified SHA required")
    bank = Path(tempfile.mkdtemp(prefix="epoch-prefix-", dir="\x2fprivate/tmp"))
    bank.chmod(0o700)
    for name in ("home", "tmp"):
        (bank / name).mkdir(mode=0o700)
    os.environ.update(HOME=str(bank / "home"), TMPDIR=str(bank / "tmp"), PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repository))

    def inventory():
        paths = [repository / "LICENSE", Path(__file__), repository / "tests/test_epoch_prefix.py"]
        for name in ("floati", "schemas", "scripts"):
            paths.extend(path for path in (repository / name).rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
        require(all(path.resolve() == path for path in paths), "source aliases refused")
        return {str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}

    receipt = {"caller_declared_source_sha": sys.argv[2], "source_identity_verified_by_probe": False,
        "children_started": 0, "before": inventory()}
    try:
        receipt["result"] = run_controls(bank / "native")
    except BaseException as failure:
        receipt["failure"] = {"type": type(failure).__name__, "detail": str(failure)}
    finally:
        receipt["source_unchanged"] = receipt["before"] == inventory()
        (bank / "result.private.json").write_text(json.dumps(receipt, indent=2) + "\n")
    checks = receipt.get("result", {}).get("checks", {})
    print(json.dumps({"bank": str(bank), "checks": len(checks), "passed": sum(checks.values()),
        "failed": [key for key, value in checks.items() if not value],
        "source_unchanged": receipt["source_unchanged"], "failure": receipt.get("failure")}))
    return 0 if checks and all(checks.values()) and receipt["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
