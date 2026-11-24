"""Native imported-hold envelope work controls; not a unittest runner.

The encoder delegates unchanged and keeps one identity from before fixture
preparation through final verification. Native cold origin preparation is
reported separately; bounded warm envelope checks are not a cold latency claim.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def run_controls(base, *, messages=48, holds=8):
    from tests.probe_epoch_prefix import roll_imported_hold_fixture
    from floati import bus_epoch, epoch_carry
    from floati.errors import IntegrityFailure
    from floati.events import EventLog
    from floati.jsonl import MAX_LEDGER_BYTES
    from floati.registry import Registry
    from floati.root import FloatiRoot
    from floati.wake_hold import WakeHoldController, project_wake_items, wake_coordination_guard

    require(4 <= holds < messages <= 128, "bounded native fixture dimensions required")
    base.mkdir(parents=True)
    native_encode = epoch_carry.encode_frame
    phase, visits, timings = ["fixture"], {}, {}

    def encode(row, *arguments, **keywords):
        counts = visits.setdefault(phase[0], Counter())
        counts["all_frames"] += 1
        if isinstance(row, dict) and row.get("kind") == "message_envelope":
            counts["message_frames"] += 1
        return native_encode(row, *arguments, **keywords)

    def timed(name, operation):
        phase[0] = name
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            return operation()
        finally:
            timings[name] = {"wall_seconds": time.perf_counter() - wall,
                "cpu_seconds": time.process_time() - cpu}

    epoch_carry.encode_frame = encode
    try:
        root = FloatiRoot.open_direct_home(base / "fleet", create=True)
        for node in ("sender", "seat"):
            Registry(root).register(node, "Codex")
        policy = root.tenant_home / "state/ledger-policy.json"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({"schema_version": 0,
            "max_bytes": MAX_LEDGER_BYTES + 1, "max_age_days": 7}) + "\n")
        pending = [EventLog(root).send("sender", "seat", "floati", "a" * 40,
            "AGENTS.md", "native envelope work control", idempotency_key="message-%d" % index)
            for index in range(messages)]
        controller = WakeHoldController(root)
        held = []
        for index in range(holds):
            decision = controller.evaluate("seat", idempotency_key="hold-%d" % index, limit=1)
            require(decision["state"] == "fresh_work" and decision["receipt"]["item_ids"]
                == [pending[index]["id"]], "native single-item hold differs")
            held.append(decision["receipt"])
        policy.write_text(json.dumps({"schema_version": 0, "max_bytes": 1, "max_age_days": 7}) + "\n")
        rolled, native_v3 = roll_imported_hold_fixture(root, pending, held)
        require(rolled.get("changed") is True and rolled.get("action") == "rotated",
            "native automatic roll missing")
        archive = Path(rolled["receipt"]["archive_path"])
        bus_epoch.verify_epoch_archive(archive)
        policy.write_text(json.dumps({"schema_version": 0,
            "max_bytes": MAX_LEDGER_BYTES + 1, "max_age_days": 7}) + "\n")

        def project(source, proofs):
            return project_wake_items(events=source[0], event_prefix_digests=source[1],
                deliveries=source[2], delivery_prefix_digests=source[3],
                acknowledgments=source[4], acknowledgment_prefix_digests=source[5],
                recipient="seat", worker_session_id=None, tenant_id=root.tenant_id,
                _hold_proofs=proofs)

        def values(states):
            return [(row.message["id"], row.state, row.presentation_count) for row in states]

        checks, refusals = {"r3_roll_uses_native_v3_holds": native_v3}, {}
        with bus_epoch.epoch_guard(root), wake_coordination_guard(root, "seat"):
            source = timed("native_live_read", lambda: controller._read("seat", None))
            require(source[2] == held and all(row["schema_version"] in (1, 2) for row in held),
                "pre-R3 imported-hold fixture precondition: exact v1/v2 holds must survive the carry")
            require(source[0][0]["kind"] == "bus_epoch_roll_receipt"
                and [row["id"] for row in source[2]] == [row["id"] for row in held],
                "roll did not preserve exact native holds")
            resolver = epoch_carry.ArchiveProofResolver(root, head=source[0][0])
            # Resolving each real archived origin is required product work and
            # may project historical cuts twice per hold. Never count it as a
            # free/cache-prewarmed cold reader or fold it into a warm bound.
            timed("native_origins_preparation", lambda: [resolver.hold_origin(
                row, "seat", None, root.tenant_home) for row in held])
            proofs = timed("verification_first", lambda: epoch_carry.verified_hold_proofs(
                root, source, "seat", resolver=resolver))
            repeated = timed("verification_repeat", lambda: epoch_carry.verified_hold_proofs(
                root, source, "seat", resolver=resolver))
            expected = [(row["id"], "held" if index < holds else "fresh", 1 if index < holds else 0)
                for index, row in enumerate(pending)]
            states = timed("native_projection", lambda: project(source, proofs))
            checks["exact_native_states"] = values(states) == expected
            checks["repeat_exact_native_states"] = values(project(source, repeated)) == expected
            checks["proof_is_native_and_present"] = proofs is not None and repeated is not None
            # Permit three complete message passes plus named references. The
            # current-message comparison and exact sealed source binding each
            # legitimately encode all live messages. No allowance grows as
            # messages * holds; no cache layout or implementation helper is
            # assumed. This is a work bound, not elapsed-time qualification.
            references = sum(len(row["item_ids"]) for row in held)
            bound = 3 * messages + references
            for name in ("verification_first", "verification_repeat"):
                checks[name + "_bounded"] = visits[name]["message_frames"] <= bound

            phase[0] = "refusal_controls"
            def refused(name, operation, detail=None):
                try:
                    operation()
                except IntegrityFailure as failure:
                    refusals[name] = {"code": failure.code, "detail": failure.detail}
                else:
                    refusals[name] = None
                checks[name] = refusals[name] is not None and refusals[name]["code"] == "epoch_carry_evidence_invalid"
                if detail is not None:
                    checks[name] = checks[name] and refusals[name]["detail"] == detail

            changed = deepcopy(source)
            next(row for row in changed[0] if row["id"] == pending[0]["id"])["note"] = "changed native envelope"
            envelope_detail = "imported hold envelope differs from its exact original native frame"
            refused("changed_current_envelope", lambda: epoch_carry.verified_hold_proofs(
                root, changed, "seat", resolver=resolver), envelope_detail)
            missing = list(deepcopy(source))
            missing[0] = [row for row in missing[0] if row["id"] != pending[0]["id"]]
            refused("missing_current_envelope", lambda: epoch_carry.verified_hold_proofs(
                root, missing, "seat", resolver=resolver), envelope_detail)
            refused("sealed_proof_rejects_changed_cut", lambda: epoch_carry.verify_hold_proof_source(
                proofs, changed, "seat", None, root.tenant_id),
                "imported hold proof does not bind this exact source cut")
            forged = deepcopy(source)
            forged[2][0]["decision_digest"] = "f" * 64
            refused("forged_imported_hold", lambda: epoch_carry.verified_hold_proofs(
                root, forged, "seat", resolver=resolver), "imported hold ID has conflicting archived testimony")

            # Corrupt only this private archive; restore its exact bytes. The
            # prior resolver retains the old identity and is never reused after
            # restoration. A fresh native resolver must admit the restored cut.
            path = archive / "events.jsonl"
            original = path.read_bytes()
            mutated = original.replace(b"native envelope work control", b"native envelope work controx", 1)
            require(mutated != original and len(mutated) == len(original), "archive mutation fixture absent")
            try:
                path.write_bytes(mutated)
                try:
                    epoch_carry.verified_hold_proofs(root, source, "seat", resolver=resolver)
                except IntegrityFailure as failure:
                    refusals["archive_tamper"] = {"code": failure.code, "detail": failure.detail}
                else:
                    refusals["archive_tamper"] = None
                checks["archive_tamper"] = refusals["archive_tamper"] == {
                    "code": "epoch_receipt_archive_invalid",
                    "detail": "epoch archive changed after its bound read"}
            finally:
                path.write_bytes(original)
            restored = epoch_carry.ArchiveProofResolver(root, head=source[0][0])
            restored_proofs = timed("restored_native_origin_and_verification", lambda:
                epoch_carry.verified_hold_proofs(root, source, "seat", resolver=restored))
            checks["restored_archive_fresh_resolver"] = values(project(source, restored_proofs)) == expected
        return {"checks": checks, "refusals": refusals, "native_messages": messages,
            "native_holds": holds, "native_item_references": references,
            "message_encoding_bound_per_verification": bound,
            "encoding_visits": {name: dict(counts) for name, counts in visits.items()},
            "timings": timings, "native_roll_receipt_id": rolled["receipt"]["id"],
            "encoder_identity_stable_during_all_preparation": epoch_carry.encode_frame is encode,
            "scope": "warm imported-envelope work after separately measured native origins; not cold latency"}
    finally:
        epoch_carry.encode_frame = native_encode


def main():
    repository = Path(__file__).resolve().parents[1]
    require(Path.cwd().resolve() == repository, "exact source worktree required")
    require(len(sys.argv) == 3 and sys.argv[1] == "--source-sha"
        and re.fullmatch(r"[0-9a-f]{40}", sys.argv[2]), "literal caller-verified source SHA required")
    bank = Path(tempfile.mkdtemp(prefix="epoch-hold-proof-work-", dir="\x2fprivate/tmp"))
    bank.chmod(0o700)
    for name in ("home", "tmp"):
        (bank / name).mkdir(mode=0o700)
    os.environ.update(HOME=str(bank / "home"), TMPDIR=str(bank / "tmp"), PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repository))
    def inventory():
        paths = [repository / "LICENSE", Path(__file__), repository / "tests/test_epoch_hold_proof_work.py",
            repository / "tests/probe_epoch_prefix.py"]
        for name in ("floati", "schemas", "scripts"):
            paths.extend(path for path in (repository / name).rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
        require(all(path.resolve() == path for path in paths), "source aliases refused")
        return {str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}
    receipt = {"caller_declared_source_sha": sys.argv[2],
        "source_identity_verified_by_probe": False, "children_started": 0, "before": inventory()}
    try:
        receipt["result"] = run_controls(bank / "native")
    except BaseException as failure:
        receipt["failure"] = {"type": type(failure).__name__, "detail": str(failure)}
    finally:
        try:
            receipt["after"] = inventory()
            receipt["source_unchanged"] = receipt["before"] == receipt["after"]
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
