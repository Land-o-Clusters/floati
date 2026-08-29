from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
from unittest import mock

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.records import validate_record
from floati.root import FloatiRoot
from tests.schema_validation import SchemaValidationError, validate_json_schema


NOW = "2026-08-13T12:00:00.000Z"
NOW_DATETIME = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
THREAD_SOURCE_HARNESS = (
    Path(__file__).parent
    / "fixtures"
    / "codex-thread-observer"
    / "reference_harness.py"
).resolve()
THREAD_OBSERVATION_KINDS = frozenset(
    {
        "thread_attachment_registered",
        "thread_observation_recorded",
        "thread_attachment_detached",
    }
)


def provider_uuid7() -> str:
    value = uuid7_hex()
    return "-".join(
        (value[:8], value[8:12], value[12:16], value[16:20], value[20:])
    )


def observation_digest(record: Dict[str, object]) -> str:
    domain = copy.deepcopy({
        field: record[field]
        for field in (
            "attachment_id",
            "provider",
            "provider_thread_id",
            "provider_status",
            "active_flags",
            "provider_updated_at",
            "attention",
            "observation_outcome",
            "observation_reason",
        )
    })
    updated = domain["provider_updated_at"]
    if isinstance(updated, dict):
        value = updated.get("value")
        if isinstance(value, float) and value.is_integer():
            updated["value"] = int(value)
    payload = json.dumps(
        domain,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def thread_record_rows(
    tenant_id: str = "alpha",
    *,
    subject_kind: str = "work_item",
) -> List[Dict[str, object]]:
    provider_thread_id = provider_uuid7()
    attachment_id = "thread-attachment-" + uuid7_hex()
    registered: Dict[str, object] = {
        "schema_version": 1,
        "id": attachment_id,
        "tenant_id": tenant_id,
        "timestamp": NOW,
        "kind": "thread_attachment_registered",
        "provider": "codex_local",
        "provider_thread_id": provider_thread_id,
        "subject_kind": subject_kind,
        "work_item_id": "work-" + uuid7_hex(),
        "registered_by": "observer-node",
        "registered_at_testimony": NOW,
    }
    if subject_kind == "attempt":
        registered["run_id"] = "run-" + uuid7_hex()
        registered["attempt_id"] = "attempt-" + uuid7_hex()
    observed: Dict[str, object] = {
        "schema_version": 1,
        "id": "thread-observation-" + uuid7_hex(),
        "tenant_id": tenant_id,
        "timestamp": NOW,
        "kind": "thread_observation_recorded",
        "attachment_id": attachment_id,
        "provider": "codex_local",
        "provider_thread_id": provider_thread_id,
        "provider_status": {"value": "active", "evidence_class": "measured"},
        "active_flags": {
            "value": ["waiting_on_user_input"],
            "evidence_class": "measured",
        },
        "provider_updated_at": {"value": 1786622400, "evidence_class": "measured"},
        "attention": {
            "value": "waiting_on_user_input",
            "evidence_class": "derived",
        },
        "observation_outcome": "observed",
        "observation_reason": "exact_thread_read",
        "observation_digest": "",
        "observed_at_testimony": NOW,
    }
    observed["observation_digest"] = observation_digest(observed)
    detached: Dict[str, object] = {
        "schema_version": 1,
        "id": "thread-attachment-detached-" + uuid7_hex(),
        "tenant_id": tenant_id,
        "timestamp": NOW,
        "kind": "thread_attachment_detached",
        "attachment_id": attachment_id,
        "provider": "codex_local",
        "provider_thread_id": provider_thread_id,
        "detached_by": "observer-node",
        "detached_at_testimony": NOW,
    }
    return [registered, observed, detached]


class ThreadObservationRecordTests(unittest.TestCase):
    """Catches runtime/schema drift in the closed testimony record family."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")

    def test_provider_thread_id_uses_canonical_hyphenated_uuidv7(self) -> None:
        rows = thread_record_rows(self.root.tenant_id)
        schemas = (
            "thread-attachment-registered-record.schema.json",
            "thread-observation-recorded-record.schema.json",
            "thread-attachment-detached-record.schema.json",
        )
        for row, schema in zip(rows, schemas):
            with self.subTest(kind=row["kind"]):
                validate_record(
                    dict(row),
                    self.root.tenant_id,
                    THREAD_OBSERVATION_KINDS,
                    integrity=False,
                )
                validate_json_schema(row, Path("schemas/v1") / schema)

                compact = copy.deepcopy(row)
                compact["provider_thread_id"] = str(row["provider_thread_id"]).replace(
                    "-", ""
                )
                if compact["kind"] == "thread_observation_recorded":
                    compact["observation_digest"] = observation_digest(compact)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        compact,
                        self.root.tenant_id,
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(compact, Path("schemas/v1") / schema)

    def test_runtime_and_schema_accept_the_exact_three_record_shapes(self) -> None:
        rows = thread_record_rows(self.root.tenant_id)
        schemas = (
            "thread-attachment-registered-record.schema.json",
            "thread-observation-recorded-record.schema.json",
            "thread-attachment-detached-record.schema.json",
        )
        for row, name in zip(rows, schemas):
            with self.subTest(kind=row["kind"]):
                try:
                    validated = validate_record(
                        dict(row),
                        self.root.tenant_id,
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )
                except ProtocolRefusal as exc:
                    self.fail(f"runtime rejected lawful {row['kind']}: {exc.code}")
                self.assertEqual(row, validated)
                try:
                    validate_json_schema(row, Path("schemas/v1") / name)
                except (FileNotFoundError, SchemaValidationError) as exc:
                    self.fail(f"schema rejected lawful {row['kind']}: {exc}")

    def test_attempt_registration_requires_the_exact_attempt_subject_fields(self) -> None:
        lawful = thread_record_rows(self.root.tenant_id, subject_kind="attempt")[0]
        schema = Path("schemas/v1/thread-attachment-registered-record.schema.json")
        try:
            validate_record(
                lawful,
                self.root.tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )
        except ProtocolRefusal as exc:
            self.fail(f"runtime rejected lawful attempt attachment: {exc.code}")
        try:
            validate_json_schema(lawful, schema)
        except SchemaValidationError as exc:
            self.fail(f"schema rejected lawful attempt attachment: {exc}")

        for field in ("run_id", "attempt_id"):
            with self.subTest(missing=field):
                candidate = dict(lawful)
                candidate.pop(field)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        candidate,
                        self.root.tenant_id,
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)

        work_item = thread_record_rows(self.root.tenant_id)[0]
        for field, value in (
            ("run_id", "run-" + uuid7_hex()),
            ("attempt_id", "attempt-" + uuid7_hex()),
        ):
            with self.subTest(extra=field):
                with self.assertRaises(ProtocolRefusal):
                    candidate = dict(work_item, **{field: value})
                    validate_record(
                        candidate,
                        self.root.tenant_id,
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)

    def test_observation_fields_require_exact_value_evidence_class_pairs(self) -> None:
        lawful = thread_record_rows(self.root.tenant_id)[1]
        schema = Path("schemas/v1/thread-observation-recorded-record.schema.json")
        try:
            validate_record(
                lawful,
                self.root.tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )
        except ProtocolRefusal as exc:
            self.fail(f"runtime rejected lawful observation: {exc.code}")

        hostile = (
            ("provider_status", {"value": "idle", "evidence_class": "derived"}),
            ("provider_status", {"value": "unknown", "evidence_class": "measured"}),
            (
                "active_flags",
                {"value": ["waiting_on_user_input"], "evidence_class": "unknown"},
            ),
            ("provider_updated_at", {"value": None, "evidence_class": "measured"}),
            ("attention", {"value": "none", "evidence_class": "measured"}),
        )
        for field, replacement in hostile:
            with self.subTest(field=field, replacement=replacement):
                candidate = copy.deepcopy(lawful)
                candidate[field] = replacement
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        candidate,
                        self.root.tenant_id,
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)

    def test_nested_unhashable_evidence_refuses_without_type_escape(self) -> None:
        lawful = thread_record_rows(self.root.tenant_id)[1]
        hostile = (
            ("provider_status", {"value": [], "evidence_class": "measured"}),
            ("attention", {"value": {}, "evidence_class": "derived"}),
            (
                "active_flags",
                {"value": [{}], "evidence_class": "measured"},
            ),
        )
        for field, replacement in hostile:
            with self.subTest(field=field):
                candidate = copy.deepcopy(lawful)
                candidate[field] = replacement
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        candidate,
                        self.root.tenant_id,
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )

    def test_unknown_observation_requires_null_values_and_unknown_evidence(self) -> None:
        lawful = thread_record_rows(self.root.tenant_id)[1]
        lawful.update(
            {
                "provider_status": {"value": "unknown", "evidence_class": "unknown"},
                "active_flags": {"value": None, "evidence_class": "unknown"},
                "provider_updated_at": {"value": None, "evidence_class": "unknown"},
                "attention": {"value": "unknown", "evidence_class": "unknown"},
                "observation_outcome": "unknown",
                "observation_reason": "provider_timeout",
            }
        )
        lawful["observation_digest"] = observation_digest(lawful)
        try:
            validate_record(
                lawful,
                self.root.tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )
        except ProtocolRefusal as exc:
            self.fail(f"runtime rejected lawful unknown testimony: {exc.code}")

    def test_attention_is_the_exact_derivative_of_measured_flags(self) -> None:
        lawful = thread_record_rows(self.root.tenant_id)[1]
        try:
            validate_record(
                lawful,
                self.root.tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )
        except ProtocolRefusal as exc:
            self.fail(f"runtime rejected lawful derived attention: {exc.code}")

        hostile = copy.deepcopy(lawful)
        hostile["attention"] = {"value": "none", "evidence_class": "derived"}
        hostile["observation_digest"] = observation_digest(hostile)
        with self.assertRaises(ProtocolRefusal):
            validate_record(
                hostile,
                self.root.tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )

    def test_observation_digest_binds_the_exact_normalized_testimony(self) -> None:
        lawful = thread_record_rows(self.root.tenant_id)[1]
        self.assertEqual(observation_digest(lawful), lawful["observation_digest"])
        try:
            validate_record(
                lawful,
                self.root.tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )
        except ProtocolRefusal as exc:
            self.fail(f"runtime rejected lawful observation digest: {exc.code}")

        hostile = copy.deepcopy(lawful)
        hostile["provider_updated_at"] = {
            "value": 1786622401,
            "evidence_class": "measured",
        }
        with self.assertRaises(ProtocolRefusal):
            validate_record(
                hostile,
                self.root.tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )

    def test_schema_matches_runtime_for_sorted_flags_and_derived_attention(self) -> None:
        schema = Path("schemas/v1/thread-observation-recorded-record.schema.json")
        lawful = thread_record_rows(self.root.tenant_id)[1]
        try:
            validate_json_schema(lawful, schema)
        except SchemaValidationError as exc:
            self.fail(f"schema rejected lawful derived testimony: {exc}")

        reversed_flags = copy.deepcopy(lawful)
        reversed_flags["active_flags"]["value"] = [
            "waiting_on_user_input",
            "waiting_on_approval",
        ]
        reversed_flags["attention"]["value"] = "multiple"
        reversed_flags["observation_digest"] = observation_digest(reversed_flags)

        wrong_attention = copy.deepcopy(lawful)
        wrong_attention["attention"]["value"] = "none"
        wrong_attention["observation_digest"] = observation_digest(wrong_attention)

        inactive_with_flag = copy.deepcopy(lawful)
        inactive_with_flag["provider_status"]["value"] = "idle"
        inactive_with_flag["observation_digest"] = observation_digest(
            inactive_with_flag
        )

        for name, candidate in (
            ("reversed-flags", reversed_flags),
            ("wrong-attention", wrong_attention),
            ("inactive-with-flag", inactive_with_flag),
        ):
            with self.subTest(case=name):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        candidate,
                        self.root.tenant_id,
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)

    def test_runtime_and_schema_agree_on_json_schema_integer_semantics(self) -> None:
        schema = Path("schemas/v1/thread-observation-recorded-record.schema.json")
        lawful = thread_record_rows(self.root.tenant_id)[1]
        lawful["provider_updated_at"]["value"] = 1786622400.0
        lawful["observation_digest"] = observation_digest(lawful)
        integer = copy.deepcopy(lawful)
        integer["provider_updated_at"]["value"] = 1786622400
        self.assertEqual(
            observation_digest(integer),
            observation_digest(lawful),
            "semantically equal JSON integers must have one normalized digest",
        )
        try:
            validate_json_schema(lawful, schema)
        except SchemaValidationError as exc:
            self.fail(f"schema rejected lawful integral JSON number: {exc}")
        try:
            validate_record(
                lawful,
                self.root.tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )
        except ProtocolRefusal as exc:
            self.fail(f"runtime rejected lawful integral JSON number: {exc.code}")

        boolean = copy.deepcopy(lawful)
        boolean["provider_updated_at"]["value"] = True
        boolean["observation_digest"] = observation_digest(boolean)
        with self.assertRaises(ProtocolRefusal):
            validate_record(
                boolean,
                self.root.tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(boolean, schema)

    def test_actor_grammar_and_terminal_unsafe_schema_parity(self) -> None:
        rows = thread_record_rows(self.root.tenant_id)
        schemas = (
            Path("schemas/v1/thread-attachment-registered-record.schema.json"),
            Path("schemas/v1/thread-observation-recorded-record.schema.json"),
            Path("schemas/v1/thread-attachment-detached-record.schema.json"),
        )

        actor_cases = (
            (rows[0], schemas[0], "registered_by"),
            (rows[2], schemas[2], "detached_by"),
        )
        for source, schema, field in actor_cases:
            with self.subTest(actor=field):
                hostile = copy.deepcopy(source)
                hostile[field] = "observer.node"
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        hostile,
                        self.root.tenant_id,
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(hostile, schema)

        lexical_cases = (
            (0, "id"),
            (0, "provider_thread_id"),
            (0, "work_item_id"),
            (1, "id"),
            (1, "attachment_id"),
            (1, "provider_thread_id"),
            (1, "observation_digest"),
            (2, "id"),
            (2, "attachment_id"),
            (2, "provider_thread_id"),
        )
        for index, field in lexical_cases:
            with self.subTest(kind=rows[index]["kind"], field=field):
                hostile = copy.deepcopy(rows[index])
                hostile[field] = str(hostile[field]) + "\n"
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        hostile,
                        self.root.tenant_id,
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(hostile, schemas[index])


class ThreadObservationProjectionTests(unittest.TestCase):
    """Catches replay that invents or reorders attachment testimony."""

    def projection(self, records: List[Dict[str, object]]):
        try:
            from floati.thread_observations import ThreadObservationProjection
        except ImportError as exc:
            self.fail(f"thread observation projection is absent: {exc}")
        return ThreadObservationProjection.from_records(records)

    def test_projection_replays_one_lawful_attachment_history(self) -> None:
        rows = thread_record_rows()
        original = copy.deepcopy(rows)
        projected = self.projection(rows).attachment(str(rows[0]["id"]))
        self.assertEqual(rows[0], projected["attachment"])
        self.assertEqual(rows[1], projected["latest_observation"])
        self.assertEqual(rows[2], projected["detachment"])
        self.assertEqual(original, rows)

    def test_projection_preserves_unknown_absence_and_lexical_attachment_order(self) -> None:
        first = thread_record_rows()[0]
        second = thread_record_rows()[0]
        records = [second, first]
        original = copy.deepcopy(records)
        projected = self.projection(records).attachments()

        self.assertEqual(
            sorted((str(first["id"]), str(second["id"]))),
            [str(row["attachment"]["id"]) for row in projected],
        )
        for row in projected:
            self.assertIsNone(row["latest_observation"])
            self.assertIsNone(row["detachment"])
        self.assertEqual(original, records)

    def test_projection_refuses_observation_before_registration_or_after_detachment(self) -> None:
        registered, observed, detached = thread_record_rows()
        cases = (
            ("before", [observed, registered]),
            ("after", [registered, detached, observed]),
        )
        for name, records in cases:
            with self.subTest(order=name):
                with self.assertRaises(IntegrityFailure):
                    self.projection(records)


class ThreadObserverControllerTests(unittest.TestCase):
    """Catches controller writes that bypass exact durable subject testimony."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = FloatiRoot.open(self.base, "alpha")

        from floati.registry import Registry
        from floati.work import WorkLog

        registry = Registry(self.root)
        registry.register("owner-node", "Codex")
        registry.register("observer-node", "Codex")
        registry.register("second-observer", "Codex")
        self.item = WorkLog(self.root).add("observe registered task", "owner-node", [])
        self.provider_thread_id = "018f3a2b-4c5d-7e8f-9a0b-1c2d3e4f5678"
        self._source_counter = 0

    def observer(self, mode: str = "idle"):
        try:
            from floati.thread_observations import ThreadObserver
            from floati.thread_source import CodexLocalThreadSource
        except ImportError as exc:
            self.fail(f"thread observer controller is absent: {exc}")
        self._source_counter += 1
        prefix = self.base / f"source-{self._source_counter}"
        source = CodexLocalThreadSource._for_test(
            [
                sys.executable,
                str(THREAD_SOURCE_HARNESS),
                mode,
                str(prefix) + "-methods",
                str(prefix) + "-params.json",
                str(prefix) + "-diagnostic.json",
            ]
        )
        return ThreadObserver._for_test(self.root, source), source

    def register(self, observer=None):
        selected = self.observer()[0] if observer is None else observer
        return selected.register_work_item(
            str(self.item["id"]),
            self.provider_thread_id,
            "observer-node",
            now=NOW_DATETIME,
        )

    def seed_attempt(self):
        from floati.contracts import TaskContract, contract_digest
        from floati.ids import uuid7_hex
        from floati.runtruth import RunLedger
        from floati.scheduler import RetryPolicy, RunScheduler

        run_id = "run-" + uuid7_hex()
        ledger = RunLedger(self.root)
        ledger.append(
            {
                "schema_version": 0,
                "id": "run-created-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": NOW,
                "kind": "run_created",
                "run_id": run_id,
                "plan_digest": "a" * 64,
                "item_ids": [self.item["id"]],
                "dependency_edges": [],
            }
        )
        policy = RetryPolicy(2, 10, 10)
        contract = TaskContract.create(
            objective="observe one exact attempt",
            non_goals=["no provider mutation"],
            areas_to_avoid=[{"path": "slip/thread_source.py", "region": "writes"}],
            input_hashes={"brief": "b" * 64},
            acceptance_checks={"tests.thread": "python3 -m unittest"},
            constraints={"network": "local"},
            risk_class="low",
            retry_policy={
                "max_attempts": 2,
                "backoff": {
                    "base_delay_ms": 10,
                    "cap_delay_ms": 10,
                    "strategy": "exponential",
                },
            },
            dependencies=[],
        )
        ledger.append(
            {
                "schema_version": 0,
                "id": "task-contract-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": NOW,
                "kind": "task_contract",
                "run_id": run_id,
                "item_id": self.item["id"],
                **contract.canonical(),
                "contract_digest": contract_digest(contract),
            }
        )
        ledger.append(
            {
                "schema_version": 0,
                "id": "run-policy-bound-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": NOW,
                "kind": "run_policy_bound",
                "run_id": run_id,
                "policy_digest": "a" * 64,
            }
        )
        ledger.append(
            {
                "schema_version": 0,
                "id": "run-worker-pool-bound-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": NOW,
                "kind": "worker_pool_bound",
                "run_id": run_id,
                "worker_ids": ["worker-a"],
            }
        )
        opened = RunScheduler(ledger).open_attempt(
            run_id, str(self.item["id"]), policy, 1, now=NOW
        )
        return run_id, opened

    def test_registration_retries_exactly_and_refuses_changed_or_missing_subjects(self) -> None:
        observer, _ = self.observer()
        first = self.register(observer)
        self.assertEqual(first, self.register(observer))
        self.assertEqual(1, len(observer.ledger.records()))

        with self.assertRaises(ProtocolRefusal):
            observer.register_work_item(
                str(self.item["id"]),
                self.provider_thread_id,
                "second-observer",
                now=NOW_DATETIME,
            )
        with self.assertRaises(ProtocolRefusal):
            observer.register_work_item(
                "work-" + uuid7_hex(),
                provider_uuid7(),
                "observer-node",
                now=NOW_DATETIME,
            )
        with self.assertRaises(ProtocolRefusal):
            observer.register_work_item(
                str(self.item["id"]),
                provider_uuid7(),
                "unregistered-node",
                now=NOW_DATETIME,
            )
        self.assertEqual(1, len(observer.ledger.records()))

    def test_attempt_registration_requires_exact_run_item_attempt_relation(self) -> None:
        observer, _ = self.observer()
        run_id, opened = self.seed_attempt()
        first = observer.register_attempt(
            run_id,
            str(self.item["id"]),
            str(opened["attempt_id"]),
            self.provider_thread_id,
            "observer-node",
            now=NOW_DATETIME,
        )
        self.assertEqual(
            first,
            observer.register_attempt(
                run_id,
                str(self.item["id"]),
                str(opened["attempt_id"]),
                self.provider_thread_id,
                "observer-node",
                now=NOW_DATETIME,
            ),
        )
        with self.assertRaises(ProtocolRefusal):
            observer.register_attempt(
                run_id,
                str(self.item["id"]),
                "attempt-" + uuid7_hex(),
                provider_uuid7(),
                "observer-node",
                now=NOW_DATETIME,
            )

    def test_observation_exact_retry_detach_and_post_detach_refusal(self) -> None:
        observer, _ = self.observer()
        attachment = self.register(observer)
        observed = observer.observe(str(attachment["id"]), now=NOW_DATETIME)
        self.assertEqual("observed", observed["observation_outcome"])
        self.assertEqual(observed, observer.observe(str(attachment["id"]), now=NOW_DATETIME))
        self.assertEqual(2, len(observer.ledger.records()))

        detached = observer.detach(
            str(attachment["id"]), "observer-node", now=NOW_DATETIME
        )
        self.assertEqual(
            detached,
            observer.detach(
                str(attachment["id"]), "observer-node", now=NOW_DATETIME
            ),
        )
        with self.assertRaises(ProtocolRefusal):
            observer.observe(str(attachment["id"]), now=NOW_DATETIME)
        self.assertEqual(3, len(observer.ledger.records()))

    def test_unknown_observation_is_bounded_and_raw_ledger_append_refuses(self) -> None:
        observer, _ = self.observer("missing")
        attachment = self.register(observer)
        row = observer.observe(str(attachment["id"]), now=NOW_DATETIME)
        self.assertEqual(
            ("unknown", "thread_missing"),
            (row["observation_outcome"], row["observation_reason"]),
        )
        self.assertEqual(
            {"value": "unknown", "evidence_class": "unknown"},
            row["provider_status"],
        )
        self.assertNotIn("HOSTILE", repr(row))
        with self.assertRaises(ProtocolRefusal):
            observer.ledger.append(row)

    def test_generic_jsonl_append_cannot_bypass_controller_authority(self) -> None:
        from floati.jsonl import append_record

        observer, _ = self.observer()
        attachment = self.register(observer)
        observed = observer.observe(str(attachment["id"]), now=NOW_DATETIME)
        forged = copy.deepcopy(observed)
        forged["id"] = "thread-observation-" + uuid7_hex()

        with self.assertRaisesRegex(ProtocolRefusal, "thread_observer_only"):
            append_record(
                self.root,
                observer.ledger.relative_path,
                forged,
                allowed_kinds=set(THREAD_OBSERVATION_KINDS),
            )
        self.assertEqual(2, len(observer.ledger.records()))

    def test_generic_jsonl_transaction_cannot_bypass_controller_authority(self) -> None:
        from floati.jsonl import transact

        observer, _ = self.observer()
        attachment = self.register(observer)
        observed = observer.observe(str(attachment["id"]), now=NOW_DATETIME)
        forged = copy.deepcopy(observed)
        forged["id"] = "thread-observation-" + uuid7_hex()

        with self.assertRaisesRegex(ProtocolRefusal, "thread_observer_only"):
            transact(
                self.root,
                observer.ledger.relative_path,
                lambda _existing: (forged, forged),
                allowed_kinds=set(THREAD_OBSERVATION_KINDS),
            )
        self.assertEqual(2, len(observer.ledger.records()))

    def test_changed_detachment_actor_refuses_without_a_second_row(self) -> None:
        observer, _ = self.observer()
        attachment = self.register(observer)
        observer.detach(
            str(attachment["id"]), "observer-node", now=NOW_DATETIME
        )
        with self.assertRaises(ProtocolRefusal):
            observer.detach(
                str(attachment["id"]), "second-observer", now=NOW_DATETIME
            )
        self.assertEqual(2, len(observer.ledger.records()))

    def test_detachment_wins_paused_observation_without_post_detach_row(self) -> None:
        observer, source = self.observer()
        attachment = self.register(observer)
        entered = threading.Event()
        release = threading.Event()
        actual_read = source.read
        outcomes: List[object] = []

        def paused_read(provider_thread_id: str, *, deadline_seconds: float = 5.0):
            entered.set()
            if not release.wait(2):
                raise AssertionError("observation pause was not released")
            return actual_read(
                provider_thread_id, deadline_seconds=deadline_seconds
            )

        def observe() -> None:
            try:
                outcomes.append(
                    observer.observe(str(attachment["id"]), now=NOW_DATETIME)
                )
            except BaseException as exc:
                outcomes.append(exc)

        with mock.patch.object(source, "read", side_effect=paused_read):
            worker = threading.Thread(target=observe)
            worker.start()
            self.assertTrue(entered.wait(1))
            observer.detach(
                str(attachment["id"]), "observer-node", now=NOW_DATETIME
            )
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(outcomes))
        self.assertIsInstance(outcomes[0], ProtocolRefusal)
        self.assertEqual(
            ["thread_attachment_registered", "thread_attachment_detached"],
            [row["kind"] for row in observer.ledger.records()],
        )


