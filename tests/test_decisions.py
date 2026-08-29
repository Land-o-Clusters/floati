from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.root import FloatiRoot
from floati.contracts import TaskContract, contract_digest
from floati.runtruth import RUN_KINDS, RunLedger

try:
    from floati.decisions import DecisionRegister, decision_digest
except ModuleNotFoundError:
    DecisionRegister = None
    decision_digest = None

try:
    from floati.decisions import validate_decision_binding
except ImportError:
    validate_decision_binding = None


UUIDS = (
    "018f7e9b3c117abc8def0123456789ab",
    "018f7e9b3c127abc8def0123456789ab",
    "018f7e9b3c137abc8def0123456789ab",
    "018f7e9b3c147abc8def0123456789ab",
    "018f7e9b3c157abc8def0123456789ab",
    "018f7e9b3c167abc8def0123456789ab",
    "018f7e9b3c177abc8def0123456789ab",
    "018f7e9b3c187abc8def0123456789ab",
)


class _StatusFlippingDict(dict):
    """Mutate after the old append path has validated proposal-only status."""

    def __init__(self, value: dict[str, object]) -> None:
        super().__init__(value)
        self._status_reads = 0

    def __getitem__(self, key: object) -> object:
        value = super().__getitem__(key)
        if key == "status":
            self._status_reads += 1
            if self._status_reads == 5:
                super().__setitem__("status", "accepted")
        return value


class _ItemsBoomDict(dict):
    """Represent a caller mapping that cannot form one stable I-JSON snapshot."""

    def items(self):
        raise RuntimeError("hostile mapping refuses item iteration")


class _StrBoom:
    def __str__(self) -> str:
        raise RuntimeError("hostile explicit identifier refuses string coercion")


class _UUIDPretender:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class _IterBoomTuple(tuple):
    def __iter__(self):
        raise RuntimeError("hostile tuple source refuses iteration")


class _IterBoomList(list):
    def __iter__(self):
        raise RuntimeError("hostile list source refuses iteration")


class _RelativePathOverrideDecisionRegister(DecisionRegister):
    """A subclass must not redirect durable operations through a display property."""

    @property
    def relative_path(self) -> Path:
        return Path("arbitrary/overridden-decisions.jsonl")


class _RepositoryOverrideDecisionRegister(DecisionRegister):
    """A subclass must not rewrite the repository used by durable operations."""

    @property
    def repository(self) -> str:
        return "Other/Repo"


class _ObservationCoordinateOverrideDecisionRegister(DecisionRegister):
    """Read-only projection must likewise ignore overridden public display properties."""

    @property
    def repository(self) -> str:
        return "Other/Repo"

    @property
    def relative_path(self) -> Path:
        return Path("arbitrary/overridden-decisions.jsonl")


class _WritableTenantOverrideDecisionRegister(DecisionRegister):
    """A writable subclass must not change the tenant used for durable validation."""

    @property
    def tenant_id(self) -> str:
        return "bravo"


class _ObservationTenantOverrideDecisionRegister(DecisionRegister):
    """An observation subclass must not change the tenant used for replay."""

    @property
    def tenant_id(self) -> str:
        return "alpha"


class DecisionRegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = FloatiRoot.open(self.base, "alpha")
        self.repository = "Owner/Repo"
        self.source_run_id = self.seed_durable_run(self.root)

    @staticmethod
    def seed_durable_run(root: FloatiRoot) -> str:
        """Create the one real durable source used by ordinary decision fixtures."""

        run_id = "run-" + UUIDS[7]
        RunLedger(root).append(
            {
                "schema_version": 0,
                "id": "run-created-" + UUIDS[6],
                "tenant_id": root.tenant_id,
                "timestamp": "2026-08-08T12:00:00.000Z",
                "kind": "run_created",
                "run_id": run_id,
                "plan_digest": "a" * 64,
                "item_ids": ["work-" + UUIDS[5]],
                "dependency_edges": [],
            }
        )
        return run_id

    @staticmethod
    def refresh_digest(record: dict[str, object]) -> dict[str, object]:
        assert decision_digest is not None
        record["decision_digest"] = decision_digest(record)
        return record

    def require_register(self) -> None:
        self.assertIsNotNone(
            DecisionRegister,
            "floati.decisions must provide the repository-scoped decision register",
        )

    def record(
        self,
        record_uuid: str,
        decision_uuid: str,
        *,
        status: str = "proposed",
        repository: str | object = "Owner/Repo",
        scope: object = None,
        statement: object = "Keep append order authoritative.",
        source_artifact_ids: object = None,
        task_contract_id: object = None,
        author_authority: object = "worker",
        decided_by: object = "fable",
        supersedes: object = None,
        timestamp: object = "2026-08-08T12:00:00.000Z",
    ) -> dict[str, object]:
        if scope is None:
            scope = {"kind": "repository"}
        if source_artifact_ids is None:
            source_artifact_ids = ["run:" + self.source_run_id]
        record = {
            "schema_version": 0,
            "id": "decision-record-" + record_uuid,
            "tenant_id": "alpha",
            "timestamp": timestamp,
            "kind": "decision_record",
            "repository": repository,
            "decision_id": "decision-" + decision_uuid,
            "scope": scope,
            "statement": statement,
            "status": status,
            "author_authority": author_authority,
            "source_artifact_ids": source_artifact_ids,
            "task_contract_id": task_contract_id,
            "decided_by": decided_by,
            "supersedes": supersedes,
        }
        return self.refresh_digest(record)

    def persist(self, register: object, records: list[dict[str, object]]) -> None:
        path = self.root.resolve_relative(register.relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(encode_frame(record) for record in records))

    def binding_record(
        self,
        *,
        status: str = "proposed",
        authority: str = "worker",
        scope: object = None,
        sources: object = None,
        task_contract_id: object = None,
    ) -> dict[str, object]:
        """One direct Item 9 binding candidate, before generic-ledger integration."""

        if scope is None:
            scope = {"kind": "repository"}
        if sources is None:
            sources = ["run:run-" + UUIDS[2]]
        record = self.record(
            UUIDS[0],
            UUIDS[1],
            status=status,
            scope=scope,
            source_artifact_ids=sources,
            task_contract_id=task_contract_id,
        )
        record["author_authority"] = authority
        assert decision_digest is not None
        record["decision_digest"] = decision_digest(record)
        return record

    def require_binding_validator(self) -> None:
        self.assertIsNotNone(
            validate_decision_binding,
            "floati.decisions must provide the ruled, snapshotting Item 9 binding validator",
        )

    def test_binding_validator_allows_terminal_records_only_for_operator_or_architect(self) -> None:
        """Catches a worker (or unknown role) creating terminal decision truth."""
        self.require_binding_validator()
        assert validate_decision_binding is not None

        resolver = lambda repository, source: repository == self.repository and source.startswith("run:")
        for authority in ("operator", "architect"):
            with self.subTest(authority=authority):
                record = self.binding_record(status="accepted", authority=authority)
                self.assertEqual(record, validate_decision_binding(record, source_resolver=resolver))

        for authority, expected in (("worker", "decision_terminal_authority_invalid"), ("reviewer", "author_authority_invalid")):
            with self.subTest(authority=authority):
                with self.assertRaises(ProtocolRefusal) as caught:
                    validate_decision_binding(
                        self.binding_record(status="rejected", authority=authority),
                        source_resolver=resolver,
                    )
                self.assertEqual(expected, caught.exception.code)

        proposed = self.binding_record(status="proposed", authority="worker")
        self.assertEqual(proposed, validate_decision_binding(proposed, source_resolver=resolver))

    def test_binding_validator_requires_one_closed_scope_shape_and_contract_identity(self) -> None:
        """Catches ambiguous, traversal, or detached scope records entering the decision boundary."""
        self.require_binding_validator()
        assert validate_decision_binding is not None
        resolver = lambda repository, source: True

        valid_contract = "task-contract-" + UUIDS[2]
        valid_scopes = (
            ({"kind": "repository"}, None),
            ({"kind": "path_prefix", "path_prefix": "slip/decisions.py"}, None),
            ({"kind": "contract"}, valid_contract),
        )
        for scope, task_contract_id in valid_scopes:
            with self.subTest(scope=scope):
                record = self.binding_record(scope=scope, task_contract_id=task_contract_id)
                self.assertEqual(record, validate_decision_binding(record, source_resolver=resolver))

        invalid_scopes = (
            ({"kind": "path_prefix", "path_prefix": "../escape"}, None, "path_prefix_invalid"),
            ({"kind": "path_prefix", "path_prefix": "slip//decisions.py"}, None, "path_prefix_invalid"),
            ({"kind": "path_prefix", "path_prefix": "slip\\decisions.py"}, None, "path_prefix_invalid"),
            ({"kind": "repository", "path_prefix": "slip"}, None, "decision_scope_invalid"),
            ({"kind": "unknown"}, None, "decision_scope_invalid"),
            ({"kind": "contract"}, None, "task_contract_required"),
        )
        for scope, task_contract_id, expected in invalid_scopes:
            with self.subTest(scope=scope):
                with self.assertRaises(ProtocolRefusal) as caught:
                    validate_decision_binding(
                        self.binding_record(scope=scope, task_contract_id=task_contract_id),
                        source_resolver=resolver,
                    )
                self.assertEqual(expected, caught.exception.code)

    def test_binding_validator_closes_source_vocabulary_and_requires_injected_resolution(self) -> None:
        """Catches free-form provenance, duplicate sources, or a document claim accepted without an explicit resolver."""
        self.require_binding_validator()
        assert validate_decision_binding is not None
        sources = [
            "run:run-" + UUIDS[2],
            "attempt:attempt-" + UUIDS[3],
            "contract:task-contract-" + UUIDS[4],
            "receipt:worker-receipt-" + UUIDS[5],
            "decision:decision-" + UUIDS[6],
            "doc:docs/design/decision.md@" + "a" * 40,
        ]
        record = self.binding_record(sources=sources)
        with self.assertRaises(ProtocolRefusal) as unavailable:
            validate_decision_binding(record)
        self.assertEqual("source_lookup_unavailable", unavailable.exception.code)

        seen: list[tuple[str, str]] = []

        def resolver(repository: str, source: str) -> bool:
            seen.append((repository, source))
            return repository == self.repository

        self.assertEqual(record, validate_decision_binding(record, source_resolver=resolver))
        self.assertEqual([(self.repository, source) for source in sources], seen)

        for malformed in ("https://example.invalid/proof", "doc:/absolute.md@" + "a" * 40, "doc:docs/../proof.md@" + "a" * 40):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ProtocolRefusal) as caught:
                    validate_decision_binding(
                        self.binding_record(sources=[malformed]), source_resolver=resolver,
                    )
                self.assertEqual("source_artifact_id_invalid", caught.exception.code)

        duplicate = self.binding_record(sources=[sources[0], sources[0]])
        with self.assertRaises(ProtocolRefusal) as duplicate_refusal:
            validate_decision_binding(duplicate, source_resolver=resolver)
        self.assertEqual("source_artifact_ids_invalid", duplicate_refusal.exception.code)

        def unavailable_resolver(repository: str, source: str) -> bool:
            raise OSError("read-only source proof unavailable")

        with self.assertRaises(IntegrityFailure) as persisted_refusal:
            validate_decision_binding(record, source_resolver=unavailable_resolver, integrity=True)
        self.assertEqual("source_lookup_unavailable", persisted_refusal.exception.code)

    def test_binding_digest_covers_full_record_except_its_own_field_and_validator_snapshots(self) -> None:
        """Catches a semantic-only digest or a caller mutation changing a validated binding record."""
        self.require_binding_validator()
        assert decision_digest is not None
        assert validate_decision_binding is not None

        record = self.binding_record(status="accepted", authority="operator")
        baseline = record["decision_digest"]
        self.assertEqual(baseline, decision_digest(record))
        for field, value in (("timestamp", "2026-08-08T12:00:00.001Z"), ("author_authority", "architect")):
            with self.subTest(field=field):
                changed = dict(record)
                changed[field] = value
                self.assertNotEqual(baseline, decision_digest(changed))

        resolver = lambda repository, source: True
        validated = validate_decision_binding(record, source_resolver=resolver)
        record["author_authority"] = "worker"
        assert isinstance(record["scope"], dict)
        record["scope"]["kind"] = "contract"
        self.assertEqual("operator", validated["author_authority"])
        self.assertEqual({"kind": "repository"}, validated["scope"])

        changed_digest = dict(validated)
        changed_digest["decision_digest"] = "0" * 64
        with self.assertRaises(ProtocolRefusal) as caught:
            validate_decision_binding(changed_digest, source_resolver=resolver)
        self.assertEqual("decision_digest_invalid", caught.exception.code)

    def test_decision_schema_pins_the_ruled_authority_scope_source_and_digest_surface(self) -> None:
        """Catches a published schema that omits the ruled decision binding or reopens its vocabulary."""
        schema_path = Path(__file__).parents[1] / "schemas/v0/decision-record.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertTrue(
            {"author_authority", "decision_digest"} <= set(schema["required"]),
        )
        self.assertEqual(
            ["operator", "architect", "worker"],
            schema["properties"]["author_authority"]["enum"],
        )
        scope_forms = schema["properties"]["scope"]["oneOf"]
        self.assertEqual(
            {"repository", "path_prefix", "contract"},
            {form["properties"]["kind"]["const"] for form in scope_forms},
        )
        sources = schema["properties"]["source_artifact_ids"]
        self.assertEqual(1, sources["minItems"])
        self.assertEqual(6, len(sources["items"]["oneOf"]))
        self.assertEqual("^[0-9a-f]{64}$", schema["properties"]["decision_digest"]["pattern"])

    def test_handoff_schema_exposes_the_bound_accepted_decision_without_reopening_it(self) -> None:
        """Catches a capsule schema that hides terminal authority or reverts to free-text scope/source fields."""
        schema_path = Path(__file__).parents[1] / "schemas/v0/handoff-capsule.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        decision = schema["properties"]["entries"]["items"]["properties"]["decision"]
        self.assertIn("author_authority", decision["required"])
        self.assertEqual(
            ["operator", "architect"],
            decision["properties"]["author_authority"]["enum"],
        )
        self.assertEqual(
            {"repository", "path_prefix", "contract"},
            {form["properties"]["kind"]["const"] for form in decision["properties"]["scope"]["oneOf"]},
        )
        self.assertEqual(1, decision["properties"]["source_artifact_ids"]["minItems"])

    def test_explicit_root_coordinate_is_confined_without_discovery(self) -> None:
        """Catches a register that derives a writable ledger from cwd, Git, home, or a traversal coordinate."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        self.assertEqual(
            Path("repositories/Owner/Repo/decisions.jsonl"), register.relative_path,
        )
        proposal = self.record(UUIDS[0], UUIDS[1])
        register.append(proposal)
        self.assertEqual(
            [proposal],
            register.records(),
        )
        self.assertTrue(self.root.resolve_relative(register.relative_path).is_file())

        for invalid in (
            "", ".", "..", "owner//repo", "owner/./repo", "owner/../repo",
            "/owner/repo", "owner\\repo", "owner/repo/extra", "owner/\u202erepo",
            ["owner/repo"],
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProtocolRefusal) as caught:
                    DecisionRegister(self.root, invalid)
                self.assertEqual("repository_invalid", caught.exception.code)

    def test_proposals_are_idempotent_and_worker_terminal_writes_refuse_without_append(self) -> None:
        """Catches a changed-ID replay or a worker creating accepted decision truth."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        proposal = self.record(UUIDS[0], UUIDS[1])
        self.assertEqual(proposal, register.append(proposal))
        path = self.root.resolve_relative(register.relative_path)
        before = path.read_bytes()
        self.assertEqual(proposal, register.append(dict(proposal)))
        self.assertEqual(before, path.read_bytes())

        changed = dict(proposal)
        changed["statement"] = "Changed payload must not replace a proposal."
        self.refresh_digest(changed)
        with self.assertRaises(ProtocolRefusal) as duplicate:
            register.append(changed)
        self.assertEqual("duplicate_record_id", duplicate.exception.code)
        self.assertEqual(before, path.read_bytes())

        terminal = self.record(UUIDS[2], UUIDS[1], status="accepted")
        with self.assertRaises(ProtocolRefusal) as authority:
            register.append(terminal)
        self.assertEqual("decision_terminal_authority_invalid", authority.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_append_snapshots_hostile_and_ordinary_caller_mappings(self) -> None:
        """Catches an authority bypass or changed return value through caller-owned mapping aliases."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        hostile = _StatusFlippingDict(self.record(UUIDS[0], UUIDS[1]))
        appended = register.append(hostile)
        self.assertEqual("proposed", appended["status"])
        self.assertEqual("proposed", register.records()[0]["status"])

        ordinary = self.record(UUIDS[2], UUIDS[3], statement="Original immutable caller snapshot.")
        returned = register.append(ordinary)
        ordinary["status"] = "accepted"
        ordinary["statement"] = "Caller mutation must not alter the append result."
        self.assertEqual("proposed", returned["status"])
        self.assertEqual("Original immutable caller snapshot.", returned["statement"])
        self.assertEqual(
            "Original immutable caller snapshot.",
            register.records()[1]["statement"],
        )

    def test_append_and_project_refuse_a_shared_physical_logical_uuid_component(self) -> None:
        """Catches a decision whose physical frame and logical identity reuse one UUIDv7 component."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        equal_components = self.record(UUIDS[4], UUIDS[4])
        with self.assertRaises(ProtocolRefusal) as append_refusal:
            register.append(equal_components)
        self.assertEqual("decision_id_not_independent", append_refusal.exception.code)
        self.assertEqual([], register.records())

        self.persist(register, [equal_components])
        with self.assertRaises(IntegrityFailure) as persisted_refusal:
            register.project()
        self.assertEqual("decision_id_not_independent", persisted_refusal.exception.code)

    def test_append_types_unserializable_hostile_mapping_without_a_partial_write(self) -> None:
        """Catches a caller mapping whose encoder exception escapes the public decision boundary."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        hostile = _ItemsBoomDict(self.record(UUIDS[5], UUIDS[6]))
        try:
            register.append(hostile)
        except ProtocolRefusal as caught:
            self.assertEqual("decision_snapshot_invalid", caught.code)
        except Exception as escaped:
            self.fail(f"append leaked {type(escaped).__name__} instead of ProtocolRefusal")
        else:
            self.fail("append accepted an unserializable hostile mapping")
        self.assertEqual([], register.records())

    def test_candidate_and_persisted_unpaired_surrogates_keep_typed_boundaries(self) -> None:
        """Catches persisted invalid Unicode escaping as a candidate protocol refusal during capsule projection."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        proposal = self.record(UUIDS[0], UUIDS[1])
        proposal["statement"] = "\ud800"
        with self.assertRaises(ProtocolRefusal):
            register.append(proposal)
        self.assertEqual([], register.records())

        accepted = self.record(
            UUIDS[2], UUIDS[1], status="accepted", author_authority="operator",
        )
        accepted["statement"] = "\ud800"
        path = self.root.resolve_relative(register.relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"".join(
                json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
                for record in (proposal, accepted)
            )
        )
        try:
            register.capsule()
        except IntegrityFailure as caught:
            self.assertEqual("statement_invalid", caught.code)
        except Exception as escaped:
            self.fail(f"persisted surrogate leaked {type(escaped).__name__} instead of IntegrityFailure")
        else:
            self.fail("persisted surrogate produced a capsule")

    def test_idempotent_retry_refuses_a_persisted_semantic_corrupt_tail(self) -> None:
        """Catches an idempotent proposal retry that reports success without replaying a later corrupt decision frame."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        proposal = self.record(UUIDS[0], UUIDS[1])
        register.append(proposal)
        corrupt_tail = self.record(
            UUIDS[2], UUIDS[3], status="accepted", author_authority="operator",
        )
        path = self.root.resolve_relative(register.relative_path)
        path.write_bytes(path.read_bytes() + encode_frame(corrupt_tail))

        with self.assertRaises(IntegrityFailure) as caught:
            register.append(proposal)
        self.assertEqual("decision_proposal_missing", caught.exception.code)

    def test_physical_projection_derives_four_states_and_current_acceptance(self) -> None:
        """Catches timestamp-led state, mutable supersession, or a rejected/proposed decision entering the accepted projection."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        old_proposal = self.record(UUIDS[0], UUIDS[1], statement="Use one explicit ledger.")
        old_accepted = self.record(
            UUIDS[2], UUIDS[1], status="accepted", author_authority="operator",
            statement="Use one explicit ledger.",
            timestamp="2036-01-01T00:00:00.000Z",
        )
        current_proposal = self.record(UUIDS[3], UUIDS[4])
        current_accepted = self.record(
            UUIDS[5], UUIDS[4], status="accepted", author_authority="operator",
            supersedes="decision-" + UUIDS[1],
            timestamp="2020-01-01T00:00:00.000Z",
        )
        rejected_proposal = self.record(UUIDS[6], UUIDS[7], statement="Rejected path.")
        rejected = self.record(
            UUIDS[1], UUIDS[7], status="rejected", author_authority="operator",
            statement="Rejected path.",
        )
        self.persist(
            register,
            [old_proposal, old_accepted, current_proposal, current_accepted, rejected_proposal, rejected],
        )

        projection = register.project()
        self.assertEqual("superseded", projection.status_for(old_proposal["decision_id"]))
        self.assertEqual("accepted", projection.status_for(current_proposal["decision_id"]))
        self.assertEqual("rejected", projection.status_for(rejected_proposal["decision_id"]))
        current = projection.current_accepted()
        self.assertEqual([current_proposal["decision_id"]], [entry.decision_id for entry in current])
        self.assertEqual([4], [entry.ledger_ordinal for entry in current])
        self.assertEqual([current_accepted["id"]], [entry.accepted_record_id for entry in current])

    def test_projection_refuses_forward_cross_repository_and_mutated_persisted_rows(self) -> None:
        """Catches a replay that partially projects causally invalid or copied decision evidence."""
        self.require_register()

        cases = []
        accepted_without_proposal = self.record(
            UUIDS[0], UUIDS[1], status="accepted", author_authority="operator",
        )
        cases.append(([accepted_without_proposal], "decision_proposal_missing"))

        copied = self.record(UUIDS[0], UUIDS[1], repository="Other/Repo")
        cases.append(([copied], "decision_repository_mismatch"))

        proposal = self.record(UUIDS[0], UUIDS[1])
        mutated = self.record(
            UUIDS[2], UUIDS[1], status="accepted", author_authority="operator",
            statement="A terminal disposition cannot rewrite its proposal.",
        )
        cases.append(([proposal, mutated], "decision_payload_mutated"))

        target_proposal = self.record(UUIDS[0], UUIDS[1])
        candidate_proposal = self.record(UUIDS[2], UUIDS[3])
        forward = self.record(
            UUIDS[4], UUIDS[3], status="accepted", author_authority="operator",
            supersedes="decision-" + UUIDS[1],
        )
        cases.append(([target_proposal, candidate_proposal, forward], "decision_supersedes_target_invalid"))

        for records, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open(Path(directory), "alpha")
                self.seed_durable_run(root)
                register = DecisionRegister(root, self.repository)
                path = root.resolve_relative(register.relative_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"".join(encode_frame(record) for record in records))
                with self.assertRaises(IntegrityFailure) as caught:
                    register.project()
                self.assertEqual(code, caught.exception.code)

    def test_capsule_is_byte_stable_current_acceptance_without_memory_or_summary(self) -> None:
        """Catches a capsule that sorts by timestamp, emits superseded evidence, or synthesizes agent memory."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        old_proposal = self.record(UUIDS[0], UUIDS[1], statement="Use one explicit ledger.")
        old_accepted = self.record(
            UUIDS[2], UUIDS[1], status="accepted", author_authority="operator",
            statement="Use one explicit ledger.",
            timestamp="2036-01-01T00:00:00.000Z",
        )
        current_proposal = self.record(UUIDS[3], UUIDS[4])
        current_accepted = self.record(
            UUIDS[5], UUIDS[4], status="accepted", author_authority="operator",
            supersedes="decision-" + UUIDS[1],
            timestamp="2020-01-01T00:00:00.000Z",
        )
        self.persist(register, [old_proposal, old_accepted, current_proposal, current_accepted])

        capsule = register.capsule()
        encoded = register.capsule_bytes()
        self.assertEqual(encoded, register.capsule_bytes())
        self.assertEqual(capsule, json.loads(encoded.decode("utf-8")))
        self.assertEqual(
            f"repositories/{capsule['repository']}/decisions.jsonl",
            capsule["ledger"],
        )
        self.assertEqual(
            {
                "schema_version": 0,
                "kind": "handoff_capsule",
                "repository": self.repository,
                "ledger": "repositories/Owner/Repo/decisions.jsonl",
                "entries": [
                    {
                        "ledger_ordinal": 4,
                        "decision_id": "decision-" + UUIDS[4],
                        "accepted_record_id": "decision-record-" + UUIDS[5],
                        "decision_digest": current_accepted["decision_digest"],
                        "decision": {
                            "scope": {"kind": "repository"},
                            "statement": "Keep append order authoritative.",
                            "author_authority": "operator",
                            "source_artifact_ids": ["run:" + self.source_run_id],
                            "task_contract_id": None,
                            "decided_by": "fable",
                            "supersedes": "decision-" + UUIDS[1],
                            "status": "accepted",
                        },
                    }
                ],
            },
            capsule,
        )
        rendered = encoded.decode("utf-8")
        for forbidden in ("memory", "summary", "inference", "score", "ranking"):
            self.assertNotIn(forbidden, rendered)

    def test_observation_capsule_is_read_only_and_never_creates_a_lock_file(self) -> None:
        """Catches read-only capsule generation that takes a writer lock or grants an observation writer authority."""
        self.require_register()
        writer = FloatiRoot.open(self.base, "bravo")
        self.seed_durable_run(writer)
        writable = DecisionRegister(writer, self.repository)
        proposal = self.record(UUIDS[0], UUIDS[1])
        proposal["tenant_id"] = "bravo"
        self.refresh_digest(proposal)
        writable.append(proposal)
        observation = self.root.observe_tenant(self.root.grant_observation("bravo"), "bravo")
        observed = DecisionRegister.observe(observation, self.repository)
        before = sorted(path.relative_to(writer.tenant_home) for path in writer.tenant_home.rglob("*"))
        self.assertEqual([], observed.capsule()["entries"])
        after = sorted(path.relative_to(writer.tenant_home) for path in writer.tenant_home.rglob("*"))
        self.assertEqual(before, after)
        with self.assertRaises(ProtocolRefusal) as caught:
            observed.append(proposal)
        self.assertEqual("write_root_required", caught.exception.code)

    def test_propose_creates_independent_uuid7_record_and_logical_ids(self) -> None:
        """Catches a proposal helper that reuses its physical and logical decision identities."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        record = register.propose(
            timestamp="2026-08-08T12:00:00.000Z",
            scope={"kind": "repository"},
            statement="Proposals remain explicit.",
            decided_by="fable",
            author_authority="worker",
            source_artifact_ids=["run:" + self.source_run_id],
        )
        self.assertIsNotNone(re.fullmatch(r"decision-record-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}", record["id"]))
        self.assertIsNotNone(re.fullmatch(r"decision-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}", record["decision_id"]))
        self.assertNotEqual(record["id"].removeprefix("decision-record-"), record["decision_id"].removeprefix("decision-"))

    def test_propose_types_hostile_explicit_values_without_coercion_or_append(self) -> None:
        """Catches raw helper exceptions or arbitrary-object coercion before proposal validation."""
        self.require_register()
        cases = (
            ("record_id_str_boom", {"record_id": _StrBoom(), "decision_id": UUIDS[1]}),
            ("decision_id_str_boom", {"record_id": UUIDS[0], "decision_id": _StrBoom()}),
            ("record_id_uuid_pretender", {"record_id": _UUIDPretender(UUIDS[0]), "decision_id": UUIDS[1]}),
            ("tuple_source_iter_boom", {"source_artifact_ids": _IterBoomTuple()}),
            ("list_source_iter_boom", {"source_artifact_ids": _IterBoomList()}),
        )
        for name, kwargs in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open(Path(directory), "alpha")
                self.seed_durable_run(root)
                register = DecisionRegister(root, self.repository)
                inputs = {
                    "author_authority": "worker",
                    "source_artifact_ids": ["run:" + self.source_run_id],
                    **kwargs,
                }
                try:
                    register.propose(
                        timestamp="2026-08-08T12:00:00.000Z",
                        scope={"kind": "repository"},
                        statement="Hostile public input must not append.",
                        decided_by="fable",
                        **inputs,
                    )
                except ProtocolRefusal:
                    pass
                except Exception as escaped:
                    self.fail(f"propose leaked {type(escaped).__name__} instead of ProtocolRefusal")
                else:
                    self.fail("propose accepted hostile or coercible public input")
                self.assertEqual([], register.records())

    def test_public_relative_path_mutation_never_retargets_append(self) -> None:
        """Catches public path replacement that writes a proposal outside its bound repository coordinate."""
        self.require_register()
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            self.seed_durable_run(root)
            register = DecisionRegister(root, self.repository)
            original = register.relative_path
            alternate = Path("arbitrary/outside.jsonl")
            try:
                register.relative_path = alternate
            except (AttributeError, ProtocolRefusal):
                proposal = self.record(UUIDS[0], UUIDS[1])
                self.assertEqual(proposal, register.append(proposal))
                self.assertTrue(root.resolve_relative(original).is_file())
            else:
                try:
                    register.append(self.record(UUIDS[0], UUIDS[1]))
                except ProtocolRefusal:
                    pass
                self.assertFalse(root.resolve_relative(alternate).exists())

    def test_public_repository_mutation_never_writes_to_the_retained_old_coordinate(self) -> None:
        """Catches a changed public repository field paired with the original physical ledger path."""
        self.require_register()
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            self.seed_durable_run(root)
            register = DecisionRegister(root, self.repository)
            original = register.relative_path
            alternate_repository = "Other/Repo"
            alternate = Path("repositories") / alternate_repository / "decisions.jsonl"
            try:
                register.repository = alternate_repository
            except (AttributeError, ProtocolRefusal):
                proposal = self.record(UUIDS[0], UUIDS[1])
                self.assertEqual(proposal, register.append(proposal))
                self.assertTrue(root.resolve_relative(original).is_file())
            else:
                try:
                    register.append(self.record(UUIDS[0], UUIDS[1], repository=alternate_repository))
                except ProtocolRefusal:
                    pass
                self.assertFalse(root.resolve_relative(original).exists())
                self.assertFalse(root.resolve_relative(alternate).exists())

    def test_observation_coordinate_is_equally_bound(self) -> None:
        """Catches a read-only register whose public coordinate can be retargeted before a capsule read."""
        self.require_register()
        writer = FloatiRoot.open(self.base, "bravo")
        self.seed_durable_run(writer)
        observation = self.root.observe_tenant(self.root.grant_observation("bravo"), "bravo")
        observed = DecisionRegister.observe(observation, self.repository)
        for attribute, value in (("repository", "Other/Repo"), ("relative_path", Path("arbitrary/outside.jsonl"))):
            with self.subTest(attribute=attribute):
                try:
                    setattr(observed, attribute, value)
                except (AttributeError, ProtocolRefusal):
                    self.assertEqual(self.repository, observed.repository)
                else:
                    with self.assertRaises(ProtocolRefusal):
                        observed.capsule()
        self.assertFalse((writer.tenant_home / "arbitrary" / "outside.jsonl").exists())

    def test_writable_subclass_properties_cannot_retarget_or_mismatch_the_bound_coordinate(self) -> None:
        """Catches virtual public display properties being used for a durable append."""
        self.require_register()
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            self.seed_durable_run(root)
            bound = Path("repositories") / self.repository / "decisions.jsonl"
            overridden = Path("arbitrary/overridden-decisions.jsonl")

            path_override = _RelativePathOverrideDecisionRegister(root, self.repository)
            proposal = self.record(UUIDS[0], UUIDS[1])
            self.assertEqual(proposal, path_override.append(proposal))
            self.assertTrue(root.resolve_relative(bound).is_file())
            self.assertFalse(root.resolve_relative(overridden).exists())

            repository_override = _RepositoryOverrideDecisionRegister(root, self.repository)
            mismatched = self.record(UUIDS[2], UUIDS[3], repository="Other/Repo")
            with self.assertRaises(ProtocolRefusal) as caught:
                repository_override.append(mismatched)
            self.assertEqual("decision_repository_mismatch", caught.exception.code)
            durable = b"".join(
                root.resolve_relative(bound).read_bytes().splitlines(keepends=True)
            )
            self.assertNotIn(b"Other/Repo", durable)

    def test_observation_subclass_properties_cannot_retarget_capsule_projection(self) -> None:
        """Catches a capsule whose records, projection, or coordinate derives from an overrideable property."""
        self.require_register()
        writer = FloatiRoot.open(self.base, "bravo")
        self.seed_durable_run(writer)
        writable = DecisionRegister(writer, self.repository)
        proposal = self.record(UUIDS[0], UUIDS[1])
        accepted = self.record(
            UUIDS[2], UUIDS[1], status="accepted", author_authority="operator",
        )
        proposal["tenant_id"] = "bravo"
        accepted["tenant_id"] = "bravo"
        self.refresh_digest(proposal)
        self.refresh_digest(accepted)
        path = writer.resolve_relative(writable.relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(encode_frame(record) for record in (proposal, accepted)))

        observation = self.root.observe_tenant(self.root.grant_observation("bravo"), "bravo")
        observed = _ObservationCoordinateOverrideDecisionRegister.observe(observation, self.repository)
        capsule = observed.capsule()
        self.assertEqual(self.repository, capsule["repository"])
        self.assertEqual("repositories/Owner/Repo/decisions.jsonl", capsule["ledger"])
        self.assertEqual([accepted["id"]], [entry["accepted_record_id"] for entry in capsule["entries"]])
        self.assertFalse((writer.tenant_home / "arbitrary" / "overridden-decisions.jsonl").exists())

    def test_writable_subclass_tenant_property_cannot_rewrite_bound_tenant_validation_or_proposal(self) -> None:
        """Catches a virtual tenant display property changing a bound root's durable records."""
        self.require_register()
        register = _WritableTenantOverrideDecisionRegister(self.root, self.repository)
        direct = self.record(UUIDS[0], UUIDS[1])
        self.assertEqual(direct, register.append(direct))
        proposed = register.propose(
            timestamp="2026-08-08T12:00:00.000Z",
            scope={"kind": "repository"},
            statement="A display tenant cannot redirect proposal validation.",
            decided_by="fable",
            author_authority="worker",
            source_artifact_ids=["run:" + self.source_run_id],
        )
        self.assertEqual("alpha", proposed["tenant_id"])
        self.assertEqual(["alpha", "alpha"], [record["tenant_id"] for record in register.records()])

    def test_observation_subclass_tenant_property_cannot_rewrite_bound_replay(self) -> None:
        """Catches an observation capsule that validates its bound ledger against an overrideable tenant."""
        self.require_register()
        writer = FloatiRoot.open(self.base, "bravo")
        self.seed_durable_run(writer)
        writable = DecisionRegister(writer, self.repository)
        proposal = self.record(UUIDS[0], UUIDS[1])
        accepted = self.record(
            UUIDS[2], UUIDS[1], status="accepted", author_authority="operator",
        )
        proposal["tenant_id"] = "bravo"
        accepted["tenant_id"] = "bravo"
        self.refresh_digest(proposal)
        self.refresh_digest(accepted)
        path = writer.resolve_relative(writable.relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(encode_frame(record) for record in (proposal, accepted)))

        observation = self.root.observe_tenant(self.root.grant_observation("bravo"), "bravo")
        observed = _ObservationTenantOverrideDecisionRegister.observe(observation, self.repository)
        capsule = observed.capsule()
        self.assertEqual([accepted["id"]], [entry["accepted_record_id"] for entry in capsule["entries"]])

    def test_private_writable_root_bindings_cannot_retarget_append(self) -> None:
        """Catches ordinary reassignment of both private roots creating a bravo ledger from an alpha register."""
        self.require_register()
        register = DecisionRegister(self.root, self.repository)
        alternate = FloatiRoot.open(self.base, "bravo")
        self.seed_durable_run(alternate)
        bound = self.root.resolve_relative(register.relative_path)
        alternate_path = alternate.resolve_relative(register.relative_path)
        try:
            register._authority = alternate
            register._write_root = alternate
        except AttributeError:
            self.assertIs(register._authority, self.root)
            self.assertIs(register._write_root, self.root)
        else:
            redirected = self.record(UUIDS[0], UUIDS[1])
            redirected["tenant_id"] = "bravo"
            self.refresh_digest(redirected)
            self.assertEqual(redirected, register.append(redirected))
            self.assertTrue(alternate_path.is_file())
            self.fail("ordinary private root reassignment retargeted the writable register")

        proposal = self.record(UUIDS[2], UUIDS[3])
        self.assertEqual(proposal, register.append(proposal))
        self.assertTrue(bound.is_file())
        self.assertFalse(alternate_path.exists())

    def test_private_observation_root_bindings_cannot_gain_writer_authority(self) -> None:
        """Catches an observed register made writable by ordinary private root reassignment."""
        self.require_register()
        writer = FloatiRoot.open(self.base, "bravo")
        self.seed_durable_run(writer)
        observation = self.root.observe_tenant(self.root.grant_observation("bravo"), "bravo")
        observed = DecisionRegister.observe(observation, self.repository)
        alternate_path = writer.resolve_relative(observed.relative_path)
        try:
            observed._authority = writer
            observed._write_root = writer
        except AttributeError:
            self.assertEqual("bravo", observed._authority.tenant_id)
            self.assertIsNone(observed._write_root)
        else:
            redirected = self.record(UUIDS[0], UUIDS[1])
            redirected["tenant_id"] = "bravo"
            self.refresh_digest(redirected)
            self.assertEqual(redirected, observed.append(redirected))
            self.assertTrue(alternate_path.is_file())
            self.fail("ordinary private root reassignment granted observation writer authority")

        with self.assertRaises(ProtocolRefusal) as caught:
            observed.append(self.record(UUIDS[2], UUIDS[3],))
        self.assertEqual("write_root_required", caught.exception.code)
        self.assertFalse(alternate_path.exists())


