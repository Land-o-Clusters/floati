"""Native invocation-local archive admission controls; not a unittest runner.

Observers delegate unchanged and retain one identity through preparation.
One real verified_hold_proofs call defines each measured invocation. Source
admission work is separate from the still-required native origin projections.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


def require(value, detail):
    if not value:
        raise AssertionError(detail)


def run_controls(base):
    from tests.probe_epoch_prefix import roll_imported_hold_fixture
    from floati import bus_epoch, epoch_carry, events, wake_hold
    from floati.errors import IntegrityFailure
    from floati.events import EventLog
    from floati.framing import encode_frame
    from floati.jsonl import MAX_LEDGER_BYTES
    from floati.registry import Registry
    from floati.root import FloatiRoot

    base.mkdir(parents=True)
    phase, project_depth, counts = ["fixture"], [0], {}
    native_project = wake_hold.project_wake_items
    native_semantics = events.validate_event_records
    native_read = bus_epoch._read_bound_regular
    native_assert = bus_epoch._assert_epoch_archive_snapshot

    def observe(name):
        counts.setdefault(phase[0], Counter())[name] += 1

    def project(**arguments):
        observe("project_calls")
        project_depth[0] += 1
        try:
            return native_project(**arguments)
        finally:
            project_depth[0] -= 1

    def semantics(rows):
        observe("project_semantic_calls" if project_depth[0] else "source_semantic_calls")
        return native_semantics(rows)

    def read(path, *arguments, **keywords):
        observe("bound_reads")
        observe("read:" + str(path))
        return native_read(path, *arguments, **keywords)

    def assert_archive(*arguments, **keywords):
        observe("archive_assertions")
        return native_assert(*arguments, **keywords)

    wake_hold.project_wake_items = project
    events.validate_event_records = semantics
    bus_epoch._read_bound_regular = read
    bus_epoch._assert_epoch_archive_snapshot = assert_archive
    try:
        root = FloatiRoot.open_direct_home(base / "fleet", create=True)
        for node in ("sender", "seat"):
            Registry(root).register(node, "Codex")
        policy = root.tenant_home / "state/ledger-policy.json"
        policy.parent.mkdir(parents=True, exist_ok=True)

        def quiet_policy():
            policy.write_text(json.dumps({"schema_version": 0,
                "max_bytes": MAX_LEDGER_BYTES + 1, "max_age_days": 7}) + "\n")

        quiet_policy()
        messages = [EventLog(root).send("sender", "seat", "floati", "a" * 40,
            "AGENTS.md", "native cut reuse", idempotency_key="mail-%d" % index)
            for index in range(32)]
        controller = wake_hold.WakeHoldController(root)
        held = [controller.evaluate("seat", idempotency_key="hold-%d" % index,
            limit=1)["receipt"] for index in range(6)]
        require([row["item_ids"] for row in held] == [[row["id"]] for row in messages[:6]],
                "native holds do not name expected physical messages")
        policy.write_text(json.dumps({"schema_version": 0,
            "max_bytes": 1, "max_age_days": 7}) + "\n")
        rolled, native_v3 = roll_imported_hold_fixture(root, messages, held)
        require(rolled.get("changed") is True, "native automatic roll absent")
        archive = Path(rolled["receipt"]["archive_path"])
        bus_epoch.verify_epoch_archive(archive)
        quiet_policy()
        checks, refusals = {"r3_roll_uses_native_v3_holds": native_v3}, {}

        def states(value):
            return [(row.message["id"], row.state, row.presentation_count) for row in value]

        def origin_bytes(value):
            source, projected = value
            return encode_frame({"source": source, "states": [
                {"message": row.message, "state": row.state, "count": row.presentation_count}
                for row in projected]})

        def refused(name, operation, code="epoch_carry_evidence_invalid", detail=None):
            try:
                operation()
            except IntegrityFailure as failure:
                refusals.setdefault(name, []).append({"code": failure.code, "detail": failure.detail})
                return failure.code == code and (detail is None or failure.detail == detail)
            refusals.setdefault(name, []).append(None)
            return False

        def one_admission(name):
            measured = counts[name]
            reads = [count for key, count in measured.items() if key.startswith("read:")]
            return measured["source_semantic_calls"] == 1 and bool(reads) and max(reads) == 1

        with bus_epoch.epoch_guard(root), wake_hold.wake_coordination_guard(root, "seat"):
            source = controller._read("seat", None)
            require(source[2] == held and all(row["schema_version"] in (1, 2) for row in held),
                "pre-R3 imported-hold fixture precondition: exact v1/v2 holds must survive the carry")
            resolver = epoch_carry.ArchiveProofResolver(root, head=source[0][0])
            resolver.archives()
            # Both cuts are native physical prefixes. The second outer call
            # introduces three origins not requested in the first invocation;
            # existing per-origin caches therefore cannot make the control vacuous.
            first_cut = (source[0], source[1], source[2][:3], source[3][:4], source[4], source[5])
            phase[0] = "first_outer_invocation"
            first_proofs = epoch_carry.verified_hold_proofs(root, first_cut, "seat", resolver=resolver)
            require(first_proofs is not None,
                "pre-R3 imported-hold fixture precondition: first cut must require sealed proofs")
            checks["first_invocation_admits_archive_source_once"] = one_admission(phase[0])
            checks["first_invocation_rechecks_membership"] = counts[phase[0]]["archive_assertions"] >= 3
            phase[0] = "second_outer_invocation"
            proofs = epoch_carry.verified_hold_proofs(root, source, "seat", resolver=resolver)
            require(proofs is not None,
                "pre-R3 imported-hold fixture precondition: complete cut must require sealed proofs")
            checks["later_invocation_admits_new_origins_afresh"] = one_admission(phase[0])
            checks["later_invocation_rechecks_membership"] = counts[phase[0]]["archive_assertions"] >= 3
            phase[0] = "projection"
            projected = project(events=source[0], event_prefix_digests=source[1],
                deliveries=source[2], delivery_prefix_digests=source[3], acknowledgments=source[4],
                acknowledgment_prefix_digests=source[5], recipient="seat", worker_session_id=None,
                tenant_id=root.tenant_id, _hold_proofs=proofs)
            checks["native_states_exact"] = states(projected) == [
                (row["id"], "held" if number < 6 else "fresh", 1 if number < 6 else 0)
                for number, row in enumerate(messages)]
            checks["progressive_cut_has_native_seal"] = first_proofs is not None and proofs is not None

            phase[0] = "outward_mutation_controls"
            returned_source = resolver.source(archive, "seat")
            wanted = encode_frame({"source": returned_source})
            exposed = next(row for row in returned_source[0] if row["kind"] == "message_envelope")
            note = exposed["note"]
            try:
                exposed["note"] = "caller changed returned archive source"
                checks["returned_source_is_detached"] = encode_frame({
                    "source": resolver.source(archive, "seat")}) == wanted
            finally:
                exposed["note"] = note

            def nested_delivery_is_detached(name, get_source, get_again):
                returned = get_source()
                expected_source = encode_frame({"source": returned})
                items = returned[2][0]["item_ids"]
                saved_item = items[0]
                try:
                    items[0] = messages[-1]["id"]
                    checks[name] = encode_frame({"source": get_again()}) == expected_source
                finally:
                    items[0] = saved_item

            nested_delivery_is_detached("returned_source_nested_items_are_detached",
                lambda: resolver.source(archive, "seat"), lambda: resolver.source(archive, "seat"))
            receipt = held[1]
            returned = resolver.hold_origin(receipt, "seat", None, root.tenant_home)
            wanted = origin_bytes(returned[1:])
            exposed = next(row for row in returned[1][0] if row["kind"] == "message_envelope")
            note = exposed["note"]
            try:
                exposed["note"] = "caller changed direct returned origin"
                checks["returned_direct_origin_is_detached"] = origin_bytes(
                    resolver.hold_origin(receipt, "seat", None, root.tenant_home)[1:]) == wanted
            finally:
                exposed["note"] = note

            nested_delivery_is_detached("returned_direct_origin_nested_items_are_detached",
                lambda: resolver.hold_origin(receipt, "seat", None, root.tenant_home)[1],
                lambda: resolver.hold_origin(receipt, "seat", None, root.tenant_home)[1])
            for name, select in (("returned_origin_is_detached", lambda pair: next(
                    row for row in pair[0][0] if row["kind"] == "message_envelope")),
                    ("returned_states_are_detached", lambda pair: pair[1][0].message)):
                returned = proofs.original(receipt)
                wanted = origin_bytes(returned)
                exposed = select(returned)
                note = exposed["note"]
                try:
                    exposed["note"] = "caller changed returned origin testimony"
                    checks[name] = (origin_bytes(proofs.original(receipt)) == wanted
                        and origin_bytes(resolver.hold_origin(receipt, "seat", None, root.tenant_home)[1:]) == wanted)
                finally:
                    exposed["note"] = note

            nested_delivery_is_detached("returned_proof_original_nested_items_are_detached",
                lambda: proofs.original(receipt)[0], lambda: proofs.original(receipt)[0])
            returned = proofs.origins[receipt["id"]]
            wanted = origin_bytes(proofs.original(receipt))
            exposed = next(row for row in returned[1][0] if row["kind"] == "message_envelope")
            note = exposed["note"]
            try:
                exposed["note"] = "caller changed public proof origins"
                checks["public_origins_do_not_alias_private_testimony"] = (
                    origin_bytes(proofs.original(receipt)) == wanted
                    and origin_bytes(resolver.hold_origin(receipt, "seat", None, root.tenant_home)[1:]) == wanted)
            finally:
                exposed["note"] = note

            nested_delivery_is_detached("public_origins_nested_items_do_not_alias_private_testimony",
                lambda: proofs.origins[receipt["id"]][1], lambda: proofs.original(receipt)[0])

            phase[0] = "changed_current_cut"
            exposed = next(row for row in source[0] if row["id"] == messages[0]["id"])
            note = exposed["note"]
            try:
                exposed["note"] = "valid changed current envelope"
                checks["changed_current_cut_refused"] = refused("changed_current_cut", lambda:
                    epoch_carry.verified_hold_proofs(root, source, "seat", resolver=resolver),
                    detail="imported hold envelope differs from its exact original native frame")
                checks["previous_seal_rejects_changed_cut"] = refused("changed_sealed_cut", lambda:
                    epoch_carry.verify_hold_proof_source(proofs, source, "seat", None, root.tenant_id),
                    detail="imported hold proof does not bind this exact source cut")
            finally:
                exposed["note"] = note

            phase[0] = "failed_origin_invocations"
            forged_hold = source[2][-1]
            old_digest = forged_hold["decision_digest"]
            try:
                forged_hold["decision_digest"] = "f" * 64
                attempts = [refused("forged_origin", lambda:
                    epoch_carry.verified_hold_proofs(root, source, "seat", resolver=resolver),
                    detail="imported hold ID has conflicting archived testimony") for _ in range(2)]
                checks["failed_origin_is_not_admitted"] = all(attempts)
            finally:
                forged_hold["decision_digest"] = old_digest

            phase[0] = "archive_rewrite"
            path = archive / "events.jsonl"
            raw = path.read_bytes()
            changed = raw.replace(b"native cut reuse", b"native cut reusx", 1)
            require(changed != raw and len(changed) == len(raw), "same-size archive mutation absent")
            try:
                path.write_bytes(changed)
                checks["archive_rewrite_refused_before_source_return"] = refused(
                    "archive_source_rewrite", lambda: resolver.source(archive, "seat"),
                    code="epoch_receipt_archive_invalid")
                checks["archive_rewrite_refused_before_origin_hit"] = refused(
                    "archive_origin_rewrite", lambda: resolver.hold_origin(receipt, "seat", None, root.tenant_home),
                    code="epoch_receipt_archive_invalid")
            finally:
                path.write_bytes(raw)
        return {"checks": checks, "refusals": refusals,
            "counts": {name: dict(value) for name, value in counts.items()},
            "native_messages": 32, "native_holds": 6, "first_cut_imported_holds": 3,
            "scope": "outer-invocation native archive admission and detached testimony; not latency"}
    finally:
        wake_hold.project_wake_items = native_project
        events.validate_event_records = native_semantics
        bus_epoch._read_bound_regular = native_read
        bus_epoch._assert_epoch_archive_snapshot = native_assert


def main():
    repository = Path(__file__).resolve().parents[1]
    require(Path.cwd().resolve() == repository, "exact source worktree required")
    require(len(sys.argv) == 3 and sys.argv[1] == "--source-sha"
        and re.fullmatch("[0-9a-f]{40}", sys.argv[2]), "literal caller-verified SHA required")
    bank = Path(tempfile.mkdtemp(prefix="epoch-cut-reuse-", dir="\x2fprivate/tmp"))
    bank.chmod(0o700)
    for name in ("home", "tmp"):
        (bank / name).mkdir(mode=0o700)
    os.environ.update(HOME=str(bank / "home"), TMPDIR=str(bank / "tmp"), PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repository))

    def inventory():
        paths = [repository / "LICENSE", Path(__file__), repository / "tests/test_epoch_cut_reuse.py",
            repository / "tests/probe_epoch_prefix.py"]
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
        try:
            receipt["source_unchanged"] = receipt["before"] == inventory()
        except BaseException as failure:
            receipt["source_unchanged"] = False
            receipt["source_failure"] = {"type": type(failure).__name__, "detail": str(failure)}
        (bank / "result.private.json").write_text(json.dumps(receipt, indent=2) + "\n")
    checks = receipt.get("result", {}).get("checks", {})
    print(json.dumps({"bank": str(bank), "checks": len(checks), "passed": sum(checks.values()),
        "source_unchanged": receipt["source_unchanged"], "failure": receipt.get("failure")}))
    return 0 if checks and all(checks.values()) and receipt["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
