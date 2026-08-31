"""G3 transport-pin and terminal-saga contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.records import validate_record
from floati.update_consent import UpdateConsentLedger
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.test_fu1_g0 import FU1G0Tests, REPOSITORY_ROOT, _tree_bytes
from tests.test_fu1_g1 import FU1G1Tests


class FU1G3RegistryTests(FU1G0Tests):
    """Real-byte tests for the surgical G3 registry boundary."""

    def setUp(self) -> None:
        super().setUp()
        self.transport = "floati-installed-v0"

    def _api(self):
        # This assertion is the intended RED: G3 has no product registry boundary.
        self.assertIsNotNone(
            importlib.util.find_spec("floati.fleet_update_registry"),
            "G3 must provide the guarded surgical registry boundary",
        )
        from floati.fleet_update_registry import rewrite_transport_pins
        return rewrite_transport_pins

    def _rewrite(self, **overrides: object) -> dict[str, object]:
        registry = self._api()
        return registry(
            self.registry_path,
            self.transport,
            manifest_sha256="c" * 64,
            source_sha="d" * 40,
            **overrides,
        )

    def _verify_post(self):
        from floati import fleet_update_registry

        verifier = getattr(
            fleet_update_registry, "verify_transport_pins_post", None
        )
        self.assertTrue(
            callable(verifier),
            "G3 post-state recovery must replay exact file and parent durability",
        )
        return verifier

    def test_g3_rewrites_both_pins_and_preserves_every_other_byte(self) -> None:
        """Catches a formatter or one-pin writer corrupting host-owned registry bytes."""

        before = self.registry_path.read_bytes()
        result = self._rewrite()
        after = self.registry_path.read_bytes()

        old_manifest = json.loads(before)["transports"][self.transport]["manifest_sha256"].encode("ascii")

        self.assertEqual(["manifest_sha256", "source_sha"], result["changed_pins"])
        self.assertEqual(
            before.replace(old_manifest, b"").replace(b"a" * 40, b""),
            after.replace(b"c" * 64, b"").replace(b"d" * 40, b""),
        )
        parsed = json.loads(after)
        self.assertEqual("c" * 64, parsed["transports"][self.transport]["manifest_sha256"])
        self.assertEqual("d" * 40, parsed["transports"][self.transport]["source_sha"])
        self.assertEqual("must stay byte-identical", parsed["unrelated"]["spacing"])
        self.assertEqual(self.transport, parsed["profiles"]["fleet-floati-builder-one"]["transport"])

    def test_g3_refuses_missing_or_non_string_selected_pin_without_mutation(self) -> None:
        """Catches an unsafe broad JSON edit when the selected lexical pin is absent."""

        document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        document["transports"][self.transport].pop("source_sha")
        self.registry_path.write_text(json.dumps(document), encoding="utf-8")
        before = self.registry_path.read_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            self._rewrite()

        self.assertEqual("fleet_update_transport_registry_invalid", caught.exception.code)
        self.assertEqual(before, self.registry_path.read_bytes())

    def test_g3_refuses_registry_content_drift_before_replace(self) -> None:
        """Catches replacement authorized by a stale registry-content witness."""

        before = self.registry_path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self._rewrite(expected_registry_sha256="0" * 64)

        self.assertEqual("fleet_update_transport_registry_drift", caught.exception.code)
        self.assertEqual(before, self.registry_path.read_bytes())

    def test_g3_refuses_registry_inode_drift_before_replace(self) -> None:
        """Catches a path swap between preflight and replacement."""

        before = self.registry_path.read_bytes()
        observed = os.stat(self.registry_path)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._rewrite(expected_identity=(observed.st_dev, observed.st_ino + 1))

        self.assertEqual("fleet_update_transport_registry_drift", caught.exception.code)
        self.assertEqual(before, self.registry_path.read_bytes())

    def test_g3_rejects_escaped_or_duplicate_pin_lexemes(self) -> None:
        """Catches JSON decoding that silently normalizes unsafe lexical pin spans."""

        raw = self.registry_path.read_text(encoding="utf-8")
        self.registry_path.write_text(
            raw.replace('"source_sha": "' + "a" * 40 + '"', '"source_sha": "\\u0061' + "a" * 39 + '"'),
            encoding="utf-8",
        )
        before = self.registry_path.read_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            self._rewrite()

        self.assertEqual("fleet_update_transport_registry_invalid", caught.exception.code)
        self.assertEqual(before, self.registry_path.read_bytes())

    def test_g3_crash_seams_leave_pre_or_exact_post_and_retry_is_idempotent(self) -> None:
        """Catches a partial registry replacement or retry that reformats a completed file."""

        before = self.registry_path.read_bytes()
        events: list[str] = []

        def crash(event: str) -> None:
            events.append(event)
            if event == "before_replace":
                raise RuntimeError("injected")

        with self.assertRaises(RuntimeError):
            self._rewrite(_fault_hook=crash)
        self.assertEqual(before, self.registry_path.read_bytes())
        first = self._rewrite()
        after = self.registry_path.read_bytes()
        second = self._rewrite()
        self.assertEqual(after, self.registry_path.read_bytes())
        self.assertEqual(first["registry_after_sha256"], second["registry_after_sha256"])

    def test_g3_post_rename_faults_require_a_durable_exact_post_recovery(self) -> None:
        """A post-rename failure cannot be followed by an unverified recovered receipt."""

        before = self.registry_path.read_bytes()
        for event in ("after_replace", "after_parent_open", "after_parent_fsync", "before_readback"):
            with self.subTest(event=event):
                self.registry_path.write_bytes(before)

                def fault(observed: str) -> None:
                    if observed == event:
                        raise OSError("injected post-rename durability failure")

                with self.assertRaises(
                    (DurabilityFailure, ProtocolRefusal)
                ) as caught:
                    self._rewrite(_fault_hook=fault)
                self.assertIsInstance(caught.exception, DurabilityFailure)
                self.assertEqual(
                    "fleet_update_transport_post_durability_failed",
                    caught.exception.code,
                )
                post = self.registry_path.read_bytes()
                self.assertNotEqual(before, post)
                verified = self._verify_post()(
                    self.registry_path,
                    self.transport,
                    manifest_sha256="c" * 64,
                    source_sha="d" * 40,
                    expected_registry_sha256=hashlib.sha256(post).hexdigest(),
                )
                self.assertEqual(hashlib.sha256(post).hexdigest(), verified["registry_after_sha256"])

    def test_g3_recovery_durability_failure_is_not_a_pre_mutation_refusal(self) -> None:
        """A post-state verifier failure reports interrupted durability, never refusal."""

        self._rewrite()
        post = self.registry_path.read_bytes()

        def fault(event: str) -> None:
            if event == "after_parent_open":
                raise OSError("injected recovery durability failure")

        with self.assertRaises((DurabilityFailure, ProtocolRefusal)) as caught:
            self._verify_post()(
                self.registry_path,
                self.transport,
                manifest_sha256="c" * 64,
                source_sha="d" * 40,
                expected_registry_sha256=hashlib.sha256(post).hexdigest(),
                _fault_hook=fault,
            )
        self.assertIsInstance(caught.exception, DurabilityFailure)
        self.assertEqual("fleet_update_transport_post_durability_failed", caught.exception.code)
        self.assertEqual(post, self.registry_path.read_bytes())

    def test_g3_post_replace_identity_or_readback_inconsistency_is_durable(self) -> None:
        """After rename, inode and byte verification failures preserve the actual post bytes."""

        before = self.registry_path.read_bytes()
        for mode in ("same-byte-inode", "inconsistent-readback"):
            with self.subTest(mode=mode):
                self.registry_path.write_bytes(before)

                def fault(event: str) -> None:
                    if event != "before_readback":
                        return
                    if mode == "same-byte-inode":
                        replacement = self.registry_path.with_suffix(".post-swap")
                        replacement.write_bytes(self.registry_path.read_bytes())
                        os.replace(replacement, self.registry_path)
                    else:
                        self.registry_path.write_bytes(
                            self.registry_path.read_bytes() + b"\n"
                        )

                with self.assertRaises(
                    (DurabilityFailure, IntegrityFailure, ProtocolRefusal)
                ) as caught:
                    self._rewrite(_fault_hook=fault)
                self.assertIsInstance(caught.exception, DurabilityFailure)
                self.assertEqual(
                    "fleet_update_transport_post_durability_failed",
                    caught.exception.code,
                )
                actual_post = self.registry_path.read_bytes()
                self.assertNotEqual(before, actual_post)
                self.assertEqual(actual_post, self.registry_path.read_bytes())

    def test_g3_direct_post_observation_is_durable_for_every_physical_loss(self) -> None:
        """A post-state verifier must not present a lost post registry as pre-mutation."""

        self._rewrite()
        post = self.registry_path.read_bytes()
        original_read = Path.read_bytes
        from floati import fleet_update_registry

        for mode in (
            "missing", "unreadable", "nonregular", "malformed", "digest", "pins",
            "identity-drift",
        ):
            with self.subTest(mode=mode):
                self.registry_path.parent.mkdir(parents=True, exist_ok=True)
                if self.registry_path.is_dir():
                    self.registry_path.rmdir()
                self.registry_path.write_bytes(post)

                def fault(event: str) -> None:
                    if mode == "identity-drift" and event == "before_readback":
                        replacement = self.registry_path.with_suffix(".direct-post-swap")
                        replacement.write_bytes(post)
                        os.replace(replacement, self.registry_path)

                if mode == "missing":
                    self.registry_path.unlink()
                    context = nullcontext()
                elif mode == "unreadable":
                    def unreadable(path: Path) -> bytes:
                        if path == self.registry_path:
                            raise OSError("injected unreadable post registry")
                        return original_read(path)
                    context = mock.patch.object(Path, "read_bytes", new=unreadable)
                elif mode == "nonregular":
                    self.registry_path.unlink()
                    self.registry_path.mkdir()
                    context = nullcontext()
                elif mode == "malformed":
                    self.registry_path.write_bytes(b"{not-json")
                    context = nullcontext()
                elif mode == "digest":
                    self.registry_path.write_bytes(post + b"\n")
                    context = nullcontext()
                elif mode == "pins":
                    self.registry_path.write_bytes(post.replace(b'"source_sha": "d', b'"source_sha": "e'))
                    context = nullcontext()
                else:
                    context = nullcontext()
                with context:
                    with self.assertRaises(
                        (DurabilityFailure, IntegrityFailure, ProtocolRefusal)
                    ) as caught:
                        self._verify_post()(
                            self.registry_path,
                            self.transport,
                            manifest_sha256="c" * 64,
                            source_sha="d" * 40,
                            expected_registry_sha256=hashlib.sha256(post).hexdigest(),
                            _fault_hook=fault,
                        )
                self.assertIsInstance(caught.exception, DurabilityFailure)
                self.assertEqual(
                    "fleet_update_transport_post_durability_failed",
                    caught.exception.code,
                )

    def test_g3_pre_replace_stat_or_read_failures_are_typed_and_programmer_faults_escape(self) -> None:
        """Writer preflight remains a no-mutation refusal, while RuntimeError is not recast."""

        before = self.registry_path.read_bytes()
        from floati import fleet_update_registry

        for seam in ("stat", "read"):
            with self.subTest(seam=seam):
                if seam == "stat":
                    patcher = mock.patch.object(
                        fleet_update_registry, "_identity", side_effect=OSError("stat")
                    )
                else:
                    original_read = Path.read_bytes

                    def unreadable(path: Path) -> bytes:
                        if path == self.registry_path:
                            raise OSError("read")
                        return original_read(path)
                    patcher = mock.patch.object(Path, "read_bytes", new=unreadable)
                with patcher:
                    with self.assertRaises((ProtocolRefusal, OSError)) as caught:
                        self._rewrite()
                self.assertIsInstance(caught.exception, ProtocolRefusal)
                self.assertEqual(before, self.registry_path.read_bytes())

        def programmer_fault(event: str) -> None:
            if event == "after_parent_open":
                raise RuntimeError("programmer defect")

        with self.assertRaises(RuntimeError):
            self._verify_post()(
                self.registry_path,
                self.transport,
                manifest_sha256="a" * 64,
                source_sha="a" * 40,
                expected_registry_sha256=hashlib.sha256(before).hexdigest(),
                _fault_hook=programmer_fault,
            )
        self.assertEqual(before, self.registry_path.read_bytes())

    def test_g3_pre_replace_filesystem_failure_remains_a_refusal(self) -> None:
        """Before rename, a writer failure is still honestly a no-mutation refusal."""

        before = self.registry_path.read_bytes()

        def fault(event: str) -> None:
            if event == "before_replace":
                raise OSError("injected pre-replace write failure")

        with self.assertRaises(ProtocolRefusal) as caught:
            self._rewrite(_fault_hook=fault)
        self.assertEqual("fleet_update_transport_write_failed", caught.exception.code)
        self.assertEqual(before, self.registry_path.read_bytes())

    def test_g3_final_inode_swap_and_content_drift_refuse_before_replace(self) -> None:
        """Catches a same-byte rename or write raced after staging the 0600 temporary."""

        before = self.registry_path.read_bytes()
        original = self.registry_path.with_suffix(".original")

        def swap(event: str) -> None:
            if event == "after_temp_fsync":
                original.write_bytes(self.registry_path.read_bytes())
                os.replace(original, self.registry_path)

        with self.assertRaises(ProtocolRefusal) as caught:
            self._rewrite(_fault_hook=swap)
        self.assertEqual("fleet_update_transport_registry_drift", caught.exception.code)
        self.assertEqual(before, self.registry_path.read_bytes())

        def drift(event: str) -> None:
            if event == "after_temp_fsync":
                self.registry_path.write_bytes(before + b"\n")

        with self.assertRaises(ProtocolRefusal) as caught:
            self._rewrite(_fault_hook=drift)
        self.assertEqual("fleet_update_transport_registry_drift", caught.exception.code)
        self.assertEqual(before + b"\n", self.registry_path.read_bytes())

    def test_g3_wrong_transport_never_changes_sibling_or_profile(self) -> None:
        """Catches a fallback edit that silently chooses a different registry transport."""

        before = self.registry_path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self._api()(self.registry_path, "other-transport", manifest_sha256="c" * 64, source_sha="d" * 40)
        self.assertEqual("fleet_update_transport_missing", caught.exception.code)
        self.assertEqual(before, self.registry_path.read_bytes())

    def test_g3_unicode_unrelated_bytes_survive_surgical_rewrite(self) -> None:
        """Catches character offsets being applied to UTF-8 bytes after a pin."""

        raw = self.registry_path.read_text(encoding="utf-8").replace(
            '"schema_version": 0', '"schema_version": "café ��� 保持"',
        )
        self.registry_path.write_text(raw, encoding="utf-8")
        before = self.registry_path.read_bytes()

        try:
            self._rewrite()
        except IntegrityFailure as exc:
            self.fail(f"UTF-8 registry rewrite falsely failed after mutation: {exc.code}")

        after = self.registry_path.read_bytes()
        self.assertIn("café ��� 保持".encode("utf-8"), after)
        self.assertEqual(
            before.replace(json.loads(before)["transports"][self.transport]["manifest_sha256"].encode(), b"").replace(b"a" * 40, b""),
            after.replace(b"c" * 64, b"").replace(b"d" * 40, b""),
        )


class FU1G3SagaTests(FU1G1Tests):
    """G3 must join the G2 frontier and not fabricate the ruled VS-7 evidence."""

    def setUp(self) -> None:
        super().setUp()
        self.transport = "floati-installed-v0"

    def _commit(self):
        from floati import fleet_update

        operation = getattr(fleet_update, "commit_fleet_update_g3", None)
        self.assertTrue(callable(operation), "G3 must expose one guarded terminal continuation")
        return operation

    def test_g3_equal_encoder_omits_epoch_roll_and_rejects_testimony(self) -> None:
        """Catches accepting caller-supplied epoch testimony for stable encoders."""

        operation = self._commit()
        self.target_source.joinpath("floati", "records.py").write_bytes(b"current encoder\n")
        self._write_manifest(self.target_source, "b" * 40)
        plan = self._preview_for_g3()
        self.assertFalse(plan["requires_epoch_roll"])
        with self.assertRaises(ProtocolRefusal) as caught:
            operation(plan=plan, root=self.root, actor=self.actor, idempotency_key="g3-equal", epoch_roll={"id": "forged"})
        self.assertEqual("fleet_update_epoch_roll_unexpected", caught.exception.code)

    def test_g3_preview_uses_one_registry_snapshot_for_pre_and_target(self) -> None:
        """The plan must not combine A's pre witness with B's surgical target."""

        self.target_source.joinpath("floati", "records.py").write_bytes(b"current encoder\n")
        self._write_manifest(self.target_source, "b" * 40)
        snapshot_a = self.registry_path.read_bytes()
        snapshot_b = snapshot_a.replace(
            b"must stay byte-identical", b"B" * len(b"must stay byte-identical")
        )
        self.assertEqual(len(snapshot_a), len(snapshot_b))
        from floati import fleet_update
        from floati.fleet_update_registry import rewrite_transport_pins

        reopened: list[object] = []
        original = fleet_update.planned_transport_registry_sha256

        def second_read(*args: object, **kwargs: object) -> str:
            reopened.append(args)
            self.registry_path.write_bytes(snapshot_b)
            return original(*args, **kwargs)

        with mock.patch.object(
            fleet_update, "planned_transport_registry_sha256", side_effect=second_read
        ):
            plan = self._preview_for_g3()
        self.assertEqual([], reopened)
        self.assertEqual(snapshot_a, self.registry_path.read_bytes())
        expected_registry = self.registry_path.with_name("expected-snapshot.json")
        expected_registry.write_bytes(snapshot_a)
        rewrite_transport_pins(
            expected_registry, self.transport,
            manifest_sha256=str(plan["target_transport_pins"]["manifest_sha256"]),
            source_sha=str(plan["target_transport_pins"]["source_sha"]),
        )
        expected_body = expected_registry.read_bytes()
        expected_registry.unlink()
        self.assertEqual(hashlib.sha256(snapshot_a).hexdigest(), plan["transport_registry_sha256"])
        self.assertEqual(
            hashlib.sha256(expected_body).hexdigest(),
            plan["target_transport_registry_sha256"],
        )
        self.assertEqual(
            snapshot_a.replace(b"a" * 40, b"").replace(
                json.loads(snapshot_a)["transports"][self.transport]["manifest_sha256"].encode(), b""
            ),
            expected_body.replace(b"b" * 40, b"").replace(
                str(plan["target_transport_pins"]["manifest_sha256"]).encode(), b""),
        )

        # A concurrent B that returns to A before the guarded G3 writer is no
        # saga state at all: only the eventual pins receipt authorizes A -> post.
        self.registry_path.write_bytes(snapshot_b)
        self.registry_path.write_bytes(snapshot_a)
        self._consent()
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger
        from floati.fleet_update import commit_fleet_update_g3

        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "g3-one-snapshot")
        predecessor = str(started["id"])
        for _ in range(1 + len(plan["waiter_bindings"])):
            step = self._append_observed_step(
                ledger, plan, self.actor, "g3-one-snapshot", predecessor
            )
            predecessor = str(step["id"])
        before_commit_rows = ledger.rows(self.actor)
        self.assertFalse(any(row.get("step_kind") == "transport_pins" for row in before_commit_rows))
        completed = commit_fleet_update_g3(
            plan=plan, root=self.root, actor=self.actor, idempotency_key="g3-one-snapshot",
        )
        self.assertEqual("fleet_update_completed", completed["kind"])
        self.assertEqual(expected_body, self.registry_path.read_bytes())

    def test_g3_changed_encoder_fails_closed_without_vs7_product_api_before_pins(self) -> None:
        """Catches invented epoch archives or pins committed ahead of unavailable VS-7."""

        operation = self._commit()
        plan = self._preview_for_g3()
        before = self.registry_path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            operation(plan=plan, root=self.root, actor=self.actor, idempotency_key="g3-roll")
        self.assertEqual("fleet_update_epoch_roll_unavailable", caught.exception.code)
        self.assertEqual(before, self.registry_path.read_bytes())

    def test_g3_requires_the_exact_g2_frontier_before_pins_or_completion(self) -> None:
        """Catches pins or a terminal receipt appended before shared and waiter G2 receipts."""

        operation = self._commit()
        self.target_source.joinpath("floati", "records.py").write_bytes(b"current encoder\n")
        self._write_manifest(self.target_source, "b" * 40)
        plan = self._preview_for_g3()
        before = self.registry_path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            operation(plan=plan, root=self.root, actor=self.actor, idempotency_key="g3-no-frontier")
        self.assertEqual("fleet_update_g2_frontier_invalid", caught.exception.code)
        self.assertEqual(before, self.registry_path.read_bytes())

    def test_g3_refuses_missing_wrong_or_foreign_epoch_roll_testimony(self) -> None:
        """Catches a caller-forged VS-7 first record or an archive outside this root."""

        operation = self._commit()
        plan = self._preview_for_g3()
        for testimony in (
            {},
            {"first_record_id": "epoch-roll-foreign", "archive_path": str(self.base / "archive")},
            {"first_record_id": "epoch-roll-local", "archive_path": str(self.base.parent / "foreign")},
        ):
            with self.assertRaises(ProtocolRefusal) as caught:
                operation(plan=plan, root=self.root, actor=self.actor, idempotency_key="g3-roll-proof", _epoch_roll_result=testimony)
            self.assertIn(caught.exception.code, {"fleet_update_epoch_roll_unavailable", "fleet_update_epoch_roll_invalid"})

    def test_g3_transport_observer_refuses_scalar_and_list_rows_without_attribute_error(self) -> None:
        """Valid JSON with a non-object selected transport remains a typed refusal."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger, _step_spec

        self.target_source.joinpath("floati", "records.py").write_bytes(b"current encoder\n")
        self._write_manifest(self.target_source, "b" * 40)
        plan = self._preview_for_g3()
        spec = next(
            _step_spec(plan, plan["seat_binding_consequences"], ordinal)
            for ordinal in range(1, 8)
            if _step_spec(plan, plan["seat_binding_consequences"], ordinal)["step_kind"] == "transport_pins"
        )
        before = self.registry_path.read_bytes()
        observer = FleetUpdateReceiptLedger(self.root)
        for hostile in ("scalar", []):
            with self.subTest(hostile=type(hostile).__name__):
                document = json.loads(before)
                document["transports"][self.transport] = hostile
                self.registry_path.write_text(json.dumps(document), encoding="utf-8")
                try:
                    observer._observe_step(plan, self.actor, "g3-observer-hostile", spec)
                except AttributeError as exc:
                    self.fail(f"transport observer leaked AttributeError: {exc}")
                except ProtocolRefusal as caught:
                    self.assertEqual("fleet_update_transport_registry_invalid", caught.code)
                else:
                    self.fail("transport observer accepted a non-object selected transport")
        self.registry_path.write_bytes(before)

    def _preview_for_g3(self) -> dict[str, object]:
        from floati.fleet_update import preview_fleet_update

        return preview_fleet_update(
            root=self.root, actor=self.actor, destination=self.destination,
            target_source=self.target_source, target_source_sha="b" * 40,
            channel=self.channel, version=self.version, binding_path=self.binding_path,
            transport_registry=self.registry_path, transport_name=self.transport,
        )


