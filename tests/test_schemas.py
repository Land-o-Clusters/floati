from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import json
import re
import tempfile
import unittest
import unicodedata
from copy import deepcopy
from pathlib import Path

from floati.decisions import decision_digest, validate_decision_binding
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.records import validate_record
from tests.schema_validation import SchemaValidationError, validate_json_schema


SCHEMA_DIR = Path("schemas/v0")
V1_SCHEMA_DIR = Path("schemas/v1")
SCHEMA_NAMES = (
    "message-envelope.schema.json",
    "message-retracted-record.schema.json",
    "delivery-receipt.schema.json",
    "ack-receipt.schema.json",
    "denial-receipt.schema.json",
    "liveness-presence-record.schema.json",
    "authority-grant-record.schema.json",
    "mutual-exclusion-hold-record.schema.json",
    "registry-entry.schema.json",
    "wake-cause-record.schema.json",
    "work-item-record.schema.json",
    "work-transition-record.schema.json",
    "capability-record.schema.json",
    "approval-request-record.schema.json",
    "approval-decision-record.schema.json",
    "worker-receipt-record.schema.json",
    "worker-refusal-record.schema.json",
    "run-created-record.schema.json", "run-policy-bound-record.schema.json",
    "run-worker-pool-bound-record.schema.json", "run-dispatch-decision-record.schema.json",
    "run-result-produced-record.schema.json", "run-result-verified-record.schema.json",
    "run-result-accepted-record.schema.json", "run-terminal-record.schema.json",
    "attempt-opened-record.schema.json", "attempt-started-record.schema.json",
    "attempt-terminal-record.schema.json", "retry-scheduled-record.schema.json",
    "retry-exhausted-record.schema.json",
    "cancel-requested-record.schema.json", "cancel-scope-resolved-record.schema.json",
    "cancel-observed-record.schema.json", "cancel-signal-sent-record.schema.json",
    "cancel-terminal-record.schema.json", "cancel-unconfirmed-record.schema.json",
    "stale-attempt-evidence-record.schema.json", "stale-evidence-adopted-record.schema.json",
    "attempt-harness-session-bound-record.schema.json", "supervisor-orphaned-record.schema.json",
    "task-contract-record.schema.json", "plan-amendment-record.schema.json",
    "acceptance-receipt-record.schema.json",
    "decision-record.schema.json",
)
COMMON_REQUIRED = {"schema_version", "id", "tenant_id", "timestamp"}
READ_CONTRACT_SCHEMAS = (
    "fleet-status-artifact.schema.json",
    "receipts-read-bundle.schema.json",
)
GATEWAY_SCHEMAS = (
    "local-gateway-config.schema.json",
    "gateway-session-ingress-record.schema.json",
    "gateway-capability-declaration-record.schema.json",
    "gateway-approval-forward-record.schema.json",
)


