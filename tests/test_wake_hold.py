from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
import json
import hashlib
import types
from pathlib import Path
from unittest import mock

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.ids import uuid7_hex
from floati.records import WAKE_HOLD_KINDS, validate_record
from floati.root import FloatiRoot
from tests.schema_validation import SchemaValidationError, validate_json_schema


NOW = "2026-08-13T12:00:00.000Z"

# A hash domain the product carries as a salt, rebuilt here from the fence's
# own governed token rather than spelled -- and deliberately NOT imported
# from the module under test, so this stays an independent witness to the
# bytes rather than a mirror of them.
EVENT_DOMAIN = RETIRED_PRODUCT_NAME + "-wake-hold-events-v1"


def _persist_wake_hold_fixture(root: FloatiRoot, recipient: str, row: dict, *, session: object = None) -> None:
    """Persist hostile/test testimony without exercising or claiming controller authority."""
    from floati.cursor import SparseCursor
    from floati.framing import encode_frame

    path = root.resolve_relative(
        SparseCursor(root)._delivery_relative_path_for(recipient, worker_session_id=session)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(encode_frame(row))


def message(message_id: str, *, recipient: str = "bob", session: object = None, tenant_id: str = "alpha") -> dict:
    result = {
        "schema_version": 0,
        "id": message_id,
        "tenant_id": tenant_id,
        "timestamp": NOW,
        "kind": "message_envelope",
        "sender": public_ids.worker('alpha'),
        "recipient": recipient,
        "repo": "floati",
        "sha": "a" * 40,
        "doc": "docs/evidence/wake.md",
        "note": "wake evidence",
        "idempotency_key": "message-key",
        "attempt_binding": "absent_legacy",
    }
    if session is not None:
        result["worker_session_id"] = session
    return result


class WakeHoldRecordTests(unittest.TestCase):
    """Closed durable receipt cases; changes to the validation branch must fail these."""

    def setUp(self) -> None:
        self.item_a = "msg-018f7e9b3c117abc8def0123456789ab"
        self.item_b = "msg-018f7e9b3c127abc8def0123456789ab"

    def receipt(self) -> dict:
        from floati.wake_hold import wake_hold_receipt

        return wake_hold_receipt(
            tenant_id="alpha",
            recipient="bob",
            worker_session_id="worker-018f7e9b3c137abc8def0123456789ab",
            idempotency_key="wake-key",
            limit=2,
            item_ids=[self.item_a, self.item_b],
            event_prefix_digest="a" * 64,
            delivery_prefix_digest="b" * 64,
            acknowledgment_prefix_digest="c" * 64,
            now=NOW,
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )

    def test_schema_and_runtime_accept_one_exact_v1_hold_receipt(self) -> None:
        """Catches a missing v1 receipt kind, schema, or semantic digest binding."""
        row = self.receipt()
        self.assertEqual(row, validate_record(row, "alpha", frozenset({"wake_hold_receipt"}), integrity=False))
        schema = Path(__file__).parents[1] / "schemas/v1/wake-hold-receipt-record.schema.json"
        validate_json_schema(row, schema)
        self.assertEqual(set(row), set(__import__("json").loads(schema.read_text())["required"]))

    def test_runtime_and_schema_refuse_closed_shape_bounds_uuid_and_terminal_unsafe_key(self) -> None:
        """Catches a permissive receipt boundary accepting malformed controller testimony."""
        schema = Path(__file__).parents[1] / "schemas/v1/wake-hold-receipt-record.schema.json"
        candidates = (
            dict(self.receipt(), unknown=True),
            dict(self.receipt(), schema_version=0),
            dict(self.receipt(), id="wake-hold-not-a-uuid"),
            dict(self.receipt(), limit=0),
            dict(self.receipt(), idempotency_key="wake\u202e-key"),
            dict(self.receipt(), item_ids=[self.item_a, self.item_a]),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(candidate, "alpha", frozenset({"wake_hold_receipt"}), integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)

    def test_runtime_refuses_forged_recomputable_decision_digest(self) -> None:
        """Catches acceptance of a receipt whose semantic testimony was altered after hashing."""
        row = dict(self.receipt(), decision_digest="f" * 64)
        with self.assertRaises(ProtocolRefusal) as caught:
            validate_record(row, "alpha", frozenset({"wake_hold_receipt"}), integrity=False)
        self.assertEqual("wake_hold_decision_digest_invalid", caught.exception.code)

    def test_roll_anchored_hold_preserves_decision_and_requires_closed_provenance(self) -> None:
        """Carried testimony preserves both legacy selection shapes without open provenance."""
        from floati.records import wake_hold_decision_digest

        schema = Path(__file__).parents[1] / "schemas/v3/wake-hold-receipt-record.schema.json"
        for released in (False, True):
            with self.subTest(released=released):
                original = self.receipt()
                if released:
                    original.update(schema_version=2, selection_reason="holder_released")
                    original["decision_digest"] = wake_hold_decision_digest(original)
                row = dict(original, schema_version=3,
                    id="wake-hold-018f7e9b3c157abc8def0123456789ab",
                    event_prefix_digest="d" * 64, delivery_prefix_digest="e" * 64,
                    acknowledgment_prefix_digest="f" * 64,
                    carried_from={"hold_id": original["id"],
                        "roll_receipt_id": "bus-epoch-roll-receipt-018f7e9b3c167abc8def0123456789ab"})
                self.assertEqual(original["decision_digest"], row["decision_digest"])
                self.assertEqual(row, validate_record(row, "alpha", {"wake_hold_receipt"}, integrity=False))
                validate_json_schema(row, schema)
                missing = dict(row)
                missing.pop("carried_from")
                candidates = (
                    missing, dict(row, unknown=True),
                    dict(row, carried_from=dict(row["carried_from"], unknown=True)),
                    dict(row, carried_from={"hold_id": original["id"]}),
                    dict(row, carried_from=dict(row["carried_from"], hold_id="msg-" + original["id"][10:])),
                    dict(row, carried_from=dict(row["carried_from"], roll_receipt_id="not-a-roll")),
                    dict(row, selection_reason="fresh"), dict(row, decision_digest="not-a-digest"),
                )
                for index, candidate in enumerate(candidates):
                    with self.subTest(released=released, malformed=index):
                        with self.assertRaises(ProtocolRefusal):
                            validate_record(candidate, "alpha", {"wake_hold_receipt"}, integrity=False)
                        with self.assertRaises(SchemaValidationError):
                            validate_json_schema(candidate, schema)

    def test_carried_envelope_verification_requires_its_exact_roll_and_inventory(self) -> None:
        """Borrowed frame testimony cannot move to another roll or change its inventory."""
        from copy import deepcopy
        from floati.events import validate_event_records
        from floati.framing import decode_frames
        from floati.records import epoch_carry_verification_digest
        from tests.test_epoch_carry import EpochCarryTests

        fixture = EpochCarryTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.send("first-carried-envelope")
        fixture.send("second-carried-envelope")
        fixture.automatic_roll()
        events = decode_frames((fixture.root.path / "events.jsonl").read_bytes())
        row = next(record for record in events if record["kind"] == "epoch_carry_verification")
        schema = Path(__file__).parents[1] / "schemas/v0/epoch-carry-verification.schema.json"
        self.assertEqual(row, validate_record(row, fixture.root.tenant_id,
            {"epoch_carry_verification"}, integrity=False))
        validate_json_schema(row, schema)
        validate_event_records(events)
        missing = dict(row)
        missing.pop("roll_receipt_id")
        for candidate in (missing, dict(row, unknown=True), dict(row, carried_envelopes=[{"id": self.item_a}])):
            with self.subTest(shape=candidate):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(candidate, fixture.root.tenant_id,
                        {"epoch_carry_verification"}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)
        candidates = [
            dict(row, roll_receipt_id="bus-epoch-roll-receipt-018f7e9b3c177abc8def0123456789ab"),
            dict(row, roll_receipt_sha256="f" * 64), dict(row, digest="f" * 64),
        ]
        for entries in (list(reversed(row["carried_envelopes"])),
                        row["carried_envelopes"] + row["carried_envelopes"][:1]):
            candidates.append(dict(row, carried_envelopes=entries,
                digest=epoch_carry_verification_digest(entries)))
        changed = deepcopy(row)
        changed["carried_envelopes"][0]["sha256"] = "f" * 64
        candidates.append(changed)
        for index, candidate in enumerate(candidates):
            with self.subTest(corruption=index):
                altered = [candidate if record["id"] == row["id"] else record for record in events]
                with self.assertRaises(IntegrityFailure):
                    validate_event_records(altered)
        for altered in (events[1:], events + [row], [row, *events]):
            with self.subTest(anchor_or_duplicate=altered):
                with self.assertRaises(IntegrityFailure):
                    validate_event_records(altered)

    def test_preserved_legacy_hold_fixtures_remain_schema_and_runtime_valid(self) -> None:
        """The assessment fixtures retain old-valid v1 and v2 receipt behavior."""
        root = Path(__file__).parents[1]
        fixtures = json.loads((root / "tests/fixtures/waiter-sealed-r3-holds.json").read_text())
        for schema_name, rows in fixtures.items():
            for row in rows:
                with self.subTest(schema=schema_name):
                    validate_json_schema(row, root / schema_name)
                    self.assertEqual(row, validate_record(row, row["tenant_id"],
                        {"wake_hold_receipt"}, integrity=False))


    def test_sealed_ledger_refuses_cross_recipient_and_session_before_writing(self) -> None:
        """Catches a controller owner writing testimony into another ledger namespace."""
        from floati.wake_hold import WakeHoldLedger

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = FloatiRoot.open(Path(temp.name), "alpha")
        for recipient, session in (("charlie", "worker-other"), ("bob", "worker-other")):
            with self.subTest(recipient=recipient, session=session):
                ledger = WakeHoldLedger(root, "bob", worker_session_id="worker-bound")
                row = self.receipt()
                row["recipient"] = recipient
                row["worker_session_id"] = session
                from floati.records import wake_hold_decision_digest
                row["decision_digest"] = wake_hold_decision_digest(row)
                path = ledger.root.resolve_relative(ledger.relative_path)
                before = path.read_bytes() if path.exists() else b""
                with self.assertRaises(ProtocolRefusal) as caught:
                    ledger.append(row)
                self.assertEqual("wake_controller_only", caught.exception.code)
                self.assertEqual(before, path.read_bytes() if path.exists() else b"")

    def test_hold_key_refuses_every_terminal_unsafe_scalar_and_bounds(self) -> None:
        """Catches a schema/runtime gap for control, surrogate, bidi, or oversized keys."""
        schema = Path(__file__).parents[1] / "schemas/v1/wake-hold-receipt-record.schema.json"
        for key in ("x\x85", "x\ud800", "x\u202e", "x" * 129):
            with self.subTest(key=repr(key)):
                row = dict(self.receipt(), idempotency_key=key)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(row, "alpha", frozenset({"wake_hold_receipt"}), integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(row, schema)

    def test_generic_and_lower_writer_refuse_hold_rows_without_bytes(self) -> None:
        """Catches a forged allowed-kind set or direct writer bypassing the sealed owner."""
        from floati.framing import encode_frame
        from floati.jsonl import _append_frame, append_record, transact

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = FloatiRoot.open(Path(temp.name), "alpha")
        row = self.receipt()
        relative = "receipts/deliveries/bob.jsonl"
        path = root.resolve_relative(relative)
        for case_index, writer in enumerate((
            lambda: append_record(root, relative, row, allowed_kinds={"wake_hold_receipt"}),
            lambda: transact(root, relative, lambda _prior: (None, row), allowed_kinds={"wake_hold_receipt"}),
            lambda: _append_frame(path, encode_frame(row)),
        )):
            with self.subTest(case=(
                "append_record",
                "transact",
                "_append_frame",
            )[case_index]):
                before = path.read_bytes() if path.exists() else b""
                with self.assertRaises(ProtocolRefusal) as caught:
                    writer()
                self.assertEqual("wake_controller_only", caught.exception.code)
                self.assertEqual(before, path.read_bytes() if path.exists() else b"")

    def test_v0_receipt_view_filters_a_real_sealed_hold_row(self) -> None:
        """Catches v1 hold testimony widening the frozen outward delivery history."""
        from floati.projection import FleetProjection
        from floati.registry import Registry
        from floati.events import EventLog
        from floati.wake_hold import WakeHoldController

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = FloatiRoot.open(Path(temp.name), "alpha")
        registry = Registry(root)
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        EventLog(root, registry).send(public_ids.worker('alpha'), "bob", "floati", "a" * 40, "docs/evidence/hold.md", "hold", idempotency_key="hold")
        WakeHoldController(root).evaluate("bob", idempotency_key="hold-view")
        self.assertEqual([], FleetProjection(root).receipts("bob")["deliveries"])

    def test_public_or_recovered_hold_ledger_writer_never_has_controller_authority(self) -> None:
        """Catches any public/bound/recovered/monkeypatched wrapper minting durable held truth."""
        from floati.jsonl import _transact_wake_hold_records
        from floati.wake_hold import WakeHoldLedger

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = FloatiRoot.open(Path(temp.name), "alpha")
        ledger = WakeHoldLedger(root, "bob", worker_session_id="worker-018f7e9b3c137abc8def0123456789ab")
        row = self.receipt()
        path = root.resolve_relative(ledger.relative_path)
        recovered = types.FunctionType(WakeHoldLedger.append.__code__, WakeHoldLedger.append.__globals__)
        attempts = (
            lambda: ledger.append(row),
            lambda: type(ledger).append(ledger, row),
            lambda: recovered(ledger, row),
        )
        for case_index, attempt in enumerate(attempts):
            with self.subTest(case=(
                "bound-ledger-append",
                "class-ledger-append",
                "recovered-function-append",
            )[case_index]):
                with self.assertRaises(ProtocolRefusal) as caught:
                    attempt()
                self.assertEqual("wake_controller_only", caught.exception.code)
                self.assertEqual(b"", path.read_bytes() if path.exists() else b"")
        with mock.patch.object(
            WakeHoldLedger,
            "append",
            lambda owner, record: _transact_wake_hold_records(owner.root, owner.relative_path, lambda _prior: (record, record)),
        ):
            with self.assertRaises(ProtocolRefusal) as monkeypatched:
                ledger.append(row)
        self.assertEqual("wake_controller_only", monkeypatched.exception.code)
        self.assertEqual(b"", path.read_bytes() if path.exists() else b"")

    def test_only_original_evaluate_body_can_reach_private_hold_append(self) -> None:
        """Catches the importable helper exploit and every non-evaluate call provenance."""
        import floati.wake_hold as wake_hold_module
        from floati.records import wake_hold_decision_digest
        from floati.wake_hold import WakeHoldController

        helper = wake_hold_module._append_controller_receipt

        def case() -> tuple[FloatiRoot, object, dict, Path]:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            root = FloatiRoot.open(Path(temp.name), "alpha")
            controller = WakeHoldController(root)
            row = self.receipt()
            row["delivery_prefix_digest"] = hashlib.sha256(
                (RETIRED_PRODUCT_NAME + "-wake-hold-deliveries-v1").encode("ascii")
                + b"\0"
            ).hexdigest()
            row["decision_digest"] = wake_hold_decision_digest(row)
            from floati.cursor import SparseCursor
            path = root.resolve_relative(
                SparseCursor(root)._delivery_relative_path_for(
                    "bob", worker_session_id=row["worker_session_id"],
                )
            )
            return root, controller, row, path

        for label in ("direct", "alias", "wrapper", "recovered", "bound", "private-bound", "private-recovered"):
            with self.subTest(label=label):
                _root, controller, row, path = case()
                if label == "direct":
                    attempt = lambda: wake_hold_module._append_controller_receipt(controller, row)
                elif label == "alias":
                    alias = helper
                    attempt = lambda: alias(controller, row)
                elif label == "wrapper":
                    attempt = lambda: (lambda value: helper(controller, value))(row)
                elif label == "recovered":
                    recovered = types.FunctionType(helper.__code__, helper.__globals__)
                    attempt = lambda: recovered(controller, row)
                elif label == "bound":
                    bound = types.MethodType(helper, controller)
                    attempt = lambda: bound(row)
                elif label == "private-bound":
                    attempt = lambda: getattr(controller, "_append_receipt_already_guarded")(
                        "bob", row["worker_session_id"], row,
                    )
                else:
                    private = WakeHoldController.__dict__["_append_receipt_already_guarded"]
                    recovered_private = types.FunctionType(private.__code__, private.__globals__)
                    attempt = lambda: recovered_private(
                        controller, "bob", row["worker_session_id"], row,
                    )
                before = path.read_bytes() if path.exists() else b""
                with self.assertRaises(ProtocolRefusal) as caught:
                    attempt()
                self.assertEqual("wake_controller_only", caught.exception.code)
                self.assertEqual(before, path.read_bytes() if path.exists() else b"")

    def test_exact_controller_evaluate_is_the_positive_hold_writer_and_subclasses_refuse(self) -> None:
        """Catches a provenance gate that blocks the lawful body or admits an overridden type."""
        from floati.events import EventLog
        from floati.jsonl import read_records
        from floati.registry import Registry
        from floati.wake_hold import WakeHoldController

        def seeded() -> tuple[FloatiRoot, str]:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            root = FloatiRoot.open(Path(temp.name), "alpha")
            registry = Registry(root)
            registry.register(public_ids.worker('alpha'), "worker")
            registry.register("bob", "worker")
            item = EventLog(root, registry).send(
                public_ids.worker('alpha'), "bob", "floati", "a" * 40, "docs/evidence/provenance.md",
                "provenance", idempotency_key="provenance-message",
            )
            return root, str(item["id"])

        root, item_id = seeded()
        artifact = WakeHoldController(root).evaluate("bob", idempotency_key="lawful-controller")
        self.assertEqual([item_id], artifact["receipt"]["item_ids"])
        self.assertEqual(1, len(read_records(root, "receipts/deliveries/bob.jsonl", allowed_kinds=set(WAKE_HOLD_KINDS))))

        subclass_root, _ = seeded()
        class UntrustedSubclass(WakeHoldController):
            pass
        with self.assertRaises(ProtocolRefusal) as subclassed:
            UntrustedSubclass(subclass_root).evaluate("bob", idempotency_key="subclass-controller")
        self.assertEqual("wake_controller_only", subclassed.exception.code)
        delivery = subclass_root.resolve_relative("receipts/deliveries/bob.jsonl")
        self.assertEqual(b"", delivery.read_bytes() if delivery.exists() else b"")

    def test_reconstructed_evaluate_with_copied_globals_never_reaches_hold_append(self) -> None:
        """Catches copied globals preserving code identity while substituting decision dependencies."""
        from floati.events import EventLog
        from floati.registry import Registry
        from floati.wake_hold import WakeHoldController, WakeItemState

        original = WakeHoldController.evaluate

        def rebuilt(globals_mapping: dict) -> object:
            function = types.FunctionType(
                original.__code__, globals_mapping, original.__name__,
                original.__defaults__, original.__closure__,
            )
            function.__kwdefaults__ = dict(original.__kwdefaults__ or {})
            return function

        for label in ("unchanged-copy", "alternate-projector"):
            with self.subTest(label=label):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                root = FloatiRoot.open(Path(temp.name), "alpha")
                registry = Registry(root)
                registry.register(public_ids.worker('alpha'), "worker")
                registry.register("bob", "worker")
                EventLog(root, registry).send(
                    public_ids.worker('alpha'), "bob", "floati", "a" * 40,
                    "docs/evidence/copied-globals.md", "copied globals",
                    idempotency_key="copied-globals-message",
                )
                copied = dict(original.__globals__)
                if label == "alternate-projector":
                    forged = message("msg-018f7e9b3c197abc8def0123456789ab")
                    copied["project_wake_items"] = lambda **_kwargs: (
                        WakeItemState(forged, "fresh", 0),
                    )
                controller = WakeHoldController(root)
                delivery = root.resolve_relative("receipts/deliveries/bob.jsonl")
                before = delivery.read_bytes() if delivery.exists() else b""
                with self.assertRaises(ProtocolRefusal) as caught:
                    rebuilt(copied)(controller, "bob", idempotency_key=f"copied-{label}")
                self.assertEqual("wake_controller_only", caught.exception.code)
                self.assertEqual(before, delivery.read_bytes() if delivery.exists() else b"")

    def test_jsonl_boundary_rejects_copied_private_method_globals(self) -> None:
        """Catches the lower gate trusting a rebound method's copied globals mapping."""
        from floati.events import EventLog
        from floati.registry import Registry
        from floati.wake_hold import WakeHoldController

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = FloatiRoot.open(Path(temp.name), "alpha")
        registry = Registry(root)
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        EventLog(root, registry).send(
            public_ids.worker('alpha'), "bob", "floati", "a" * 40,
            "docs/evidence/private-globals.md", "private globals",
            idempotency_key="private-globals-message",
        )
        original = WakeHoldController.__dict__["_append_receipt_already_guarded"]
        copied = dict(original.__globals__)
        reconstructed = types.FunctionType(
            original.__code__, copied, original.__name__, original.__defaults__, original.__closure__,
        )
        reconstructed.__kwdefaults__ = dict(original.__kwdefaults__ or {})
        delivery = root.resolve_relative("receipts/deliveries/bob.jsonl")
        with mock.patch.object(WakeHoldController, "_append_receipt_already_guarded", reconstructed):
            with self.assertRaises(ProtocolRefusal) as caught:
                WakeHoldController(root).evaluate("bob", idempotency_key="private-globals")
        self.assertEqual("wake_controller_only", caught.exception.code)
        self.assertEqual(b"", delivery.read_bytes() if delivery.exists() else b"")


class WakeAttemptReceiptTests(unittest.TestCase):
    """Actual wake action testimony is separate from delivery and hold decisions."""

    def setUp(self) -> None:
        from floati.events import EventLog
        from floati.registry import Registry

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        self.events = EventLog(self.root, registry)

    def test_attempt_uses_certified_selected_envelopes_and_preserves_physical_order(self) -> None:
        """Unrelated retained mail must not be replayed to prove an existing decision."""
        from floati import jsonl
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController

        selected = [self.events.send(
            public_ids.worker('alpha'), "bob", "floati", "a" * 40,
            "docs/evidence/wake-attempt.md", "selected attempt",
            idempotency_key="selected-attempt-" + str(index),
        ) for index in range(2)]
        decision = WakeHoldController(self.root).evaluate("bob", idempotency_key="selected-decision")
        for index in range(3):
            self.events.send(public_ids.worker('alpha'), "bob", "floati", "a" * 40,
                "docs/evidence/wake-attempt.md", "unrelated retained mail",
                idempotency_key="unrelated-attempt-" + str(index))
        reads = []
        native = jsonl._read_path_records
        def observe(path, *args, **kwargs):
            if Path(path) == self.events.path:
                reads.append(str(path))
            return native(path, *args, **kwargs)
        arguments = dict(recipient="bob", acting_session_id="selected-session",
            item_ids=[row["id"] for row in selected], decision_receipt_id=decision["receipt"]["id"],
            message_worker_session_id=None, idempotency_key="selected-action", outcome="queued")
        with mock.patch.object(jsonl, "_read_path_records", side_effect=observe):
            receipt = WakeAttemptLedger(self.root).record(**arguments)
            self.assertEqual("queued", receipt["outcome"])
            self.assertEqual(receipt, WakeAttemptLedger(self.root).record(**arguments))
            with self.assertRaises(ProtocolRefusal) as refused:
                WakeAttemptLedger(self.root).record(**dict(arguments,
                    item_ids=list(reversed(arguments["item_ids"])), idempotency_key="reversed-action"))
            self.assertEqual("wake_envelope_not_owned", refused.exception.code)
        self.assertEqual([], reads, "certified selected-envelope proof replayed retained events")

    def test_cached_attempt_envelope_lookup_does_not_block_a_native_event_append(self) -> None:
        import threading
        from floati.send_prefix import Prefix
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController

        message = self.events.send(public_ids.worker('alpha'), "bob", "floati", "a" * 40,
            "docs/evidence/wake-attempt.md", "selected attempt",
            idempotency_key="concurrent-selected-attempt")
        decision = WakeHoldController(self.root).evaluate("bob",
            idempotency_key="concurrent-selected-decision")
        arguments = dict(recipient="bob", acting_session_id="selected-session",
            item_ids=[message["id"]], decision_receipt_id=decision["receipt"]["id"],
            message_worker_session_id=None, idempotency_key="concurrent-selected-action",
            outcome="queued")
        native_get = Prefix.get

        def exercise(mutation):
            lookup_entered, release_lookup, append_completed = (
                threading.Event(), threading.Event(), threading.Event())
            reader_identity, receipts, failures = [], [], []

            def get(prefix, domain, value):
                if (threading.get_ident() in reader_identity and domain == "id"
                        and value == message["id"] and not lookup_entered.is_set()):
                    lookup_entered.set()
                    if not release_lookup.wait(60):
                        raise AssertionError("selected-envelope lookup was not released")
                return native_get(prefix, domain, value)

            def read():
                reader_identity.append(threading.get_ident())
                try:
                    receipts.append(WakeAttemptLedger(self.root).record(**arguments))
                except Exception as error:
                    failures.append(error)

            def append():
                try:
                    if mutation == "corrupt":
                        # Hostile fixture bytes invalidate the captured cut;
                        # they are never authored as a native ledger record.
                        with self.events.path.open("ab") as stream:
                            stream.write(b"{partial")
                    else:
                        self.events.send(public_ids.worker('alpha'), public_ids.worker('alpha'),
                            "floati", "a" * 40, "docs/evidence/wake-attempt.md",
                            "unrelated concurrent append", idempotency_key="concurrent-unrelated-append")
                    append_completed.set()
                except Exception as error:
                    failures.append(error)

            reader = threading.Thread(target=read, name="selected-attempt-reader")
            writer = threading.Thread(target=append, name="unrelated-event-writer")
            progressed = False
            with mock.patch.object(Prefix, "get", get):
                reader.start()
                try:
                    self.assertTrue(lookup_entered.wait(2), "native selected-envelope lookup was not reached")
                    writer.start()
                    # This event is a concurrency watchdog, not a timing metric.
                    progressed = append_completed.wait(2)
                finally:
                    release_lookup.set()
                    for thread in (reader, writer):
                        if thread.ident is not None:
                            thread.join(timeout=10)
            for thread in (reader, writer):
                self.assertFalse(thread.is_alive(), "native concurrency test did not collect " + thread.name)
            self.assertTrue(progressed,
                "cached selected-envelope lookup retained the event lock and blocked a native append")
            return receipts, failures

        for mutation in ("append", "corrupt"):
            with self.subTest(mutation=mutation):
                events_before = self.events.path.read_bytes()
                attempt_path = self.root.resolve_relative("receipts/wakes/bob.jsonl")
                attempts_before = attempt_path.read_bytes() if attempt_path.exists() else None
                try:
                    receipts, failures = exercise(mutation)
                    if mutation == "corrupt":
                        with self.assertRaises(IntegrityFailure) as native:
                            self.events.event_records()
                        self.assertEqual([], receipts)
                        self.assertEqual(1, len(failures), failures)
                        self.assertIsInstance(failures[0], IntegrityFailure)
                        self.assertEqual((native.exception.code, native.exception.detail),
                            (failures[0].code, failures[0].detail))
                        self.assertEqual(attempts_before,
                            attempt_path.read_bytes() if attempt_path.exists() else None)
                    else:
                        self.assertEqual([], failures)
                        self.assertEqual(1, len(receipts))
                        self.assertEqual(("queued", [message["id"]]),
                            (receipts[0]["outcome"], receipts[0]["item_ids"]))
                        self.assertEqual(receipts[0], WakeAttemptLedger(self.root).record(**arguments))
                finally:
                    if mutation == "corrupt":
                        self.events.path.write_bytes(events_before)

    def test_attempt_accepts_only_proven_original_decision_after_two_rolls(self) -> None:
        from tests.test_epoch_carry import EpochCarryTests
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController

        fixture = EpochCarryTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        message = fixture.send('attempt-before-carry')
        original = WakeHoldController(fixture.root).evaluate('seat-a',
            idempotency_key='attempt-decision')['receipt']
        fixture.automatic_roll()
        fixture.automatic_roll()
        for known in (True, False):
            with self.subTest(known=known):
                decision_id = original['id'] if known else 'wake-hold-' + uuid7_hex()
                def record():
                    return WakeAttemptLedger(fixture.root).record(recipient='seat-a',
                    acting_session_id='attempt-session', item_ids=[message['id']],
                    decision_receipt_id=decision_id, message_worker_session_id=None,
                    idempotency_key='carried-attempt-' + str(known), outcome='queued')
                if known:
                    self.assertEqual('queued', record()['outcome'])
                else:
                    with self.assertRaises(ProtocolRefusal) as refused:
                        record()
                    self.assertEqual('wake_decision_missing', refused.exception.code)

    def test_successful_prompt_records_closed_node_session_and_envelope_receipt(self) -> None:
        """Catches an actual wake remaining invisible after a lawful decision."""
        from floati.cursor import SparseCursor
        from floati.jsonl import read_records
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController

        message = self.events.send(
            public_ids.worker('alpha'), "bob", "floati", "a" * 40,
            "docs/evidence/wake-attempt.md", "wake attempt",
            idempotency_key="wake-attempt-message",
        )
        decision = WakeHoldController(self.root).evaluate(
            "bob", idempotency_key="wake-attempt-decision",
        )
        receipt = WakeAttemptLedger(self.root).record(
            recipient="bob",
            acting_session_id="session-018f7e9b3c137abc8def0123456789ab",
            item_ids=[message["id"]],
            decision_receipt_id=decision["receipt"]["id"],
            message_worker_session_id=None,
            idempotency_key="wake-attempt-action",
            outcome="woke",
        )

        self.assertEqual("wake_attempt_receipt", receipt["kind"])
        self.assertEqual("bob", receipt["node_id"])
        self.assertEqual("session-018f7e9b3c137abc8def0123456789ab", receipt["acting_session_id"])
        self.assertEqual("woke", receipt["outcome"])
        schema = Path(__file__).parents[1] / "schemas/v1/wake-attempt-record.schema.json"
        validate_json_schema(receipt, schema)
        durable = read_records(
            self.root, "receipts/wakes/bob.jsonl",
            allowed_kinds={"wake_attempt_receipt"},
        )
        self.assertEqual([receipt], durable)
        self.assertEqual(frozenset(), SparseCursor(self.root).acked_ids("bob"))

    def test_old_attempt_fixtures_validate_before_and_after_loosening(self) -> None:
        import json
        repository = Path(__file__).parents[1]
        fixtures = repository / "tests/fixtures/wd-r4-f1"
        for outcome in ("woke", "refused"):
            record = json.loads((fixtures / f"old-{outcome}.json").read_text())
            for schema in (fixtures / "schema-before.json", repository / "schemas/v1/wake-attempt-record.schema.json"):
                with self.subTest(outcome=outcome, schema=schema.name):
                    validate_json_schema(record, schema)

    def test_queued_attempt_is_owned_validated_and_idempotent(self) -> None:
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController
        from floati.jsonl import read_records
        message = self.events.send(
            public_ids.worker('alpha'), "bob", "floati", "a" * 40,
            "docs/evidence/wake-attempt.md", "queue attempt",
            idempotency_key="queue-message",
        )
        decision = WakeHoldController(self.root).evaluate("bob", idempotency_key="queue-decision")
        ledger = WakeAttemptLedger(self.root)
        args = dict(recipient="bob", acting_session_id="queue-session",
                    item_ids=[message["id"]], decision_receipt_id=decision["receipt"]["id"],
                    message_worker_session_id=None, idempotency_key="queue-action", outcome="queued")
        receipt = ledger.record(**args)
        self.assertEqual("queued", receipt["outcome"])
        self.assertIsNone(receipt["reason_code"])
        validate_json_schema(receipt, Path(__file__).parents[1] / "schemas/v1/wake-attempt-record.schema.json")
        self.assertEqual(receipt, ledger.record(**args))
        self.assertEqual([receipt], read_records(self.root, "receipts/wakes/bob.jsonl", allowed_kinds={"wake_attempt_receipt"}))
        for changes, reason in [
            ({"decision_receipt_id": None}, "wake_decision_missing"),
            ({"recipient": public_ids.worker('alpha')}, "wake_envelope_not_owned"),
        ]:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ProtocolRefusal, reason):
                    ledger.record(**dict(args, **changes, idempotency_key=reason))

    def test_wrong_session_attempt_records_typed_refusal_and_never_acknowledges(self) -> None:
        """Catches a session waking on an envelope owned by a different session without evidence."""
        from floati.cursor import SparseCursor
        from floati.jsonl import read_records
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController

        owner = "session-018f7e9b3c137abc8def0123456789ab"
        intruder = "session-018f7e9b3c147abc8def0123456789ab"
        message = self.events.send(
            public_ids.worker('alpha'), "bob", "floati", "a" * 40,
            "docs/evidence/wake-attempt.md", "wake attempt",
            idempotency_key="owned-wake-message", worker_session_id=owner,
        )
        decision = WakeHoldController(self.root).evaluate(
            "bob", idempotency_key="owned-wake-decision", worker_session_id=owner,
        )

        with self.assertRaises(ProtocolRefusal) as caught:
            WakeAttemptLedger(self.root).record(
                recipient="bob", acting_session_id=intruder,
                item_ids=[message["id"]],
                decision_receipt_id=decision["receipt"]["id"],
                message_worker_session_id=owner,
                idempotency_key="wrong-session-action", outcome="woke",
            )

        self.assertEqual("wake_envelope_not_owned", caught.exception.code)
        durable = read_records(
            self.root, "receipts/wakes/bob.jsonl",
            allowed_kinds={"wake_attempt_receipt"},
        )
        self.assertEqual(1, len(durable))
        self.assertEqual("refused", durable[0]["outcome"])
        self.assertEqual("wake_envelope_not_owned", durable[0]["reason_code"])
        self.assertEqual(intruder, durable[0]["acting_session_id"])
        self.assertEqual(frozenset(), SparseCursor(self.root).acked_ids("bob", worker_session_id=owner))

    def test_refused_without_reason_code_records_wake_prompt_failed(self) -> None:
        """WD-R1-F1: WakeAttemptLedger.record(outcome=refused, reason_code=None) hits the fallback."""
        from floati.jsonl import read_records
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController

        message = self.events.send(
            public_ids.worker('alpha'), "bob", "floati", "a" * 40,
            "docs/evidence/wake-attempt.md", "wake attempt omitted reason",
            idempotency_key="wake-attempt-omitted-reason-message",
        )
        decision = WakeHoldController(self.root).evaluate(
            "bob", idempotency_key="wake-attempt-omitted-reason-decision",
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            WakeAttemptLedger(self.root).record(
                recipient="bob",
                acting_session_id="session-018f7e9b3c137abc8def0123456789ab",
                item_ids=[message["id"]],
                decision_receipt_id=decision["receipt"]["id"],
                message_worker_session_id=None,
                idempotency_key="wake-attempt-omitted-reason-action",
                outcome="refused",
            )
        self.assertEqual("wake_prompt_failed", caught.exception.code)
        durable = read_records(
            self.root, "receipts/wakes/bob.jsonl",
            allowed_kinds={"wake_attempt_receipt"},
        )
        self.assertEqual(1, len(durable))
        self.assertEqual("refused", durable[0]["outcome"])
        self.assertEqual("wake_prompt_failed", durable[0]["reason_code"])

    def test_refused_with_explicit_reason_code_is_not_rewritten_to_wake_prompt_failed(self) -> None:
        """WD-R1-F1: an owned refused attempt keeps every adapter-emitted reason."""
        from floati.jsonl import read_records
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController
        from tests.test_wake_daemon_adapters import _emitted_wake_adapter_reasons

        for code in sorted(_emitted_wake_adapter_reasons()):
            with self.subTest(code=code):
                message = self.events.send(
                    public_ids.worker('alpha'), "bob", "floati", "a" * 40,
                    "docs/evidence/wake-attempt.md", f"wake attempt {code}",
                    idempotency_key=f"wake-attempt-emitted-{code}-message",
                )
                decision = WakeHoldController(self.root).evaluate(
                    "bob", idempotency_key=f"wake-attempt-emitted-{code}-decision",
                )
                with self.assertRaises(ProtocolRefusal) as caught:
                    WakeAttemptLedger(self.root).record(
                        recipient="bob",
                        acting_session_id="session-018f7e9b3c137abc8def0123456789ab",
                        item_ids=[message["id"]],
                        decision_receipt_id=decision["receipt"]["id"],
                        message_worker_session_id=None,
                        idempotency_key=f"wake-attempt-emitted-{code}-action",
                        outcome="refused",
                        reason_code=code,
                    )
                self.assertEqual(code, caught.exception.code)
                durable = read_records(
                    self.root, "receipts/wakes/bob.jsonl",
                    allowed_kinds={"wake_attempt_receipt"},
                )
                matching = [row for row in durable if row["reason_code"] == code]
                self.assertEqual(1, len(matching))
                self.assertEqual("refused", matching[0]["outcome"])

    def test_hook_trust_lapse_reasons_survive_in_the_wake_attempt_ledger(self) -> None:
        """Catches three actionable hook causes collapsing into wake_prompt_failed."""
        from floati.jsonl import read_records
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController

        for code in (
            "wake_hook_untrusted",
            "wake_hook_modified",
            "wake_hook_disabled",
        ):
            with self.subTest(code=code):
                message = self.events.send(
                    public_ids.worker('alpha'), "bob", "floati", "a" * 40,
                    "docs/evidence/wake-attempt.md", f"wake attempt {code}",
                    idempotency_key=f"wake-attempt-{code}-message",
                )
                decision = WakeHoldController(self.root).evaluate(
                    "bob", idempotency_key=f"wake-attempt-{code}-decision",
                )
                with self.assertRaises(ProtocolRefusal) as caught:
                    WakeAttemptLedger(self.root).record(
                        recipient="bob",
                        acting_session_id="session-018f7e9b3c137abc8def0123456789ab",
                        item_ids=[message["id"]],
                        decision_receipt_id=decision["receipt"]["id"],
                        message_worker_session_id=None,
                        idempotency_key=f"wake-attempt-{code}-action",
                        outcome="refused",
                        reason_code=code,
                    )
                self.assertEqual(code, caught.exception.code)
                durable = read_records(
                    self.root, "receipts/wakes/bob.jsonl",
                    allowed_kinds={"wake_attempt_receipt"},
                )
                matching = [row for row in durable if row["reason_code"] == code]
                self.assertEqual(1, len(matching))
                validate_json_schema(
                    matching[0],
                    Path(__file__).parents[1]
                    / "schemas/v1/wake-attempt-record.schema.json",
                )

    def test_zcode_output_codes_record_as_themselves_not_wake_prompt_failed(self) -> None:
        """WD-R2-F1: zcode adapter reasons join the refused set; they are not rewritten."""
        from floati.jsonl import read_records
        from floati.records import WAKE_ATTEMPT_REFUSED_REASONS
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController

        codes = (
            "wake_daemon_zcode_output_empty",
            "wake_daemon_zcode_output_invalid",
            "wake_daemon_zcode_result_invalid",
        )
        self.assertTrue(set(codes) <= set(WAKE_ATTEMPT_REFUSED_REASONS))
        for code in codes:
            with self.subTest(code=code):
                message = self.events.send(
                    public_ids.worker('alpha'), "bob", "floati", "a" * 40,
                    "docs/evidence/wake-attempt.md", f"wake attempt {code}",
                    idempotency_key=f"wake-attempt-{code}-message",
                )
                decision = WakeHoldController(self.root).evaluate(
                    "bob", idempotency_key=f"wake-attempt-{code}-decision",
                )
                with self.assertRaises(ProtocolRefusal) as caught:
                    WakeAttemptLedger(self.root).record(
                        recipient="bob",
                        acting_session_id="session-018f7e9b3c137abc8def0123456789ab",
                        item_ids=[message["id"]],
                        decision_receipt_id=decision["receipt"]["id"],
                        message_worker_session_id=None,
                        idempotency_key=f"wake-attempt-{code}-action",
                        outcome="refused",
                        reason_code=code,
                    )
                self.assertEqual(code, caught.exception.code)
                durable = read_records(
                    self.root, "receipts/wakes/bob.jsonl",
                    allowed_kinds={"wake_attempt_receipt"},
                )
                matching = [row for row in durable if row["reason_code"] == code]
                self.assertEqual(1, len(matching))
                self.assertEqual("refused", matching[0]["outcome"])


class WakeHoldProjectionTests(unittest.TestCase):
    """Pure physical-order replay; a stateful seen cursor would not satisfy these cases."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.first = "msg-018f7e9b3c117abc8def0123456789ab"
        self.second = "msg-018f7e9b3c127abc8def0123456789ab"

    def test_projection_uses_only_four_states_and_counts_each_physical_presentation(self) -> None:
        """Catches holds being mistaken for acknowledgments or replay losing multiplicity."""
        from floati.wake_hold import project_wake_items

        events = [message(self.first), message(self.second)]
        from floati.wake_hold import wake_hold_receipt
        hold = wake_hold_receipt(
            tenant_id="alpha", recipient="bob", worker_session_id=None,
            idempotency_key="count-key", limit=2, item_ids=[self.first, self.second],
            event_prefix_digest="2" * 64, delivery_prefix_digest="4" * 64,
            acknowledgment_prefix_digest="5" * 64, now=NOW,
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )
        deliveries = [
            {"kind": "delivery_receipt", "recipient": "bob", "item_ids": [self.first], "presentation_count": 1},
            hold,
        ]
        acknowledgments = [{"kind": "ack_receipt", "recipient": "bob", "item_ids": [self.second]}]
        states = project_wake_items(
            events=events, deliveries=deliveries, acknowledgments=acknowledgments,
            recipient="bob", worker_session_id=None, tenant_id="alpha",
            event_prefix_digests=("0" * 64, "1" * 64, "2" * 64), delivery_prefix_digests=("3" * 64, "4" * 64, "5" * 64),
            acknowledgment_prefix_digests=("5" * 64, "6" * 64),
        )
        self.assertEqual(
            [(self.first, "held", 2), (self.second, "acknowledged", 1)],
            [(item.message["id"], item.state, item.presentation_count) for item in states],
        )
        self.assertTrue(all(item.state in {"fresh", "held", "acknowledged", "retracted"} for item in states))

    def test_projection_refuses_order_and_recipient_session_evidence_mismatches(self) -> None:
        """Catches replay accepting a receipt that names the right IDs in a false causal order."""
        from floati.wake_hold import project_wake_items

        events = [message(self.first), message(self.second)]
        cases = (
            [{"kind": "wake_hold_receipt", "recipient": "bob", "worker_session_id": None, "item_ids": [self.second, self.first]}],
            [{"kind": "wake_hold_receipt", "recipient": "charlie", "worker_session_id": None, "item_ids": [self.first]}],
            [{"kind": "wake_hold_receipt", "recipient": "bob", "worker_session_id": "other", "item_ids": [self.first]}],
        )
        for deliveries in cases:
            with self.subTest(deliveries=deliveries):
                with self.assertRaises(IntegrityFailure) as caught:
                    project_wake_items(
                        events=events, deliveries=deliveries, acknowledgments=[],
                        recipient="bob", worker_session_id=None, tenant_id="alpha",
                        event_prefix_digests=("0" * 64, "1" * 64, "2" * 64), delivery_prefix_digests=("3" * 64, "4" * 64),
                        acknowledgment_prefix_digests=("5" * 64,),
                    )
                self.assertEqual("consumption_state_unavailable", caught.exception.code)

    def test_prefix_digest_reader_is_stable_and_detects_one_framed_byte_mutation(self) -> None:
        """Catches digesting parsed objects instead of exact canonical durable frame bytes."""
        from floati.jsonl import read_records_with_prefix_digests
        from floati.jsonl import append_record

        path = "events.jsonl"
        first = message(self.first)
        append_record(self.root, path, first, allowed_kinds={"message_envelope"})
        left, left_digests = read_records_with_prefix_digests(
            self.root, path, allowed_kinds={"message_envelope"}, domain=EVENT_DOMAIN,
        )
        right, right_digests = read_records_with_prefix_digests(
            self.root, path, allowed_kinds={"message_envelope"}, domain=EVENT_DOMAIN,
        )
        self.assertEqual((left, left_digests), (right, right_digests))
        event_path = self.root.resolve_relative(path)
        original = event_path.read_bytes()
        mutated = original.replace(b"wake evidence", b"wake evidencf")
        self.assertNotEqual(original, mutated)
        event_path.write_bytes(mutated)
        _changed, changed_digests = read_records_with_prefix_digests(
            self.root, path, allowed_kinds={"message_envelope"}, domain=EVENT_DOMAIN,
        )
        self.assertNotEqual(left_digests, changed_digests)
        event_path.write_bytes(mutated.replace(b"\n", b" \n"))
        with self.assertRaises(IntegrityFailure):
            read_records_with_prefix_digests(
                self.root, path, allowed_kinds={"message_envelope"}, domain=EVENT_DOMAIN,
            )

    def test_replay_requires_ancestor_and_immediate_prefix_testimony(self) -> None:
        """Catches replay accepting a minimal hold row without its raw-prefix proof."""
        from floati.wake_hold import project_wake_items, wake_hold_receipt

        events = [message(self.first)]
        prefixes = {
            "event_prefix_digests": ("0" * 64, "1" * 64),
            "delivery_prefix_digests": ("2" * 64, "3" * 64),
            "acknowledgment_prefix_digests": ("4" * 64,),
        }
        lawful = wake_hold_receipt(
            tenant_id="alpha", recipient="bob", worker_session_id=None,
            idempotency_key="prefix-key", limit=1, item_ids=[self.first],
            event_prefix_digest="1" * 64, delivery_prefix_digest="2" * 64,
            acknowledgment_prefix_digest="4" * 64, now=NOW,
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )
        projected = project_wake_items(
            events=events, deliveries=[lawful], acknowledgments=[], recipient="bob",
            worker_session_id=None, tenant_id="alpha", **prefixes,
        )
        self.assertEqual("held", projected[0].state)
        for field, value in (("event_prefix_digest", "f" * 64), ("delivery_prefix_digest", "3" * 64), ("acknowledgment_prefix_digest", "f" * 64)):
            with self.subTest(field=field):
                forged = dict(lawful, **{field: value})
                from floati.records import wake_hold_decision_digest
                forged["decision_digest"] = wake_hold_decision_digest(forged)
                with self.assertRaises(IntegrityFailure) as caught:
                    project_wake_items(
                        events=events, deliveries=[forged], acknowledgments=[], recipient="bob",
                        worker_session_id=None, tenant_id="alpha", **prefixes,
                    )
                self.assertEqual("consumption_state_unavailable", caught.exception.code)

    def test_replay_requires_exact_event_and_acknowledgment_prefix_counts(self) -> None:
        """Catches fabricated extra or missing raw-prefix positions at replay."""
        from floati.wake_hold import project_wake_items, wake_hold_receipt

        hold = wake_hold_receipt(
            tenant_id="alpha", recipient="bob", worker_session_id=None,
            idempotency_key="exact-prefix-counts", limit=1, item_ids=[self.first],
            event_prefix_digest="2" * 64, delivery_prefix_digest="3" * 64,
            acknowledgment_prefix_digest="5" * 64, now=NOW,
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )
        common = {
            "deliveries": [hold], "recipient": "bob", "worker_session_id": None,
            "tenant_id": "alpha", "delivery_prefix_digests": ("3" * 64, "4" * 64),
        }
        held = project_wake_items(
            events=[message(self.first)], acknowledgments=[],
            event_prefix_digests=("0" * 64, "2" * 64),
            acknowledgment_prefix_digests=("5" * 64,), **common,
        )
        self.assertEqual("held", held[0].state)
        for field, prefixes in (
            ("event_prefix_digests", ("0" * 64, "1" * 64, "2" * 64)),
            ("acknowledgment_prefix_digests", ("5" * 64, "6" * 64)),
        ):
            with self.subTest(cardinality="extra", field=field):
                arguments = {
                    "event_prefix_digests": ("0" * 64, "2" * 64),
                    "acknowledgment_prefix_digests": ("5" * 64,), field: prefixes,
                }
                with self.assertRaises(IntegrityFailure) as caught:
                    project_wake_items(
                        events=[message(self.first)], acknowledgments=[],
                        **arguments, **common,
                    )
                self.assertEqual("consumption_state_unavailable", caught.exception.code)

        acknowledgments = [
            {"kind": "ack_receipt", "recipient": "bob", "item_ids": [self.first]},
        ]
        exact = {
            "event_prefix_digests": ("0" * 64, "1" * 64, "2" * 64),
            "acknowledgment_prefix_digests": ("5" * 64, "6" * 64),
        }
        projected = project_wake_items(
            events=[message(self.first), message(self.second)],
            acknowledgments=acknowledgments, **exact, **common,
        )
        self.assertEqual(["acknowledged", "fresh"], [item.state for item in projected])
        for field, prefixes in (
            ("event_prefix_digests", ("0" * 64, "2" * 64)),
            ("acknowledgment_prefix_digests", ("5" * 64,)),
        ):
            with self.subTest(cardinality="missing", field=field):
                arguments = dict(exact, **{field: prefixes})
                with self.assertRaises(IntegrityFailure) as caught:
                    project_wake_items(
                        events=[message(self.first), message(self.second)],
                        acknowledgments=acknowledgments, **arguments, **common,
                    )
                self.assertEqual("consumption_state_unavailable", caught.exception.code)

    def test_hold_event_prefix_must_cover_every_named_physical_message(self) -> None:
        """Catches a real earlier event-prefix digest being reused for a later held message."""
        from floati.wake_hold import project_wake_items, wake_hold_receipt

        events = [message(self.first), message(self.second)]
        prefixes = {
            "event_prefix_digests": ("0" * 64, "1" * 64, "2" * 64),
            "delivery_prefix_digests": ("3" * 64, "4" * 64),
            "acknowledgment_prefix_digests": ("5" * 64,),
        }
        lawful = wake_hold_receipt(
            tenant_id="alpha", recipient="bob", worker_session_id=None,
            idempotency_key="covers-all", limit=2, item_ids=[self.first, self.second],
            event_prefix_digest="2" * 64, delivery_prefix_digest="3" * 64,
            acknowledgment_prefix_digest="5" * 64, now=NOW,
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )
        self.assertEqual(
            ["held", "held"],
            [item.state for item in project_wake_items(events=events, deliveries=[lawful], acknowledgments=[], recipient="bob", worker_session_id=None, tenant_id="alpha", **prefixes)],
        )
        forged = dict(lawful, event_prefix_digest="1" * 64)
        from floati.records import wake_hold_decision_digest
        forged["decision_digest"] = wake_hold_decision_digest(forged)
        with self.assertRaises(IntegrityFailure) as caught:
            project_wake_items(events=events, deliveries=[forged], acknowledgments=[], recipient="bob", worker_session_id=None, tenant_id="alpha", **prefixes)
        self.assertEqual("consumption_state_unavailable", caught.exception.code)

    def test_closed_decision_artifact_runtime_and_schema_parity(self) -> None:
        """Catches an open artifact or a state/wake/receipt contradiction."""
        from floati.wake_hold import validate_wake_decision_artifact, wake_hold_receipt

        receipt = wake_hold_receipt(
            tenant_id="alpha", recipient="bob", worker_session_id=None,
            idempotency_key="artifact-key", limit=1, item_ids=[self.first],
            event_prefix_digest="a" * 64, delivery_prefix_digest="b" * 64,
            acknowledgment_prefix_digest="c" * 64, now=NOW,
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )
        artifact = {
            "schema_version": 1, "artifact_version": 1, "kind": "wake_decision",
            "state": "fresh_work", "wake_required": True, "recipient": "bob",
            "worker_session_id": None, "limit": 1, "fresh_total": 1, "held_total": 0,
            "fresh_truncated": False, "held_truncated": False, "fresh_messages": [message(self.first)],
            "held_items": [], "receipt": receipt, "event_prefix_digest": "a" * 64,
            "delivery_prefix_digest": "b" * 64, "acknowledgment_prefix_digest": "c" * 64,
        }
        path = Path(__file__).parents[1] / "schemas/v1/wake-decision-artifact.schema.json"
        self.assertEqual(artifact, validate_wake_decision_artifact(artifact, tenant_id="alpha"))
        validate_json_schema(artifact, path)
        held_only = dict(artifact, state="held_only", wake_required=False, fresh_messages=[], fresh_total=0, held_items=[{"item_id": self.first, "presentation_count": 1}], held_total=1, receipt=None)
        self.assertEqual(held_only, validate_wake_decision_artifact(held_only, tenant_id="alpha"))
        validate_json_schema(held_only, path)
        for changed in (
            dict(artifact, wake_required=False), dict(artifact, receipt=None), dict(artifact, fresh_messages=[]), dict(artifact, unexpected=True),
            dict(artifact, worker_session_id="bad\u202e-session"), dict(artifact, event_prefix_digest="g" * 64),
            dict(artifact, fresh_messages=[{}]), dict(held_only, held_items=[{"item_id": "not-a-message", "presentation_count": 1}]),
            dict(held_only, fresh_messages=[message(self.second)]), dict(held_only, state="caught_up"),
        ):
            with self.subTest(changed=changed):
                runtime = schema = True
                try:
                    validate_wake_decision_artifact(changed, tenant_id="alpha")
                except ProtocolRefusal:
                    runtime = False
                try:
                    validate_json_schema(changed, path)
                except SchemaValidationError:
                    schema = False
                self.assertEqual((False, False), (runtime, schema))

    def test_complete_event_prefix_may_include_later_unselected_messages(self) -> None:
        """Catches equality at the named message frame rejecting a lawful complete scan prefix."""
        from floati.wake_hold import project_wake_items, wake_hold_receipt

        events = [message(self.first), message(self.second)]
        prefixes = {
            "event_prefix_digests": ("0" * 64, "1" * 64, "2" * 64),
            "delivery_prefix_digests": ("3" * 64, "4" * 64),
            "acknowledgment_prefix_digests": ("5" * 64,),
        }
        receipt = wake_hold_receipt(
            tenant_id="alpha", recipient="bob", worker_session_id=None,
            idempotency_key="bounded-complete", limit=1, item_ids=[self.first],
            event_prefix_digest="2" * 64, delivery_prefix_digest="3" * 64,
            acknowledgment_prefix_digest="5" * 64, now=NOW,
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )
        states = project_wake_items(events=events, deliveries=[receipt], acknowledgments=[], recipient="bob", worker_session_id=None, tenant_id="alpha", **prefixes)
        self.assertEqual(["held", "fresh"], [item.state for item in states])

    def test_replay_and_artifact_bind_the_explicit_tenant(self) -> None:
        """Catches alpha hard-coding or acceptance of record testimony from another tenant."""
        from floati.wake_hold import project_wake_items, validate_wake_decision_artifact, wake_hold_receipt

        event = message(self.first, tenant_id="bravo")
        receipt = wake_hold_receipt(
            tenant_id="bravo", recipient="bob", worker_session_id=None,
            idempotency_key="bravo-key", limit=1, item_ids=[self.first],
            event_prefix_digest="1" * 64, delivery_prefix_digest="2" * 64,
            acknowledgment_prefix_digest="3" * 64, now=NOW,
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )
        prefixes = {"event_prefix_digests": ("0" * 64, "1" * 64), "delivery_prefix_digests": ("2" * 64, "4" * 64), "acknowledgment_prefix_digests": ("3" * 64,)}
        self.assertEqual("held", project_wake_items(events=[event], deliveries=[receipt], acknowledgments=[], recipient="bob", worker_session_id=None, tenant_id="bravo", **prefixes)[0].state)
        with self.assertRaises(IntegrityFailure):
            project_wake_items(events=[event], deliveries=[receipt], acknowledgments=[], recipient="bob", worker_session_id=None, tenant_id="alpha", **prefixes)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        FloatiRoot.open(Path(temp.name), "bravo")
        artifact = {"schema_version": 1, "artifact_version": 1, "kind": "wake_decision", "state": "fresh_work", "wake_required": True, "recipient": "bob", "worker_session_id": None, "limit": 1, "fresh_total": 1, "held_total": 0, "fresh_truncated": False, "held_truncated": False, "fresh_messages": [event], "held_items": [], "receipt": receipt, "event_prefix_digest": "1" * 64, "delivery_prefix_digest": "2" * 64, "acknowledgment_prefix_digest": "3" * 64}
        self.assertEqual(artifact, validate_wake_decision_artifact(artifact, tenant_id="bravo"))
        validate_json_schema(artifact, Path(__file__).parents[1] / "schemas/v1/wake-decision-artifact.schema.json")
        with self.assertRaises(ProtocolRefusal):
            validate_wake_decision_artifact(artifact, tenant_id="alpha")

    def test_replay_refuses_cross_tenant_event_and_malformed_retraction(self) -> None:
        """Catches replay trusting event/retraction mappings outside the selected tenant contract."""
        from floati.wake_hold import project_wake_items, wake_hold_receipt

        bravo_receipt = wake_hold_receipt(
            tenant_id="bravo", recipient="bob", worker_session_id=None,
            idempotency_key="cross-event", limit=1, item_ids=[self.first],
            event_prefix_digest="1" * 64, delivery_prefix_digest="2" * 64,
            acknowledgment_prefix_digest="3" * 64, now=NOW,
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )
        prefixes = {"event_prefix_digests": ("0" * 64, "1" * 64), "delivery_prefix_digests": ("2" * 64, "4" * 64), "acknowledgment_prefix_digests": ("3" * 64,)}
        with self.assertRaises(IntegrityFailure) as cross_tenant:
            project_wake_items(events=[message(self.first, tenant_id="alpha")], deliveries=[bravo_receipt], acknowledgments=[], recipient="bob", worker_session_id=None, tenant_id="bravo", **prefixes)
        self.assertEqual("consumption_state_unavailable", cross_tenant.exception.code)
        malformed_retraction = {"kind": "message_retracted", "retracted_message_id": self.first}
        with self.assertRaises(IntegrityFailure) as malformed:
            project_wake_items(events=[message(self.first, tenant_id="bravo"), malformed_retraction], deliveries=[], acknowledgments=[], recipient="bob", worker_session_id=None, tenant_id="bravo", event_prefix_digests=("0" * 64, "1" * 64, "2" * 64), delivery_prefix_digests=("3" * 64,), acknowledgment_prefix_digests=("4" * 64,))
        self.assertEqual("consumption_state_unavailable", malformed.exception.code)

    def test_replay_enforces_shared_retraction_session_and_party_semantics(self) -> None:
        """Catches schema-valid retractions that do not bind the original session or parties."""
        from floati.wake_hold import project_wake_items

        session = "worker-018f7e9b3c137abc8def0123456789ab"
        original = message(self.first, tenant_id="bravo", session=session)
        lawful = {
            "schema_version": 0,
            "id": "ret-018f7e9b3c147abc8def0123456789ab",
            "tenant_id": "bravo",
            "timestamp": NOW,
            "kind": "message_retracted",
            "retracted_message_id": self.first,
            "worker_session_id": session,
            "reason": "sent_in_error",
            "author": public_ids.worker('alpha'),
        }
        arguments = {
            "deliveries": [], "acknowledgments": [], "recipient": "bob",
            "worker_session_id": session, "tenant_id": "bravo",
            "event_prefix_digests": ("0" * 64, "1" * 64, "2" * 64),
            "delivery_prefix_digests": ("3" * 64,),
            "acknowledgment_prefix_digests": ("4" * 64,),
        }
        projected = project_wake_items(events=[original, lawful], **arguments)
        self.assertEqual("retracted", projected[0].state)
        for changed in (
            dict(lawful, worker_session_id="worker-018f7e9b3c157abc8def0123456789ab"),
            dict(lawful, author="mallory"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(IntegrityFailure) as caught:
                    project_wake_items(events=[original, changed], **arguments)
                self.assertEqual("consumption_state_unavailable", caught.exception.code)

    def test_artifact_totals_and_truncation_follow_returned_slices(self) -> None:
        """Catches totals or truncation flags that disagree with the returned bounded slices."""
        from floati.wake_hold import validate_wake_decision_artifact, wake_hold_receipt

        receipt = wake_hold_receipt(tenant_id="alpha", recipient="bob", worker_session_id=None, idempotency_key="total-key", limit=1, item_ids=[self.first], event_prefix_digest="a" * 64, delivery_prefix_digest="b" * 64, acknowledgment_prefix_digest="c" * 64, now=NOW, record_id="wake-hold-018f7e9b3c147abc8def0123456789ab")
        artifact = {"schema_version": 1, "artifact_version": 1, "kind": "wake_decision", "state": "fresh_work", "wake_required": True, "recipient": "bob", "worker_session_id": None, "limit": 1, "fresh_total": 2, "held_total": 0, "fresh_truncated": True, "held_truncated": False, "fresh_messages": [message(self.first)], "held_items": [], "receipt": receipt, "event_prefix_digest": "a" * 64, "delivery_prefix_digest": "b" * 64, "acknowledgment_prefix_digest": "c" * 64}
        self.assertEqual(artifact, validate_wake_decision_artifact(artifact, tenant_id="alpha"))
        for changed in (dict(artifact, fresh_total=0), dict(artifact, fresh_truncated=False), dict(artifact, fresh_total=1, fresh_truncated=True)):
            with self.subTest(changed=changed):
                with self.assertRaises(ProtocolRefusal):
                    validate_wake_decision_artifact(changed, tenant_id="alpha")


class WakeHoldControllerTests(unittest.TestCase):
    """Controller transactions; removing the shared guard or receipt replay breaks these."""

    def setUp(self) -> None:
        from floati.events import EventLog
        from floati.registry import Registry

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        registry = Registry(self.root)
        for node in (public_ids.worker('alpha'), "bob", "charlie"):
            registry.register(node, "worker")
        self.events = EventLog(self.root, registry)

    def send(self, recipient: str = "bob", *, session: object = None, key: str = "message") -> dict:
        return self.events.send(
            public_ids.worker('alpha'), recipient, "floati", "a" * 40,
            "docs/evidence/wake-controller.md", "controller evidence",
            idempotency_key=key, worker_session_id=session,
        )

    def evaluate(self, key: str, **kwargs: object) -> dict:
        from floati.wake_hold import WakeHoldController

        return WakeHoldController(self.root).evaluate("bob", idempotency_key=key, **kwargs)

    def test_bound_and_unbound_polls_share_only_a_current_event_cut(self):
        from floati import cut_prefix, inbox_prefix, send_prefix
        from floati.wake_hold import WakeHoldController
        from tests.bus_scale_io import IOProbe

        session = "worker-018f7e9b3c137abc8def0123456789ab"
        unbound = self.send(key="unbound")
        for bank in (send_prefix, inbox_prefix, cut_prefix):
            self.assertIsNotNone(bank.warm(self.root))
        controller = WakeHoldController(self.root)
        bound = controller.evaluate("bob", worker_session_id=session, idempotency_key="bound")
        self.assertEqual("caught_up", bound["state"])
        with IOProbe(self.root.tenant_home) as probe:
            lane = controller.evaluate("bob", idempotency_key="lane")
        probe.assert_complete()
        self.assertEqual([unbound], lane["fresh_messages"])
        self.assertEqual(bound["event_prefix_digest"], lane["event_prefix_digest"])
        directory_bytes = sum(probe.read_bytes_by_path.get(name + "/directory.bin", 0)
                              for name in (cut_prefix.cache_name(), inbox_prefix.CACHE_NAME))
        with self.subTest(source="unchanged"):
            self.assertEqual(0, directory_bytes, "the second mail plane reread complete certified directories")

        added = self.send(session=session, key="bound-after-append")
        after_append = controller.evaluate("bob", worker_session_id=session, idempotency_key="changed")
        self.assertEqual([added], after_append["fresh_messages"])
        self.assertNotEqual(bound["event_prefix_digest"], after_append["event_prefix_digest"])

        revision = send_prefix._revision()
        current = [revision]
        original = inbox_prefix.InboxPrefix.recipient_count
        def change_reader(bank, node):
            current[0] = "different-source-revision"
            return original(bank, node)
        with self.subTest(source="changed_during_acquisition"),\
                mock.patch.object(send_prefix, "_revision", side_effect=lambda: current[0]),\
                mock.patch.object(inbox_prefix.InboxPrefix, "recipient_count", change_reader):
            self.assertIsNone(cut_prefix.certified_source(self.root, "bob", session),
                              "the reader changed while the three certificates were being joined")

        previous = cut_prefix.certified_source(self.root, "bob", session)
        self.assertIsNotNone(previous)
        checkpoint = self.root.tenant_home / inbox_prefix.CACHE_NAME / "checkpoint.json"
        original_checkpoint = checkpoint.read_bytes()
        checkpoint.write_bytes(b"{}")
        with self.subTest(source="damaged_checkpoint"):
            self.assertIsNone(cut_prefix.certified_source(self.root, "bob", previous=previous),
                              "a second plane accepted a damaged checkpoint from the first plane")
        checkpoint.write_bytes(original_checkpoint)

        with self.subTest(source="reader_rebuilt_before_session_takeover"):
            recipient = "charlie"
            new_session = "worker-018f7e9b3c147abc8def0123456789ab"
            first = self.send(recipient, session=session, key="before-takeover")
            decision = controller.evaluate(recipient, worker_session_id=session, idempotency_key="first-poll")
            self.assertEqual([first], decision["fresh_messages"])

            def idle_poll():
                for plane in (new_session, None):
                    idle = controller.evaluate(recipient, worker_session_id=plane, idempotency_key="idle-poll")
                    self.assertEqual("caught_up", idle["state"])

            next_revision = hashlib.sha256((revision + "-updated").encode()).hexdigest()
            with mock.patch.object(send_prefix, "_revision", return_value=next_revision):
                # An ordinary writer rebuilds the native banks after the source
                # update. A retained controller then polls the new binding.
                self.send("bob", key="after-reader-update")
                self.assertIsNotNone(cut_prefix.certified_source(self.root, recipient, new_session))
                idle_poll()
                with IOProbe(self.root.tenant_home) as recovered:
                    idle_poll()
                recovered.assert_complete()
                self.assertEqual(0, recovered.read_bytes_by_path.get(cut_prefix.cache_name() + "/directory.bin", 0),
                                 "the new binding kept replaying after the writer rebuilt the certified event cut")

    def test_fresh_controller_keeps_exact_raw_digest_without_replaying_unrelated_history(self) -> None:
        """Cold decisions retain exact testimony while reading only their certified answer."""
        from floati import send_prefix
        from floati.wake_hold import WakeHoldController
        from tests.bus_scale_io import IOProbe

        for number in range(3):
            self.send("charlie", key="cold-unrelated-" + str(number))
        for own_mail in (False, True):
            with self.subTest(own_mail=own_mail):
                selected = self.send(key="cold-selected") if own_mail else None
                self.assertIsNotNone(send_prefix.warm(self.root))
                from floati import cut_prefix, inbox_prefix
                self.assertIsNotNone(inbox_prefix.warm(self.root))
                self.assertIsNotNone(cut_prefix.warm(self.root))
                before = self.events.path.read_bytes()
                expected = hashlib.sha256(EVENT_DOMAIN.encode("ascii") + b"\0" + before).hexdigest()
                first_frame = len(before.splitlines(keepends=True)[0])
                with IOProbe(self.root.tenant_home) as probe:
                    result = WakeHoldController(self.root).evaluate(
                        "bob", idempotency_key="cold-controller-" + str(own_mail))
                probe.assert_complete()
                self.assertEqual(expected, result["event_prefix_digest"])
                self.assertEqual("fresh_work" if own_mail else "caught_up", result["state"])
                self.assertEqual([] if selected is None else [selected], result["fresh_messages"])
                self.assertEqual(before, self.events.path.read_bytes())
                self.assertLessEqual(probe.read_bytes_by_path.get("events.jsonl", 0), first_frame,
                    "fresh controller replayed retained mail to reproduce raw prefix testimony")

    def test_unregistered_wake_spelling_refuses_before_minting_coordination_identity(self) -> None:
        """Catches wake evaluation creating a second identity system outside the registry."""
        from floati.wake_hold import WakeHoldController

        alias_path = self.root.resolve_relative("receipts/wake-coordination/bob_alias")
        with self.assertRaises(ProtocolRefusal) as caught:
            WakeHoldController(self.root).evaluate(
                "bob_alias", idempotency_key="alias-must-not-mint",
            )
        self.assertEqual("unknown_node", caught.exception.code)
        self.assertFalse(alias_path.exists())

    def test_certified_and_native_decisions_and_refusals_match_on_small_histories(self):
        import random
        import shutil
        from floati import cut_prefix, inbox_prefix, send_prefix
        from floati.cursor import SparseCursor
        from floati.framing import encode_frame
        from floati.wake_hold import WakeHoldController
        rng = random.Random(362)
        session = "worker-018f7e9b3c137abc8def0123456789ab"
        for fault in ("current", "missing", "stale", "corrupt", "unknown", "foreign_delivery"):
            with self.subTest(fault=fault):
                for number in range(rng.randrange(1, 4)):
                    self.send("charlie", key=fault + "-unrelated-" + str(number))
                own = self.send(session=session, key=fault + "-own")
                # Native receipt creation precedes the comparison; both sides
                # reconstruct that exact public decision on a retry.
                with mock.patch.object(cut_prefix, "certified_source", return_value=None):
                    original = WakeHoldController(self.root).evaluate("bob",
                        worker_session_id=session, idempotency_key="diff-" + fault)
                if rng.randrange(2):
                    SparseCursor(self.root).ack("bob", [own["id"]],
                        worker_session_id=session, acting_session_id="differential")
                if rng.randrange(2):
                    self.events.retract(own["id"], worker_session_id=session,
                        reason="sent_in_error", author=public_ids.worker('alpha'))
                send_prefix.warm(self.root)
                inbox_prefix.warm(self.root)
                cut_prefix.warm(self.root)
                self.assertIsNotNone(cut_prefix.certified_source(self.root, "bob", session))
                bank = self.root.tenant_home / cut_prefix.cache_name()
                if fault == "missing":
                    shutil.rmtree(bank)
                elif fault == "stale":
                    raw = self.events.path.read_bytes()
                    self.events.path.write_bytes(raw)
                elif fault == "corrupt":
                    target = next((bank / "nodes").iterdir())
                    target.write_bytes(target.read_bytes() + b" ")
                elif fault == "unknown":
                    future = {"schema_version": 2, "id": "future-" + uuid7_hex(),
                        "tenant_id": self.root.tenant_id, "timestamp": NOW,
                        "kind": "future_event_kind", "payload": {"newer": True}}
                    with self.events.path.open("ab") as stream:
                        stream.write(encode_frame(future))
                elif fault == "foreign_delivery":
                    held = dict(original["receipt"], id="wake-hold-" + uuid7_hex(),
                        item_ids=["msg-" + uuid7_hex()], idempotency_key="foreign-testimony")
                    from floati.records import wake_hold_decision_digest
                    held["decision_digest"] = wake_hold_decision_digest(held)
                    _persist_wake_hold_fixture(self.root, "bob", held, session=session)
                def result():
                    try:
                        return ("ok", WakeHoldController(self.root).evaluate("bob",
                            worker_session_id=session, idempotency_key="diff-" + fault))
                    except (IntegrityFailure, ProtocolRefusal) as error:
                        return (error.code, error.detail)
                # Evaluate certified first so native fallback cannot repair the
                # fault before the optimized reader observes it.
                certified = result()
                with mock.patch.object(cut_prefix, "certified_source", return_value=None):
                    native = result()
                self.assertEqual(native, certified)
                if fault == "unknown":
                    # Keep the next variant a known-kind source; no rewrite is
                    # performed by either reader under comparison.
                    self.events.path.write_bytes(self.events.path.read_bytes().rsplit(b"\n", 2)[0] + b"\n")

        # Nearest native guard is test_prefix_digest_reader_is_stable_and_detects_one_framed_byte_mutation.
        # A decodable but noncanonical frame must not become admissible merely
        # because all three derived banks were rebuilt from those same bytes.
        with self.subTest(fault="noncanonical_frame"):
            session = "worker-018f7e9b3c147abc8def0123456789ab"
            fault = "noncanonical_frame"
            self.send(session=session, key="noncanonical-own")
            self.events.path.write_bytes(self.events.path.read_bytes().replace(b"\n", b" \n", 1))
            send_prefix.warm(self.root)
            inbox_prefix.warm(self.root)
            cut_prefix.warm(self.root)
            certified = result()
            with mock.patch.object(cut_prefix, "certified_source", return_value=None):
                native = result()
            self.assertEqual(native, certified)

    def test_first_evaluation_presents_fresh_work_once_with_a_wake_receipt(self) -> None:
        """Catches a controller that returns fresh work without durable hold testimony."""
        first = self.send(key="first")
        artifact = self.evaluate("wake-first")
        self.assertEqual("fresh_work", artifact["state"])
        self.assertTrue(artifact["wake_required"])
        self.assertEqual([first], artifact["fresh_messages"])
        self.assertEqual([], artifact["held_items"])
        self.assertEqual([first["id"]], artifact["receipt"]["item_ids"])

    def test_exact_retry_reuses_physical_receipt_then_ack_or_retraction_suppresses_wake(self) -> None:
        """Catches stale replay after a selected item is no longer current."""
        from floati.cursor import SparseCursor

        session = "worker-018f7e9b3c137abc8def0123456789ab"
        first, second = self.send(session=session, key="retry-a"), self.send(session=session, key="retry-b")
        original = self.evaluate("response-loss", worker_session_id=session)
        replay = self.evaluate("response-loss", worker_session_id=session)
        self.assertEqual(original, replay)
        self.assertEqual(original["receipt"], replay["receipt"])
        SparseCursor(self.root).ack(
            "bob", [first["id"]], acting_session_id="wake-hold-session",
            worker_session_id=session,
        )
        after_ack = self.evaluate("response-loss", worker_session_id=session)
        self.assertFalse(after_ack["wake_required"])
        self.assertEqual([], after_ack["fresh_messages"])
        self.assertIsNone(after_ack["receipt"])
        self.events.retract(second["id"], worker_session_id=session, reason="sent_in_error", author=public_ids.worker('alpha'))
        after_retraction = self.evaluate("response-loss", worker_session_id=session)
        self.assertEqual("caught_up", after_retraction["state"])
        self.assertFalse(after_retraction["wake_required"])

    def test_acknowledged_then_retracted_first_receipt_is_caught_up_for_exact_and_new_keys(self) -> None:
        """Catches retraction leaving an acknowledged presentation as held or eligible to wake again."""
        from floati.cursor import SparseCursor

        session = "worker-018f7e9b3c137abc8def0123456789ab"
        message = self.send(key="ack-then-retract", session=session)
        self.assertEqual("fresh_work", self.evaluate("first-key", worker_session_id=session)["state"])
        SparseCursor(self.root).ack(
            "bob", [message["id"]], acting_session_id="wake-hold-session",
            worker_session_id=session,
        )
        self.events.retract(message["id"], worker_session_id=session, reason="sent_in_error", author=public_ids.worker('alpha'))
        for key in ("first-key", "new-key"):
            with self.subTest(key=key):
                artifact = self.evaluate(key, worker_session_id=session)
                self.assertEqual("caught_up", artifact["state"])
                self.assertFalse(artifact["wake_required"])
                self.assertIsNone(artifact["receipt"])
        from floati.jsonl import read_records
        history = read_records(
            self.root, SparseCursor(self.root)._relative_path_for("bob", worker_session_id=session),
            allowed_kinds={"ack_receipt"},
        )
        self.assertEqual([message["id"]], history[0]["item_ids"])

    def test_empty_legacy_delivery_receipt_is_a_noop_before_later_fresh_work(self) -> None:
        """Catches a valid empty legacy receipt corrupting a later independent wake decision."""
        from floati.jsonl import append_record
        from floati.registry import utc_now

        append_record(
            self.root, "receipts/deliveries/bob.jsonl",
            {"schema_version": 0, "id": "delivery-018f7e9b3c117abc8def0123456789ab",
             "tenant_id": "alpha", "timestamp": utc_now(), "kind": "delivery_receipt",
             "recipient": "bob", "item_ids": [], "presentation_count": 1},
            allowed_kinds={"delivery_receipt", "wake_hold_receipt"},
        )
        message = self.send(key="after-empty-delivery")
        artifact = self.evaluate("after-empty-key")
        self.assertEqual("fresh_work", artifact["state"])
        self.assertEqual([message], artifact["fresh_messages"])

    def test_wake_hold_record_bound_refuses_before_a_second_append_and_keeps_exact_retry(self) -> None:
        """Catches the sealed hold writer appending beyond MAX_LEDGER_RECORDS after its raw-prefix read.

        Amended 2026-08-25 (one-seat-four-wake-identities): the wake path now
        lawfully reads the registry ledger too, so the injected bound is sized
        above the fixed registry/event traffic and is tripped by the delivery
        append exactly as before.
        """
        from floati.jsonl import read_records

        from floati.jsonl import append_record
        from floati.registry import utc_now

        for suffix in ("11", "12", "13"):
            append_record(
                self.root, "receipts/deliveries/bob.jsonl",
                {"schema_version": 0, "id": f"delivery-018f7e9b3c{suffix}7abc8def0123456789ab",
                 "tenant_id": "alpha", "timestamp": utc_now(), "kind": "delivery_receipt",
                 "recipient": "bob", "item_ids": [], "presentation_count": 1},
                allowed_kinds={"delivery_receipt", "wake_hold_receipt"},
            )
        first = self.send(key="record-bound-first")
        with mock.patch("floati.jsonl.MAX_LEDGER_RECORDS", 3):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.evaluate("record-bound-key")
            self.assertEqual("ledger_record_limit", caught.exception.code)
        rows = read_records(self.root, "receipts/deliveries/bob.jsonl", allowed_kinds=set(WAKE_HOLD_KINDS))
        self.assertTrue(all(row["item_ids"] == [] for row in rows))
        self.assertEqual(3, len(rows))
        original = self.evaluate("record-bound-key")
        self.assertEqual([first], original["fresh_messages"])
        self.assertEqual(original, self.evaluate("record-bound-key"))

    def test_new_key_reports_existing_held_and_only_later_messages_are_fresh(self) -> None:
        """Catches held work starving a later fresh message or becoming full fresh output."""
        old = self.send(key="old")
        self.evaluate("old-key")
        new = self.send(key="new")
        artifact = self.evaluate("new-key")
        self.assertEqual("fresh_work", artifact["state"])
        self.assertEqual([new], artifact["fresh_messages"])
        self.assertEqual([{"item_id": old["id"], "presentation_count": 1}], artifact["held_items"])
        held = self.evaluate("held-key")
        self.assertEqual("held_only", held["state"])
        self.assertFalse(held["wake_required"])
        self.assertEqual([], held["fresh_messages"])

    def test_limit_is_independent_for_fresh_and_held_slices(self) -> None:
        """Catches one shared prefix limit hiding fresh work behind held work."""
        old_a, old_b = self.send(key="old-a"), self.send(key="old-b")
        self.evaluate("old-batch")
        new_a, new_b = self.send(key="new-a"), self.send(key="new-b")
        artifact = self.evaluate("mixed", limit=1)
        self.assertEqual([new_a], artifact["fresh_messages"])
        self.assertEqual([{"item_id": old_a["id"], "presentation_count": 1}], artifact["held_items"])
        self.assertEqual(2, artifact["fresh_total"])
        self.assertEqual(2, artifact["held_total"])
        self.assertTrue(artifact["fresh_truncated"])
        self.assertTrue(artifact["held_truncated"])
        self.assertNotIn(new_b["id"], [row["item_id"] for row in artifact["held_items"]])
        self.assertNotIn(old_b["id"], [row["id"] for row in artifact["fresh_messages"]])

    def test_same_key_conflicts_are_local_to_recipient_session_namespace(self) -> None:
        """Catches changed retry input being accepted or a key leaking across session ledgers."""
        from floati.wake_hold import WakeHoldController

        self.send(key="same-message")
        self.evaluate("same-key", limit=1)
        with self.assertRaises(ProtocolRefusal) as changed_limit:
            self.evaluate("same-key", limit=2)
        self.assertEqual("wake_idempotency_conflict", changed_limit.exception.code)
        session = "worker-018f7e9b3c137abc8def0123456789ab"
        session_message = self.send(session=session, key="session-message")
        session_artifact = WakeHoldController(self.root).evaluate(
            "bob", idempotency_key="same-key", worker_session_id=session,
        )
        self.assertEqual([session_message], session_artifact["fresh_messages"])

    def test_truncated_exact_retry_reconstructs_identical_original_artifact(self) -> None:
        """Catches retry totals, truncation, or held summaries being recomputed from later state."""
        from floati.jsonl import read_records

        self.send(key="truncate-a")
        self.send(key="truncate-b")
        original = self.evaluate("truncate-key", limit=1)
        self.assertEqual(2, original["fresh_total"])
        self.assertTrue(original["fresh_truncated"])
        self.send(key="unrelated-later")
        replay = self.evaluate("truncate-key", limit=1)
        self.assertEqual(original, replay)
        rows = read_records(self.root, "receipts/deliveries/bob.jsonl", allowed_kinds=set(WAKE_HOLD_KINDS))
        self.assertEqual(1, len(rows))
        with self.assertRaises(ProtocolRefusal) as changed:
            self.evaluate("truncate-key", limit=2)
        self.assertEqual("wake_idempotency_conflict", changed.exception.code)

    def test_controller_normalizes_corrupt_hold_and_raw_prefix_evidence(self) -> None:
        """Catches raw/schema/semantic ledger failures escaping the consumption boundary."""
        from floati.framing import encode_frame

        self.send(key="corrupt-message")
        artifact = self.evaluate("corrupt-key")
        path = self.root.resolve_relative("receipts/deliveries/bob.jsonl")
        canonical = path.read_bytes()
        forged = dict(artifact["receipt"], decision_digest="f" * 64)
        cases = (
            encode_frame(forged),
            canonical[:-1],
            canonical.replace(b"corrupt-key", b"corrupt-keY"),
        )
        for hostile in cases:
            with self.subTest(hostile=hostile[-16:]):
                path.write_bytes(hostile)
                with self.assertRaises(IntegrityFailure) as caught:
                    self.evaluate("later-key")
                self.assertEqual("consumption_state_unavailable", caught.exception.code)
        path.write_bytes(canonical)

    def test_duplicate_same_key_receipt_and_coordination_lock_deletion_fail_closed_or_preserve_projection(self) -> None:
        """Catches first-row-wins duplicate corruption and lock bytes becoming hidden truth."""
        from floati.jsonl import read_records_with_prefix_digests
        from floati.records import wake_hold_decision_digest
        from floati.wake_hold import WakeHoldLedger, wake_coordination_guard

        self.send(key="dup-message")
        artifact = self.evaluate("dup-key")
        receipt = dict(artifact["receipt"])
        receipt["id"] = "wake-hold-018f7e9b3c147abc8def0123456789ab"
        receipt["decision_digest"] = wake_hold_decision_digest(receipt)
        _persist_wake_hold_fixture(self.root, "bob", receipt)
        with self.assertRaises(IntegrityFailure) as duplicate:
            self.evaluate("dup-key")
        self.assertEqual("consumption_state_unavailable", duplicate.exception.code)
        lock = self.root.resolve_relative("receipts/wake-coordination/bob/lane.lock")
        lock.unlink()
        with wake_coordination_guard(self.root, "bob"):
            self.assertEqual(b"", lock.read_bytes())


if __name__ == "__main__":
    unittest.main()