class DecisionBindingIntegrationTests(unittest.TestCase):
    """Ruling-bound decision register behavior across its existing durable ledgers."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.repository = "Owner/Repo"
        self.ledger = RunLedger(self.root)
        self._seed = 0

    def seed_contract(self, repository: object = "Owner/Repo") -> tuple[str, str]:
        self._seed += 1
        run_uuid = UUIDS[self._seed]
        item_uuid = UUIDS[self._seed + 1]
        contract_uuid = UUIDS[self._seed + 2]
        run_id = "run-" + run_uuid
        item_id = "work-" + item_uuid
        self.ledger.append(
            {
                "schema_version": 0, "id": "run-created-" + UUIDS[self._seed + 3],
                "tenant_id": "alpha", "timestamp": "2026-08-08T12:00:00.000Z",
                "kind": "run_created", "run_id": run_id, "plan_digest": "a" * 64,
                "item_ids": [item_id], "dependency_edges": [],
            }
        )
        contract = TaskContract.create(
            objective="bind repository evidence", non_goals=["no inference"],
            areas_to_avoid=[{"path": "slip/graph.py", "region": "all"}],
            input_hashes={"brief": "a" * 64}, acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"}, risk_class="high",
            retry_policy={"max_attempts": 1, "backoff": {"base_delay_ms": 0, "cap_delay_ms": 0, "strategy": "fixed"}},
            dependencies=[],
        )
        record = {
            "schema_version": 0, "id": "task-contract-" + contract_uuid,
            "tenant_id": "alpha", "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "task_contract", "run_id": run_id, "item_id": item_id,
            **contract.canonical(), "contract_digest": contract_digest(contract),
        }
        if repository is not None:
            record["repository"] = repository
        self.ledger.append(record)
        return run_id, record["id"]

    def record(
        self,
        record_uuid: str,
        decision_uuid: str,
        *,
        run_id: str,
        status: str = "proposed",
        authority: str = "worker",
        sources: object = None,
        scope: object = None,
        task_contract_id: object = None,
    ) -> dict[str, object]:
        if sources is None:
            sources = ["run:" + run_id]
        if scope is None:
            scope = {"kind": "repository"}
        record = {
            "schema_version": 0, "id": "decision-record-" + record_uuid,
            "tenant_id": "alpha", "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "decision_record", "repository": self.repository,
            "decision_id": "decision-" + decision_uuid, "scope": scope,
            "statement": "Use one explicit decision ledger.", "status": status,
            "author_authority": authority, "source_artifact_ids": sources,
            "task_contract_id": task_contract_id, "decided_by": "fable", "supersedes": None,
        }
        assert decision_digest is not None
        record["decision_digest"] = decision_digest(record)
        return record

    def test_terminal_append_requires_ruled_authority_and_capsule_exposes_verified_frame(self) -> None:
        """Catches worker terminal authority or a capsule that hides the accepted binding fields."""
        run_id, _ = self.seed_contract()
        register = DecisionRegister(self.root, self.repository)
        proposal = self.record(UUIDS[0], UUIDS[1], run_id=run_id)
        self.assertEqual(proposal, register.append(proposal))
        before = self.root.resolve_relative(register.relative_path).read_bytes()
        worker_terminal = self.record(UUIDS[2], UUIDS[1], run_id=run_id, status="accepted")
        with self.assertRaises(ProtocolRefusal) as worker_refusal:
            register.append(worker_terminal)
        self.assertEqual("decision_terminal_authority_invalid", worker_refusal.exception.code)
        self.assertEqual(before, self.root.resolve_relative(register.relative_path).read_bytes())

        accepted = self.record(UUIDS[2], UUIDS[1], run_id=run_id, status="accepted", authority="operator")
        self.assertEqual(accepted, register.append(accepted))
        entry = register.capsule()["entries"][0]
        self.assertEqual(accepted["decision_digest"], entry["decision_digest"])
        self.assertEqual("operator", entry["decision"]["author_authority"])
        self.assertEqual({"kind": "repository"}, entry["decision"]["scope"])

    def test_register_resolves_durable_sources_and_never_infers_document_proof(self) -> None:
        """Catches an absent durable source or doc proof accepted from cwd/remote/default context."""
        run_id, _ = self.seed_contract()
        register = DecisionRegister(self.root, self.repository)
        missing = self.record(UUIDS[0], UUIDS[1], run_id=run_id, sources=["run:run-" + UUIDS[7]])
        with self.assertRaises(ProtocolRefusal) as missing_refusal:
            register.append(missing)
        self.assertEqual("source_lookup_missing", missing_refusal.exception.code)

        document = self.record(
            UUIDS[0], UUIDS[1], run_id=run_id,
            sources=["doc:docs/design/decision.md@" + "a" * 40],
        )
        with self.assertRaises(ProtocolRefusal) as document_refusal:
            register.append(document)
        self.assertEqual("source_lookup_unavailable", document_refusal.exception.code)

        seen: list[tuple[str, str]] = []
        document_register = DecisionRegister(
            self.root, self.repository,
            document_resolver=lambda repository, source: seen.append((repository, source)) or True,
        )
        self.assertEqual(document, document_register.append(document))
        self.assertEqual([(self.repository, document["source_artifact_ids"][0])], seen)

    def test_register_resolves_each_declared_ledger_source_in_physical_order(self) -> None:
        """Catches source prefixes accepted merely by spelling rather than their ruled durable ledger target."""
        run_id, contract_id = self.seed_contract()
        run_rows = self.ledger.records()
        created = next(row for row in run_rows if row["kind"] == "run_created" and row["run_id"] == run_id)
        item_id = created["item_ids"][0]
        assert isinstance(item_id, str)
        contract = next(row for row in run_rows if row["id"] == contract_id)
        attempt_id = "attempt-" + uuid7_hex()
        append_record(
            self.root,
            "runs/events.jsonl",
            {
                "schema_version": 0,
                "id": "attempt-opened-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-08T12:00:00.000Z",
                "kind": "attempt_opened",
                "run_id": run_id,
                "item_id": item_id,
                "attempt_id": attempt_id,
                "ordinal": 1,
                "scheduler_epoch": 1,
                "fence_token": "a" * 64,
                "max_attempts": 1,
                "backoff": {
                    "strategy": "fixed",
                    "base_delay_ms": 0,
                    "cap_delay_ms": 0,
                    "jitter": "sha256_25pct",
                },
            },
            allowed_kinds=RUN_KINDS,
        )
        worker_receipt_id = "worker-receipt-" + uuid7_hex()
        append_record(
            self.root,
            "receipts/workers.jsonl",
            {
                "schema_version": 0,
                "id": worker_receipt_id,
                "tenant_id": "alpha",
                "timestamp": "2026-08-08T12:00:00.000Z",
                "kind": "worker_receipt",
                "session_id": "worker-" + uuid7_hex(),
                "work_item_id": item_id,
                "node_id": "worker-a",
                "adapter": "codex",
                "transition": "claim",
                "outcome_code": None,
                "authority_subject": "authority",
                "authority_epoch": 1,
                "artifact_bindings": [],
            },
            allowed_kinds={"worker_receipt"},
        )
        acceptance_receipt_id = "acceptance-receipt-" + uuid7_hex()
        append_record(
            self.root,
            "runs/events.jsonl",
            {
                "schema_version": 0,
                "id": acceptance_receipt_id,
                "tenant_id": "alpha",
                "timestamp": "2026-08-08T12:00:00.000Z",
                "kind": "acceptance_receipt",
                "run_id": run_id,
                "item_id": item_id,
                "attempt_id": attempt_id,
                "contract_digest": contract["contract_digest"],
                "check_ids": ["tests.unit"],
                "reviewer": "reviewer-a",
                "evidence_bindings": [worker_receipt_id],
                "deviations": [],
                "result": "accepted",
            },
            allowed_kinds=RUN_KINDS,
        )

        register = DecisionRegister(self.root, self.repository)
        first = self.record(UUIDS[0], UUIDS[1], run_id=run_id)
        self.assertEqual(first, register.append(first))
        all_sources = [
            "run:" + run_id,
            "attempt:" + attempt_id,
            "contract:" + contract_id,
            "receipt:" + worker_receipt_id,
            "receipt:" + acceptance_receipt_id,
            "decision:" + first["decision_id"],
        ]
        second = self.record(UUIDS[2], UUIDS[3], run_id=run_id, sources=all_sources)
        self.assertEqual(second, register.append(second))

        future = self.record(
            UUIDS[4], UUIDS[5], run_id=run_id,
            sources=["decision:decision-" + UUIDS[7]],
        )
        with self.assertRaises(ProtocolRefusal) as future_refusal:
            register.append(future)
        self.assertEqual("source_lookup_missing", future_refusal.exception.code)

    def test_nonnull_contract_binding_requires_matching_optional_repository(self) -> None:
        """Catches cross-repository or legacy task-contract evidence becoming a decision binding by inference."""
        matching_run, matching_contract = self.seed_contract("Owner/Repo")
        register = DecisionRegister(self.root, self.repository)
        matching = self.record(
            UUIDS[0], UUIDS[1], run_id=matching_run,
            sources=["contract:" + matching_contract], scope={"kind": "contract"},
            task_contract_id=matching_contract,
        )
        self.assertEqual(matching, register.append(matching))

        mismatch_run, mismatch_contract = self.seed_contract("Other/Repo")
        mismatch = self.record(
            UUIDS[2], UUIDS[3], run_id=mismatch_run,
            sources=["contract:" + mismatch_contract], scope={"kind": "contract"},
            task_contract_id=mismatch_contract,
        )
        with self.assertRaises(ProtocolRefusal) as mismatch_refusal:
            register.append(mismatch)
        self.assertEqual("task_contract_repository_mismatch", mismatch_refusal.exception.code)

        legacy_run, legacy_contract = self.seed_contract(None)
        legacy = self.record(
            UUIDS[4], UUIDS[5], run_id=legacy_run,
            sources=["contract:" + legacy_contract], scope={"kind": "contract"},
            task_contract_id=legacy_contract,
        )
        with self.assertRaises(ProtocolRefusal) as legacy_refusal:
            register.append(legacy)
        self.assertEqual("task_contract_repository_unavailable", legacy_refusal.exception.code)


if __name__ == "__main__":
    unittest.main()