def load_schema(name: str) -> dict:
    path = SCHEMA_DIR / name
    if not path.is_file():
        raise AssertionError(f"required schema is absent: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class SchemaContractTests(unittest.TestCase):
    def test_v4_keeps_v0_tide_schema_bytes_frozen(self) -> None:
        expected = {
            "tide-policy-record.schema.json": (
                "9df7aabec5578746185ea81f8f1f22ebfaa73252fcc70e318be01d5267b520ea"
            ),
            "tide-receipt-record.schema.json": (
                "641328a24b22a9a4248b1801726e3db1e2792e290c55eb34f162ddbe14b11afd"
            ),
        }
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(
                    digest,
                    hashlib.sha256((SCHEMA_DIR / name).read_bytes()).hexdigest(),
                )

    def test_ow1_keeps_v0_wake_daemon_schema_bytes_frozen(self) -> None:
        expected = {
            "wake-daemon-adapter-record.schema.json": (
                "8ba9102ac7542997a9b99220134c1bc20fbd73a33ef2aff8caaec9958e89103b"
            ),
            "wake-daemon-consent-receipt.schema.json": (
                "8235046a508448997d6019e20fe3cc73a84f66d4780399b3a960b4c1703a28c3"
            ),
            "wake-daemon-lifecycle-receipt.schema.json": (
                "eb20fad8ca8644864b32174f75db0c9d288e2befdd00319988cf4f48cd22066d"
            ),
        }
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(
                    digest,
                    hashlib.sha256((SCHEMA_DIR / name).read_bytes()).hexdigest(),
                )

    def test_ow1_adds_closed_v1_wake_daemon_schema_family(self) -> None:
        for name in (
            "wake-daemon-consent-receipt.schema.json",
            "wake-daemon-lifecycle-receipt.schema.json",
            "wake-daemon-adapter-record.schema.json",
        ):
            with self.subTest(name=name):
                schema = load_schema("../v1/" + name)
                self.assertEqual(1, schema["properties"]["schema_version"]["const"])
                self.assertEqual(
                    ["codex", "cursor", "grok-build", "zcode"],
                    schema["properties"]["harness"]["enum"],
                )
                self.assertIs(False, schema["additionalProperties"])

    def test_wake_daemon_contract_schemas_are_closed(self) -> None:
        for name in (
            "wake-daemon-consent-receipt.schema.json",
            "wake-daemon-lifecycle-receipt.schema.json",
            "wake-daemon-adapter-record.schema.json",
        ):
            with self.subTest(name=name):
                schema = load_schema(name)
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
                self.assertEqual("object", schema["type"])
                self.assertIs(False, schema["additionalProperties"])

    def test_codex_wait_contract_schemas_are_closed_and_match_runtime(self) -> None:
        from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitReceiptLedger, resolve_participant
        from floati.registry import Registry
        from floati.root import FloatiRoot

        with tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp") as temporary:
            base = Path(temporary)
            home = base / "demo-fleet"
            root = FloatiRoot.open_direct_home(home, create=True)
            Registry(root).register(public_ids.builder('floati'), "worker")
            workspace = base / "workspace"
            workspace.mkdir()
            workspace_map = {
                "schema_version": 0,
                "tenant_id": "demo-fleet",
                "mappings": [{"workspace": str(workspace), "node_id": public_ids.builder('floati')}],
            }
            map_path = home / "codex-wait" / "workspaces.v0.json"
            map_path.parent.mkdir()
            map_path.write_text(json.dumps(workspace_map, sort_keys=True, separators=(",", ":")) + "\n")
            validate_json_schema(workspace_map, SCHEMA_DIR / "codex-wait-workspace-map.schema.json")
            participant = resolve_participant(home, workspace)
            assert participant is not None
            consent = CodexWaitConsentLedger(root).arm(
                participant.binding,
                hook_timeout_seconds=10,
                wait_deadline_seconds=2,
                idempotency_key="schema-consent",
            )
            exhaustion = CodexWaitReceiptLedger(root).record_exhaustion(
                node_id=public_ids.builder('floati'),
                session_digest="a" * 64,
                waited_seconds=2,
                idempotency_key="schema-exhaustion",
            )
            for row, name in (
                (consent, "codex-wait-consent-receipt.schema.json"),
                (exhaustion, "codex-wait-exhaustion-receipt.schema.json"),
            ):
                with self.subTest(schema=name):
                    path = V1_SCHEMA_DIR / name
                    validate_json_schema(row, path)
                    schema = json.loads(path.read_text(encoding="utf-8"))
                    self.assertFalse(schema["additionalProperties"])
                    self.assertEqual(set(row), set(schema["required"]))

    def test_v1_wake_hold_schemas(self) -> None:
        """Catches missing closed schemas for the additive wake-hold protocol."""
        from tests.test_wake_hold import WakeHoldRecordTests

        fixture = WakeHoldRecordTests()
        fixture.setUp()
        row = fixture.receipt()
        path = V1_SCHEMA_DIR / "wake-hold-receipt-record.schema.json"
        validate_json_schema(row, path)
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(row), set(schema["required"]))
        self.assertTrue((V1_SCHEMA_DIR / "wake-decision-artifact.schema.json").is_file())

    def test_thread_observation_record_schemas(self) -> None:
        """Catches schema/runtime drift across the closed thread testimony family."""
        from tests.test_thread_observations import thread_record_rows

        names = (
            "thread-attachment-registered-record.schema.json",
            "thread-observation-recorded-record.schema.json",
            "thread-attachment-detached-record.schema.json",
        )
        for row, name in zip(thread_record_rows("alpha"), names):
            with self.subTest(schema=name):
                path = V1_SCHEMA_DIR / name
                if not path.is_file():
                    self.fail(f"required schema is absent: {path}")
                validate_json_schema(row, path)
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(row), set(schema["required"]))

    def test_v1_result_acceptance_schema_matches_runtime_effect_binding(self) -> None:
        """Catches runtime/schema drift in the additive effect-bound acceptance row."""
        first = "effect-op-" + uuid7_hex()
        second = "effect-op-" + uuid7_hex()
        operation_ids = sorted((first, second))
        row = {
            "schema_version": 1,
            "id": "run-result-accepted-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-13T12:00:00.000Z",
            "kind": "result_accepted",
            "run_id": "run-" + uuid7_hex(),
            "item_id": "work-" + uuid7_hex(),
            "attempt_id": "attempt-" + uuid7_hex(),
            "predecessor_result_id": "run-result-produced-" + uuid7_hex(),
            "acceptance_mode": "accepted_unverified",
            "acceptance_receipt_id": None,
            "worker_receipt_ids": ["worker-receipt-" + uuid7_hex()],
            "effect_operation_ids": operation_ids,
            "effect_ledger_high_watermark": 7,
            "effect_evidence_digest": "a" * 64,
        }
        path = V1_SCHEMA_DIR / "run-result-accepted-record.schema.json"
        validated = validate_record(row, "alpha", frozenset({"result_accepted"}), integrity=False)
        self.assertEqual(row, validated)
        validate_json_schema(row, path)
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(row), set(schema["required"]))
        with self.assertRaises(ProtocolRefusal):
            validate_record(
                dict(row, effect_operation_ids=list(reversed(operation_ids))),
                "alpha", frozenset({"result_accepted"}), integrity=False,
            )
        for changed in (
            dict(row, effect_operation_ids=[first, first]),
            dict(row, effect_ledger_high_watermark=0),
            dict(row, effect_evidence_digest="A" * 64),
        ):
            runtime_accepts = True
            schema_accepts = True
            try:
                validate_record(changed, "alpha", frozenset({"result_accepted"}), integrity=False)
            except ProtocolRefusal:
                runtime_accepts = False
            try:
                validate_json_schema(changed, path)
            except SchemaValidationError:
                schema_accepts = False
            self.assertEqual((False, False), (runtime_accepts, schema_accepts))

    def test_v1_effect_record_contracts(self) -> None:
        """Catches any effect schema that diverges from the runtime closed-record contract."""
        from tests.test_effects import EffectRecordFixture

        fixture = EffectRecordFixture()
        for kind, row in fixture.rows().items():
            with self.subTest(kind=kind):
                path = V1_SCHEMA_DIR / (kind.replace("_", "-") + "-record.schema.json")
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(row), set(schema["required"]))
                validate_json_schema(row, path)

    def test_v1_effect_unicode_control_parity(self) -> None:
        """Catches a v1 effect string schema guard omitting a terminal-unsafe Unicode value."""
        from tests.test_effects import EffectRecordFixture

        fixture = EffectRecordFixture()
        unsafe = tuple(
            chr(codepoint)
            for codepoint in range(0x110000)
            if unicodedata.category(chr(codepoint)) in {"Cc", "Cs"}
            or unicodedata.bidirectional(chr(codepoint))
            in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        )
        self.assertEqual(2248, len(unsafe))
        for kind, row in fixture.rows().items():
            path = V1_SCHEMA_DIR / (kind.replace("_", "-") + "-record.schema.json")
            for character in unsafe:
                with self.subTest(kind=kind, codepoint=f"U+{ord(character):04X}"):
                    candidate = dict(row, target=dict(row["target"], coordinate="safe" + character))
                    with self.assertRaises(SchemaValidationError):
                        validate_json_schema(candidate, path)

    def test_v1_approval_suspension_terminal_unsafe_parity_is_complete(self) -> None:
        """Catches any runtime terminal-unsafe code point omitted by a new v1 schema guard."""
        from floati.records import _terminal_unsafe
        from tests.test_approval_suspension import ApprovalSuspensionProjectionTests

        fixtures = ApprovalSuspensionProjectionTests()
        _, state = fixtures.started_attempt()
        suspension = fixtures.suspension_record(state)
        consumed = fixtures.consumption_record(state, suspension)
        paths = {
            "attempt_suspended_for_approval": V1_SCHEMA_DIR / "attempt-suspended-for-approval-record.schema.json",
            "approval_consumed_for_resume": V1_SCHEMA_DIR / "approval-consumed-for-resume-record.schema.json",
        }
        unsafe = tuple(
            chr(codepoint)
            for codepoint in range(0x110000)
            if unicodedata.category(chr(codepoint)) in {"Cc", "Cs"}
            or unicodedata.bidirectional(chr(codepoint))
            in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        )
        self.assertEqual(2248, len(unsafe))
        for codepoint in (0x180E, 0x1BCA0, 0x1D173, 0xE0001, 0xE0020):
            self.assertIn(chr(codepoint), unsafe)

        safe_controls = ("\u061c", "\u200e", "\u200f", "\u2065", "\ufff9", "\ufffa", "\ufffb")
        self.assertTrue(all(not _terminal_unsafe(value) for value in safe_controls))

        for path in paths.values():
            schema = json.loads(path.read_text(encoding="utf-8"))
            guards = {
                "provider_session_or_thread_id": schema["properties"]["provider_session_or_thread_id"]["not"]["pattern"],
                **{
                    field: schema["properties"]["workspace_checkpoint"]["allOf"][1]["properties"][field]["not"]["pattern"]
                    for field in ("repo", "sha", "doc")
                },
            }
            for field, source in guards.items():
                guard = re.compile(source)
                mismatches = [
                    f"U+{ord(character):04X}"
                    for character in unsafe
                    if guard.search(character) is None
                ]
                with self.subTest(schema=path.name, field=field):
                    self.assertEqual([], mismatches)
                    self.assertTrue(all(guard.search(value) is None for value in safe_controls))

        def accepted(record: dict) -> tuple[bool, bool]:
            try:
                validate_record(dict(record), "alpha", frozenset({record["kind"]}), integrity=False)
            except ProtocolRefusal:
                runtime = False
            else:
                runtime = True
            try:
                validate_json_schema(record, paths[record["kind"]])
            except SchemaValidationError:
                schema = False
            else:
                schema = True
            return runtime, schema

        representative = ("\u180e", "\U0001bca0", "\U0001d173", "\U000e0001", "\U000e0020")
        for source in (suspension, consumed):
            with self.subTest(positive=source["kind"]):
                self.assertEqual((True, True), accepted(source))
            for character in representative:
                provider = dict(source, provider_session_or_thread_id="provider" + character)
                with self.subTest(kind=source["kind"], provider=ord(character)):
                    self.assertEqual((False, False), accepted(provider))
                for field in ("repo", "sha", "doc"):
                    checkpoint = dict(source["workspace_checkpoint"])
                    checkpoint[field] = str(checkpoint[field]) + character
                    mutated = dict(source, workspace_checkpoint=checkpoint)
                    with self.subTest(kind=source["kind"], field=field, character=ord(character)):
                        self.assertEqual((False, False), accepted(mutated))

    def test_v1_approval_suspension_schema_runtime_newline_parity(self) -> None:
        """Catches Draft schema final-newline admission where runtime uses full-match semantics."""
        from tests.test_approval_suspension import ApprovalSuspensionProjectionTests

        fixtures = ApprovalSuspensionProjectionTests()
        _, state = fixtures.started_attempt()
        suspension = fixtures.suspension_record(state)
        consumed = fixtures.consumption_record(state, suspension)
        paths = {
            "attempt_suspended_for_approval": V1_SCHEMA_DIR / "attempt-suspended-for-approval-record.schema.json",
            "approval_consumed_for_resume": V1_SCHEMA_DIR / "approval-consumed-for-resume-record.schema.json",
        }

        def accepts(record: dict) -> tuple[bool, bool]:
            try:
                validate_record(
                    dict(record),
                    "alpha",
                    frozenset({record["kind"]}),
                    integrity=False,
                )
            except ProtocolRefusal:
                runtime = False
            else:
                runtime = True
            try:
                validate_json_schema(record, paths[record["kind"]])
            except SchemaValidationError:
                schema = False
            else:
                schema = True
            return runtime, schema

        for record in (suspension, consumed):
            with self.subTest(positive=record["kind"]):
                self.assertEqual((True, True), accepts(record))

        lexical_fields = (
            (suspension, "id"), (suspension, "tenant_id"),
            (suspension, "timestamp"), (suspension, "run_id"),
            (suspension, "item_id"), (suspension, "attempt_id"),
            (suspension, "attempt_started_id"), (suspension, "fence_token"),
            (suspension, "adapter"), (suspension, "approval_request_id"),
            (suspension, "exact_action_digest"), (suspension, "requested_scope"),
            (suspension, "workspace"),
            (suspension, "execution_authority_subject"),
            (suspension, "execution_authority_holder"),
            (suspension, "approval_expiry"),
            (consumed, "id"), (consumed, "tenant_id"),
            (consumed, "timestamp"), (consumed, "run_id"),
            (consumed, "item_id"), (consumed, "attempt_id"),
            (consumed, "fence_token"), (consumed, "attempt_suspended_id"),
            (consumed, "approval_request_id"), (consumed, "approval_decision_id"),
            (consumed, "exact_action_digest"), (consumed, "requested_scope"),
            (consumed, "workspace"), (consumed, "resume_authority_subject"),
            (consumed, "resume_authority_holder"),
            (consumed, "consumed_at_testimony"),
        )
        for source, field in lexical_fields:
            mutated = dict(source, **{field: str(source[field]) + "\n"})
            with self.subTest(kind=source["kind"], field=field):
                self.assertEqual((False, False), accepts(mutated))

        for source in (suspension, consumed):
            for field in ("repo", "sha", "doc"):
                checkpoint = dict(source["workspace_checkpoint"])
                checkpoint[field] = str(checkpoint[field]) + "\n"
                mutated = dict(source, workspace_checkpoint=checkpoint)
                with self.subTest(kind=source["kind"], checkpoint_field=field):
                    self.assertEqual((False, False), accepts(mutated))

    def test_v1_approval_action_binding_schemas(self) -> None:
        """Catches schema/runtime drift in closed action-bound request and decision shapes."""
        uuid_request = "018f7e9b3c117abc8def0123456789ab"
        uuid_decision = "018f7e9b3c127abc8def0123456789ab"
        request = {
            "schema_version": 1,
            "id": "approval-request-" + uuid_request,
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "approval_request",
            "requester": public_ids.worker('alpha'),
            "capability": "workspace.patch",
            "scope": "repo:slipway",
            "requested_ttl_seconds": 60,
            "requested_at": "2026-08-09T12:00:00.000Z",
            "expires_at": "2026-08-09T12:01:00.000Z",
            "authority_subject": "approve-build",
            "authority_epoch": 7,
            "exact_action_digest": "a" * 64,
        }
        approved = {
            "schema_version": 1,
            "id": "approval-decision-" + uuid_decision,
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:01.000Z",
            "kind": "approval_decision",
            "request_id": request["id"],
            "decider": public_ids.reviewer(),
            "decision": "approved",
            "granted_scope": "repo:slipway",
            "granted_ttl_seconds": 30,
            "reason_code": None,
            "decided_at": "2026-08-09T12:00:01.000Z",
            "expires_at": "2026-08-09T12:00:31.000Z",
            "authority_subject": "approve-build",
            "authority_epoch": 7,
            "exact_action_digest": request["exact_action_digest"],
        }
        denied = dict(
            approved,
            decision="denied",
            granted_scope=None,
            granted_ttl_seconds=None,
            reason_code="operator_denied",
            expires_at=None,
        )
        paths = {
            "approval_request": V1_SCHEMA_DIR / "approval-request-record.schema.json",
            "approval_decision": V1_SCHEMA_DIR / "approval-decision-record.schema.json",
        }

        def runtime_accepts(record: dict) -> bool:
            try:
                validate_record(
                    dict(record),
                    "alpha",
                    frozenset({str(record["kind"])}),
                    integrity=False,
                )
            except ProtocolRefusal:
                return False
            return True

        def schema_accepts(record: dict) -> bool:
            try:
                validate_json_schema(record, paths[str(record["kind"])])
            except (SchemaValidationError, FileNotFoundError):
                return False
            return True

        for record in (request, approved, denied):
            with self.subTest(positive=record.get("decision", "request")):
                self.assertEqual((True, True), (runtime_accepts(record), schema_accepts(record)))

        newline_terminated_lexical_fields = (
            (request, "id"),
            (request, "tenant_id"),
            (request, "timestamp"),
            (request, "requester"),
            (request, "capability"),
            (request, "scope"),
            (request, "requested_at"),
            (request, "expires_at"),
            (request, "authority_subject"),
            (request, "exact_action_digest"),
            (approved, "id"),
            (approved, "tenant_id"),
            (approved, "timestamp"),
            (approved, "request_id"),
            (approved, "decider"),
            (approved, "granted_scope"),
            (approved, "decided_at"),
            (approved, "expires_at"),
            (approved, "authority_subject"),
            (approved, "exact_action_digest"),
            (denied, "reason_code"),
        )
        for source, field in newline_terminated_lexical_fields:
            record = dict(source, **{field: str(source[field]) + "\n"})
            with self.subTest(trailing_newline_field=field, kind=record["kind"]):
                self.assertEqual(
                    (False, False),
                    (runtime_accepts(record), schema_accepts(record)),
                )

        hostile = (
            dict(request, exact_action_digest="0" * 63),
            dict(request, exact_action_digest="A" * 64),
            dict(request, requested_ttl_seconds=True),
            dict(request, authority_epoch=0),
            dict(request, caller_authority=True),
            dict(approved, granted_scope=None),
            dict(approved, granted_ttl_seconds=None),
            dict(approved, reason_code="operator_denied"),
            dict(approved, expires_at=None),
            dict(denied, granted_scope="repo:slipway"),
            dict(denied, granted_ttl_seconds=1),
            dict(denied, reason_code=None),
            dict(denied, reason_code="operator\u202edenied"),
            dict(denied, expires_at="2026-08-09T12:00:31.000Z"),
        )
        for record in hostile:
            with self.subTest(hostile=record):
                self.assertEqual((False, False), (runtime_accepts(record), schema_accepts(record)))

        for record, field in (
            (request, "schema_version"),
            (request, "requested_ttl_seconds"),
            (request, "authority_epoch"),
            (approved, "granted_ttl_seconds"),
            (approved, "authority_epoch"),
        ):
            integral = dict(record, **{field: float(record[field])})
            with self.subTest(integral_field=field):
                self.assertEqual(
                    (True, True),
                    (runtime_accepts(integral), schema_accepts(integral)),
                )

    def test_sequencer_epoch_schema_and_runtime_accept_identical_scalar_shapes(self) -> None:
        """Catches timestamp or JSON-integer shapes drifting between schema and runtime."""
        schema_path = V1_SCHEMA_DIR / "sequencer-epoch-record.schema.json"
        uuid = "0" * 12 + "7" + "0" * 3 + "8" + "0" * 15
        baseline = {
            "schema_version": 1,
            "id": "sequencer-epoch-" + uuid,
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "sequencer_epoch",
            "epoch": 1,
            "operation": "entered",
            "sequencer_id": "sequencer-a",
            "previous_epoch_record_id": None,
            "absence_reason": "initial",
        }

        def runtime_accepts(record: dict) -> bool:
            try:
                validate_record(
                    record, "alpha", frozenset({"sequencer_epoch"}), integrity=False
                )
            except ProtocolRefusal:
                return False
            return True

        def schema_accepts(record: dict) -> bool:
            try:
                validate_json_schema(record, schema_path)
            except SchemaValidationError:
                return False
            return True

        cases = (
            ("timestamp", "2026-08-09T12:00:00Z", True),
            ("timestamp", "2026-08-09T12:00:00.000Z", True),
            ("timestamp", "2026-08-09T12:00:00+00:00", False),
            ("timestamp", "2026-08-09T12:00:00.0Z", False),
            ("timestamp", "2026-08-09T12:00:00.000000Z", False),
            ("schema_version", 1, True),
            ("schema_version", 1.0, True),
            ("schema_version", 1.5, False),
            ("schema_version", True, False),
            ("schema_version", float("nan"), False),
            ("schema_version", float("inf"), False),
            ("schema_version", float("-inf"), False),
            ("epoch", 1, True),
            ("epoch", 1.0, True),
            ("epoch", 1.5, False),
            ("epoch", 0, False),
            ("epoch", 2**63, False),
            ("epoch", True, False),
            ("epoch", float("nan"), False),
            ("epoch", float("inf"), False),
            ("epoch", float("-inf"), False),
        )
        for field, value, expected in cases:
            record = dict(baseline, **{field: value})
            with self.subTest(field=field, value=value):
                self.assertEqual(
                    (expected, expected),
                    (runtime_accepts(record), schema_accepts(record)),
                )

        for field in ("schema_version", "epoch"):
            with self.subTest(normalized_field=field):
                record = dict(baseline, **{field: 1.0})
                try:
                    normalized = validate_record(
                        record,
                        "alpha",
                        frozenset({"sequencer_epoch"}),
                        integrity=False,
                    )
                except ProtocolRefusal:
                    normalized = None
                self.assertIsNotNone(normalized)
                if normalized is not None:
                    self.assertIs(type(normalized[field]), int)

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual("integer", schema["properties"]["schema_version"].get("type"))
        self.assertEqual(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$",
            schema["properties"]["timestamp"].get("pattern"),
        )

    def test_draft202012_probe_accepts_integral_numeric_epoch_values(self) -> None:
        """Catches project parity expectations diverging from a conforming Draft 2020-12 validator."""
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("optional jsonschema standards probe is unavailable")

        schema_path = V1_SCHEMA_DIR / "sequencer-epoch-record.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        uuid = "0" * 12 + "7" + "0" * 3 + "8" + "0" * 15
        baseline = {
            "schema_version": 1,
            "id": "sequencer-epoch-" + uuid,
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "sequencer_epoch",
            "epoch": 1,
            "operation": "entered",
            "sequencer_id": "sequencer-a",
            "previous_epoch_record_id": None,
            "absence_reason": "initial",
        }
        cases = (
            ("schema_version", 1, True),
            ("schema_version", 1.0, True),
            ("schema_version", 1.5, False),
            ("schema_version", True, False),
            ("schema_version", float("nan"), False),
            ("schema_version", float("inf"), False),
            ("schema_version", float("-inf"), False),
            ("epoch", 1, True),
            ("epoch", 1.0, True),
            ("epoch", 1.5, False),
            ("epoch", 0, False),
            ("epoch", 2**63, False),
            ("epoch", True, False),
            ("epoch", float("nan"), False),
            ("epoch", float("inf"), False),
            ("epoch", float("-inf"), False),
        )
        for field, value, expected in cases:
            with self.subTest(field=field, value=value):
                self.assertEqual(
                    expected,
                    validator.is_valid(dict(baseline, **{field: value})),
                )

    def test_run_admission_v1_schema_matches_mandatory_validator_version(self) -> None:
        """Catches the shipped schema and durable validator disagreeing on binding version."""
        workers = [{"node_id": public_ids.worker('alpha'), "worker_profile": "codex"}]
        reservations = [{"budget_id": "build", "amount": 1}]
        items = [{
            "item_id": "work-018f7e9b3c117abc8def0123456789ab",
            "workspace_key": "workspace-a",
            "concurrency_key": "concurrency-a",
            "capability_selector": "review-write",
        }]
        record = {
            "schema_version": 1,
            "id": "run-admission-bound-018f7e9b3c127abc8def0123456789ab",
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "run_admission_bound",
            "run_id": "run-018f7e9b3c137abc8def0123456789ab",
            "plan_digest": "a" * 64,
            "policy_digest": "b" * 64,
            "max_active_attempts": 1,
            "workers": workers,
            "budget_reservations": reservations,
            "items": items,
            "admission_digest": "c" * 64,
        }
        schema_path = V1_SCHEMA_DIR / "run-admission-bound-record.schema.json"
        validate_json_schema(record, schema_path)
        downgraded = dict(record, schema_version=0)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(downgraded, schema_path)

    def test_post_v1_capability_schemas_are_strict_and_bind_dispatch_snapshot(self) -> None:
        """Catches packaging a permissive grant, revocation, snapshot, or v1 dispatch contract."""
        suffixes = {
            "grant": "018f7e9b3c117abc8def0123456789ab",
            "revoke": "018f7e9b3c127abc8def0123456789ab",
            "snapshot": "018f7e9b3c137abc8def0123456789ab",
            "dispatch": "018f7e9b3c147abc8def0123456789ab",
            "run": "018f7e9b3c157abc8def0123456789ab",
            "item": "018f7e9b3c167abc8def0123456789ab",
            "attempt": "018f7e9b3c177abc8def0123456789ab",
            "request": "018f7e9b3c187abc8def0123456789ab",
            "decision": "018f7e9b3c197abc8def0123456789ab",
        }
        common = {"schema_version": 1, "tenant_id": "alpha", "timestamp": "2026-08-08T12:00:00.000Z"}
        grant = dict(common, id="capability-grant-" + suffixes["grant"], kind="capability_grant",
                     worker_id=public_ids.worker('alpha'), capability_name="review", policy_digest="a" * 64,
                     approval_request_id="approval-request-" + suffixes["request"],
                     approval_decision_id="approval-decision-" + suffixes["decision"],
                     authority_subject="approve-build", authority_epoch=1,
                     expires_at="2026-08-08T12:01:00.000Z", grant_digest="b" * 64)
        revoked = dict(common, id="capability-revoked-" + suffixes["revoke"], kind="capability_revoked",
                       grant_id=grant["id"], reason_code="operator_revoked",
                       replacement_policy_digest=None)
        effective = [{"capability_name": "review", "grant_id": grant["id"], "physical_position": 1}]
        snapshot = dict(common, id="capability-set-bound-" + suffixes["snapshot"], kind="capability_set_bound",
                        run_id="run-" + suffixes["run"], item_id="work-" + suffixes["item"],
                        attempt_id="attempt-" + suffixes["attempt"], fence_token="c" * 64,
                        chosen_worker=public_ids.worker('alpha'), policy_digest="a" * 64, routing_rank=0,
                        evaluated_at_testimony="2026-08-08T12:00:01.000Z",
                        grant_ledger_high_watermark=1, effective_grants=effective,
                        capability_digest="d" * 64)
        dispatch = dict(common, id="run-dispatch-decision-" + suffixes["dispatch"], kind="dispatch_decision",
                        run_id=snapshot["run_id"], item_id=snapshot["item_id"], attempt_id=snapshot["attempt_id"],
                        eligible_workers=[public_ids.worker('alpha')], chosen_worker=public_ids.worker('alpha'), capability_digest="d" * 64,
                        reason_code="policy.route", policy_digest="a" * 64, routing_rank=0,
                        scheduler_epoch=1, capability_set_bound_id=snapshot["id"])
        cases = {
            "capability-grant-record.schema.json": grant,
            "capability-revoked-record.schema.json": revoked,
            "capability-set-bound-record.schema.json": snapshot,
            "run-dispatch-decision-record.schema.json": dispatch,
        }
        for name, instance in cases.items():
            with self.subTest(name=name):
                path = V1_SCHEMA_DIR / name
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(1, schema["properties"]["schema_version"]["const"])
                self.assertFalse(schema["additionalProperties"])
                validate_json_schema(instance, path)
                hostile = dict(instance, caller_authority=True)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(hostile, path)
        replaced = dict(revoked, reason_code="policy_replaced")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(replaced, V1_SCHEMA_DIR / "capability-revoked-record.schema.json")

    def test_dependency_free_schema_helper_selects_then_and_else_branches(self) -> None:
        """Catches the stdlib helper ignoring a selected conditional branch inside allOf."""
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "authority"],
            "properties": {
                "status": {"enum": ["proposed", "accepted"]},
                "authority": {"enum": ["worker", "operator"]},
            },
            "allOf": [
                {
                    "if": {
                        "properties": {"status": {"const": "accepted"}},
                        "required": ["status"],
                    },
                    "then": {"properties": {"authority": {"const": "operator"}}},
                    "else": {"properties": {"authority": {"const": "worker"}}},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            schema_path = Path(directory) / "conditional.schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            validate_json_schema(
                {"status": "accepted", "authority": "operator"}, schema_path
            )
            validate_json_schema(
                {"status": "proposed", "authority": "worker"}, schema_path
            )
            for instance in (
                {"status": "accepted", "authority": "worker"},
                {"status": "proposed", "authority": "operator"},
            ):
                with self.subTest(instance=instance), self.assertRaises(SchemaValidationError):
                    validate_json_schema(instance, schema_path)

    def test_schema_helper_executes_x_floati_sorted_unique_budget_without_legacy_fallback(
        self,
    ) -> None:
        """Catches a dead renamed budget extension or an x-slipway compatibility alias."""

        ordered = [{"budget_id": "build"}, {"budget_id": "review"}]
        unordered = list(reversed(ordered))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            floati_schema = root / "floati-budget.schema.json"
            floati_schema.write_text(
                json.dumps(
                    {
                        "type": "array",
                        "x-floati-sorted-unique-budget": True,
                    }
                ),
                encoding="utf-8",
            )
            legacy_schema = root / "legacy-budget.schema.json"
            legacy_schema.write_text(
                json.dumps(
                    {
                        "type": "array",
                        "x-slipway-sorted-unique-budget": True,
                    }
                ),
                encoding="utf-8",
            )

            validate_json_schema(ordered, floati_schema)
            with self.assertRaises(SchemaValidationError):
                validate_json_schema(unordered, floati_schema)
            validate_json_schema(unordered, legacy_schema)

    def test_schema_helper_executes_x_floati_terminal_unsafe_without_legacy_fallback(
        self,
    ) -> None:
        """Catches a dead renamed Unicode guard or an x-slipway compatibility alias."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            floati_schema = root / "floati-terminal.schema.json"
            floati_schema.write_text(
                json.dumps(
                    {
                        "type": "string",
                        "x-floati-terminal-unsafe": True,
                    }
                ),
                encoding="utf-8",
            )
            legacy_schema = root / "legacy-terminal.schema.json"
            legacy_schema.write_text(
                json.dumps(
                    {
                        "type": "string",
                        "x-slipway-terminal-unsafe": True,
                    }
                ),
                encoding="utf-8",
            )

            validate_json_schema("plain text", floati_schema)
            with self.assertRaises(SchemaValidationError):
                validate_json_schema("unsafe\ntext", floati_schema)
            validate_json_schema("unsafe\ntext", legacy_schema)

    def test_v1_harness_binding_schema_selects_root_and_transition_shapes(self) -> None:
        """Catches a v1 schema that permits a root predecessor or a predecessorless transition."""
        schema_path = V1_SCHEMA_DIR / "attempt-harness-session-bound-record.schema.json"
        self.assertTrue(schema_path.is_file(), f"required schema is absent: {schema_path}")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            "https://landoclusters.com/floati/schemas/v1/attempt-harness-session-bound-record.schema.json",
            schema["$id"],
        )
        self.assertEqual(1, schema["properties"]["schema_version"]["const"])

        uuid = "0" * 12 + "7" + "0" * 3 + "8" + "0" * 15
        root_segment_id = "seg-" + uuid
        record = {
            "schema_version": 1,
            "id": "attempt-harness-session-bound-" + uuid,
            "tenant_id": "alpha",
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "attempt_harness_session_bound",
            "run_id": "run-" + uuid,
            "item_id": "work-" + uuid,
            "attempt_id": "attempt-" + uuid,
            "fence_token": "a" * 64,
            "claim_id": "claim-" + uuid,
            "lease_id": "lease-" + uuid,
            "worker_session_id": "worker-" + uuid,
            "harness_segments": [
                {
                    "ordinal": 1,
                    "harness_session_id": "worker-" + uuid,
                    "segment_id": root_segment_id,
                    "segment_kind": "initial",
                },
                {
                    "ordinal": 2,
                    "harness_session_id": "worker-" + uuid,
                    "segment_id": "seg-" + "1" * 12 + "7" + "1" * 3 + "8" + "1" * 15,
                    "segment_kind": "handoff",
                    "predecessor_segment_id": root_segment_id,
                },
            ],
        }
        validate_json_schema(record, schema_path)
        invalid_root = deepcopy(record)
        invalid_root["harness_segments"][0]["predecessor_segment_id"] = root_segment_id
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(invalid_root, schema_path)
        invalid_transition = deepcopy(record)
        invalid_transition["harness_segments"][1].pop("predecessor_segment_id")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(invalid_transition, schema_path)

    def test_run_record_schemas_match_the_strict_validator_surface(self) -> None:
        run_schemas = {
            "run-created-record.schema.json": ("run_created", "run-created-", {"plan_digest", "item_ids", "dependency_edges"}),
            "run-policy-bound-record.schema.json": ("run_policy_bound", "run-policy-bound-", {"policy_digest"}),
            "run-worker-pool-bound-record.schema.json": ("worker_pool_bound", "run-worker-pool-bound-", {"worker_ids"}),
            "run-dispatch-decision-record.schema.json": ("dispatch_decision", "run-dispatch-decision-", {"item_id", "attempt_id", "eligible_workers", "chosen_worker", "capability_digest", "reason_code", "policy_digest", "routing_rank", "scheduler_epoch"}),
            "run-result-produced-record.schema.json": ("result_produced", "run-result-produced-", {"item_id", "attempt_id", "dispatch_decision_id", "worker_receipt_ids"}),
            "run-result-verified-record.schema.json": ("result_verified", "run-result-verified-", {"item_id", "attempt_id", "result_produced_id", "worker_receipt_ids"}),
            "run-result-accepted-record.schema.json": ("result_accepted", "run-result-accepted-", {"item_id", "attempt_id", "predecessor_result_id", "acceptance_mode", "acceptance_receipt_id", "worker_receipt_ids"}),
            "run-terminal-record.schema.json": ("run_terminal", "run-terminal-", {"outcome"}),
        }
        for name, (kind, prefix, extras) in run_schemas.items():
            with self.subTest(schema=name):
                schema = load_schema(name); props = schema["properties"]
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(kind, props["kind"]["const"])
                self.assertTrue(props["id"]["pattern"].startswith("^" + prefix))
                for digest in {"plan_digest", "policy_digest", "capability_digest"} & set(props):
                    self.assertEqual("^[0-9a-f]{64}$", props[digest]["pattern"])
                self.assertTrue(extras | {"run_id"} <= set(schema["required"]))
        dispatch = load_schema("run-dispatch-decision-record.schema.json")["properties"]
        self.assertEqual(0, dispatch["routing_rank"]["minimum"])
        self.assertEqual(2147483647, dispatch["routing_rank"]["maximum"])
        self.assertEqual(9223372036854775807, dispatch["scheduler_epoch"]["maximum"])
        self.assertIn(".", dispatch["reason_code"]["pattern"])
        for name in run_schemas:
            with self.subTest(tenant=name):
                pattern = load_schema(name)["properties"]["tenant_id"]["pattern"]
                self.assertEqual("^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$", pattern)
                self.assertTrue(re.fullmatch(pattern, "a"))
                self.assertTrue(re.fullmatch(pattern, "1tenant"))
                self.assertIsNone(re.fullmatch(pattern, "-invalid"))
        created_edge = load_schema("run-created-record.schema.json")["properties"]["dependency_edges"]["items"]["properties"]
        self.assertEqual(["fail_run", "skip_dependent", "continue"], created_edge["failure_policy"]["enum"])
        created = load_schema("run-created-record.schema.json")
        self.assertEqual("^[0-9a-f]{64}$", created["properties"]["policy_digest"]["pattern"])
        self.assertNotIn("policy_digest", created["required"])
        terminal = load_schema("run-terminal-record.schema.json")["properties"]["outcome"]["enum"]
        self.assertEqual(["succeeded", "failed", "cancelled", "skipped", "needs_operator", "uncertain", "partially_succeeded"], terminal)

    def test_task_contract_provenance_schemas_are_strict_and_forbid_semantic_scores(self) -> None:
        """Catches schema drift that loses append-only contract provenance or permits model-score authority."""
        expected = {
            "task-contract-record.schema.json": ("task_contract", "task-contract-", {"run_id", "item_id", "objective", "non_goals", "areas_to_avoid", "input_hashes", "acceptance_checks", "constraints", "risk_class", "retry_policy", "dependencies", "contract_digest"}),
            "plan-amendment-record.schema.json": ("plan_amendment", "plan-amendment-", {"run_id", "item_id", "task_contract_id", "previous_digest", "replacement_fields", "contract_digest"}),
            "acceptance-receipt-record.schema.json": ("acceptance_receipt", "acceptance-receipt-", {"run_id", "item_id", "attempt_id", "contract_digest", "check_ids", "reviewer", "evidence_bindings", "deviations", "result"}),
        }
        for name, (kind, prefix, fields) in expected.items():
            with self.subTest(schema=name):
                schema = load_schema(name)
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(kind, schema["properties"]["kind"]["const"])
                self.assertTrue(schema["properties"]["id"]["pattern"].startswith("^" + prefix))
                self.assertTrue(COMMON_REQUIRED | fields <= set(schema["required"]))
                self.assertNotIn("semantic_score", schema["properties"])
        amendment = load_schema("plan-amendment-record.schema.json")["properties"]["replacement_fields"]["properties"]
        self.assertEqual("string", amendment["objective"]["type"])
        self.assertEqual(["low", "medium", "high", "critical"], amendment["risk_class"]["enum"])
        self.assertEqual("array", amendment["dependencies"]["type"])
        contract = load_schema("task-contract-record.schema.json")
        self.assertIn("repository", contract["properties"])
        self.assertNotIn("repository", contract["required"])
        self.assertEqual(
            load_schema("decision-record.schema.json")["properties"]["repository"],
            contract["properties"]["repository"],
        )
        self.assertNotIn("repository", amendment)

    def test_decision_register_schemas_are_strict_and_bind_ruled_authority(self) -> None:
        """Catches a decision/capsule schema that reopens authority, source, scope, or digest semantics."""
        decision = load_schema("decision-record.schema.json")
        capsule = load_schema("handoff-capsule.schema.json")
        self.assertEqual("https://landoclusters.com/floati/schemas/v0/decision-record.schema.json", decision["$id"])
        self.assertEqual("https://landoclusters.com/floati/schemas/v0/handoff-capsule.schema.json", capsule["$id"])
        self.assertIs(False, decision["additionalProperties"])
        self.assertIs(False, capsule["additionalProperties"])
        self.assertEqual("decision_record", decision["properties"]["kind"]["const"])
        self.assertEqual(
            ["proposed", "accepted", "rejected"],
            decision["properties"]["status"]["enum"],
        )
        self.assertNotIn("superseded", decision["properties"]["status"]["enum"])
        self.assertEqual(
            {
                "schema_version", "id", "tenant_id", "timestamp", "kind", "repository",
                "decision_id", "scope", "statement", "status", "source_artifact_ids",
                "task_contract_id", "decided_by", "supersedes", "author_authority",
                "decision_digest",
            },
            set(decision["required"]),
        )
        self.assertEqual(
            "^decision-record-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$",
            decision["properties"]["id"]["pattern"],
        )
        self.assertEqual(
            "^decision-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$",
            decision["properties"]["decision_id"]["pattern"],
        )
        self.assertEqual(["operator", "architect", "worker"], decision["properties"]["author_authority"]["enum"])
        self.assertEqual(1, decision["properties"]["source_artifact_ids"]["minItems"])
        self.assertEqual(64, decision["properties"]["source_artifact_ids"]["maxItems"])
        self.assertEqual(6, len(decision["properties"]["source_artifact_ids"]["items"]["oneOf"]))
        self.assertEqual(
            {"repository", "path_prefix", "contract"},
            {form["properties"]["kind"]["const"] for form in decision["properties"]["scope"]["oneOf"]},
        )
        self.assertEqual("^[0-9a-f]{64}$", decision["properties"]["decision_digest"]["pattern"])
        self.assertEqual(["string", "null"], decision["properties"]["task_contract_id"]["type"])
        self.assertEqual(["string", "null"], decision["properties"]["supersedes"]["type"])

        def contract_scope_guard(schema: dict) -> dict:
            return next(
                clause
                for clause in schema["allOf"]
                if clause["if"]["properties"]["scope"]["properties"]["kind"]["const"] == "contract"
            )

        decision_contract_guard = contract_scope_guard(decision)
        self.assertEqual(
            "string",
            decision_contract_guard["then"]["properties"]["task_contract_id"]["type"],
        )

        self.assertEqual(
            {"schema_version", "kind", "repository", "ledger", "entries"},
            set(capsule["required"]),
        )
        self.assertNotIn("id", capsule["properties"])
        self.assertNotIn("timestamp", capsule["properties"])
        entry = capsule["properties"]["entries"]["items"]
        self.assertEqual(
            {"ledger_ordinal", "decision_id", "accepted_record_id", "decision_digest", "decision"},
            set(entry["required"]),
        )
        self.assertEqual("accepted", entry["properties"]["decision"]["properties"]["status"]["const"])
        capsule_decision = entry["properties"]["decision"]
        self.assertIn("author_authority", capsule_decision["required"])
        self.assertEqual(["operator", "architect"], capsule_decision["properties"]["author_authority"]["enum"])
        capsule_contract_guard = contract_scope_guard(capsule_decision)
        self.assertEqual(
            "string",
            capsule_contract_guard["then"]["properties"]["task_contract_id"]["type"],
        )
        self.assertEqual(
            {"repository", "path_prefix", "contract"},
            {form["properties"]["kind"]["const"] for form in capsule_decision["properties"]["scope"]["oneOf"]},
        )
        encoded = json.dumps((decision, capsule), sort_keys=True)
        for forbidden in ("memory", "summary", "inference", "score", "ranking"):
            self.assertNotIn('"' + forbidden + '"', encoded)

    def test_decision_and_capsule_schemas_pin_expressible_lexical_safety(self) -> None:
        """Catches generic timestamps or visible/path strings that admit controls, Bidi, or final newlines."""
        decision = load_schema("decision-record.schema.json")
        capsule = load_schema("handoff-capsule.schema.json")
        utc_timestamp = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$"
        visible = r"^[^\u0000-\u001F\u007F-\u009F\u00AD\u061C\u200B-\u200F\u202A-\u202E\u2060-\u206F\uFEFF\uFFF9-\uFFFB\uD800-\uDFFF]*$"
        final_newline = {"pattern": r"[\r\n]"}

        self.assertEqual(utc_timestamp, decision["properties"]["timestamp"].get("pattern"))
        self.assertIsNone(re.fullmatch(utc_timestamp, "2026-08-08T12:00:00+00:00"))
        self.assertIsNotNone(re.fullmatch(utc_timestamp, "2026-08-08T12:00:00.000Z"))
        visible_fields = (
            decision["properties"]["statement"],
            decision["properties"]["decided_by"],
            capsule["properties"]["entries"]["items"]["properties"]["decision"]["properties"]["statement"],
            capsule["properties"]["entries"]["items"]["properties"]["decision"]["properties"]["decided_by"],
        )
        for field in visible_fields:
            self.assertEqual(visible, field.get("pattern"))
            self.assertEqual(final_newline, field.get("not"))
            self.assertIsNone(re.fullmatch(visible, "safe\u202evalue"))
            self.assertIsNone(re.fullmatch(visible, "safe\ud800value"))

        for scope in (
            decision["properties"]["scope"],
            capsule["properties"]["entries"]["items"]["properties"]["decision"]["properties"]["scope"],
        ):
            path_prefix = next(
                form for form in scope["oneOf"]
                if form["properties"]["kind"]["const"] == "path_prefix"
            )["properties"]["path_prefix"]
            self.assertEqual("string", path_prefix["type"])
            self.assertGreater(path_prefix["minLength"], 0)
            self.assertIn("not", path_prefix)

        path_fields = (
            decision["properties"]["repository"],
            capsule["properties"]["repository"],
            capsule["properties"]["ledger"],
        )
        for field in path_fields:
            self.assertEqual(final_newline, field.get("not"))

    def test_decision_and_capsule_draft_validation_matches_terminal_and_path_refusals(self) -> None:
        """Catches published schemas accepting worker terminal truth or non-normal relative evidence paths."""
        decision_schema_path = SCHEMA_DIR / "decision-record.schema.json"
        capsule_schema_path = SCHEMA_DIR / "handoff-capsule.schema.json"

        def schema_accepts(instance: object, schema_path: Path) -> bool:
            try:
                validate_json_schema(instance, schema_path)
            except SchemaValidationError:
                return False
            return True

        record_uuid = "018f7e9b3c117abc8def0123456789ab"
        decision_uuid = "018f7e9b3c127abc8def0123456789ab"
        run_uuid = "018f7e9b3c137abc8def0123456789ab"
        sha = "a" * 40
        record = {
            "schema_version": 0,
            "id": "decision-record-" + record_uuid,
            "tenant_id": "alpha",
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "decision_record",
            "repository": "owner/repo",
            "decision_id": "decision-" + decision_uuid,
            "scope": {"kind": "repository"},
            "statement": "Keep schema and runtime refusal surfaces equal.",
            "status": "proposed",
            "author_authority": "worker",
            "source_artifact_ids": ["run:run-" + run_uuid],
            "task_contract_id": None,
            "decided_by": public_ids.reviewer(),
            "supersedes": None,
            "decision_digest": "b" * 64,
        }
        capsule = {
            "schema_version": 0,
            "kind": "handoff_capsule",
            "repository": "owner/repo",
            "ledger": "repositories/owner/repo/decisions.jsonl",
            "entries": [
                {
                    "ledger_ordinal": 2,
                    "decision_id": "decision-" + decision_uuid,
                    "accepted_record_id": "decision-record-" + record_uuid,
                    "decision_digest": "b" * 64,
                    "decision": {
                        "scope": {"kind": "repository"},
                        "statement": "Keep schema and runtime refusal surfaces equal.",
                        "author_authority": "operator",
                        "source_artifact_ids": ["run:run-" + run_uuid],
                        "task_contract_id": None,
                        "decided_by": public_ids.reviewer(),
                        "supersedes": None,
                        "status": "accepted",
                    },
                }
            ],
        }

        self.assertTrue(schema_accepts(record, decision_schema_path))
        self.assertTrue(schema_accepts(capsule, capsule_schema_path))
        for authority in ("operator", "architect"):
            for status in ("accepted", "rejected"):
                with self.subTest(authority=authority, status=status):
                    terminal = deepcopy(record)
                    terminal["status"] = status
                    terminal["author_authority"] = authority
                    self.assertTrue(schema_accepts(terminal, decision_schema_path))

        for status in ("accepted", "rejected"):
            worker_terminal = deepcopy(record)
            worker_terminal["status"] = status
            with self.subTest(authority="worker", status=status):
                self.assertFalse(schema_accepts(worker_terminal, decision_schema_path))

        invalid_paths = (
            "slip//decisions.py",
            "slip/./decisions.py",
            "slip/../decisions.py",
            "/slip/decisions.py",
            "slip/decisions.py/",
            "slip\\decisions.py",
            "slip/\x01decisions.py",
            "slip/\u202edecisions.py",
        )
        for path in invalid_paths:
            scoped_record = deepcopy(record)
            scoped_record["scope"] = {"kind": "path_prefix", "path_prefix": path}
            with self.subTest(schema="decision", path_prefix=repr(path)):
                self.assertFalse(schema_accepts(scoped_record, decision_schema_path))
            scoped_capsule = deepcopy(capsule)
            scoped_capsule["entries"][0]["decision"]["scope"] = {
                "kind": "path_prefix",
                "path_prefix": path,
            }
            with self.subTest(schema="capsule", path_prefix=repr(path)):
                self.assertFalse(schema_accepts(scoped_capsule, capsule_schema_path))

        for path in invalid_paths:
            source = "doc:" + path + "@" + sha
            sourced_record = deepcopy(record)
            sourced_record["source_artifact_ids"] = [source]
            with self.subTest(schema="decision", document_path=repr(path)):
                self.assertFalse(schema_accepts(sourced_record, decision_schema_path))
            sourced_capsule = deepcopy(capsule)
            sourced_capsule["entries"][0]["decision"]["source_artifact_ids"] = [source]
            with self.subTest(schema="capsule", document_path=repr(path)):
                self.assertFalse(schema_accepts(sourced_capsule, capsule_schema_path))
            runtime_record = deepcopy(sourced_record)
            runtime_record["decision_digest"] = decision_digest(runtime_record)
            with self.subTest(runtime_document_path=repr(path)):
                with self.assertRaises(ProtocolRefusal):
                    validate_decision_binding(
                        runtime_record,
                        source_resolver=lambda _repository, _source: True,
                    )

        valid_documents = (
            "doc:docs/design/fleet-observation-contract.md@" + sha,
            "doc:docs/@decision.md@" + sha,
            "doc:@decision.md@" + sha,
        )
        for source in valid_documents:
            document_record = deepcopy(record)
            document_record["source_artifact_ids"] = [source]
            with self.subTest(schema="decision", valid_document=source):
                self.assertTrue(schema_accepts(document_record, decision_schema_path))
            document_capsule = deepcopy(capsule)
            document_capsule["entries"][0]["decision"]["source_artifact_ids"] = [source]
            with self.subTest(schema="capsule", valid_document=source):
                self.assertTrue(schema_accepts(document_capsule, capsule_schema_path))

            runtime_record = deepcopy(document_record)
            runtime_record["decision_digest"] = decision_digest(runtime_record)
            with self.subTest(runtime_document=source):
                self.assertEqual(
                    runtime_record,
                    validate_decision_binding(
                        runtime_record,
                        source_resolver=lambda _repository, _source: True,
                    ),
                )

        for schema in (
            load_schema("decision-record.schema.json"),
            load_schema("handoff-capsule.schema.json")["properties"]["entries"]["items"]["properties"]["decision"],
        ):
            document_branch = next(
                branch
                for branch in schema["properties"]["source_artifact_ids"]["items"]["oneOf"]
                if branch.get("pattern", "").startswith("^doc:")
            )
            self.assertEqual(2048, document_branch["maxLength"])

        # Draft 2020-12 cannot express NFC normalization. The schema keeps
        # this decomposed path expressible; the runtime validator remains the
        # authoritative NFC refusal boundary.
        decomposed_document = "doc:docs/cafe\u0301.md@" + sha
        decomposed_record = deepcopy(record)
        decomposed_record["source_artifact_ids"] = [decomposed_document]
        self.assertTrue(schema_accepts(decomposed_record, decision_schema_path))
        decomposed_capsule = deepcopy(capsule)
        decomposed_capsule["entries"][0]["decision"]["source_artifact_ids"] = [decomposed_document]
        self.assertTrue(schema_accepts(decomposed_capsule, capsule_schema_path))

    def test_attempt_retry_schemas_are_strict_and_preserve_reserved_closure_fields(self) -> None:
        """Catches a schema that permits retry drift or loses deterministic closure reservations."""
        expected = {
            "attempt-opened-record.schema.json": ("attempt_opened", "attempt-opened-", {"ordinal", "scheduler_epoch", "fence_token", "max_attempts", "backoff"}),
            "attempt-started-record.schema.json": ("attempt_started", "attempt-started-", {"attempt_id", "ordinal", "attempt_opened_id", "dispatch_decision_id", "fence_token"}),
            "attempt-terminal-record.schema.json": ("attempt_terminal", "attempt-terminal-", {"retry_record_id", "next_attempt_id", "next_ordinal", "retry_delay_ms", "next_scheduler_epoch", "next_fence_token"}),
            "retry-scheduled-record.schema.json": ("retry_scheduled", "retry-scheduled-", {"previous_attempt_id", "attempt_terminal_id", "next_attempt_id", "next_ordinal", "delay_ms", "scheduler_epoch", "next_fence_token"}),
            "retry-exhausted-record.schema.json": ("retry_exhausted", "retry-exhausted-", {"attempt_id", "ordinal", "attempt_terminal_id", "max_attempts", "reason_code"}),
        }
        for name, (kind, prefix, fields) in expected.items():
            with self.subTest(schema=name):
                schema = load_schema(name)
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(kind, schema["properties"]["kind"]["const"])
                self.assertTrue(schema["properties"]["id"]["pattern"].startswith("^" + prefix))
                self.assertTrue(fields | {"run_id", "item_id"} <= set(schema["required"]))
        terminal = load_schema("attempt-terminal-record.schema.json")["properties"]
        self.assertEqual(["none", "scheduled", "exhausted"], terminal["retry_disposition"]["enum"])
        self.assertEqual("^[0-9a-f]{64}$", terminal["next_fence_token"]["pattern"])

    def test_cancellation_and_fence_schemas_are_strict_and_keep_the_frozen_vocabulary(self) -> None:
        """Catches a durable cancellation family that permits unknown fields or renamed governed values."""
        expected = {
            "cancel-requested-record.schema.json": ("cancel_requested", "cancel-requested-"),
            "cancel-scope-resolved-record.schema.json": ("cancel_scope_resolved", "cancel-scope-resolved-"),
            "cancel-observed-record.schema.json": ("cancel_observed", "cancel-observed-"),
            "cancel-signal-sent-record.schema.json": ("cancel_signal_sent", "cancel-signal-sent-"),
            "cancel-terminal-record.schema.json": ("cancel_terminal", "cancel-terminal-"),
            "cancel-unconfirmed-record.schema.json": ("cancel_unconfirmed", "cancel-unconfirmed-"),
            "stale-attempt-evidence-record.schema.json": ("stale_attempt_evidence", "stale-attempt-evidence-"),
            "stale-evidence-adopted-record.schema.json": ("stale_evidence_adopted", "stale-evidence-adopted-"),
            "attempt-harness-session-bound-record.schema.json": ("attempt_harness_session_bound", "attempt-harness-session-bound-"),
            "supervisor-orphaned-record.schema.json": ("supervisor_orphaned", "supervisor-orphaned-"),
        }
        for name, (kind, prefix) in expected.items():
            with self.subTest(schema=name):
                schema = load_schema(name)
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(kind, schema["properties"]["kind"]["const"])
                self.assertTrue(schema["properties"]["id"]["pattern"].startswith("^" + prefix))
        observed = load_schema("cancel-observed-record.schema.json")["properties"]
        self.assertEqual(["native", "local_process_only", "unavailable"], observed["cancel_mode"]["enum"])
        stale = load_schema("stale-attempt-evidence-record.schema.json")["properties"]
        self.assertEqual("^[0-9a-f]{64}$", stale["current_fence_token"]["pattern"])
        for name in (
            "stale-evidence-adopted-record.schema.json",
            "supervisor-orphaned-record.schema.json",
        ):
            with self.subTest(authority_schema=name):
                self.assertTrue(
                    {"authority_subject", "authority_epoch", "capability_record_id"}
                    <= set(load_schema(name)["required"])
                )
    def test_gateway_contract_schemas_are_strict_version_zero_objects(self) -> None:
        for name in GATEWAY_SCHEMAS:
            with self.subTest(name=name):
                schema = load_schema(name)
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
                self.assertEqual(f"https://landoclusters.com/floati/schemas/v0/{name}", schema["$id"])
                self.assertEqual("object", schema["type"])
                self.assertIs(False, schema["additionalProperties"])

    def test_confluence_read_contract_schemas_and_fixtures_are_strict_and_versioned(self) -> None:
        for name in READ_CONTRACT_SCHEMAS:
            with self.subTest(name=name):
                schema = load_schema(name)
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
                self.assertEqual(f"https://landoclusters.com/floati/schemas/v0/{name}", schema["$id"])
                self.assertEqual("object", schema["type"])
                self.assertIs(False, schema["additionalProperties"])

        fixture_root = Path("tests/fixtures/confluence/v0")
        status = json.loads((fixture_root / "fleet-status.json").read_text(encoding="utf-8"))
        receipts = json.loads((fixture_root / "receipts-read.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {"artifact_version", "command", "status", "evidence"}, set(status)
        )
        self.assertEqual(0, status["evidence"]["status_schema_version"])
        self.assertEqual("fleet_status", status["evidence"]["kind"])
        self.assertEqual(0, receipts["schema_version"])
        self.assertEqual("receipts_read_bundle", receipts["kind"])
        self.assertEqual(
            list(range(1, len(receipts["entries"]) + 1)),
            [entry["sequence"] for entry in receipts["entries"]],
        )
        encoded = json.dumps((status, receipts)).lower()
        self.assertNotIn('"pid"', encoded)
        self.assertNotIn('"process"', encoded)
        self.assertNotIn('"network"', encoded)

    def test_confluence_fixtures_validate_against_their_published_schemas(self) -> None:
        fixture_root = Path("tests/fixtures/confluence/v0")
        for fixture_name, schema_name in (
            ("fleet-status.json", "fleet-status-artifact.schema.json"),
            ("receipts-read.json", "receipts-read-bundle.schema.json"),
        ):
            with self.subTest(fixture=fixture_name):
                instance = json.loads(
                    (fixture_root / fixture_name).read_text(encoding="utf-8")
                )
                validate_json_schema(instance, SCHEMA_DIR / schema_name)

        status = json.loads(
            (fixture_root / "fleet-status.json").read_text(encoding="utf-8")
        )
        del status["evidence"]["mode"]
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                status, SCHEMA_DIR / "fleet-status-artifact.schema.json"
            )

    def test_receipts_read_schema_reuses_durable_record_contracts(self) -> None:
        schema = load_schema("receipts-read-bundle.schema.json")
        entry = schema["properties"]["entries"]["items"]
        refs = {choice["$ref"] for choice in entry["properties"]["record"]["oneOf"]}
        self.assertEqual(
            {
                "work-item-record.schema.json",
                "work-transition-record.schema.json",
                "delivery-receipt.schema.json",
                "ack-receipt.schema.json",
                "denial-receipt.schema.json",
                "worker-receipt-record.schema.json",
                "worker-refusal-record.schema.json",
            },
            refs,
        )
    def test_dark_adapter_schemas_are_not_durable_bus_record_kinds(self) -> None:
        adapter_names = (
            "codex-app-server-request.schema.json",
            "codex-app-server-response.schema.json",
            "codex-app-server-notification.schema.json",
        )
        for name in adapter_names:
            with self.subTest(name=name):
                schema = load_schema(name)
                self.assertNotIn("kind", schema["properties"])
                self.assertNotIn("tenant_id", schema["properties"])
                self.assertNotIn(name, SCHEMA_NAMES)

    def test_all_v0_schemas_are_strict_versioned_objects(self) -> None:
        for name in SCHEMA_NAMES:
            with self.subTest(name=name):
                schema = load_schema(name)
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
                self.assertEqual(f"https://landoclusters.com/floati/schemas/v0/{name}", schema["$id"])
                self.assertEqual("object", schema["type"])
                self.assertIs(False, schema["additionalProperties"])
                self.assertTrue(COMMON_REQUIRED.issubset(schema["required"]))
                self.assertEqual({"const": 0}, schema["properties"]["schema_version"])

    def test_delivery_and_ack_are_distinct_and_never_claim_action(self) -> None:
        delivery = load_schema("delivery-receipt.schema.json")
        ack = load_schema("ack-receipt.schema.json")
        self.assertEqual("delivery_receipt", delivery["properties"]["kind"]["const"])
        self.assertEqual("ack_receipt", ack["properties"]["kind"]["const"])
        self.assertIn("item_ids", delivery["required"])
        self.assertIn("item_ids", ack["required"])
        combined = json.dumps((delivery, ack)).lower()
        self.assertNotIn('"done"', combined)
        self.assertNotIn('"acted"', combined)

    def test_actor_bound_ack_v1_schema_is_closed_and_keeps_v0_history_separate(self) -> None:
        path = V1_SCHEMA_DIR / "ack-receipt.schema.json"
        self.assertTrue(path.is_file(), "actor-bound acknowledgment schema is absent")
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(1, schema["properties"]["schema_version"]["const"])
        self.assertIn("acting_session_id", schema["required"])
        for field in (
            "node_lease_id",
            "node_lease_state_at_ack",
            "node_lease_expires_at",
        ):
            self.assertIn(field, schema["properties"])
            self.assertNotIn(field, schema["required"])
            self.assertIn(field, schema["dependentRequired"])
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("acting_session_id", load_schema("ack-receipt.schema.json")["required"])

    def test_message_retraction_and_attempt_binding_preserve_the_architect_shapes(self) -> None:
        message = load_schema("message-envelope.schema.json")
        retraction = load_schema("message-retracted-record.schema.json")
        binding = message["properties"]["attempt_binding"]["oneOf"]
        self.assertEqual("absent_legacy", binding[0]["const"])
        complete = binding[1]
        self.assertFalse(complete["additionalProperties"])
        self.assertEqual(
            {"attempt_id", "claim_id", "lease_id", "worker_session_id"},
            set(complete["required"]),
        )
        self.assertEqual("message_retracted", retraction["properties"]["kind"]["const"])
        self.assertTrue(retraction["properties"]["id"]["pattern"].startswith("^ret-"))
        self.assertEqual(
            ["sent_in_error", "superseded_by_correction", "stale_recipient", "security_scrub"],
            retraction["properties"]["reason"]["enum"],
        )

    def test_three_planes_have_three_names_and_record_kinds(self) -> None:
        expected = {
            "liveness-presence-record.schema.json": "liveness_presence",
            "authority-grant-record.schema.json": "authority_grant",
            "mutual-exclusion-hold-record.schema.json": "mutual_exclusion_hold",
        }
        titles = set()
        for name, kind in expected.items():
            schema = load_schema(name)
            titles.add(schema["title"])
            self.assertEqual(kind, schema["properties"]["kind"]["const"])
        self.assertEqual(3, len(titles))
        self.assertFalse((SCHEMA_DIR / "lease.schema.json").exists())

    def test_wake_causes_and_cost_surfaces_are_exact_and_bounded(self) -> None:
        schema = load_schema("wake-cause-record.schema.json")
        cause = schema["properties"]["cause"]
        self.assertEqual(["self_wake", "external_injection", "resurrection"], cause["enum"])
        self.assertEqual(0, schema["properties"]["context_bytes"]["minimum"])
        self.assertEqual(65536, schema["properties"]["context_bytes"]["maximum"])
        self.assertEqual(1, schema["properties"]["wake_count"]["minimum"])

    def test_message_and_registry_fields_are_explicit(self) -> None:
        envelope = load_schema("message-envelope.schema.json")
        registry = load_schema("registry-entry.schema.json")
        expected = {
            "schema_version", "id", "tenant_id", "timestamp", "kind",
            "sender", "recipient", "repo", "sha", "doc", "note",
            "idempotency_key",
        }
        self.assertEqual(expected, set(envelope["required"]))
        optional = {"reply_to", "worker_session_id", "attempt_binding"}
        self.assertEqual(expected | optional, set(envelope["properties"]))
        self.assertTrue(optional.isdisjoint(envelope["required"]))
        self.assertEqual(
            {"absent_legacy"},
            {
                branch["const"]
                for branch in envelope["properties"]["attempt_binding"]["oneOf"]
                if "const" in branch
            },
        )
        self.assertNotIn("body", envelope["properties"])
        self.assertNotIn("wake_cause", envelope["properties"])
        self.assertEqual(128, envelope["properties"]["repo"]["maxLength"])
        self.assertEqual(1024, envelope["properties"]["doc"]["maxLength"])
        self.assertEqual(1024, envelope["properties"]["note"]["maxLength"])
        self.assertTrue({"node_id", "role", "state"}.issubset(registry["required"]))
        self.assertEqual(["active", "retired"], registry["properties"]["state"]["enum"])

    def test_record_identifiers_require_uuid7_version_and_variant_bits(self) -> None:
        prefixes = {
            "message-envelope.schema.json": "msg-",
            "delivery-receipt.schema.json": "delivery-",
            "ack-receipt.schema.json": "ack-",
            "denial-receipt.schema.json": "denial-",
            "liveness-presence-record.schema.json": "presence-",
            "authority-grant-record.schema.json": "authority-",
            "mutual-exclusion-hold-record.schema.json": "hold-",
            "registry-entry.schema.json": "registry-",
            "wake-cause-record.schema.json": "wake-",
            "work-item-record.schema.json": "work-",
            "work-transition-record.schema.json": "transition-",
            "capability-record.schema.json": "capability-",
            "approval-request-record.schema.json": "approval-request-",
            "approval-decision-record.schema.json": "approval-decision-",
            "worker-receipt-record.schema.json": "worker-receipt-",
            "worker-refusal-record.schema.json": "worker-refusal-",
        }
        valid = "018f0f23abcd71238000000000000000"
        invalid_version = "018f0f23abcd41238000000000000000"
        invalid_variant = "018f0f23abcd71237000000000000000"
        for name, prefix in prefixes.items():
            with self.subTest(name=name):
                pattern = load_schema(name)["properties"]["id"]["pattern"]
                self.assertIsNotNone(re.fullmatch(pattern, prefix + valid))
                self.assertIsNone(re.fullmatch(pattern, prefix + invalid_version))
                self.assertIsNone(re.fullmatch(pattern, prefix + invalid_variant))

    def test_work_items_and_transitions_are_distinct_orchestration_truth(self) -> None:
        item = load_schema("work-item-record.schema.json")
        transition = load_schema("work-transition-record.schema.json")

        self.assertEqual("work_item", item["properties"]["kind"]["const"])
        self.assertEqual("work_transition", transition["properties"]["kind"]["const"])
        self.assertEqual(["claim", "complete"], transition["properties"]["action"]["enum"])
        self.assertIn("artifact_bindings", item["required"])
        self.assertIn("artifact_bindings", transition["required"])
        self.assertNotIn("recipient", item["properties"])
        self.assertNotIn("message", transition["properties"])
        self.assertIn("workspace", item["properties"])
        self.assertNotIn("workspace", item["required"])
        workspace = item["properties"]["workspace"]
        self.assertEqual(["string", "null"], workspace["type"])
        self.assertIsNotNone(
            re.fullmatch(
                workspace["pattern"],
                "\x2fprivate\x2ftmp/floati-work/work-018f0f23abcd71238000000000000000",
            )
        )
        self.assertIsNone(re.fullmatch(workspace["pattern"], "\x2ftmp/inferred"))
        self.assertNotIn("needs", item["required"])
        needs = item["properties"]["needs"]
        self.assertEqual(64, needs["maxItems"])
        self.assertTrue(needs["uniqueItems"])
        self.assertIsNotNone(
            re.fullmatch(
                needs["items"]["pattern"],
                "work-018f0f23abcd71238000000000000000",
            )
        )

    def test_work_item_runtime_accepts_only_the_floati_workspace_root(self) -> None:
        """Catches runtime acceptance of a retired governed-workspace coordinate."""

        item_id = "work-018f0f23abcd71238000000000000000"
        record = {
            "schema_version": 0,
            "id": item_id,
            "tenant_id": "alpha",
            "timestamp": "2026-08-15T12:00:00.000Z",
            "kind": "work_item",
            "title": "rebaseline frozen workspace",
            "owner": public_ids.worker('alpha'),
            "artifact_bindings": [],
            "workspace": f"\x2fprivate\x2ftmp/floati-work/{item_id}",
        }

        def runtime_accepts(candidate: dict[str, object]) -> bool:
            try:
                validate_record(
                    candidate,
                    "alpha",
                    frozenset({"work_item"}),
                    integrity=False,
                )
            except ProtocolRefusal:
                return False
            return True

        for workspace, expected in (
            (f"\x2fprivate\x2ftmp/floati-work/{item_id}", True),
            (f"\x2fprivate\x2ftmp/slipway-work/{item_id}", False),
        ):
            with self.subTest(workspace=workspace):
                self.assertIs(expected, runtime_accepts(dict(record, workspace=workspace)))

    def test_work_item_schema_accepts_only_the_floati_workspace_root(self) -> None:
        """Catches schema validation accepting a retired governed-workspace coordinate."""

        item_id = "work-018f0f23abcd71238000000000000000"
        record = {
            "schema_version": 0,
            "id": item_id,
            "tenant_id": "alpha",
            "timestamp": "2026-08-15T12:00:00.000Z",
            "kind": "work_item",
            "title": "rebaseline frozen workspace",
            "owner": public_ids.worker('alpha'),
            "artifact_bindings": [],
            "workspace": f"\x2fprivate\x2ftmp/floati-work/{item_id}",
        }
        schema_path = SCHEMA_DIR / "work-item-record.schema.json"

        def schema_accepts(candidate: dict[str, object]) -> bool:
            try:
                validate_json_schema(candidate, schema_path)
            except SchemaValidationError:
                return False
            return True

        for workspace, expected in (
            (f"\x2fprivate\x2ftmp/floati-work/{item_id}", True),
            (f"\x2fprivate\x2ftmp/slipway-work/{item_id}", False),
        ):
            with self.subTest(workspace=workspace):
                self.assertIs(expected, schema_accepts(dict(record, workspace=workspace)))

    def test_capabilities_and_approvals_are_explicit_ttl_bound_records(self) -> None:
        capability = load_schema("capability-record.schema.json")
        request = load_schema("approval-request-record.schema.json")
        decision = load_schema("approval-decision-record.schema.json")

        self.assertEqual(
            ["unavailable", "read_only", "read_write"],
            capability["properties"]["mode"]["enum"],
        )
        self.assertTrue({"requester", "capability", "scope", "requested_ttl_seconds", "authority_subject", "authority_epoch"}.issubset(request["required"]))
        self.assertEqual(["approved", "denied"], decision["properties"]["decision"]["enum"])
        self.assertTrue({"request_id", "granted_scope", "granted_ttl_seconds", "reason_code", "expires_at"}.issubset(decision["required"]))

    def test_worker_receipts_are_typed_evidence_not_process_introspection(self) -> None:
        receipt = load_schema("worker-receipt-record.schema.json")
        self.assertEqual("worker_receipt", receipt["properties"]["kind"]["const"])
        self.assertEqual(
            ["claim", "spawn", "drive", "bind_artifact", "complete", "degrade"],
            receipt["properties"]["transition"]["enum"],
        )
        self.assertNotIn("pid", receipt["properties"])
        self.assertNotIn("process", receipt["properties"])
        self.assertEqual(
            {
                None,
                "adapter_error",
                "adapter_malformed_output",
                "credential_network_boundary_unruled",
                "approval_required_unattended",
                "artifact_ambiguous",
                "artifact_missing",
                "authority_deadline_unavailable",
                "authority_expired_mid_claim",
                "authority_state_unavailable",
                "git_finalize_failed",
                "process_cancelled",
                "process_died",
                "process_start_failed",
                "process_timeout",
                "protocol_error",
                "turn_failed",
                "worker_authority_changed",
                "workspace_invalid",
                "workspace_mapping_missing",
            },
            set(receipt["properties"]["outcome_code"]["enum"]),
        )

        refusal = load_schema("worker-refusal-record.schema.json")
        self.assertEqual("worker_refusal", refusal["properties"]["kind"]["const"])
        self.assertIn("reason_code", refusal["required"])
        self.assertEqual(
            {
                "authority_state_unavailable",
                "consumption_state_unavailable",
                "worker_adapter_absent",
                "worker_authority_changed",
                "worker_authority_ambiguous",
                "worker_authority_missing",
                "worker_claim_lost",
                "worker_node_inactive",
                "worker_work_blocked",
                "worker_work_absent",
                "worker_workspace_missing",
            },
            set(refusal["properties"]["reason_code"]["enum"]),
        )

    def test_v1_spawn_group_schemas(self) -> None:
        """Catches absent/open Task 1 schemas and the spawn-aware dispatch conditional."""
        from tests.test_spawn_groups import NOW, SpawnGroupFixtures, _record

        fixture = SpawnGroupFixtures()
        started = fixture.started_parent()
        group = fixture.group()
        amendment = fixture.amendment(group)
        rows = {
            "run-spawn-admission-enabled-record.schema.json": next(
                row for row in started if row["kind"] == "run_spawn_admission_enabled"
            ),
            "attempt-spawn-policy-bound-record.schema.json": next(
                row for row in started if row["kind"] == "attempt_spawn_policy_bound"
            ),
            "spawn-plan-amendment-record.schema.json": amendment,
            "spawn-group-created-record.schema.json": group,
            "spawn-group-aborted-record.schema.json": _record(
                "spawn_group_aborted", "spawn-group-aborted-", run_id=fixture.run_id,
                spawn_group_id=group["id"], parent_attempt_id=fixture.attempt,
                parent_fence_token=fixture.fence, reason_code="operator_abandonment",
                cancel_scope_resolved_id=None, operator_id="operator-a",
                authority_subject="authority", authority_epoch=1,
                capability_record_id="capability-" + uuid7_hex(),
                aborted_at_testimony=NOW,
            ),
            "child-admitted-record.schema.json": fixture.admitted(group, amendment),
            "child-rejected-record.schema.json": _record(
                "child_rejected", "child-rejected-", run_id=fixture.run_id,
                spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
                parent_attempt_id=fixture.attempt, child_item_id=fixture.child,
                reason_code="policy_refusal", evaluated_at_testimony=NOW,
            ),
            "spawn-group-closed-record.schema.json": _record(
                "spawn_group_closed", "spawn-group-closed-", run_id=fixture.run_id,
                spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
                parent_attempt_id=fixture.attempt, member_item_ids=[fixture.child],
                accepted_item_ids=[], terminal_item_ids=[], rejected_item_ids=[fixture.child],
                join_mode="all_terminal", required_count=None, outcome="satisfied",
                close_reason="all_members_terminal", cancel_scope_resolved_id=None,
                closed_at_testimony=NOW,
            ),
            "untracked-descendant-record.schema.json": _record(
                "untracked_descendant", "untracked-descendant-", run_id=fixture.run_id,
                parent_item_id=fixture.parent, parent_attempt_id=fixture.attempt,
                adapter="codex", provider_descendant_id="thread-1", state="observed",
                adopted_item_id=None, reason_code="native_descendant_observed",
                observed_at_testimony=NOW,
            ),
            "descendant-observation-closed-record.schema.json": _record(
                "descendant_observation_closed", "descendant-observation-closed-",
                run_id=fixture.run_id, parent_item_id=fixture.parent,
                parent_attempt_id=fixture.attempt, parent_fence_token=fixture.fence,
                attempt_spawn_policy_id=fixture.spawn_policy_id, adapter="codex",
                observed_descendant_ids=[], closed_at_testimony=NOW,
            ),
            "spawn-late-result-disposition-record.schema.json": _record(
                "spawn_late_result_disposition", "spawn-late-result-disposition-",
                run_id=fixture.run_id, spawn_group_id=group["id"],
                child_item_id=fixture.child,
                result_record_id="run-result-produced-" + uuid7_hex(),
                disposition="quarantine", operator_id="operator-a",
                authority_subject="authority", authority_epoch=1,
                capability_record_id="capability-" + uuid7_hex(),
                decided_at_testimony=NOW,
            ),
        }
        for name, row in rows.items():
            with self.subTest(schema=name):
                path = V1_SCHEMA_DIR / name
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(1, schema["properties"]["schema_version"]["const"])
                self.assertEqual(set(row), set(schema["required"]))
                validate_json_schema(row, path)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(dict(row, unexpected=True), path)

        dispatch_path = V1_SCHEMA_DIR / "run-dispatch-decision-record.schema.json"
        spawn_dispatch = next(row for row in started if row["kind"] == "dispatch_decision")
        validate_json_schema(spawn_dispatch, dispatch_path)
        with self.assertRaises(SchemaValidationError):
            invalid = dict(spawn_dispatch)
            invalid.pop("adapter")
            validate_json_schema(invalid, dispatch_path)


if __name__ == "__main__":
    unittest.main()
