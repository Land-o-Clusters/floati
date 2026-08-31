"""G4 read-only recovery projection contracts."""

from __future__ import annotations

import hashlib
import json
import os
import unittest

from floati.errors import IntegrityFailure
from floati.errors import ProtocolRefusal
from floati.records import validate_record
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.test_fu1_g0 import REPOSITORY_ROOT, _tree_bytes
from tests.test_fu1_g3 import FU1G3CompletionTests


class FU1G4Tests(FU1G3CompletionTests):
    """The production breaks are losing durable recovery facts or reading history partially."""

    def _projector(self):
        from floati import doctor

        projection = getattr(doctor, "project_fleet_update", None)
        self.assertTrue(
            callable(projection),
            "G4 must expose a read-only fleet-update receipt projection",
        )
        return projection

    def _preview_for_actor(self, actor: str) -> dict[str, object]:
        from floati.fleet_update import preview_fleet_update

        return preview_fleet_update(
            root=self.root, actor=actor, destination=self.destination,
            target_source=self.target_source, target_source_sha="b" * 40,
            channel=self.channel, version=self.version,
            binding_path=self.binding_path,
            transport_registry=self.registry_path,
            transport_name=self.transport,
        )

    def test_g4_root_wide_active_saga_blocks_a_second_actor_after_release(self) -> None:
        """Catches actor-local start checks stranding an earlier root saga."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        first_plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        first = ledger.start(first_plan, self.actor, "g4-root-wide-first")
        first_path = self._receipt_path()
        first_bytes = first_path.read_bytes()
        other = "worker-other"
        other_path = self.root.path / "receipts" / "fleet-update" / f"{other}.jsonl"

        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.start(
                self._preview_for_actor(other), other, "g4-root-wide-second"
            )

        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)
        self.assertFalse(other_path.exists())
        self.assertEqual(first_bytes, first_path.read_bytes())
        self.assertEqual(
            first,
            ledger.exact_retry(first_plan, self.actor, "g4-root-wide-first"),
        )
        self.assertEqual(first_bytes, first_path.read_bytes())

    def test_g4_root_wide_completed_saga_permits_cross_actor_succession(self) -> None:
        """Catches a completed actor history becoming a permanent root lock."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._complete_equal_encoder_g3_context("g4-root-wide-completed")
        other = "worker-successor"
        started = FleetUpdateReceiptLedger(self.root).start(
            self._preview_for_actor(other), other, "g4-root-wide-successor"
        )

        self.assertEqual(other, started["actor"])
        self.assertEqual(
            [started["id"]],
            [row["id"] for row in FleetUpdateReceiptLedger(self.root).rows(other)],
        )

    def test_g4_root_wide_rejects_multiple_valid_active_saga_coordinates(self) -> None:
        """Catches contradictory valid active histories becoming continuation-eligible."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger, authenticate_plan, recovery_witness
        from floati.jsonl import append_record

        self._consent()
        first_plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        first = ledger.start(first_plan, self.actor, "g4-root-wide-first-active")
        other = "worker-other"
        other_plan = self._preview_for_actor(other)
        authenticated = authenticate_plan(other_plan, other, self.root)
        plan, digest, actor, readers, consequences, exclusions, batch = ledger._plan(
            authenticated, other, "g4-root-wide-second-active"
        )
        second = ledger._base(
            kind="fleet_update_started", plan_digest=digest, actor=actor,
            key="g4-root-wide-second-active",
            consent={"id": first["consent_receipt_id"]}, predecessor=None,
            readers=readers, consequences=consequences, exclusions=exclusions,
            batch=batch, operation="start", step_kind=None, pre_digest=None,
            post_digest=None, state="started", recovery_witness=recovery_witness(plan),
        )
        second["tenant_id"] = self.root.tenant_id
        append_record(
            self.root, FleetUpdateReceiptLedger.relative(other), second,
            allowed_kinds={
                "fleet_update_started", "fleet_update_step", "fleet_update_completed",
            },
        )
        first_path = self._receipt_path()
        second_path = self.root.path / FleetUpdateReceiptLedger.relative(other)
        before = (first_path.read_bytes(), second_path.read_bytes())

        with self.assertRaises(IntegrityFailure) as caught:
            ledger.exact_retry(first_plan, self.actor, "g4-root-wide-first-active")

        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)
        self.assertEqual(before, (first_path.read_bytes(), second_path.read_bytes()))

    def test_g4_root_wide_start_rejects_a_malformed_sibling_history(self) -> None:
        """Catches an unreadable sibling receipt being ignored during start exclusion."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        receipts = self.root.path / "receipts" / "fleet-update"
        receipts.mkdir(parents=True, exist_ok=True)
        (receipts / "gate-corrupt.jsonl").write_text("{not json}\n", encoding="utf-8")
        actor_path = self._receipt_path()
        before = actor_path.read_bytes() if actor_path.exists() else None

        with self.assertRaises(IntegrityFailure):
            FleetUpdateReceiptLedger(self.root).start(
                self._preview(), self.actor, "g4-root-wide-malformed"
            )

        self.assertFalse(actor_path.exists())
        self.assertIsNone(before)

    def test_g4_root_wide_start_rejects_a_symlinked_sibling_history(self) -> None:
        """Catches a symlinked sibling receipt being ignored during start exclusion."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        receipts = self.root.path / "receipts" / "fleet-update"
        receipts.mkdir(parents=True, exist_ok=True)
        (receipts / "gate-symlink.jsonl").symlink_to(self.base / "missing-receipt")
        actor_path = self._receipt_path()
        before = actor_path.read_bytes() if actor_path.exists() else None

        with self.assertRaises(IntegrityFailure):
            FleetUpdateReceiptLedger(self.root).start(
                self._preview(), self.actor, "g4-root-wide-symlink"
            )

        self.assertFalse(actor_path.exists())
        self.assertIsNone(before)

    def test_g4_root_wide_exact_retry_rejects_a_later_malformed_sibling(self) -> None:
        """Catches an active selected actor hiding a later malformed receipt file."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "g4-root-wide-later-retry")
        actor_path = self._receipt_path()
        before = actor_path.read_bytes()
        receipts = self.root.path / "receipts" / "fleet-update"
        (receipts / "zz-corrupt.jsonl").write_text("{not json}\n", encoding="utf-8")

        with self.assertRaises(IntegrityFailure) as caught:
            ledger.exact_retry(plan, self.actor, "g4-root-wide-later-retry")

        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)
        self.assertEqual(started, json.loads(actor_path.read_text(encoding="utf-8")))
        self.assertEqual(before, actor_path.read_bytes())

    def test_g4_root_wide_continuation_rejects_a_later_symlink_before_writes(self) -> None:
        """Catches a G3 physical continuation proceeding past a later bad sibling."""

        from floati.fleet_update import commit_fleet_update_g3

        plan, _ledger = self._equal_encoder_g3_frontier(
            "g4-root-wide-later-continuation"
        )
        receipts = self.root.path / "receipts" / "fleet-update"
        corrupt = receipts / "zz-corrupt.jsonl"
        corrupt.symlink_to(self.base / "missing-receipt")
        corrupt_before = corrupt.lstat()
        corrupt_target = os.readlink(corrupt)
        before = _tree_bytes(self.base)

        with self.assertRaises(IntegrityFailure) as caught:
            commit_fleet_update_g3(
                plan=plan, root=self.root, actor=self.actor,
                idempotency_key="g4-root-wide-later-continuation",
            )

        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)
        self.assertEqual(before, _tree_bytes(self.base))
        corrupt_after = corrupt.lstat()
        self.assertEqual(corrupt_target, os.readlink(corrupt))
        self.assertEqual(
            (corrupt_before.st_dev, corrupt_before.st_ino, corrupt_before.st_mode),
            (corrupt_after.st_dev, corrupt_after.st_ino, corrupt_after.st_mode),
        )

    def test_g4_started_receipt_carries_plan_derived_recovery_witness(self) -> None:
        """Catches a start row that cannot name the exact remaining physical steps."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        plan = self._preview()
        self._consent()
        started = FleetUpdateReceiptLedger(self.root).start(
            plan, self.actor, "g4-recovery-witness"
        )

        witness = started.get("recovery_witness")
        self.assertIsInstance(witness, dict)
        self.assertEqual(
            {key: value for key, value in plan.items() if key not in {"plan_digest", "apply_argv"}},
            witness,
        )

    def test_g4_projects_completed_history_without_writing_or_reachability_upgrade(self) -> None:
        """Catches doctor inventing completion facts, changing bytes, or promoting trust to reachability."""

        plan, ledger, completed = self._complete_equal_encoder_g3_context(
            "g4-completed-projection"
        )
        before = _tree_bytes(self.root.path)
        projected = self._projector()(self.root)

        self.assertEqual(before, _tree_bytes(self.root.path))
        self.assertEqual("completed", projected["state"])
        self.assertEqual(str(plan["plan_digest"]), projected["plan_digest"])
        self.assertEqual(completed["id"], projected["completion_receipt_id"])
        self.assertEqual([], projected["remaining_steps"])
        self.assertEqual("not_required", projected["epoch_roll_state"])
        self.assertEqual(plan["target_transport_pins"], projected["target_transport_pins"])
        self.assertEqual(
            plan["owner_review_batch_digest"], projected["owner_review_batch_digest"]
        )
        self.assertEqual([], projected["seat_binding_consequences"])
        self.assertEqual([], projected["seat_exclusions"])
        self.assertEqual([self.actor], projected["actors"])
        self.assertEqual(ledger.rows(self.actor)[-1]["id"], completed["id"])

    def test_g4_projects_interrupted_history_from_receipts_not_hook_observation(self) -> None:
        """Catches a doctor replacing planned consequences with unreceipted hook state."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        plan = self._preview()
        self._consent()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "g4-interrupted")
        step = self._append_observed_step(
            ledger, plan, self.actor, "g4-interrupted", str(started["id"])
        )

        projected = self._projector()(self.root)

        self.assertEqual("in_progress", projected["state"])
        self.assertEqual(step["id"], projected["last_completed_step_receipt_id"])
        self.assertEqual(
            ["waiter_binding", "epoch_roll", "transport_pins"],
            [row["kind"] for row in projected["remaining_steps"]],
        )
        self.assertEqual(
            plan["seat_binding_consequences"], projected["seat_binding_consequences"]
        )

    def test_g4_refuses_all_projection_when_any_actor_history_is_malformed(self) -> None:
        """Catches a partial healthy projection that silently skips corrupt actor evidence."""

        receipts = self.root.path / "receipts" / "fleet-update"
        receipts.mkdir(parents=True, exist_ok=True)
        (receipts / "other-seat.jsonl").write_text(
            json.dumps({"kind": "fleet_update_started"}) + "\n", encoding="utf-8"
        )

        with self.assertRaises(IntegrityFailure) as caught:
            self._projector()(self.root)

        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)

    def test_g4_stale_pins_project_together_without_an_apply_write(self) -> None:
        """Catches approval output omitting either stale host pin or altering the apply shape."""

        plan = self._preview()
        before = _tree_bytes(self.base)
        from floati.fleet_update import fleet_update_pin_approval_artifact

        artifact = fleet_update_pin_approval_artifact(plan)

        self.assertEqual("fleet_update_pin_approval_required", artifact["code"])
        self.assertEqual(2, len(artifact["stale_pins"]))
        self.assertEqual(plan["apply_argv"], artifact["apply_argv"])
        self.assertEqual(before, _tree_bytes(self.base))

    def test_g4_doctor_artifact_exposes_machine_projection_and_integrity_precedence(self) -> None:
        """Catches the CLI doctor omitting G4 facts or masking corrupt receipt history as health."""

        from floati.doctor import Doctor

        artifact, _rc = Doctor(REPOSITORY_ROOT, self.root.path).artifact()
        self.assertIn("fleet_update", artifact)
        self.assertEqual("absent", artifact["fleet_update"]["state"])

        receipts = self.root.path / "receipts" / "fleet-update"
        receipts.mkdir(parents=True, exist_ok=True)
        (receipts / "other-seat.jsonl").write_text("{not json}\n", encoding="utf-8")
        degraded, rc = Doctor(REPOSITORY_ROOT, self.root.path).artifact()
        self.assertEqual(33, rc)
        self.assertEqual("malformed_evidence", degraded["state"])
        self.assertEqual("integrity_failure", degraded["fleet_update"]["state"])

    def test_g4_equal_encoder_completion_retries_without_new_receipts(self) -> None:
        """Catches an exact completed retry appending a second G4-visible terminal row."""

        plan, ledger, completed = self._complete_equal_encoder_g3_context("g4-equal-retry")
        before = self._receipt_path().read_bytes()
        from floati.fleet_update import commit_fleet_update_g3

        retried = commit_fleet_update_g3(
            plan=plan, root=self.root, actor=self.actor, idempotency_key="g4-equal-retry"
        )
        self.assertEqual(completed, retried)
        self.assertEqual(before, self._receipt_path().read_bytes())
        self.assertEqual("completed", self._projector()(self.root)["state"])
        self.assertEqual(completed["id"], ledger.rows(self.actor)[-1]["id"])

    def test_g4_rejects_missing_and_self_hashed_semantic_witnesses(self) -> None:
        """Crosses schema, record, and replay against hostile recovery starts."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "g4-hostile-witness")
        receipt = self._receipt_path()
        schema = REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json"
        for mutation in ("missing", "unknown", "semantic"):
            with self.subTest(mutation=mutation):
                hostile = json.loads(json.dumps(started))
                if mutation == "missing":
                    hostile.pop("recovery_witness")
                elif mutation == "unknown":
                    hostile["recovery_witness"]["unexpected"] = True
                else:
                    import hashlib

                    hostile["recovery_witness"]["requires_epoch_roll"] = not hostile["recovery_witness"]["requires_epoch_roll"]
                    hostile["plan_digest"] = hashlib.sha256(
                        json.dumps(
                            hostile["recovery_witness"], ensure_ascii=True,
                            allow_nan=False, sort_keys=True, separators=(",", ":"),
                        ).encode("ascii")
                    ).hexdigest()
                if mutation == "semantic":
                    # Structural schema validation cannot derive the encoder
                    # formula, but record/replay validation must reject it.
                    validate_json_schema(hostile, schema)
                else:
                    with self.assertRaises(SchemaValidationError):
                        validate_json_schema(hostile, schema)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        hostile, self.root.tenant_id,
                        frozenset({"fleet_update_started"}), integrity=False,
                    )
                receipt.write_text(json.dumps(hostile, sort_keys=True) + "\n", encoding="utf-8")
                with self.assertRaises(IntegrityFailure):
                    ledger.rows(self.actor)

    def test_g4_projects_nonempty_owner_batch_and_synthetic_epoch_history(self) -> None:
        """Catches projection dropping conservative owner rows or synthetic joined epoch receipts."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        from floati.fleet_update import commit_fleet_update_g3
        from floati.registry import Registry
        first, second = self._two_by_two()
        excluded_workspace = self.base / "workspace-c"
        excluded_workspace.mkdir()
        Registry(self.root).register("seat-c", "Cursor")
        self._map([
            (first, "seat-a"), (second, "seat-b"),
            (excluded_workspace, "seat-c"),
        ])
        plan = self._preview()
        self.assertTrue(plan["seat_binding_consequences"])
        self.assertEqual(
            [{
                "node_id": "seat-c", "workspace": str(excluded_workspace),
                "authoritative_state": "active", "harness": "Cursor",
                "reason": "harness_not_codex",
            }],
            plan["seat_exclusions"],
        )
        before_product = _tree_bytes(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            commit_fleet_update_g3(
                plan=plan, root=self.root, actor=self.actor,
                idempotency_key="g4-product-epoch-unavailable",
            )
        self.assertEqual("fleet_update_epoch_roll_unavailable", caught.exception.code)
        self.assertEqual(before_product, _tree_bytes(self.base))
        self._consent()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "g4-owner-epoch")
        start_projection = self._projector()(self.root)
        self.assertIsNone(start_projection["last_completed_step_receipt_id"])
        self.assertEqual(
            ["shared_install", "waiter_binding", "waiter_binding", "epoch_roll", "transport_pins"],
            [row["kind"] for row in start_projection["remaining_steps"]],
        )
        shared = self._append_observed_step(
            ledger, plan, self.actor, "g4-owner-epoch", str(started["id"])
        )
        waiter = self._append_observed_step(
            ledger, plan, self.actor, "g4-owner-epoch", str(shared["id"])
        )
        interrupted = self._projector()(self.root)
        self.assertEqual(waiter["id"], interrupted["last_completed_step_receipt_id"])
        self.assertEqual(
            ["waiter_binding", "epoch_roll", "transport_pins"],
            [row["kind"] for row in interrupted["remaining_steps"]],
        )
        steps = [shared, waiter]
        predecessor = str(waiter["id"])
        for expected in ("waiter_binding", "epoch_roll", "transport_pins"):
            step = self._append_observed_step(
                ledger, plan, self.actor, "g4-owner-epoch", predecessor
            )
            self.assertEqual(expected, step["step_kind"])
            steps.append(step)
            predecessor = str(step["id"])
        completed = ledger.complete(plan, self.actor, "g4-owner-epoch", predecessor)
        projected = self._projector()(self.root)
        self.assertEqual(plan["seat_binding_consequences"], projected["seat_binding_consequences"])
        self.assertEqual(plan["seat_exclusions"], projected["seat_exclusions"])
        self.assertEqual("completed", projected["epoch_roll_state"])
        self.assertEqual(completed["id"], projected["completion_receipt_id"])

    def test_g4_schema_closes_projection_and_recovery_witness_nested_shapes(self) -> None:
        """Catches permissive nested machine shapes which hide hostile evidence."""

        from floati.doctor import Doctor
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        plan, _ledger, _completed = self._complete_equal_encoder_g3_context(
            "g4-schema-closure"
        )
        artifact, _rc = Doctor(REPOSITORY_ROOT, self.root.path).artifact()
        hostile_projection = json.loads(json.dumps(artifact))
        hostile_projection["fleet_update"]["operations"][0]["current_transport_pins"]["extra"] = True
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                hostile_projection,
                REPOSITORY_ROOT / "schemas/v1/doctor-artifact.schema.json",
            )

        witness = FleetUpdateReceiptLedger(self.root).start(
            plan, self.actor, "g4-schema-closure-witness"
        )
        hostile_witness = json.loads(json.dumps(witness))
        hostile_witness["recovery_witness"]["stale_pins"] = [{"unexpected": True}]
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                hostile_witness,
                REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json",
            )

    def test_g4_schema_and_runtime_reject_helper_definition_names_as_receipt_fields(self) -> None:
        """Catches helper-definition names becoming legal durable receipt fields."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        plan = self._preview()
        started = FleetUpdateReceiptLedger(self.root).start(
            plan, self.actor, "g4-helper-field-rejection"
        )
        schema = REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json"
        for field in ("transport_pins", "shared_install_intent", "stale_pin"):
            with self.subTest(field=field):
                hostile = dict(started, **{field: {}})
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(hostile, schema)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        hostile, self.root.tenant_id,
                        frozenset({"fleet_update_started"}), integrity=False,
                    )

    def test_g4_projection_integrity_covers_dangling_directory_and_actor_filename(self) -> None:
        """Catches absent projection or a crash for hostile receipt directory identities."""

        from floati.doctor import Doctor

        directory = self.root.path / "receipts" / "fleet-update"
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.symlink_to(self.base / "missing-receipt-directory")
        with self.assertRaises(IntegrityFailure):
            self._projector()(self.root)
        artifact, rc = Doctor(REPOSITORY_ROOT, self.root.path).artifact()
        self.assertEqual(33, rc)
        self.assertEqual("integrity_failure", artifact["fleet_update"]["state"])

        directory.unlink()
        directory.mkdir()
        (directory / "BAD!.jsonl").write_text("", encoding="utf-8")
        with self.assertRaises(IntegrityFailure):
            self._projector()(self.root)
        artifact, rc = Doctor(REPOSITORY_ROOT, self.root.path).artifact()
        self.assertEqual(33, rc)
        self.assertEqual("integrity_failure", artifact["fleet_update"]["state"])

    def test_g4_in_progress_aggregate_selects_an_in_progress_current_operation(self) -> None:
        """Catches a completed newer saga contradicting aggregate in-progress state."""

        from floati.fleet_update import commit_fleet_update_g3
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self.target_source.joinpath("floati", "records.py").write_bytes(
            b"current encoder\n"
        )
        self._write_manifest(self.target_source, "b" * 40)
        self._consent()
        ledger = FleetUpdateReceiptLedger(self.root)
        other = self._preview_for_actor("other-seat")
        started = ledger.start(other, "other-seat", "g4-newer-completed")
        predecessor = str(started["id"])
        steps = []
        for _expected in range(1 + len(other["waiter_bindings"])):
            step = self._append_observed_step(
                ledger, other, "other-seat", "g4-newer-completed", predecessor
            )
            steps.append(step)
            predecessor = str(step["id"])
        commit_fleet_update_g3(
            plan=other, root=self.root, actor="other-seat",
            idempotency_key="g4-newer-completed",
        )
        other_path = self.root.path / "receipts" / "fleet-update" / "other-seat.jsonl"
        future = []
        for row in ledger.rows("other-seat"):
            future.append(dict(row, timestamp="2099-01-01T00:00:00.000Z"))
        other_path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in future) + "\n",
            encoding="utf-8",
        )
        self._consent()
        ledger.start(self._preview(), self.actor, "g4-older-in-progress")

        projected = self._projector()(self.root)
        self.assertEqual("in_progress", projected["state"])
        self.assertEqual(self.actor, projected["actor"])
        self.assertEqual(2, len(projected["operations"]))

    def test_g4_history_binds_self_hashed_recovery_witness_to_its_actual_root(self) -> None:
        """Catches a different absolute root becoming valid receipt testimony."""

        from floati.doctor import Doctor
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "g4-foreign-witness-root")
        hostile = json.loads(json.dumps(started))
        hostile["recovery_witness"]["inputs"]["root"] = str(
            self.base / "other-floati-root"
        )
        hostile["plan_digest"] = hashlib.sha256(
            json.dumps(
                hostile["recovery_witness"], ensure_ascii=True,
                allow_nan=False, sort_keys=True, separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()

        # A context-free record validator must not claim it bound this root.
        validate_record(
            hostile, self.root.tenant_id, frozenset({"fleet_update_started"}),
            integrity=False,
        )
        self._receipt_path().write_text(
            json.dumps(hostile, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaises(IntegrityFailure):
            ledger.rows(self.actor)
        artifact, rc = Doctor(REPOSITORY_ROOT, self.root.path).artifact()
        self.assertEqual(33, rc)
        self.assertEqual("integrity_failure", artifact["fleet_update"]["state"])

    def test_g4_history_rejects_plan_divergent_steps_and_terminal_projection(self) -> None:
        """Catches forged physical rows that merely satisfy receipt shape checks."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        plan, ledger, completed = self._complete_equal_encoder_g3_context(
            "g4-history-step-binding"
        )
        rows = ledger.rows(self.actor)
        receipt = self._receipt_path()
        shared = next(row for row in rows if row.get("step_kind") == "shared_install")
        pins = next(row for row in rows if row.get("step_kind") == "transport_pins")
        waiter = next(row for row in rows if row.get("step_kind") == "waiter_binding")

        hostile_shared = json.loads(json.dumps(rows))
        next(row for row in hostile_shared if row["id"] == shared["id"])["post_digest"] = "f" * 64
        receipt.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in hostile_shared) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(IntegrityFailure):
            ledger.rows(self.actor)

        hostile_coordinate = json.loads(json.dumps(rows))
        next(
            row for row in hostile_coordinate if row["id"] == shared["id"]
        )["step_coordinate"]["destination"] = str(self.base / "forged-destination")
        receipt.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in hostile_coordinate) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(IntegrityFailure):
            ledger.rows(self.actor)

        hostile_kind = json.loads(json.dumps(rows))
        next(
            row for row in hostile_kind if row["id"] == shared["id"]
        )["step_kind"] = "waiter_binding"
        receipt.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in hostile_kind) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(IntegrityFailure):
            ledger.rows(self.actor)

        hostile_waiter = json.loads(json.dumps(rows))
        waiter_row = next(row for row in hostile_waiter if row["id"] == waiter["id"])
        waiter_row["step_evidence"]["hook_post_observation"]["current_hook_hash"] = "not-a-digest"
        receipt.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in hostile_waiter) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(IntegrityFailure):
            ledger.rows(self.actor)

        hostile_pins = json.loads(json.dumps(rows))
        pin_row = next(row for row in hostile_pins if row["id"] == pins["id"])
        pin_row["step_evidence"]["epoch_roll_state"] = "completed"
        terminal = next(row for row in hostile_pins if row["id"] == completed["id"])
        terminal["epoch_roll_state"] = "completed"
        receipt.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in hostile_pins) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(IntegrityFailure):
            ledger.rows(self.actor)
        with self.assertRaises(IntegrityFailure):
            self._projector()(self.root)

    def test_g4_doctor_integrity_overrides_an_earlier_refusal(self) -> None:
        """Catches receipt corruption retaining an unrelated refusal exit code."""

        from floati.doctor import Doctor

        receipts = self.root.path / "receipts" / "fleet-update"
        receipts.mkdir(parents=True, exist_ok=True)
        (receipts / "other-seat.jsonl").write_text("{not json}\n", encoding="utf-8")
        artifact, rc = Doctor(
            REPOSITORY_ROOT, self.root.path,
            gateway_config=self.base / "missing-gateway.json",
        ).artifact()
        self.assertEqual(33, rc)
        self.assertEqual("malformed_evidence", artifact["state"])
        self.assertEqual("integrity_failure", artifact["fleet_update"]["state"])

    def test_g4_real_equal_encoder_g2_to_g3_projection_and_retry(self) -> None:
        """Catches an acceptance path that replaces the real G2 product commit."""

        from floati.fleet_update import commit_fleet_update_g3
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger
        from tests.test_fu1_g2 import FU1G2Tests

        for relative in ("floati/events.py", "floati/records.py"):
            self.target_source.joinpath(relative).write_bytes(
                self.destination.joinpath(relative).read_bytes()
            )
        plan = FU1G2Tests._prepare_real_g2_plan(self)
        key = "g4-real-g2-g3-retry"
        g2 = FU1G2Tests._commit_real_g2(self, plan, key)
        g2_receipts = self._receipt_path().read_bytes()
        g2_retry = FU1G2Tests._commit_real_g2(self, plan, key)
        self.assertEqual(g2, g2_retry)
        self.assertEqual(g2_receipts, self._receipt_path().read_bytes())
        completed = commit_fleet_update_g3(
            plan=plan, root=self.root, actor=self.actor, idempotency_key=key,
        )
        before = self._receipt_path().read_bytes()
        projected = self._projector()(self.root)
        retried = commit_fleet_update_g3(
            plan=plan, root=self.root, actor=self.actor, idempotency_key=key,
        )

        self.assertEqual("g2_committed_pending_later_gates", g2["state"])
        self.assertFalse(plan["requires_epoch_roll"])
        self.assertEqual("completed", projected["state"])
        self.assertEqual(completed, retried)
        self.assertEqual(before, self._receipt_path().read_bytes())
        self.assertEqual(completed["id"], FleetUpdateReceiptLedger(self.root).rows(self.actor)[-1]["id"])


if __name__ == "__main__":
    unittest.main()
