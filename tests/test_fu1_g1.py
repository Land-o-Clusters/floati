"""G1 consent-joined, append-only fleet-update receipt contracts."""

from __future__ import annotations

import hashlib
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.registry import REGISTRY_KINDS, Registry
from floati.registry import utc_now
from floati.records import validate_record
from floati.update_consent import UpdateConsentLedger
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.test_fu1_g0 import FU1G0Tests, REPOSITORY_ROOT, _tree_bytes


class FU1G1Tests(FU1G0Tests):
    """The production change that must fail these tests is omitting G1 receipt state."""

    def setUp(self) -> None:
        super().setUp()
        self.transport = "floati-installed-v0"
        trust = self.destination / "trust"
        trust.mkdir()
        key = trust / "release.pub"
        key.write_text("public test key\n", encoding="utf-8")
        (trust / "keys.json").write_text(
            json.dumps({"format": "floati-trust-keys.v1", "keys": [{
                "key_id": "release", "public_key_file": "release.pub",
                "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2030-01-01T00:00:00Z",
                "status": "active", "transition": "initial",
            }]}, sort_keys=True),
            encoding="utf-8",
        )

    def _consent(self) -> dict[str, object]:
        return UpdateConsentLedger(self.destination).consent(
            channel=self.channel, epoch=1, idempotency_key="fu1-g1-consent"
        )

    def _installed_metadata_path(self) -> Path:
        return self.destination / ".floati-install" / "manifest.v0.json"

    def _downgrade_installed_metadata_to_v0(self) -> dict[str, object]:
        """Create the legal legacy testimony whose writer target adds ownership."""

        path = self._installed_metadata_path()
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["schema_version"] = 0
        metadata.pop("ownership")
        path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry["transports"][self.transport]["manifest_sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        self.registry_path.write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return metadata

    def _reader_row(self) -> dict[str, object]:
        return {
            "reader": "codex_fleet_bus_gateway",
            "surface": "install_manifest",
            "registry": str(self.registry_path),
            "transport": self.transport,
            "manifest_path": str(self._installed_metadata_path()),
            "current_schema_version": 0,
            "target_schema_version": 1,
            "added_fields": ["ownership"],
            "removed_fields": [],
            "change": "additive_widened",
            "compatibility_after_update": "not_observed",
            "remedy": "review the Codex fleet gateway reader before applying the widened manifest vocabulary",
        }

    @staticmethod
    def _hand_batch_digest(
        readers: list[object], consequences: list[object], exclusions: list[object]
    ) -> str:
        payload = json.dumps(
            {
                "reader_consequences": readers,
                "seat_binding_consequences": consequences,
                "seat_exclusions": exclusions,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _canonical_step_kinds(plan: dict[str, object]) -> list[str]:
        kinds = ["shared_install"]
        kinds.extend("waiter_binding" for _ in plan["waiter_bindings"])
        if plan["requires_epoch_roll"]:
            kinds.append("epoch_roll")
        kinds.append("transport_pins")
        return kinds

    def _synthetic_step_evidence(
        self,
        plan: dict[str, object],
        actor: str,
        key: str,
        spec: dict[str, object],
    ) -> dict[str, object]:
        """Build only schema-valid evidence for the plan-derived test step."""

        kind = str(spec["step_kind"])
        if kind == "shared_install":
            from floati.fleet_update import _shared_install_join_id
            from floati.wiring_journal import journal_path

            intents = list(plan["shared_install_intents"])
            entry_hashes = [
                hashlib.sha256(
                    json.dumps(
                        intent,
                        ensure_ascii=True,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                ).hexdigest()
                for intent in intents
            ]
            return {
                "kind": kind,
                "journal_path": str(
                    journal_path(Path(str(plan["inputs"]["destination"])))
                ),
                "join_id": _shared_install_join_id(
                    str(plan["plan_digest"]), actor, key
                ),
                "predecessor_ordinal": None,
                "predecessor_entry_hash": None,
                "first_ordinal": 1,
                "last_ordinal": len(entry_hashes),
                "entry_hashes": entry_hashes,
            }
        if kind == "waiter_binding":
            coordinate = spec["step_coordinate"]
            consequence = next(
                (
                    row
                    for row in plan["seat_binding_consequences"]
                    if row["configuration"] == coordinate["configuration"]
                    and row["store"] == coordinate["store"]
                ),
                None,
            )
            if consequence is None:
                from floati.codex_hook_install import plan_waiter_rebind
                from floati.codex_hook_trust import observe_codex_waiter_hooks

                staged = plan_waiter_rebind(
                    Path(str(coordinate["configuration"])),
                    Path(str(coordinate["store"])),
                    str(plan["target_waiter_digest"]),
                )
                trust_rows = observe_codex_waiter_hooks(
                    Path(str(coordinate["configuration"]))
                )
                self.assertEqual(1, len(trust_rows))
                trust_key = str(trust_rows[0]["hook_trust_key"])
                target_hash = str(staged["target_hook_hash"])
            else:
                trust_key = str(consequence["hook_trust_key"])
                target_hash = str(consequence["target_hook_hash"])
                trusted_hash = consequence["observed_trusted_hash"]
                enabled = consequence["observed_enabled"]
            if consequence is None:
                trusted_hash = trust_rows[0]["hook_trust_observed_hash"]
                enabled = trust_rows[0]["hook_enabled"]
            return {
                "kind": kind,
                "hook_post_observation": {
                    "hook_trust_key": trust_key,
                    "current_hook_hash": target_hash,
                    "observed_trusted_hash": trusted_hash,
                    "observed_enabled": enabled,
                },
            }
        self.assertIn(kind, {"epoch_roll", "transport_pins"})
        if kind == "transport_pins":
            registry = str(plan["inputs"]["transport_registry"])
            return {
                "kind": kind,
                "registry": registry,
                "transport": str(plan["inputs"]["transport"]),
                "registry_before_sha256": str(plan["transport_registry_sha256"]),
                "registry_after_sha256": str(plan["target_transport_registry_sha256"]),
                "previous_source_sha": str(plan["current_source_sha"]),
                "target_source_sha": str(plan["target_source_sha"]),
                "epoch_roll_state": (
                    "not_required"
                    if plan["requires_epoch_roll"] is False
                    else "completed"
                ),
            }
        return {"kind": kind}

    def _append_observed_step(
        self,
        ledger: object,
        plan: dict[str, object],
        actor: str,
        key: str,
        predecessor: str,
    ) -> dict[str, object]:
        """Exercise prepare+commit while replacing physical I/O in G1 fixtures."""

        existing = any(
            row.get("kind") == "fleet_update_step"
            and row.get("predecessor_receipt_id") == predecessor
            and row.get("idempotency_key") == key
            for row in ledger.rows(actor)
        )
        calls = 0

        def observe(
            observed_plan: dict[str, object],
            observed_actor: str,
            observed_key: str,
            spec: dict[str, object],
        ) -> dict[str, object]:
            nonlocal calls
            calls += 1
            evidence = self._synthetic_step_evidence(
                observed_plan, observed_actor, observed_key, spec
            )
            if existing or calls > 1:
                phase = (
                    "unchanged"
                    if spec["pre_digest"] == spec["post_digest"]
                    else "post"
                )
                return {"phase": phase, "evidence": evidence}
            phase = (
                "unchanged"
                if spec["pre_digest"] == spec["post_digest"]
                and spec["step_kind"] != "shared_install"
                else "pre"
            )
            return {"phase": phase, "evidence": None}

        with mock.patch.object(ledger, "_observe_step", side_effect=observe):
            ledger.prepare_step(plan, actor, key, predecessor)
            return ledger.step(plan, actor, key, predecessor)

    def _append_canonical_steps(
        self,
        ledger: object,
        plan: dict[str, object],
        actor: str,
        key: str,
        predecessor: str,
    ) -> list[dict[str, object]]:
        steps = []
        for ordinal, kind in enumerate(self._canonical_step_kinds(plan), start=1):
            try:
                step = self._append_observed_step(
                    ledger, plan, actor, key, predecessor
                )
            except ProtocolRefusal as exc:
                self.fail(f"canonical {kind} step {ordinal} refused: {exc.code}")
            self.assertEqual(kind, step["step_kind"])
            steps.append(step)
            predecessor = str(step["id"])
        return steps

    def _rewrite_target_manifest_digest(self, relative: str) -> None:
        manifest_path = self.target_source / "bundle-manifest.v0.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for row in manifest["files"]:
            if row["path"] == relative:
                row["sha256"] = hashlib.sha256(
                    (self.target_source / relative).read_bytes()
                ).hexdigest()
                break
        else:
            self.fail(f"target manifest omits {relative}")
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8"
        )

    @staticmethod
    def _with_waiter_count(plan: dict[str, object], count: int) -> dict[str, object]:
        changed = json.loads(json.dumps(plan))
        bindings = list(changed["waiter_bindings"])
        while len(bindings) < count:
            bindings.append(dict(bindings[-1]))
        changed["waiter_bindings"] = bindings[:count]
        changed["moves"][2]["count"] = count
        digest_input = {
            key: value
            for key, value in changed.items()
            if key not in {"plan_digest", "apply_argv"}
        }
        changed["plan_digest"] = hashlib.sha256(
            json.dumps(
                digest_input,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        return changed

    @staticmethod
    def _with_inputs(plan: dict[str, object], **inputs: object) -> dict[str, object]:
        """Re-authenticate a direct receipt fixture after an intentional input change."""

        changed = json.loads(json.dumps(plan))
        changed["inputs"].update(inputs)
        body = {key: value for key, value in changed.items() if key not in {"plan_digest", "apply_argv"}}
        changed["plan_digest"] = hashlib.sha256(json.dumps(
            body, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("ascii")).hexdigest()
        return changed

    def _map(self, entries: list[tuple[Path, str]]) -> None:
        path = self.root.path / "codex-wait" / "workspaces.v0.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"schema_version": 0, "tenant_id": self.root.tenant_id, "mappings": [
            {"workspace": str(workspace), "node_id": node} for workspace, node in sorted(entries)
        ]}, sort_keys=True), encoding="utf-8")

    def _binding(self, ordinal: int, *, root: Path | None = None) -> tuple[Path, Path]:
        store = self.base / f"waiters-{ordinal}"
        digest = (str(ordinal) * 64)[:64]
        tree = store / digest
        launcher = tree / "scripts" / "floati-codex-wait"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        (tree / "floati").mkdir()
        (tree / "floati" / "codex_wait.py").write_text("WAIT = 1\n", encoding="utf-8")
        for relative in ("LICENSE", "floati/events.py", "floati/records.py"):
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.target_source / relative).read_bytes())
        hooks = self.base / f"hooks-{ordinal}.json"
        hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"/usr/bin/python3 {launcher} --root {root or self.root.path}", "timeout": 900, "statusMessage": "Watching Floati bus"}]}]}}, sort_keys=True), encoding="utf-8")
        return hooks, store

    def _two_by_two(self) -> tuple[Path, Path]:
        registry = Registry(self.root)
        registry.register("seat-a", "Codex")
        registry.register("seat-b", "codex")
        first, second = self.base / "workspace-a", self.base / "workspace-b"
        first.mkdir(); second.mkdir()
        self._map([(first, "seat-a"), (second, "seat-b")])
        first_binding = self._binding(1)
        second_binding = self._binding(2)
        self.binding_path.write_text(json.dumps({"schema_version": 0, "bindings": [
            {"kind": "codex_stop_hook", "configuration": str(first_binding[0]), "store": str(first_binding[1])},
            {"kind": "codex_stop_hook", "configuration": str(second_binding[0]), "store": str(second_binding[1])},
        ]}, sort_keys=True), encoding="utf-8")
        return first, second

    def test_g1_00_shared_runtime_digest_names_complete_inventory(self) -> None:
        """Catches planner/installer divergence or an abbreviated waiter inventory."""

        from floati.waiter_bundle import waiter_runtime_digest, waiter_runtime_files

        paths = waiter_runtime_files(REPOSITORY_ROOT)
        relative = {path.relative_to(REPOSITORY_ROOT).as_posix() for path in paths}
        self.assertIn("LICENSE", relative)
        self.assertIn("scripts/floati-codex-wait", relative)
        self.assertIn("floati/fleet_update.py", relative)
        self.assertIn("schemas/v1/codex-wait-consent-receipt.schema.json", relative)
        hand = hashlib.sha256()
        for path in paths:
            hand.update(path.relative_to(REPOSITORY_ROOT).as_posix().encode("utf-8") + b"\0")
            hand.update(hashlib.sha256(path.read_bytes()).digest())
        self.assertEqual(hand.hexdigest(), waiter_runtime_digest(REPOSITORY_ROOT))

    def test_g1_01_active_consent_starts_one_exact_receipt_and_retry_projects_it(self) -> None:
        """Catches apply creating a second start or re-checking consent on an exact retry."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        consent = self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        first = ledger.start(plan, self.actor, "fu1-g1-exact")
        second = ledger.start(plan, self.actor, "fu1-g1-exact")

        self.assertEqual(first, second)
        self.assertEqual("fleet_update_started", first["kind"])
        self.assertEqual(str(plan["plan_digest"]), first["plan_digest"])
        self.assertEqual(consent["id"], first["consent_receipt_id"])
        self.assertEqual([], first["seat_binding_consequences"])
        self.assertEqual([], first["seat_exclusions"])
        rows = ledger.rows(self.actor)
        self.assertEqual([first["id"]], [row["id"] for row in rows])

    def test_g1_02_missing_or_wrong_channel_consent_creates_no_receipt(self) -> None:
        """Catches a valid plan reaching receipt creation before its AU-1 consent join."""

        plan = self._preview()
        before = _tree_bytes(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._apply(str(plan["plan_digest"]), "fu1-g1-no-consent")
        self.assertEqual("update_consent_missing", caught.exception.code)
        self.assertEqual(before, _tree_bytes(self.base))

        self._consent()
        self.channel = "https://updates.example.invalid/other-channel"
        wrong = self._preview()
        with self.assertRaises(ProtocolRefusal) as caught:
            self._apply(str(wrong["plan_digest"]), "fu1-g1-wrong-channel")
        self.assertEqual("update_consent_mismatch", caught.exception.code)

    def test_g1_02a_revoked_consent_creates_no_receipt(self) -> None:
        """Catches revocation becoming advisory after local observation but before receipt append."""

        consent = self._consent()
        UpdateConsentLedger(self.destination).revoke(channel=self.channel, idempotency_key="fu1-g1-revoke")
        plan = self._preview(); before = _tree_bytes(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._apply(str(plan["plan_digest"]), "fu1-g1-revoked")
        self.assertEqual("update_consent_revoked", caught.exception.code)
        self.assertEqual(before, _tree_bytes(self.base))
        self.assertIsInstance(consent["id"], str)

    def test_g1_02b_exact_apply_retry_projects_started_receipt_without_new_consent(self) -> None:
        """Catches a response-loss retry re-authorizing or appending a second start."""

        consent = self._consent(); plan = self._preview()
        first = self._apply(str(plan["plan_digest"]), "fu1-g1-apply-retry")
        UpdateConsentLedger(self.destination).revoke(channel=self.channel, idempotency_key="fu1-g1-apply-retry-revoke")
        second = self._apply(str(plan["plan_digest"]), "fu1-g1-apply-retry")
        self.assertEqual(first, second)
        self.assertEqual(consent["id"], first["consent_receipt_id"])
        receipt_path = self.root.path / "receipts" / "fleet-update" / f"{self.actor}.jsonl"
        self.assertEqual(1, len(receipt_path.read_text(encoding="utf-8").splitlines()))

    def test_g1_fix1_ledger_start_joins_authoritative_consent_not_caller_data(self) -> None:
        """Catches receipt starts that can be minted from fabricated consent testimony."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.start(plan, self.actor, "fix1-missing")
        self.assertEqual("update_consent_missing", caught.exception.code)
        self._consent()
        started = ledger.start(plan, self.actor, "fix1-active")
        self.assertTrue(str(started["consent_receipt_id"]).startswith("update-consent-"))
        with self.assertRaises(TypeError):
            ledger.start(plan, self.actor, "fix1-forged", {"id": "update-consent-forged"})
        wrong = self._with_inputs(plan, channel="https://updates.example.invalid/wrong")
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.start(wrong, self.actor, "fix1-wrong")
        self.assertEqual("update_consent_mismatch", caught.exception.code)
        UpdateConsentLedger(self.destination).revoke(channel=self.channel, idempotency_key="fix1-revoke")
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.start(plan, self.actor, "fix1-revoked")
        self.assertEqual("update_consent_revoked", caught.exception.code)

    def test_g1_fix2_terminal_rows_are_schema_valid_and_history_is_start_bound(self) -> None:
        """Catches self-consistent receipt tampering outside the start's exact owner-review batch."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent(); plan = self._preview(); ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "fix2")
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, "fix2", started["id"]
        )
        completed = ledger.complete(plan, self.actor, "fix2", steps[-1]["id"])
        validate_record(completed, self.root.tenant_id, frozenset({"fleet_update_completed"}), integrity=False)
        validate_json_schema(completed, REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json")
        hostile = dict(steps[0], consent_receipt_id="update-consent-" + uuid7_hex())
        receipt = self.root.path / "receipts/fleet-update" / f"{self.actor}.jsonl"
        receipt.write_text("\n".join(json.dumps(row, sort_keys=True) for row in (started, hostile, *steps[1:], completed)) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as caught:
            ledger.rows(self.actor)
        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)
        malformed = dict(completed, extra=True)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(malformed, REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(dict(steps[0], moves=[]), REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json")

    def test_g1_fix3_bindings_validate_before_empty_or_excluded_workspace_maps(self) -> None:
        """Catches an invalid explicit hook becoming invisible when no active Codex mapping survives."""

        foreign = self.base / "foreign"; foreign.mkdir()
        hooks, store = self._binding(6, root=foreign)
        self.binding_path.write_text(json.dumps({"schema_version": 0, "bindings": [{"kind": "codex_stop_hook", "configuration": str(hooks), "store": str(store)}]}), encoding="utf-8")
        for mappings in ([], [(self.base / "retired-workspace", "retired")], [(self.base / "claude-workspace", "claude")]):
            with self.subTest(mappings=mappings):
                if mappings:
                    mappings[0][0].mkdir(exist_ok=True)
                    Registry(self.root).register(mappings[0][1], "Codex" if mappings[0][1] == "retired" else "Claude")
                    if mappings[0][1] == "retired":
                        Registry(self.root).retire("retired")
                self._map(mappings)
                with self.assertRaises(ProtocolRefusal) as caught:
                    self._preview()
                self.assertEqual("fleet_update_waiter_binding_invalid", caught.exception.code)

    def test_g1_fix4_apply_orders_lexical_then_observation_then_consent_then_drift(self) -> None:
        """Catches malformed argv reaching local reads or stale plans outranking revoked consent."""

        malformed = self.target_source / "bundle-manifest.v0.json"
        original_manifest = malformed.read_bytes()
        malformed.write_text("not json", encoding="utf-8")
        before = _tree_bytes(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._apply("not-a-digest", "fix4-key")
        self.assertEqual("fleet_update_plan_drift", caught.exception.code)
        self.assertEqual(before, _tree_bytes(self.base))

        malformed.write_bytes(original_manifest)
        plan = self._preview(); self._consent()
        hooks = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 901
        self.hooks_path.write_text(json.dumps(hooks, sort_keys=True), encoding="utf-8")
        UpdateConsentLedger(self.destination).revoke(channel=self.channel, idempotency_key="fix4-revoke")
        with self.assertRaises(ProtocolRefusal) as caught:
            self._apply(str(plan["plan_digest"]), "fix4-first")
        self.assertEqual("update_consent_revoked", caught.exception.code)

    def test_g1_fix4a_preview_rejects_every_lexical_input_before_destination_observation(self) -> None:
        """Catches any late scalar/path validation being masked by an unreadable destination."""

        from floati.fleet_update import preview_fleet_update

        inputs = dict(root=self.root, actor=self.actor, destination=self.base / "missing-destination", target_source=self.target_source, target_source_sha="b" * 40, channel=self.channel, version=self.version, binding_path=self.binding_path, transport_registry=self.registry_path, transport_name="floati-installed-v0")
        variants = (({"target_source_sha": "BAD"}, "fleet_update_source_sha_invalid"), ({"channel": "http://not-https"}, "update_channel_invalid"), ({"version": "bad value"}, "fleet_update_version_invalid"), ({"transport_name": "BAD TRANSPORT"}, "transport_invalid"), ({"binding_path": Path("relative-binding")}, "fleet_update_binding_invalid"), ({"transport_registry": Path("relative-registry")}, "fleet_update_transport_registry_invalid"))
        for changed, code in variants:
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    preview_fleet_update(**dict(inputs, **changed))
                self.assertEqual(code, caught.exception.code)

    def test_g1_fix4b_exact_retry_requires_the_supplied_fresh_digest(self) -> None:
        """Catches a valid but wrong digest returning an existing receipt without reauthorization."""

        self._consent(); plan = self._preview()
        first = self._apply(str(plan["plan_digest"]), "fix4b")
        UpdateConsentLedger(self.destination).revoke(channel=self.channel, idempotency_key="fix4b-revoke")
        self.assertEqual(first, self._apply(str(plan["plan_digest"]), "fix4b"))
        with self.assertRaises(ProtocolRefusal) as caught:
            self._apply("c" * 64, "fix4b")
        self.assertEqual("fleet_update_plan_drift", caught.exception.code)

    def test_g1_fix5_completion_is_terminal_and_exactly_idempotent(self) -> None:
        """Catches later physical rows reopening a completed fleet-update saga."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent(); plan = self._preview(); ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "fix5")
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, "fix5", started["id"]
        )
        completed = ledger.complete(plan, self.actor, "fix5", steps[-1]["id"])
        self.assertEqual(completed, ledger.complete(plan, self.actor, "fix5", steps[-1]["id"]))
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.complete(plan, self.actor, "fix5", started["id"])
        self.assertEqual("fleet_update_idempotency_conflict", caught.exception.code)
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.prepare_step(plan, self.actor, "fix5", completed["id"])
        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)
        trailing = dict(steps[0], id="fleet-update-step-" + uuid7_hex(), predecessor_receipt_id=completed["id"])
        receipt = self.root.path / "receipts/fleet-update" / f"{self.actor}.jsonl"
        receipt.write_text(receipt.read_text(encoding="utf-8") + json.dumps(trailing, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as caught:
            ledger.rows(self.actor)
        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)

    def test_g1_fix_round2_exact_step_retry_uses_its_physical_coordinate(self) -> None:
        """Catches response-loss retry depending on the saga's current last row."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "round2-step-retry")
        shared = self._append_observed_step(
            ledger, plan, self.actor, "round2-step-retry", started["id"]
        )
        self._append_observed_step(
            ledger, plan, self.actor, "round2-step-retry", shared["id"]
        )
        try:
            retried = self._append_observed_step(
                ledger, plan, self.actor, "round2-step-retry", started["id"]
            )
        except ProtocolRefusal as exc:
            self.fail(f"exact physical step retry refused: {exc.code}")
        self.assertEqual(shared, retried)
        self.assertEqual(plan["current_manifest_sha256"], shared["pre_digest"])
        self.assertEqual(plan["target_manifest_sha256"], shared["post_digest"])
        divergent = dict(shared["step_evidence"])
        divergent["entry_hashes"] = ["d" * 64] * len(divergent["entry_hashes"])
        with mock.patch.object(
            ledger,
            "_observe_step",
            return_value={"phase": "post", "evidence": divergent},
        ):
            with self.assertRaises(IntegrityFailure) as caught:
                ledger.prepare_step(
                    plan, self.actor, "round2-step-retry", started["id"]
                )
        self.assertEqual("fleet_update_step_invalid", caught.exception.code)

    def test_g1_fix_round2_multiple_waiter_steps_are_predecessor_distinct(self) -> None:
        """Catches globally collapsing repeated waiter moves by step kind alone."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        self._two_by_two()
        plan = self._preview()
        self.assertEqual(2, len(plan["waiter_bindings"]))
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "round2-multi-waiter")
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, "round2-multi-waiter", started["id"]
        )
        self.assertEqual(
            [
                "shared_install",
                "waiter_binding",
                "waiter_binding",
                "epoch_roll",
                "transport_pins",
            ],
            [step["step_kind"] for step in steps],
        )
        self.assertEqual(len(steps), len({step["id"] for step in steps}))
        completed = ledger.complete(
            plan, self.actor, "round2-multi-waiter", steps[-1]["id"]
        )
        self.assertEqual([step["id"] for step in steps], completed["step_receipt_ids"])

    def test_g1_fix_round2_completion_requires_the_exact_step_sequence(self) -> None:
        """Catches terminal testimony after zero, partial, or out-of-order work."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)

        started = ledger.start(plan, self.actor, "round2-empty")
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.complete(plan, self.actor, "round2-empty", started["id"])
        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)
        empty_steps = self._append_canonical_steps(
            ledger, plan, self.actor, "round2-empty", started["id"]
        )
        ledger.complete(
            plan, self.actor, "round2-empty", empty_steps[-1]["id"]
        )

        started = ledger.start(plan, self.actor, "round2-partial")
        shared = self._append_observed_step(
            ledger, plan, self.actor, "round2-partial", started["id"]
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.complete(plan, self.actor, "round2-partial", shared["id"])
        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)
        predecessor = str(shared["id"])
        remaining = []
        for _kind in self._canonical_step_kinds(plan)[1:]:
            step = self._append_observed_step(
                ledger, plan, self.actor, "round2-partial", predecessor
            )
            remaining.append(step)
            predecessor = str(step["id"])
        ledger.complete(
            plan, self.actor, "round2-partial", remaining[-1]["id"]
        )

        started = ledger.start(plan, self.actor, "round2-out-of-order")
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.step(
                plan, self.actor, "round2-out-of-order", None
            )
        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)

    def test_g1_fix_round2_epoch_step_is_exactly_conditional(self) -> None:
        """Catches adding an epoch step when current and target encoders already agree."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        (self.target_source / "floati" / "records.py").write_bytes(
            (self.destination / "floati" / "records.py").read_bytes()
        )
        self._rewrite_target_manifest_digest("floati/records.py")
        self._consent()
        plan = self._preview()
        self.assertFalse(plan["requires_epoch_roll"])
        self.assertNotIn("epoch_roll", self._canonical_step_kinds(plan))
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "round2-no-epoch")
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, "round2-no-epoch", started["id"]
        )
        completed = ledger.complete(
            plan, self.actor, "round2-no-epoch", steps[-1]["id"]
        )
        self.assertEqual([step["id"] for step in steps], completed["step_receipt_ids"])

        started = ledger.start(plan, self.actor, "round2-extra-epoch")
        shared = self._append_observed_step(
            ledger, plan, self.actor, "round2-extra-epoch", started["id"]
        )
        waiter = self._append_observed_step(
            ledger, plan, self.actor, "round2-extra-epoch", shared["id"]
        )
        pins = self._append_observed_step(
            ledger, plan, self.actor, "round2-extra-epoch", waiter["id"]
        )
        self.assertEqual("transport_pins", pins["step_kind"])
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.prepare_step(
                plan, self.actor, "round2-extra-epoch", pins["id"]
            )
        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)

    def test_g1_fix_round2_target_manifest_inventory_is_contained_and_canonical(self) -> None:
        """Catches target entries escaping or ambiguously naming the staged checkout."""

        manifest_path = self.target_source / "bundle-manifest.v0.json"
        baseline = json.loads(manifest_path.read_text(encoding="utf-8"))
        outside = self.base / "outside-target.txt"
        outside.write_text("outside\n", encoding="utf-8")
        absolute = self.base / "absolute-target.txt"
        absolute.write_text("absolute\n", encoding="utf-8")
        symlink_target = self.base / "symlink-target"
        symlink_target.mkdir()
        (symlink_target / "payload.txt").write_text("linked\n", encoding="utf-8")
        (self.target_source / "linked").symlink_to(symlink_target, target_is_directory=True)

        def clone() -> dict[str, object]:
            return json.loads(json.dumps(baseline))

        absolute_manifest = clone()
        absolute_manifest["files"].append(
            {
                "path": str(absolute),
                "sha256": hashlib.sha256(absolute.read_bytes()).hexdigest(),
            }
        )
        parent_manifest = clone()
        parent_manifest["files"].append(
            {
                "path": "../outside-target.txt",
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        )
        duplicate_manifest = clone()
        duplicate_manifest["files"].append(dict(duplicate_manifest["files"][0]))
        unsorted_manifest = clone()
        unsorted_manifest["files"] = list(reversed(unsorted_manifest["files"]))
        symlink_manifest = clone()
        symlink_manifest["files"].append(
            {
                "path": "linked/payload.txt",
                "sha256": hashlib.sha256(
                    (symlink_target / "payload.txt").read_bytes()
                ).hexdigest(),
            }
        )

        for label, manifest in (
            ("absolute", absolute_manifest),
            ("parent", parent_manifest),
            ("duplicate", duplicate_manifest),
            ("unsorted", unsorted_manifest),
            ("symlink-parent", symlink_manifest),
        ):
            with self.subTest(label=label):
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True), encoding="utf-8"
                )
                with self.assertRaises(ProtocolRefusal) as caught:
                    self._preview()
                self.assertEqual(
                    "fleet_update_target_manifest_invalid", caught.exception.code
                )
        self.assertFalse((self.root.path / "receipts" / "fleet-update").exists())

    def test_g1_fix_round2_nonfinite_owner_review_is_always_typed(self) -> None:
        """Catches canonical JSON failures escaping as raw ValueError exceptions."""

        from floati.fleet_update_receipts import (
            FleetUpdateReceiptLedger,
            owner_review_batch_digest,
        )

        bad_consequences = [{"node_id": "seat-a", "workspace": float("nan")}]
        try:
            owner_review_batch_digest([], bad_consequences, [])
        except ValueError as exc:
            self.fail(f"digest helper leaked raw ValueError: {exc}")
        except ProtocolRefusal as exc:
            self.assertEqual("fleet_update_owner_review_invalid", exc.code)
        else:
            self.fail("digest helper accepted nonfinite owner-review evidence")

        plan = self._preview()
        hostile_plan = json.loads(json.dumps(plan))
        hostile_plan["seat_binding_consequences"] = [
            {
                "node_id": "seat-a",
                "workspace": float("nan"),
                "configuration": "\x2ftmp/hooks.json",
            }
        ]
        ledger = FleetUpdateReceiptLedger(self.root)
        try:
            ledger.start(hostile_plan, self.actor, "round2-nan-plan")
        except ValueError as exc:
            self.fail(f"plan validation leaked raw ValueError: {exc}")
        except ProtocolRefusal as exc:
            self.assertEqual("fleet_update_plan_invalid", exc.code)
        else:
            self.fail("plan validation accepted nonfinite owner-review evidence")

        self._downgrade_installed_metadata_to_v0()
        self._consent()
        plan = self._preview()
        started = ledger.start(plan, self.actor, "round2-nan-row")
        hostile = json.loads(json.dumps(started))
        hostile["reader_consequences"][0]["current_schema_version"] = float("nan")
        receipt = self.root.path / "receipts" / "fleet-update" / f"{self.actor}.jsonl"
        receipt.write_text(json.dumps(hostile, sort_keys=True) + "\n", encoding="utf-8")
        try:
            ledger.rows(self.actor)
        except ValueError as exc:
            self.fail(f"ledger reader leaked raw ValueError: {exc}")
        except IntegrityFailure as exc:
            self.assertIn(
                exc.code,
                {"fleet_update_receipt_invalid", "fleet_update_owner_review_invalid"},
            )
        else:
            self.fail("ledger reader accepted nonfinite owner-review evidence")

    def test_g1_fix2a_history_key_is_unique_across_plan_digests(self) -> None:
        """Catches a tampered second start reusing one actor key under a new plan digest."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent(); plan = self._preview(); ledger = FleetUpdateReceiptLedger(self.root)
        start = ledger.start(plan, self.actor, "fix2a")
        forged = dict(start, id="fleet-update-started-" + uuid7_hex(), plan_digest="d" * 64, predecessor_receipt_id=start["id"])
        receipt = self.root.path / "receipts/fleet-update" / f"{self.actor}.jsonl"
        receipt.write_text(receipt.read_text(encoding="utf-8") + json.dumps(forged, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as caught:
            ledger.rows(self.actor)
        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)

    def test_g1_fix3_history_step_and_completion_digest_must_equal_the_start(self) -> None:
        """Catches a later row escaping its actor/key start by carrying another valid digest."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent(); plan = self._preview(); ledger = FleetUpdateReceiptLedger(self.root)
        start = ledger.start(plan, self.actor, "fix3-step")
        forged_spec = {
            "step_kind": "shared_install",
            "step_ordinal": 1,
            "pre_digest": plan["current_manifest_sha256"],
            "post_digest": plan["target_manifest_sha256"],
            "step_coordinate": {
                "kind": "shared_install",
                "destination": plan["inputs"]["destination"],
                "metadata_relative": ".floati-install/manifest.v0.json",
            },
        }
        forged_step = ledger._base(
            kind="fleet_update_step",
            plan_digest="d" * 64,
            actor=self.actor,
            key="fix3-step",
            consent={"id": start["consent_receipt_id"]},
            predecessor=start["id"],
            readers=list(plan["reader_consequences"]),
            consequences=list(plan["seat_binding_consequences"]),
            exclusions=list(plan["seat_exclusions"]),
            batch=plan["owner_review_batch_digest"],
            operation="step",
            step_kind="shared_install",
            pre_digest=forged_spec["pre_digest"],
            post_digest=forged_spec["post_digest"],
            state="completed",
            step_ordinal=1,
            step_coordinate=forged_spec["step_coordinate"],
            commit_disposition="applied",
            step_evidence=self._synthetic_step_evidence(
                plan, self.actor, "fix3-step", forged_spec
            ),
        )
        forged_step["tenant_id"] = self.root.tenant_id
        receipt = self.root.path / "receipts/fleet-update" / f"{self.actor}.jsonl"
        receipt.write_text(receipt.read_text(encoding="utf-8") + json.dumps(forged_step, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as caught:
            ledger.rows(self.actor)
        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)

        # The next independent hostile history needs a root with no active or
        # malformed sibling saga.
        receipt.unlink()

        other_actor = "worker-two"
        other_plan = self._with_inputs(plan, actor=other_actor)
        start = ledger.start(other_plan, other_actor, "fix3-complete")
        step = self._append_observed_step(
            ledger, other_plan, other_actor, "fix3-complete", start["id"]
        )
        forged_complete = ledger._base(kind="fleet_update_completed", plan_digest="d" * 64, actor=other_actor, key="fix3-complete", consent={"id": start["consent_receipt_id"]}, predecessor=step["id"], readers=list(other_plan["reader_consequences"]), consequences=list(other_plan["seat_binding_consequences"]), exclusions=list(other_plan["seat_exclusions"]), batch=other_plan["owner_review_batch_digest"], operation="complete", step_kind=None, pre_digest=None, post_digest=None, state="completed")
        forged_complete.update(tenant_id=self.root.tenant_id, step_receipt_ids=[step["id"]], moves=other_plan["moves"], unchanged=other_plan["unchanged"])
        receipt = self.root.path / "receipts/fleet-update" / f"{other_actor}.jsonl"
        receipt.write_text(receipt.read_text(encoding="utf-8") + json.dumps(forged_complete, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as caught:
            ledger.rows(other_actor)
        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)

    def test_g1_fix2b_record_and_schema_enforce_derived_owner_flags_and_completion_moves(self) -> None:
        """Catches self-consistent-looking derived flags, remedies, or move lists that disagree with inputs."""

        plan = self._preview()
        consequence = {"node_id": "seat-a", "workspace": "\x2ftmp/a", "harness": "Codex", "configuration": "\x2ftmp/hooks.json", "store": "\x2ftmp/store", "association_basis": "conservative_root_scope", "hook_trust_key": "stop", "current_hook_hash": "a" * 64, "target_hook_hash": "b" * 64, "observed_trusted_hash": "a" * 64, "observed_enabled": True, "current_waiter_digest": "c" * 64, "target_waiter_digest": "d" * 64, "trust_rotated_by_update": False, "review_required_after_update": True, "enable_required_after_update": False, "relaunch_required_after_update": True, "reachability_after_update": "unknown_until_review_and_relaunch", "remedy": "review and trust the exact Stop hook in Codex settings;relaunch the affected session"}
        from floati.fleet_update_receipts import owner_review_batch_digest
        with self.assertRaises(ProtocolRefusal) as caught:
            validate_record({"schema_version": 1, "id": "fleet-update-started-" + uuid7_hex(), "tenant_id": self.root.tenant_id, "timestamp": utc_now(), "kind": "fleet_update_started", "plan_digest": plan["plan_digest"], "recovery_witness": {key: value for key, value in plan.items() if key not in {"plan_digest", "apply_argv"}}, "actor": self.actor, "consent_receipt_id": "update-consent-" + uuid7_hex(), "operation": "start", "step_kind": None, "pre_digest": None, "post_digest": None, "step_ordinal": None, "step_coordinate": None, "commit_disposition": None, "step_evidence": None, "state": "started", "predecessor_receipt_id": None, "idempotency_key": "fix2b", "owner_review_batch_digest": owner_review_batch_digest([], [consequence], []), "reader_consequences": [], "seat_binding_consequences": [consequence], "seat_exclusions": []}, self.root.tenant_id, frozenset({"fleet_update_started"}), integrity=False)
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual("fleet update consequence flags are not derived from hook observation", caught.exception.detail)
        self._consent()
        from floati.fleet_update_receipts import FleetUpdateReceiptLedger
        ledger = FleetUpdateReceiptLedger(self.root); start = ledger.start(plan, self.actor, "fix2b")
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, "fix2b", start["id"]
        )
        complete = ledger.complete(plan, self.actor, "fix2b", steps[-1]["id"])
        malformed_move = dict(complete["moves"][0])
        malformed_move.pop("path")
        with self.assertRaises(ProtocolRefusal):
            validate_record(
                dict(complete, moves=[malformed_move]), self.root.tenant_id,
                frozenset({"fleet_update_completed"}), integrity=False,
            )
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(dict(start, operation="complete"), REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json")

    def test_g1_03_divergent_key_and_out_of_order_step_refuse(self) -> None:
        """Catches an idempotency collision or a saga step detached from physical history."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        consent = self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "fu1-g1-order")
        changed = dict(plan, plan_digest="c" * 64)
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.start(changed, self.actor, "fu1-g1-order")
        self.assertEqual("fleet_update_plan_invalid", caught.exception.code)
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.step(plan, self.actor, "fu1-g1-order", None)
        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)
        self.assertEqual([started["id"]], [row["id"] for row in ledger.rows(self.actor)])

    def test_g1_04_literal_owner_review_batch_digest(self) -> None:
        """Catches any non-canonical field, separator, Unicode, or array ordering in owner review binding."""

        from floati.fleet_update_receipts import owner_review_batch_digest

        readers = [{"reader": "codex_fleet_bus_gateway", "surface": "install_manifest"}]
        consequences = [{"node_id": "builder-a", "workspace": "\x2ftmp/a"}]
        exclusions = [{"node_id": "worker-b", "workspace": "\x2ftmp/b", "reason": "harness_not_codex"}]
        literal = b'{"reader_consequences":[{"reader":"codex_fleet_bus_gateway","surface":"install_manifest"}],"seat_binding_consequences":[{"node_id":"builder-a","workspace":"\x2ftmp/a"}],"seat_exclusions":[{"node_id":"worker-b","reason":"harness_not_codex","workspace":"\x2ftmp/b"}]}'
        self.assertEqual(
            hashlib.sha256(literal).hexdigest(),
            owner_review_batch_digest(readers, consequences, exclusions),
        )

    def test_g1_reader_v0_to_v1_names_gateway_without_compatibility_claim(self) -> None:
        """Catches schema widening that omits its reader or fabricates compatibility."""

        self._downgrade_installed_metadata_to_v0()
        plan = self._preview()
        self.assertIn("reader_consequences", plan, "plan omits reader_consequences")
        self.assertEqual([self._reader_row()], plan["reader_consequences"])

        target = json.loads(
            (self.target_source / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        expected_metadata = {
            "schema_version": 1,
            "source_ref": "HEAD",
            "source_sha": target["source_sha"],
            "files": target["files"],
            "ownership": {
                "kind": "unknown",
                "destination": str(self.destination),
                "entrypoint": "scripts/floati",
                "entrypoint_sha256": hashlib.sha256(
                    (self.target_source / "scripts" / "floati").read_bytes()
                ).hexdigest(),
                "manager": None,
                "remedy": "reinstall with the governed standalone installer",
            },
        }
        expected_bytes = (
            json.dumps(expected_metadata, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(expected_bytes).hexdigest(),
            plan["target_manifest_sha256"],
        )
        self.assertEqual([], plan["seat_binding_consequences"])
        self.assertNotIn(
            "gateway", " ".join(str(row.get("kind")) for row in plan["moves"])
        )

        without_reader = {
            key: value
            for key, value in plan.items()
            if key not in {"plan_digest", "apply_argv", "reader_consequences"}
        }
        without_reader["owner_review_batch_digest"] = self._hand_batch_digest(
            [],
            list(plan["seat_binding_consequences"]),
            list(plan["seat_exclusions"]),
        )
        without_reader_digest = hashlib.sha256(
            json.dumps(
                without_reader,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        self.assertNotEqual(without_reader_digest, plan["plan_digest"])

    def test_g1_reader_target_metadata_forecasts_staged_head_source_ref(self) -> None:
        """Catches preview forecasting bundle canonical or current-install source refs."""

        self._downgrade_installed_metadata_to_v0()
        plan = self._preview()
        target = json.loads(
            (self.target_source / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        ownership = {
            "kind": "unknown",
            "destination": str(self.destination),
            "entrypoint": "scripts/floati",
            "entrypoint_sha256": hashlib.sha256(
                (self.target_source / "scripts" / "floati").read_bytes()
            ).hexdigest(),
            "manager": None,
            "remedy": "reinstall with the governed standalone installer",
        }
        expected = {
            "schema_version": 1,
            "source_ref": "HEAD",
            "source_sha": target["source_sha"],
            "files": target["files"],
            "ownership": ownership,
        }
        canonical = dict(expected, source_ref=target["canonical_ref"])
        expected_digest = hashlib.sha256(
            (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).hexdigest()
        canonical_digest = hashlib.sha256(
            (json.dumps(canonical, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected_digest, plan["target_manifest_sha256"])
        self.assertNotEqual(canonical_digest, plan["target_manifest_sha256"])

    def test_g1_reader_batch_is_bound_to_every_receipt(self) -> None:
        """Catches a start, step, completion, or exact retry losing reader testimony."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._downgrade_installed_metadata_to_v0()
        self._consent()
        plan = self._preview()
        self.assertIn("reader_consequences", plan, "plan omits reader_consequences")
        ledger = FleetUpdateReceiptLedger(self.root)
        start = ledger.start(plan, self.actor, "reader-bound")
        retry = ledger.start(plan, self.actor, "reader-bound")
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, "reader-bound", start["id"]
        )
        completed = ledger.complete(
            plan, self.actor, "reader-bound", steps[-1]["id"]
        )
        expected = [self._reader_row()]
        for row in (start, retry, *steps, completed):
            self.assertEqual(expected, row["reader_consequences"])
            self.assertEqual(plan["owner_review_batch_digest"], row["owner_review_batch_digest"])
        self.assertEqual(
            self._hand_batch_digest(
                expected,
                list(plan["seat_binding_consequences"]),
                list(plan["seat_exclusions"]),
            ),
            plan["owner_review_batch_digest"],
        )
        self.assertEqual(
            self._canonical_step_kinds(plan),
            [row["step_kind"] for row in steps],
        )
        self.assertNotIn(
            "gateway", " ".join(str(row.get("kind")) for row in completed["moves"])
        )

    def test_g1_reader_equal_vocabulary_is_empty_and_manifest_path_is_exact(self) -> None:
        """Catches value-only updates manufacturing a reader row or an inferred coordinate."""

        plan = self._preview()
        self.assertIn("reader_consequences", plan, "plan omits reader_consequences")
        self.assertEqual([], plan["reader_consequences"])

        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry["transports"][self.transport]["manifest_path"] = str(
            self.destination / ".floati-install" / "other.json"
        )
        self.registry_path.write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        before = _tree_bytes(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_transport_registry_invalid", caught.exception.code)
        self.assertEqual(before, _tree_bytes(self.base))

    def test_g1_reader_schema1_never_downgrades_invalid_ownership_to_unknown(self) -> None:
        """Catches malformed schema-1 testimony being projected as legacy unknown ownership."""

        path = self._installed_metadata_path()
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["ownership"] = "malformed"
        path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_install_metadata_invalid", caught.exception.code)

    def test_g1_reader_schema_and_runtime_reject_fabricated_rows(self) -> None:
        """Catches missing, duplicated, incompatible, or formula-breaking reader testimony."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._downgrade_installed_metadata_to_v0()
        self._consent()
        plan = self._preview()
        self.assertIn("reader_consequences", plan, "plan omits reader_consequences")
        start = FleetUpdateReceiptLedger(self.root).start(plan, self.actor, "reader-schema")
        validate_record(
            start, self.root.tenant_id, frozenset({"fleet_update_started"}), integrity=False
        )
        validate_json_schema(
            start, REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json"
        )

        missing = dict(start)
        missing.pop("reader_consequences")
        with self.assertRaises(ProtocolRefusal):
            validate_record(
                missing,
                self.root.tenant_id,
                frozenset({"fleet_update_started"}),
                integrity=False,
            )
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                missing, REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json"
            )

        row = self._reader_row()
        variants = (
            [row, row],
            [{**row, "reader": "another_reader"}],
            [{**row, "added_fields": ["files", "ownership"]}],
            [{**row, "current_schema_version": False, "target_schema_version": True}],
            [{**row, "compatibility_after_update": "compatible"}],
        )
        for readers in variants:
            with self.subTest(readers=readers):
                malformed = dict(
                    start,
                    reader_consequences=readers,
                    owner_review_batch_digest=self._hand_batch_digest(
                        readers,
                        list(start["seat_binding_consequences"]),
                        list(start["seat_exclusions"]),
                    ),
                )
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        malformed,
                        self.root.tenant_id,
                        frozenset({"fleet_update_started"}),
                        integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(
                        malformed,
                        REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json",
                    )

        old_two_array = dict(start)
        old_two_array["owner_review_batch_digest"] = hashlib.sha256(
            b'{"seat_binding_consequences":[],"seat_exclusions":[]}'
        ).hexdigest()
        with self.assertRaises(ProtocolRefusal):
            validate_record(
                old_two_array,
                self.root.tenant_id,
                frozenset({"fleet_update_started"}),
                integrity=False,
            )

    def test_g1_reader_extra_field_is_rejected_by_runtime_and_schema(self) -> None:
        """Catches a reader row widening receipt vocabulary without a schema amendment."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._downgrade_installed_metadata_to_v0()
        self._consent()
        plan = self._preview()
        start = FleetUpdateReceiptLedger(self.root).start(
            plan, self.actor, "reader-extra-field"
        )
        reader = {**self._reader_row(), "extra": "unruled"}
        malformed = dict(
            start,
            reader_consequences=[reader],
            owner_review_batch_digest=self._hand_batch_digest(
                [reader],
                list(start["seat_binding_consequences"]),
                list(start["seat_exclusions"]),
            ),
        )
        with self.assertRaises(ProtocolRefusal):
            validate_record(
                malformed,
                self.root.tenant_id,
                frozenset({"fleet_update_started"}),
                integrity=False,
            )
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                malformed,
                REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json",
            )

    def test_g1_reader_hostile_step_and_completion_cannot_escape_start(self) -> None:
        """Catches individually valid later rows diverging from the start reader batch."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._downgrade_installed_metadata_to_v0()
        self._consent()
        plan = self._preview()
        self.assertIn("reader_consequences", plan, "plan omits reader_consequences")
        ledger = FleetUpdateReceiptLedger(self.root)
        start = ledger.start(plan, self.actor, "reader-hostile-step")
        step = self._append_observed_step(
            ledger, plan, self.actor, "reader-hostile-step", start["id"]
        )
        hostile_step = dict(
            step,
            reader_consequences=[],
            owner_review_batch_digest=self._hand_batch_digest(
                [], list(step["seat_binding_consequences"]), list(step["seat_exclusions"])
            ),
        )
        validate_record(
            hostile_step,
            self.root.tenant_id,
            frozenset({"fleet_update_step"}),
            integrity=False,
        )
        receipt = self.root.path / "receipts/fleet-update" / f"{self.actor}.jsonl"
        receipt.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in (start, hostile_step))
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(IntegrityFailure) as caught:
            ledger.rows(self.actor)
        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)

        receipt.unlink()

        other = "worker-reader-complete"
        other_plan = self._with_inputs(plan, actor=other)
        start = ledger.start(other_plan, other, "reader-hostile-complete")
        steps = self._append_canonical_steps(
            ledger, other_plan, other, "reader-hostile-complete", start["id"]
        )
        complete = ledger.complete(
            other_plan, other, "reader-hostile-complete", steps[-1]["id"]
        )
        hostile_complete = dict(
            complete,
            reader_consequences=[],
            owner_review_batch_digest=self._hand_batch_digest(
                [],
                list(complete["seat_binding_consequences"]),
                list(complete["seat_exclusions"]),
            ),
        )
        validate_record(
            hostile_complete,
            self.root.tenant_id,
            frozenset({"fleet_update_completed"}),
            integrity=False,
        )
        receipt = self.root.path / "receipts/fleet-update" / f"{other}.jsonl"
        receipt.write_text(
            "\n".join(
                json.dumps(row, sort_keys=True)
                for row in (start, *steps, hostile_complete)
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(IntegrityFailure) as caught:
            ledger.rows(other)
        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)

    def test_g1_05_two_codex_seats_and_two_bindings_produce_all_four_consequences(self) -> None:
        """Catches a planner joining only matching workspace/config names instead of conservative root scope."""

        self._two_by_two()
        plan = self._preview()
        rows = plan["seat_binding_consequences"]
        self.assertEqual(4, len(rows))
        self.assertEqual([("seat-a", "Codex"), ("seat-a", "Codex"), ("seat-b", "codex"), ("seat-b", "codex")], [(row["node_id"], row["harness"]) for row in rows])
        self.assertEqual({"conservative_root_scope"}, {row["association_basis"] for row in rows})
        self.assertEqual({plan["target_waiter_digest"]}, {row["target_waiter_digest"] for row in rows})
        self.assertEqual([], plan["seat_exclusions"])

    def test_g1_06_retired_expired_and_non_codex_mappings_are_complete_exclusions(self) -> None:
        """Catches active-only filtering silently dropping a retired, expired, or switched mapping."""

        registry = Registry(self.root)
        registry.register("retired", "Codex"); registry.retire("retired")
        registry.register("expired", "Codex")
        registry.register("switched", "Claude")
        workspaces = []
        for node in ("retired", "expired", "switched"):
            workspace = self.base / node; workspace.mkdir(); workspaces.append((workspace, node))
        append_record(self.root, "registry/entries.jsonl", {"schema_version": 0, "id": "lease-" + uuid7_hex(), "tenant_id": self.root.tenant_id, "timestamp": "2020-01-01T00:00:00.000Z", "kind": "node_lease", "node_id": "expired", "workspace": str(workspaces[1][0]), "expires_at": "2020-01-01T00:01:00.000Z", "state": "active"}, allowed_kinds=REGISTRY_KINDS)
        self._map(workspaces)
        plan = self._preview()
        exclusions = plan["seat_exclusions"]
        self.assertEqual(["harness_not_codex", "lease_expired", "node_retired"], sorted(row["reason"] for row in exclusions))
        switched = next(row for row in exclusions if row["node_id"] == "switched")
        self.assertEqual("Claude", switched["harness"])

    def test_g1_owner_map_rejects_a_dangling_workspace_map_symlink(self) -> None:
        """Catches a dangling product-owned map symlink becoming an empty batch."""

        mapping_path = self.root.path / "codex-wait" / "workspaces.v0.json"
        mapping_path.parent.mkdir(parents=True, exist_ok=True)
        if mapping_path.exists():
            mapping_path.unlink()
        mapping_path.symlink_to(self.base / "missing-workspace-map")

        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()

        self.assertEqual("fleet_update_mapping_invalid", caught.exception.code)

    def test_g1_owner_map_retired_and_expired_absent_workspaces_are_exclusions(self) -> None:
        """Catches inactive mapped seats requiring a workspace that need not exist."""

        registry = Registry(self.root)
        registry.register("retired-absent", "Codex")
        registry.retire("retired-absent")
        registry.register("expired-absent", "Codex")
        retired = self.base / "retired-absent-workspace"
        expired = self.base / "expired-absent-workspace"
        append_record(
            self.root,
            "registry/entries.jsonl",
            {
                "schema_version": 0,
                "id": "lease-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": "2020-01-01T00:00:00.000Z",
                "kind": "node_lease",
                "node_id": "expired-absent",
                "workspace": str(expired),
                "expires_at": "2020-01-01T00:01:00.000Z",
                "state": "active",
            },
            allowed_kinds=REGISTRY_KINDS,
        )
        self._map([(retired, "retired-absent"), (expired, "expired-absent")])

        try:
            plan = self._preview()
        except ProtocolRefusal as exc:
            self.fail(f"inactive mapped workspace was refused: {exc.code}")

        self.assertEqual(
            [
                ("expired-absent", str(expired), "lease_expired"),
                ("retired-absent", str(retired), "node_retired"),
            ],
            [
                (row["node_id"], row["workspace"], row["reason"])
                for row in plan["seat_exclusions"]
            ],
        )
        self.assertEqual([], plan["seat_binding_consequences"])

    def test_g1_owner_map_active_absent_workspace_still_refuses(self) -> None:
        """Catches inactive-map handling weakening active workspace validation."""

        Registry(self.root).register("active-absent", "Codex")
        self._map([(self.base / "active-absent-workspace", "active-absent")])

        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()

        self.assertEqual("fleet_update_mapping_invalid", caught.exception.code)

    def test_g1_binding_parser_matches_schema_version_and_terminal_path_guards(self) -> None:
        """Catches binding inventory runtime accepting schema-rejected primitives or paths."""

        from floati.fleet_update import _load_bindings

        schema = REPOSITORY_ROOT / "schemas/v0/fleet-update-binding.schema.json"
        unsafe_configuration = self.base / "unsafe\nhooks.json"
        unsafe_configuration.write_bytes(self.hooks_path.read_bytes())
        unsafe_store = self.base / "unsafe\u202estore"
        unsafe_store.mkdir()
        cases = (
            (
                "schema-version-bool",
                False,
                str(self.hooks_path),
                str(self.waiter_store),
            ),
            (
                "newline-configuration",
                0,
                str(unsafe_configuration),
                str(self.waiter_store),
            ),
            (
                "bidi-store",
                0,
                str(self.hooks_path),
                str(unsafe_store),
            ),
        )
        for name, version, configuration, store in cases:
            with self.subTest(name=name):
                payload = {
                    "schema_version": version,
                    "bindings": [{
                        "kind": "codex_stop_hook",
                        "configuration": configuration,
                        "store": store,
                    }],
                }
                self.binding_path.write_text(
                    json.dumps(payload, sort_keys=True), encoding="utf-8"
                )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(payload, schema)
                with self.assertRaises(ProtocolRefusal) as caught:
                    _load_bindings(self.binding_path)
                self.assertEqual("fleet_update_binding_invalid", caught.exception.code)

    def test_g1_binding_inventory_max_items_matches_schema_before_observation(self) -> None:
        """Catches runtime admitting more binding coordinates than the closed schema."""

        from floati.fleet_update import _load_bindings

        schema = REPOSITORY_ROOT / "schemas/v0/fleet-update-binding.schema.json"
        inventory = self.base / "binding-inventory"
        inventory.mkdir()
        bindings = []
        for ordinal in range(1024):
            configuration = inventory / f"hook-{ordinal:04d}.json"
            configuration.write_text("{}", encoding="utf-8")
            bindings.append({
                "kind": "codex_stop_hook",
                "configuration": str(configuration),
                "store": str(self.waiter_store),
            })
        admitted = {"schema_version": 0, "bindings": bindings}
        validate_json_schema(admitted, schema)
        self.binding_path.write_text(json.dumps(admitted, sort_keys=True), encoding="utf-8")
        _digest, loaded = _load_bindings(self.binding_path)
        self.assertEqual(1024, len(loaded))

        excess = {
            "schema_version": 0,
            "bindings": bindings + [{
                "kind": "codex_stop_hook",
                "configuration": str(inventory / "unobserved.json"),
                "store": str(self.waiter_store),
            }],
        }
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(excess, schema)
        self.binding_path.write_text(json.dumps(excess, sort_keys=True), encoding="utf-8")
        before = _tree_bytes(self.base)
        observed: list[tuple[str, str]] = []

        def observe(path: Path, code: str) -> Path:
            observed.append((str(path), code))
            return path

        with mock.patch("floati.fleet_update._canonical_file", side_effect=observe),\
             mock.patch("floati.fleet_update._canonical_directory", side_effect=observe):
            with self.assertRaises(ProtocolRefusal) as caught:
                _load_bindings(self.binding_path)
        self.assertEqual("fleet_update_binding_invalid", caught.exception.code)
        self.assertEqual(
            [(str(self.binding_path), "fleet_update_binding_invalid")], observed
        )
        self.assertEqual(before, _tree_bytes(self.base))

    def test_g1_06a_provider_switch_back_restores_case_preserving_consequence(self) -> None:
        """Catches a current provider assignment remaining excluded after a switch back to Codex."""

        registry = Registry(self.root)
        first = registry.register("seat-switch", "Claude")
        workspace = self.base / "switch-workspace"; workspace.mkdir(); self._map([(workspace, "seat-switch")])
        excluded = self._preview()
        self.assertEqual("harness_not_codex", excluded["seat_exclusions"][0]["reason"])
        replacement = {"schema_version": 0, "id": "registry-" + uuid7_hex(), "tenant_id": self.root.tenant_id, "timestamp": utc_now(), "kind": "registry_entry", "node_id": "seat-switch", "role": "cOdEx", "state": "active"}
        append_record(self.root, "registry/entries.jsonl", replacement, allowed_kinds=REGISTRY_KINDS)
        append_record(self.root, "registry/entries.jsonl", {"schema_version": 0, "id": "provider-switch-receipt-" + uuid7_hex(), "tenant_id": self.root.tenant_id, "timestamp": utc_now(), "kind": "provider_switch_receipt", "node_id": "seat-switch", "previous_registry_entry_id": first["id"], "previous_harness": "Claude", "previous_model": None, "harness": "cOdEx", "model": "test-model", "registry_entry_id": replacement["id"]}, allowed_kinds=REGISTRY_KINDS)
        restored = self._preview()
        self.assertEqual([], restored["seat_exclusions"])
        self.assertEqual("cOdEx", restored["seat_binding_consequences"][0]["harness"])
        self.assertNotEqual(excluded["plan_digest"], restored["plan_digest"])

    def test_g1_07_cross_root_unregistered_and_malformed_mappings_refuse_before_receipt(self) -> None:
        """Catches foreign roots or unresolved mappings being converted into a partial owner-review batch."""

        workspace = self.base / "workspace"; workspace.mkdir()
        self._map([(workspace, "ghost")])
        before = _tree_bytes(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_mapping_unregistered", caught.exception.code)
        self.assertEqual(before, _tree_bytes(self.base))
        Registry(self.root).register("seat-a", "Codex")
        self._map([(workspace, "seat-a")])
        foreign = self.base / "foreign"; foreign.mkdir()
        hooks, store = self._binding(3, root=foreign)
        self.binding_path.write_text(json.dumps({"schema_version": 0, "bindings": [{"kind": "codex_stop_hook", "configuration": str(hooks), "store": str(store)}]}), encoding="utf-8")
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_waiter_binding_invalid", caught.exception.code)
        good_hooks, good_store = self._binding(4)
        self.binding_path.write_text(json.dumps({"schema_version": 0, "bindings": [{"kind": "codex_stop_hook", "configuration": str(good_hooks), "store": str(good_store)}]}), encoding="utf-8")
        (self.root.path / "codex-wait" / "workspaces.v0.json").write_text(
            json.dumps({"schema_version": 0, "tenant_id": self.root.tenant_id, "mappings": [{"workspace": str(workspace)}]}),
            encoding="utf-8",
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_mapping_invalid", caught.exception.code)
        self._map([(self.base / "missing", "seat-a")])
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_mapping_invalid", caught.exception.code)

    def test_g1_08_completion_repeats_batch_and_lists_step_ids_moves_and_unchanged(self) -> None:
        """Catches completion losing the exact physical step evidence or owner-review batch."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger
        consent = self._consent(); plan = self._preview(); ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "fu1-g1-complete")
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, "fu1-g1-complete", started["id"]
        )
        complete = ledger.complete(
            plan, self.actor, "fu1-g1-complete", steps[-1]["id"]
        )
        self.assertEqual([step["id"] for step in steps], complete["step_receipt_ids"])
        self.assertEqual(plan["moves"], complete["moves"])
        self.assertEqual(plan["unchanged"], complete["unchanged"])
        self.assertEqual(plan["owner_review_batch_digest"], complete["owner_review_batch_digest"])
        self.assertEqual(plan["seat_binding_consequences"], complete["seat_binding_consequences"])

    def test_g1_completion_moves_and_unchanged_are_bound_to_the_start_witness(self) -> None:
        """Catches a schema-valid terminal summary detaching from its authenticated start."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "fu1-g1-completion-moves")
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, "fu1-g1-completion-moves", started["id"]
        )
        ledger.complete(plan, self.actor, "fu1-g1-completion-moves", steps[-1]["id"])
        receipt_path = self.root.path / ledger.relative(self.actor)
        rows = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
        rows[-1]["moves"][0]["from"] = "0" * 64
        receipt_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

        with self.assertRaises(IntegrityFailure) as caught:
            ledger.rows(self.actor)
        self.assertEqual("fleet_update_completion_invalid", caught.exception.code)

    def test_g1_plan_retains_each_named_waiter_tree_by_path_and_digest(self) -> None:
        """Catches a multi-binding sweep collapsing old content-addressed trees."""

        self._two_by_two()
        plan = self._preview()

        retained = [
            row for row in plan["unchanged"]
            if row["kind"] == "waiter_generation"
        ]
        self.assertEqual(
            [
                {
                    "kind": "waiter_generation",
                    "path": binding["current_tree"],
                    "named_tree_digest": binding["named_tree_digest"],
                    "current_tree_digest": binding["current_tree_digest"],
                    "retained": True,
                }
                for binding in plan["waiter_bindings"]
            ],
            retained,
        )

    def test_g1_completion_move_shapes_have_schema_runtime_parity(self) -> None:
        """Catches the closed path-derived action summary widening in just one validator."""

        from floati.fleet_update_receipts import FleetUpdateReceiptLedger

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "fu1-g1-completion-shapes")
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, "fu1-g1-completion-shapes", started["id"]
        )
        complete = ledger.complete(
            plan, self.actor, "fu1-g1-completion-shapes", steps[-1]["id"]
        )
        complete["moves"] = [
            {
                "kind": "shared_install", "path": str(self.destination),
                "from": plan["current_manifest_sha256"],
                "to": plan["target_manifest_sha256"],
            },
            {
                "kind": "transport_pins", "path": str(self.registry_path),
                "from": plan["current_transport_pins"],
                "to": plan["target_transport_pins"],
            },
            {
                "kind": "waiter_binding", "path": str(self.hooks_path),
                "store": str(self.waiter_store),
                "configuration_from_sha256": plan["waiter_bindings"][0]["configuration_sha256"],
                "configuration_to_sha256": plan["waiter_bindings"][0]["target_configuration_sha256"],
                "current_tree_digest": plan["waiter_bindings"][0]["current_tree_digest"],
                "target_tree_digest": plan["target_waiter_digest"],
            },
        ]
        complete["unchanged"] = [
            {
                "kind": "waiter_generation", "path": str(self.current_waiter),
                "named_tree_digest": self.named_waiter_digest,
                "current_tree_digest": self.observed_waiter_digest,
                "retained": True,
            }
        ]
        schema = REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json"

        validate_json_schema(complete, schema)
        validate_record(
            complete, self.root.tenant_id,
            frozenset({"fleet_update_completed"}), integrity=True,
        )

    def test_g1_09_planner_installer_and_content_addressed_directory_share_one_digest(self) -> None:
        """Catches any private installer inventory drifting from planner target identity."""

        from floati.codex_hook_install import CodexHookInstaller
        from floati.waiter_bundle import waiter_runtime_digest, waiter_runtime_files

        runtime_source = self.base / "complete-runtime-source"
        files = waiter_runtime_files(REPOSITORY_ROOT)
        for source in files:
            destination = runtime_source / source.relative_to(REPOSITORY_ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        launcher_source = REPOSITORY_ROOT / "scripts" / "floati"
        launcher_destination = runtime_source / "scripts" / "floati"
        launcher_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(launcher_source, launcher_destination)
        manifest_files = [
            {"path": source.relative_to(REPOSITORY_ROOT).as_posix(), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
            for source in files
        ]
        manifest_files.append({"path": "scripts/floati", "sha256": hashlib.sha256(launcher_source.read_bytes()).hexdigest()})
        manifest_files.sort(key=lambda row: row["path"])
        (runtime_source / "bundle-manifest.v0.json").write_text(
            json.dumps({"schema_version": 0, "protocol_version": "0", "canonical_ref": "refs/heads/lane/hm0", "files": manifest_files, "source_sha": "b" * 40}, sort_keys=True),
            encoding="utf-8",
        )
        self.target_source = runtime_source
        Registry(self.root).register("seat-a", "Codex")
        workspace = self.base / "runtime-workspace"; workspace.mkdir(); self._map([(workspace, "seat-a")])
        plan = self._preview()
        hooks = self.base / "runtime-hooks.json"
        hooks.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")
        destination = self.base / "runtime-store"
        receipt = CodexHookInstaller(source_root=runtime_source, bus_home=self.root.path, hooks_path=hooks, destination=destination).install(workspace, "seat-a", hook_timeout_seconds=10, wait_deadline_seconds=2)
        expected = waiter_runtime_digest(runtime_source)
        self.assertEqual(expected, receipt["bundle_digest"])
        self.assertEqual(expected, Path(receipt["installed_path"]).name)
        self.assertEqual(expected, plan["target_waiter_digest"])


if __name__ == "__main__":
    unittest.main()