class ThreadObservationProjectionAdditionalTests(unittest.TestCase):
    """Retains the remaining pure replay corruption controls."""

    def projection(self, records: List[Dict[str, object]]):
        from floati.thread_observations import ThreadObservationProjection

        return ThreadObservationProjection.from_records(records)

    def test_projection_refuses_duplicate_physical_testimony_digest(self) -> None:
        registered, observed, _ = thread_record_rows()
        duplicate = copy.deepcopy(observed)
        duplicate["id"] = "thread-observation-" + uuid7_hex()
        with self.assertRaises(IntegrityFailure):
            self.projection([registered, observed, duplicate])

        changed = copy.deepcopy(duplicate)
        changed["provider_updated_at"] = {
            "value": 1786622401,
            "evidence_class": "measured",
        }
        changed["observation_digest"] = observation_digest(changed)
        projected = self.projection([registered, observed, changed])
        self.assertEqual(
            changed,
            projected.attachment(str(registered["id"]))["latest_observation"],
        )

    def test_projection_refuses_reused_global_record_identity(self) -> None:
        registered, observed, _ = thread_record_rows()
        changed = copy.deepcopy(observed)
        changed["provider_updated_at"] = {
            "value": 1786622401,
            "evidence_class": "measured",
        }
        changed["observation_digest"] = observation_digest(changed)
        self.assertEqual(observed["id"], changed["id"])
        with self.assertRaises(IntegrityFailure):
            self.projection([registered, observed, changed])

    def test_projection_refuses_duplicate_coordinates_mismatches_and_double_detach(self) -> None:
        registered, observed, detached = thread_record_rows()

        duplicate_registration = copy.deepcopy(registered)
        duplicate_registration["id"] = "thread-attachment-" + uuid7_hex()
        mismatched_observation = copy.deepcopy(observed)
        mismatched_observation["provider_thread_id"] = provider_uuid7()
        mismatched_observation["observation_digest"] = observation_digest(
            mismatched_observation
        )
        second_detachment = copy.deepcopy(detached)
        second_detachment["id"] = "thread-attachment-detached-" + uuid7_hex()

        cases = (
            ("duplicate-coordinate", [registered, duplicate_registration]),
            ("coordinate-mismatch", [registered, mismatched_observation]),
            ("double-detach", [registered, detached, second_detachment]),
        )
        for name, records in cases:
            with self.subTest(case=name):
                with self.assertRaises(IntegrityFailure):
                    self.projection(records)


if __name__ == "__main__":
    unittest.main()