class FU1G3CompletionTests(FU1G1Tests):
    """The equal-encoder terminal branch retains real registry bytes and receipts."""

    def test_g3_equal_encoder_receipts_pins_before_terminal_completion(self) -> None:
        """Catches completion appended before the one joined two-pin replacement receipt."""

        from floati.fleet_update import commit_fleet_update_g3, preview_fleet_update
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self.target_source.joinpath("floati", "records.py").write_bytes(b"current encoder\n")
        self._write_manifest(self.target_source, "b" * 40)
        plan = preview_fleet_update(
            root=self.root, actor=self.actor, destination=self.destination,
            target_source=self.target_source, target_source_sha="b" * 40,
            channel=self.channel, version=self.version, binding_path=self.binding_path,
            transport_registry=self.registry_path, transport_name=self.transport,
        )
        self.assertFalse(plan["requires_epoch_roll"])
        self._consent()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "g3-completes")
        predecessor = str(started["id"])
        for _ in range(1 + len(plan["waiter_bindings"])):
            step = self._append_observed_step(ledger, plan, self.actor, "g3-completes", predecessor)
            predecessor = str(step["id"])
        completed = commit_fleet_update_g3(plan=plan, root=self.root, actor=self.actor, idempotency_key="g3-completes")
        rows = [row for row in ledger.rows(self.actor) if row.get("idempotency_key") == "g3-completes"]
        self.assertEqual("fleet_update_completed", completed["kind"])
        self.assertEqual(["shared_install", "waiter_binding", "transport_pins"], [row["step_kind"] for row in rows if row["kind"] == "fleet_update_step"])
        self.assertEqual("transport_pins", rows[-2]["step_kind"])
        self.assertEqual(str(plan["transport_registry_sha256"]), rows[-2]["step_evidence"]["registry_before_sha256"])
        self.assertEqual(rows[-2]["id"], completed["predecessor_receipt_id"])

    def test_g3_completion_carries_g4_terminal_projection(self) -> None:
        """Catches a terminal row that loses source, epoch, or registry recovery facts."""

        completed = self._complete_equal_encoder_g3("g3-terminal-projection")
        for field in (
            "previous_source_sha", "target_source_sha", "epoch_roll_state",
            "registry_before_sha256", "registry_after_sha256",
        ):
            self.assertIn(field, completed)

    def test_g3_transport_evidence_and_coordinate_are_closed(self) -> None:
        """Catches placeholder, extra, or unbound transport receipt testimony."""

        completed = self._complete_equal_encoder_g3("g3-closed-evidence")
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger
        rows = FleetUpdateReceiptLedger(self.root).rows(self.actor)
        pins = next(row for row in rows if row.get("idempotency_key") == "g3-closed-evidence" and row.get("step_kind") == "transport_pins")
        self.assertEqual(
            {
                "kind", "registry", "transport", "registry_before_sha256",
                "registry_after_sha256", "previous_source_sha",
                "target_source_sha", "epoch_roll_state",
            },
            set(pins["step_evidence"]),
        )
        self.assertEqual(
            {"kind", "registry", "transport"}, set(pins["step_coordinate"])
        )

    def test_g3_completed_retry_reobserves_exact_registry_bytes(self) -> None:
        """Catches a completed retry accepting target pins with corrupted sibling bytes."""

        completed = self._complete_equal_encoder_g3("g3-post-readback")
        from floati.fleet_update import commit_fleet_update_g3, preview_fleet_update

        self.registry_path.write_bytes(
            self.registry_path.read_bytes().replace(
                b"must stay byte-identical", b"corrupted-sibling-bytes"
            )
        )
        plan = preview_fleet_update(root=self.root, actor=self.actor, destination=self.destination, target_source=self.target_source, target_source_sha="b" * 40, channel=self.channel, version=self.version, binding_path=self.binding_path, transport_registry=self.registry_path, transport_name=self.transport)
        with self.assertRaises(ProtocolRefusal) as caught:
            commit_fleet_update_g3(plan=plan, root=self.root, actor=self.actor, idempotency_key="g3-post-readback")
        self.assertIn(caught.exception.code, {"fleet_update_plan_drift", "fleet_update_g2_frontier_invalid", "fleet_update_step_evidence_missing", "fleet_update_transport_registry_invalid"})

    def test_g3_completed_exact_retry_uses_original_plan_and_preserves_receipts(self) -> None:
        """A completed retry re-observes the original plan without appending history."""

        plan, _ledger, _completed = self._complete_equal_encoder_g3_context(
            "g3-original-plan-retry"
        )
        before_receipts = self._receipt_path().read_bytes()
        self.registry_path.write_bytes(
            self.registry_path.read_bytes().replace(
                b"must stay byte-identical", b"corrupted-sibling-bytes"
            )
        )
        from floati.fleet_update import commit_fleet_update_g3

        with self.assertRaises(DurabilityFailure) as caught:
            commit_fleet_update_g3(
                plan=plan, root=self.root, actor=self.actor,
                idempotency_key="g3-original-plan-retry",
            )
        self.assertEqual("fleet_update_transport_post_durability_failed", caught.exception.code)
        self.assertEqual(before_receipts, self._receipt_path().read_bytes())

    def test_g3_product_refuses_same_byte_inode_swap_after_prepare(self) -> None:
        """The guarded pre-observation inode reaches the real product registry writer."""

        plan, ledger = self._equal_encoder_g3_frontier("g3-product-inode-swap")
        before_registry = self.registry_path.read_bytes()
        before_receipts = self._receipt_path().read_bytes()
        from floati import fleet_update

        original = fleet_update.rewrite_transport_pins

        def swap_then_rewrite(*args: object, **kwargs: object) -> dict[str, object]:
            replacement = self.registry_path.with_suffix(".same-byte-race")
            replacement.write_bytes(before_registry)
            os.replace(replacement, self.registry_path)
            return original(*args, **kwargs)

        with mock.patch.object(
            fleet_update, "rewrite_transport_pins", side_effect=swap_then_rewrite
        ):
            with self.assertRaises(ProtocolRefusal) as caught:
                fleet_update.commit_fleet_update_g3(
                    plan=plan, root=self.root, actor=self.actor,
                    idempotency_key="g3-product-inode-swap",
                )
        self.assertEqual("fleet_update_transport_registry_drift", caught.exception.code)
        self.assertEqual(before_registry, self.registry_path.read_bytes())
        self.assertEqual(before_receipts, self._receipt_path().read_bytes())
        self.assertEqual([], [
            row for row in ledger.rows(self.actor)
            if row.get("idempotency_key") == "g3-product-inode-swap"
            and row.get("step_kind") == "transport_pins"
        ])

    def test_g3_post_replace_durability_failure_keeps_receipts_absent(self) -> None:
        """Product G3 preserves post bytes and history when durability is interrupted."""

        plan, ledger = self._equal_encoder_g3_frontier("g3-product-durability")
        before_registry = self.registry_path.read_bytes()
        before_receipts = self._receipt_path().read_bytes()
        from floati import fleet_update

        original = fleet_update.rewrite_transport_pins

        def fault(event: str) -> None:
            if event == "after_parent_fsync":
                raise OSError("injected product post-replace durability failure")

        def fail_after_replace(*args: object, **kwargs: object) -> dict[str, object]:
            return original(*args, _fault_hook=fault, **kwargs)

        with mock.patch.object(
            fleet_update, "rewrite_transport_pins", side_effect=fail_after_replace
        ):
            with self.assertRaises(
                (DurabilityFailure, ProtocolRefusal)
            ) as caught:
                fleet_update.commit_fleet_update_g3(
                    plan=plan, root=self.root, actor=self.actor,
                    idempotency_key="g3-product-durability",
                )
        self.assertIsInstance(caught.exception, DurabilityFailure)
        self.assertEqual("fleet_update_transport_post_durability_failed", caught.exception.code)
        self.assertNotEqual(before_registry, self.registry_path.read_bytes())
        self.assertEqual(before_receipts, self._receipt_path().read_bytes())
        self.assertEqual([], [
            row for row in ledger.rows(self.actor)
            if row.get("idempotency_key") == "g3-product-durability"
            and row.get("step_kind") == "transport_pins"
        ])

    def test_g3_product_recovery_identity_drift_is_durable_and_history_stable(self) -> None:
        """Completed recovery reports an after-mutation inode drift as durability loss."""

        plan, _ledger, _completed = self._complete_equal_encoder_g3_context(
            "g3-product-recovery-inode"
        )
        before_registry = self.registry_path.read_bytes()
        before_receipts = self._receipt_path().read_bytes()
        from floati import fleet_update_registry

        original = fleet_update_registry.verify_transport_pins_post

        def swap_before_readback(event: str) -> None:
            if event == "before_readback":
                replacement = self.registry_path.with_suffix(".recovery-swap")
                replacement.write_bytes(self.registry_path.read_bytes())
                os.replace(replacement, self.registry_path)

        def drifted_verify(*args: object, **kwargs: object) -> dict[str, object]:
            return original(*args, _fault_hook=swap_before_readback, **kwargs)

        with mock.patch.object(
            fleet_update_registry,
            "verify_transport_pins_post",
            side_effect=drifted_verify,
        ):
            from floati.fleet_update import commit_fleet_update_g3
            with self.assertRaises(
                (DurabilityFailure, IntegrityFailure, ProtocolRefusal)
            ) as caught:
                commit_fleet_update_g3(
                    plan=plan, root=self.root, actor=self.actor,
                    idempotency_key="g3-product-recovery-inode",
                )
        self.assertIsInstance(caught.exception, DurabilityFailure)
        self.assertEqual("fleet_update_transport_post_durability_failed", caught.exception.code)
        self.assertEqual(before_registry, self.registry_path.read_bytes())
        self.assertEqual(before_receipts, self._receipt_path().read_bytes())

    def test_g3_product_recovery_inconsistent_readback_is_durable_and_history_stable(self) -> None:
        """Completed recovery reports inconsistent post bytes without appending receipts."""

        plan, _ledger, _completed = self._complete_equal_encoder_g3_context(
            "g3-product-recovery-readback"
        )
        before_registry = self.registry_path.read_bytes()
        before_receipts = self._receipt_path().read_bytes()
        from floati import fleet_update_registry

        original = fleet_update_registry.verify_transport_pins_post

        def corrupt_before_readback(event: str) -> None:
            if event == "before_readback":
                self.registry_path.write_bytes(
                    self.registry_path.read_bytes() + b"\n"
                )

        def inconsistent_verify(*args: object, **kwargs: object) -> dict[str, object]:
            return original(*args, _fault_hook=corrupt_before_readback, **kwargs)

        with mock.patch.object(
            fleet_update_registry,
            "verify_transport_pins_post",
            side_effect=inconsistent_verify,
        ):
            from floati.fleet_update import commit_fleet_update_g3
            with self.assertRaises(
                (DurabilityFailure, IntegrityFailure, ProtocolRefusal)
            ) as caught:
                commit_fleet_update_g3(
                    plan=plan, root=self.root, actor=self.actor,
                    idempotency_key="g3-product-recovery-readback",
                )
        self.assertIsInstance(caught.exception, DurabilityFailure)
        self.assertEqual("fleet_update_transport_post_durability_failed", caught.exception.code)
        self.assertNotEqual(before_registry, self.registry_path.read_bytes())
        self.assertEqual(before_receipts, self._receipt_path().read_bytes())

    def test_g3_completed_recovery_physical_loss_is_durable_and_never_rewrites_history(self) -> None:
        """Persisted pins make every later registry loss a post-mutation durability failure."""

        plan, _ledger, _completed = self._complete_equal_encoder_g3_context(
            "g3-completed-physical-loss"
        )
        post = self.registry_path.read_bytes()
        receipts = self._receipt_path().read_bytes()
        original_read = Path.read_bytes
        from floati import fleet_update_registry
        from floati.fleet_update import commit_fleet_update_g3

        for mode in (
            "missing", "unreadable", "nonregular", "malformed", "digest", "pins",
            "identity-drift",
        ):
            with self.subTest(mode=mode):
                if self.registry_path.is_dir():
                    self.registry_path.rmdir()
                self.registry_path.write_bytes(post)

                def swap_before_readback(event: str) -> None:
                    if event == "before_readback":
                        replacement = self.registry_path.with_suffix(".completed-swap")
                        replacement.write_bytes(post)
                        os.replace(replacement, self.registry_path)

                def drifted_verify(*args: object, **kwargs: object) -> dict[str, object]:
                    return original_verify(*args, _fault_hook=swap_before_readback, **kwargs)

                if mode == "missing":
                    self.registry_path.unlink()
                    context = nullcontext()
                elif mode == "unreadable":
                    def unreadable(path: Path) -> bytes:
                        if path == self.registry_path:
                            raise OSError("injected completed recovery read failure")
                        return original_read(path)
                    context = mock.patch.object(Path, "read_bytes", new=unreadable)
                elif mode == "nonregular":
                    self.registry_path.unlink()
                    self.registry_path.mkdir()
                    context = nullcontext()
                elif mode == "malformed":
                    self.registry_path.write_bytes(b"{not-json")
                    context = nullcontext()
                elif mode == "digest":
                    self.registry_path.write_bytes(post + b"\n")
                    context = nullcontext()
                elif mode == "pins":
                    self.registry_path.write_bytes(post.replace(b'"source_sha": "b', b'"source_sha": "e'))
                    context = nullcontext()
                else:
                    original_verify = fleet_update_registry.verify_transport_pins_post
                    context = mock.patch.object(
                        fleet_update_registry,
                        "verify_transport_pins_post",
                        side_effect=drifted_verify,
                    )
                with context:
                    with self.assertRaises(
                        (DurabilityFailure, IntegrityFailure, ProtocolRefusal)
                    ) as caught:
                        commit_fleet_update_g3(
                            plan=plan, root=self.root, actor=self.actor,
                            idempotency_key="g3-completed-physical-loss",
                        )
                self.assertIsInstance(caught.exception, DurabilityFailure)
                self.assertEqual(
                    "fleet_update_transport_post_durability_failed",
                    caught.exception.code,
                )
                self.assertEqual(receipts, self._receipt_path().read_bytes())

    def test_g3_pins_only_completion_reobserves_persisted_evidence(self) -> None:
        """A forged saved post digest cannot be promoted to terminal evidence."""

        plan, ledger, _completed = self._complete_equal_encoder_g3_context(
            "g3-persisted-pins-tamper"
        )
        rows = self._receipt_rows()
        rows.pop()
        pins = next(row for row in rows if row.get("step_kind") == "transport_pins")
        pins["step_evidence"]["registry_after_sha256"] = "f" * 64
        self._write_receipt_rows(rows)

        from floati.fleet_update import commit_fleet_update_g3

        with self.assertRaises(IntegrityFailure) as caught:
            commit_fleet_update_g3(
                plan=plan, root=self.root, actor=self.actor,
                idempotency_key="g3-persisted-pins-tamper",
            )
        self.assertEqual("fleet_update_step_invalid", caught.exception.code)
        self.assertEqual([], [
            row for row in self._receipt_rows()
            if row.get("kind") == "fleet_update_completed"
            and row.get("idempotency_key") == "g3-persisted-pins-tamper"
        ])

    def test_g3_completed_retry_rejects_later_malformed_sibling_before_return(self) -> None:
        """Catches the completed retry branch returning before root-history validation."""

        from floati.fleet_update import commit_fleet_update_g3

        key = "g3-completed-later-sibling"
        plan, _ledger, completed = self._complete_equal_encoder_g3_context(key)
        corrupt = self.root.path / "receipts" / "fleet-update" / "zz-corrupt.jsonl"
        corrupt.write_text("{not json}\n", encoding="utf-8")
        before = _tree_bytes(self.base)
        receipt_before = self._receipt_path().read_bytes()

        with self.assertRaises(IntegrityFailure) as caught:
            commit_fleet_update_g3(
                plan=plan, root=self.root, actor=self.actor, idempotency_key=key,
            )

        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)
        self.assertEqual(completed, self._receipt_rows()[-1])
        self.assertEqual(receipt_before, self._receipt_path().read_bytes())
        self.assertEqual(before, _tree_bytes(self.base))

    def test_g3_terminal_projection_tampering_refuses_history_and_exact_retry(self) -> None:
        """Every terminal field is a canonical plan/pins projection, never shape-only."""

        fields = {
            "previous_source_sha": "c" * 40,
            "target_source_sha": "d" * 40,
            "epoch_roll_state": "completed",
            "registry_before_sha256": "e" * 64,
            "registry_after_sha256": "f" * 64,
        }
        plan, ledger, _completed = self._complete_equal_encoder_g3_context(
            "g3-terminal-projection-tamper"
        )
        pristine = self._receipt_rows()
        for field, wrong in fields.items():
            with self.subTest(field=field):
                rows = json.loads(json.dumps(pristine))
                rows[-1][field] = wrong
                self._write_receipt_rows(rows)
                with self.assertRaises(IntegrityFailure):
                    ledger.rows(self.actor)
                from floati.fleet_update import commit_fleet_update_g3
                with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
                    commit_fleet_update_g3(
                        plan=plan, root=self.root, actor=self.actor,
                        idempotency_key="g3-terminal-projection-tamper",
                    )
        self._write_receipt_rows(pristine)

    def test_g3_terminal_projection_types_are_typed_refusals_everywhere(self) -> None:
        """Terminal summary/evidence type substitutions never escape as TypeError."""

        _plan, ledger, _completed = self._complete_equal_encoder_g3_context(
            "g3-terminal-type-refusals"
        )
        pristine = self._receipt_rows()
        schema = REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json"
        substitutions = (None, 7, {}, [])
        fields = (
            "previous_source_sha", "target_source_sha", "epoch_roll_state",
            "registry_before_sha256", "registry_after_sha256",
        )
        for surface in ("step_evidence", "completion"):
            for field in fields:
                for value in substitutions:
                    with self.subTest(surface=surface, field=field, value=type(value).__name__):
                        rows = json.loads(json.dumps(pristine))
                        pins = next(
                            row for row in rows
                            if row.get("step_kind") == "transport_pins"
                        )
                        record = pins if surface == "step_evidence" else rows[-1]
                        target = (
                            record["step_evidence"]
                            if surface == "step_evidence" else record
                        )
                        target[field] = value
                        with self.assertRaises(SchemaValidationError):
                            validate_json_schema(record, schema)
                        failures: list[str] = []
                        try:
                            validate_record(
                                record, self.root.tenant_id,
                                frozenset({str(record["kind"])}), integrity=False,
                            )
                        except TypeError as exc:
                            failures.append(f"runtime leaked TypeError: {exc}")
                        except ProtocolRefusal:
                            pass
                        else:
                            failures.append("runtime accepted malformed value")
                        self._write_receipt_rows(rows)
                        try:
                            ledger.rows(self.actor)
                        except TypeError as exc:
                            failures.append(f"history leaked TypeError: {exc}")
                        except IntegrityFailure:
                            pass
                        else:
                            failures.append("history accepted malformed value")
                        if failures:
                            self.fail(
                                f"{surface}.{field} " + "; ".join(failures)
                            )
        self._write_receipt_rows(pristine)

    def test_g3_schema_runtime_close_every_discriminator_branch(self) -> None:
        """Draft-2020-12 and runtime reject the same crossed G3 vocabulary."""

        _plan, _ledger, completed = self._complete_equal_encoder_g3_context(
            "g3-schema-runtime"
        )
        rows = self._receipt_rows()
        schema = REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json"
        pins = next(row for row in rows if row.get("step_kind") == "transport_pins")
        malformed_transport = json.loads(json.dumps(pins))
        malformed_transport["step_coordinate"]["transport"] = "BAD TRANSPORT"
        malformed_transport["step_evidence"]["transport"] = "BAD TRANSPORT"
        for record in (malformed_transport,):
            with self.assertRaises(SchemaValidationError):
                validate_json_schema(record, schema)
            with self.assertRaises(ProtocolRefusal):
                validate_record(record, self.root.tenant_id, frozenset({"fleet_update_step"}), integrity=False)
        branches = [
            next(row for row in rows if row["kind"] == "fleet_update_started"),
            next(row for row in rows if row.get("step_kind") == "shared_install"),
            next(row for row in rows if row.get("step_kind") == "waiter_binding"),
            pins,
        ]
        epoch = json.loads(json.dumps(pins))
        epoch["step_kind"] = "epoch_roll"
        epoch["step_coordinate"] = {"kind": "epoch_roll"}
        epoch["step_evidence"] = {"kind": "epoch_roll"}
        branches.append(epoch)
        for record in branches:
            with self.subTest(kind=record.get("step_kind", record["kind"])):
                hostile = json.loads(json.dumps(record))
                hostile["previous_source_sha"] = completed["previous_source_sha"]
                hostile["target_source_sha"] = completed["target_source_sha"]
                hostile["epoch_roll_state"] = completed["epoch_roll_state"]
                hostile["registry_before_sha256"] = completed["registry_before_sha256"]
                hostile["registry_after_sha256"] = completed["registry_after_sha256"]
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(hostile, schema)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        hostile, self.root.tenant_id,
                        frozenset({str(hostile["kind"])}), integrity=False,
                    )

    def _complete_equal_encoder_g3(self, key: str) -> dict[str, object]:
        return self._complete_equal_encoder_g3_context(key)[2]

    def _complete_equal_encoder_g3_context(
        self, key: str,
    ) -> tuple[dict[str, object], object, dict[str, object]]:
        from floati.fleet_update import commit_fleet_update_g3, preview_fleet_update
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        plan, ledger = self._equal_encoder_g3_frontier(key)
        completed = commit_fleet_update_g3(
            plan=plan, root=self.root, actor=self.actor, idempotency_key=key,
        )
        return plan, ledger, completed

    def _equal_encoder_g3_frontier(
        self, key: str,
    ) -> tuple[dict[str, object], object]:
        from floati.fleet_update import preview_fleet_update
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self.target_source.joinpath("floati", "records.py").write_bytes(b"current encoder\n")
        self._write_manifest(self.target_source, "b" * 40)
        plan = preview_fleet_update(root=self.root, actor=self.actor, destination=self.destination, target_source=self.target_source, target_source_sha="b" * 40, channel=self.channel, version=self.version, binding_path=self.binding_path, transport_registry=self.registry_path, transport_name=self.transport)
        self._consent(); ledger = FleetUpdateReceiptLedger(self.root); started = ledger.start(plan, self.actor, key); predecessor = str(started["id"])
        for _ in range(1 + len(plan["waiter_bindings"])):
            step = self._append_observed_step(ledger, plan, self.actor, key, predecessor); predecessor = str(step["id"])
        return plan, ledger

    def _receipt_path(self) -> Path:
        return self.root.path / "receipts/fleet-update" / f"{self.actor}.jsonl"

    def _receipt_rows(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self._receipt_path().read_text(encoding="utf-8").splitlines()
        ]

    def _write_receipt_rows(self, rows: list[dict[str, object]]) -> None:
        self._receipt_path().write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )
