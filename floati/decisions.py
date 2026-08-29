"""Explicit repository-context decisions and deterministic handoff capsules."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .records import (
    decision_record_digest,
    validate_record,
    validate_repository_coordinate,
)
from .root import FloatiRoot, TenantObservation
from .runtruth import RUN_KINDS
from .workers import WORKER_KINDS


DECISION_KINDS = frozenset({"decision_record"})
DECISION_EVENT_STATUSES = frozenset({"proposed", "accepted", "rejected"})
DECISION_STATES = frozenset({"proposed", "accepted", "superseded", "rejected"})
DECISION_AUTHOR_AUTHORITIES = frozenset({"operator", "architect", "worker"})
_TERMINAL_DECISION_STATUSES = frozenset({"accepted", "rejected"})
_UUID7 = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_TASK_CONTRACT_ID = re.compile(r"^task-contract-" + _UUID7 + r"$")
_SOURCE_PATTERNS = (
    re.compile(r"^run:run-" + _UUID7 + r"$"),
    re.compile(r"^attempt:attempt-" + _UUID7 + r"$"),
    re.compile(r"^contract:task-contract-" + _UUID7 + r"$"),
    re.compile(r"^receipt:(?:worker-receipt|acceptance-receipt)-" + _UUID7 + r"$"),
    re.compile(r"^decision:decision-" + _UUID7 + r"$"),
)
_DOC_SOURCE = re.compile(r"^doc:(.+)@(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BIDI_CONTROLS = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
)
_IMMUTABLE_PROPOSAL_FIELDS = (
    "repository",
    "decision_id",
    "scope",
    "statement",
    "source_artifact_ids",
    "task_contract_id",
)
_RUN_LEDGER_PATH = Path("runs/events.jsonl")
_WORKER_RECEIPT_LEDGER_PATH = Path("receipts/workers.jsonl")
_Authority = Union[FloatiRoot, TenantObservation]
DecisionSourceResolver = Callable[[str, str], bool]


def _compact_ijson(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal("decision_not_ijson", "decision cannot form canonical I-JSON") from exc


def _snapshot_decision_record(record: object) -> Dict[str, object]:
    """Detach one caller mapping into plain I-JSON data before any decision check."""

    if not isinstance(record, dict):
        raise ProtocolRefusal("record_not_object", "each durable record must be an object")
    try:
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        snapshot = json.loads(encoded)
    except Exception as exc:
        raise ProtocolRefusal(
            "decision_snapshot_invalid",
            "decision record cannot form one stable plain-data snapshot",
        ) from exc
    if type(snapshot) is not dict:
        raise ProtocolRefusal("record_not_object", "each durable record must be an object")
    return snapshot


def _propose_uuid_component(value: object, field: str) -> str:
    """Accept an explicitly supplied UUID component only as a plain string."""

    if value is None:
        return uuid7_hex()
    if type(value) is not str:
        raise ProtocolRefusal(
            f"{field}_invalid",
            f"{field} must be an omitted or plain UUIDv7 component string",
        )
    return value


def _propose_source_artifact_ids(value: object) -> list[object]:
    """Copy only ordinary sequence containers before the source-empty record fence."""

    if type(value) not in (tuple, list):
        raise ProtocolRefusal(
            "source_artifact_ids_invalid",
            "source_artifact_ids must be an ordinary tuple or list",
        )
    return list(value)


def decision_digest(record: Mapping[str, object]) -> str:
    """Digest the full decision record excluding only its self-referential digest."""

    return decision_record_digest(_snapshot_decision_record(record))


def _unsafe_lexical(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character) in _BIDI_CONTROLS
        for character in value
    )


def _validate_relative_prefix(value: object, *, field: str, refuse: Callable[[str, str], None]) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 1024
        or value.startswith("/")
        or "\\" in value
        or _unsafe_lexical(value)
        or unicodedata.normalize("NFC", value) != value
        or any(component in {"", ".", ".."} for component in value.split("/"))
    ):
        refuse(field + "_invalid", field + " must be one normalized repository-relative lexical prefix")


def _validate_scope_binding(
    scope: object,
    task_contract_id: object,
    *,
    refuse: Callable[[str, str], None],
) -> None:
    if type(scope) is not dict or type(scope.get("kind")) is not str:
        refuse("decision_scope_invalid", "scope must use one closed tagged v0 object")
    kind = scope["kind"]
    if kind == "repository":
        if set(scope) != {"kind"}:
            refuse("decision_scope_invalid", "repository scope must have only its kind")
        return
    if kind == "path_prefix":
        if set(scope) != {"kind", "path_prefix"}:
            refuse("decision_scope_invalid", "path-prefix scope must name one prefix")
        _validate_relative_prefix(scope["path_prefix"], field="path_prefix", refuse=refuse)
        return
    if kind == "contract":
        if set(scope) != {"kind"}:
            refuse("decision_scope_invalid", "contract scope must have only its kind")
        if type(task_contract_id) is not str or _TASK_CONTRACT_ID.fullmatch(task_contract_id) is None:
            refuse("task_contract_required", "contract scope requires one task-contract identity")
        return
    refuse("decision_scope_invalid", "scope kind is outside the closed v0 vocabulary")


def _validate_source_syntax(value: object, *, refuse: Callable[[str, str], None]) -> str:
    if type(value) is not str or not 1 <= len(value) <= 2048 or _unsafe_lexical(value):
        refuse("source_artifact_id_invalid", "source artifact id is not a closed durable identity")
    if any(pattern.fullmatch(value) is not None for pattern in _SOURCE_PATTERNS):
        return value
    document = _DOC_SOURCE.fullmatch(value)
    if document is None:
        refuse("source_artifact_id_invalid", "source artifact id is not in the closed v0 taxonomy")
    _validate_relative_prefix(document.group(1), field="source_artifact_id", refuse=refuse)
    return value


def _validate_sources(
    source_artifact_ids: object,
    repository: object,
    source_resolver: Optional[DecisionSourceResolver],
    *,
    refuse: Callable[[str, str], None],
) -> None:
    if type(source_artifact_ids) is not list or not 1 <= len(source_artifact_ids) <= 64:
        refuse("source_artifact_ids_invalid", "source artifact ids must be a nonempty bounded list")
    sources = [_validate_source_syntax(value, refuse=refuse) for value in source_artifact_ids]
    if len(set(sources)) != len(sources):
        refuse("source_artifact_ids_invalid", "source artifact ids must be unique")
    if type(repository) is not str:
        refuse("repository_invalid", "decision source resolution requires one repository coordinate")
    if source_resolver is None or not callable(source_resolver):
        refuse("source_lookup_unavailable", "source lookup requires an injected read-only resolver")
    for source in sources:
        try:
            resolved = source_resolver(repository, source)
        except (ProtocolRefusal, IntegrityFailure) as exc:
            # A resolver may distinguish an unavailable proof from a missing
            # durable target.  Keep the public caller/replay boundary while
            # preserving that typed reason rather than collapsing it into a
            # generic callback failure.
            refuse(exc.code, exc.detail)
        except Exception:
            refuse("source_lookup_unavailable", "source resolver could not prove this durable source")
        if resolved is not True:
            refuse("source_lookup_missing", "source resolver did not prove this durable source")


def validate_decision_binding(
    record: object,
    *,
    source_resolver: Optional[DecisionSourceResolver] = None,
    integrity: bool = False,
) -> Dict[str, object]:
    """Validate ruled Item 9 fields using only an explicit read-only resolver.

    This pure boundary intentionally performs no cwd, checkout, remote, or
    ledger discovery.  It validates the shared generic record contract first,
    then delegates every durable-target proof to the injected resolver.
    """

    error = IntegrityFailure if integrity else ProtocolRefusal

    def refuse(code: str, detail: str) -> None:
        raise error(code, detail)

    candidate = _snapshot_decision_record(record)
    tenant_id = candidate.get("tenant_id")
    if type(tenant_id) is not str:
        refuse("tenant_invalid", "tenant identifier is invalid")
    # Keep the public Item 9 contract-specific refusal for a detached
    # contract scope, while the generic validator remains the shared source
    # of all durable-record shape, authority, and digest checks.
    _validate_scope_binding(candidate.get("scope"), candidate.get("task_contract_id"), refuse=refuse)
    candidate = validate_record(candidate, tenant_id, DECISION_KINDS, integrity=integrity)
    _validate_sources(
        candidate.get("source_artifact_ids"),
        candidate.get("repository"),
        source_resolver,
        refuse=refuse,
    )
    return candidate


@dataclass(frozen=True)
class CurrentDecision:
    """One currently accepted decision in accepted-frame physical order."""

    decision_id: str
    accepted_record_id: str
    ledger_ordinal: int
    decision_digest: str
    scope: Mapping[str, object]
    statement: str
    author_authority: str
    source_artifact_ids: Tuple[str, ...]
    task_contract_id: Optional[str]
    decided_by: str
    supersedes: Optional[str]
    status: str = "accepted"

    def capsule_entry(self) -> Dict[str, object]:
        return {
            "ledger_ordinal": self.ledger_ordinal,
            "decision_id": self.decision_id,
            "accepted_record_id": self.accepted_record_id,
            "decision_digest": self.decision_digest,
            "decision": {
                "scope": dict(self.scope),
                "statement": self.statement,
                "author_authority": self.author_authority,
                "source_artifact_ids": list(self.source_artifact_ids),
                "task_contract_id": self.task_contract_id,
                "decided_by": self.decided_by,
                "supersedes": self.supersedes,
                "status": self.status,
            },
        }


@dataclass
class _DecisionState:
    proposal: Dict[str, object]
    status: str = "proposed"
    terminal: Optional[Dict[str, object]] = None
    terminal_ordinal: Optional[int] = None


@dataclass(frozen=True)
class _DecisionCoordinate:
    repository: str
    relative_path: Path


@dataclass(frozen=True)
class _DecisionEvidence:
    """One read-only durable lookup snapshot for a decision operation."""

    run_ids: frozenset[str]
    attempt_ids: frozenset[str]
    task_contracts: Mapping[str, Mapping[str, object]]
    acceptance_receipt_ids: frozenset[str]
    worker_receipt_ids: frozenset[str]

    def resolve(
        self,
        repository: str,
        source: str,
        document_resolver: Optional[DecisionSourceResolver],
    ) -> bool:
        """Resolve only the ruled durable source forms from this snapshot.

        The caller has already applied the closed syntax validator.  In
        particular, this method never expands a document source through cwd,
        Git configuration, a remote, or any ambient checkout state.
        """

        if source.startswith("run:"):
            return source.removeprefix("run:") in self.run_ids
        if source.startswith("attempt:"):
            return source.removeprefix("attempt:") in self.attempt_ids
        if source.startswith("contract:"):
            return source.removeprefix("contract:") in self.task_contracts
        if source.startswith("receipt:acceptance-receipt-"):
            return source.removeprefix("receipt:") in self.acceptance_receipt_ids
        if source.startswith("receipt:worker-receipt-"):
            return source.removeprefix("receipt:") in self.worker_receipt_ids
        if source.startswith("doc:"):
            if document_resolver is None or not callable(document_resolver):
                raise ProtocolRefusal(
                    "source_lookup_unavailable",
                    "document proof requires an injected read-only resolver",
                )
            return document_resolver(repository, source) is True
        # decision: is resolved by the projection's physical-order frame, not
        # here; all other forms were rejected by generic syntax validation.
        return False


def _validate_task_contract_repository_binding(
    record: Mapping[str, object],
    task_contracts: Optional[Mapping[str, Mapping[str, object]]],
    repository: str,
    *,
    refuse: Callable[[str, str], None],
) -> None:
    """Require every non-null decision contract identity to prove its repo."""

    contract_id = record["task_contract_id"]
    if contract_id is None:
        return
    if task_contracts is None:
        refuse(
            "task_contract_lookup_unavailable",
            "same-repository contract binding requires an injected ledger snapshot",
        )
    contract = task_contracts.get(str(contract_id))
    if contract is None:
        refuse("task_contract_missing", "decision task contract is not present in the run ledger")
    contract_repository = contract.get("repository")
    if type(contract_repository) is not str:
        refuse(
            "task_contract_repository_unavailable",
            "legacy task contracts without repository cannot bind a decision",
        )
    if contract_repository != repository:
        refuse(
            "task_contract_repository_mismatch",
            "decision task contract repository does not match the decision ledger",
        )


class DecisionProjection:
    """Pure physical-order decision projection; timestamps never select state."""

    def __init__(
        self,
        statuses: Mapping[str, str],
        current: Sequence[CurrentDecision],
    ) -> None:
        self._statuses = dict(statuses)
        self._current = tuple(current)

    @classmethod
    def from_records(
        cls,
        records: Sequence[Dict[str, object]],
        *,
        repository: str,
        tenant_id: str,
        integrity: bool,
        source_resolver: Optional[DecisionSourceResolver] = None,
        task_contracts: Optional[Mapping[str, Mapping[str, object]]] = None,
    ) -> "DecisionProjection":
        error = IntegrityFailure if integrity else ProtocolRefusal

        def refuse(code: str, detail: str) -> None:
            raise error(code, detail)

        states: Dict[str, _DecisionState] = {}
        seen_record_ids = set()
        seen_decision_ids = set()
        accepted_order = []
        for ordinal, raw in enumerate(records, start=1):
            record = validate_record(raw, tenant_id, DECISION_KINDS, integrity=integrity)
            if record["id"] in seen_record_ids:
                refuse("duplicate_record_id", "decision ledger repeats a physical record id")
            seen_record_ids.add(record["id"])
            if record["repository"] != repository:
                refuse("decision_repository_mismatch", "decision record repository does not equal its ledger coordinate")

            _validate_task_contract_repository_binding(
                record,
                task_contracts,
                repository,
                refuse=refuse,
            )

            def resolve_source(source_repository: str, source: str) -> bool:
                if source_repository != repository:
                    return False
                if source.startswith("decision:"):
                    return source.removeprefix("decision:") in seen_decision_ids
                if source_resolver is None:
                    raise ProtocolRefusal(
                        "source_lookup_unavailable",
                        "source lookup requires an injected read-only resolver",
                    )
                return source_resolver(source_repository, source)

            validate_decision_binding(
                record,
                source_resolver=resolve_source,
                integrity=integrity,
            )

            decision_id = str(record["decision_id"])
            status = str(record["status"])
            state = states.get(decision_id)
            if state is None:
                if status != "proposed":
                    refuse("decision_proposal_missing", "a terminal disposition must follow its proposal")
                if record["supersedes"] is not None:
                    refuse("decision_supersedes_invalid", "a proposal cannot supersede a decision")
                states[decision_id] = _DecisionState(proposal=record)
                seen_decision_ids.add(decision_id)
                continue

            if status == "proposed":
                refuse("decision_proposal_duplicate", "a logical decision has one immutable proposal")
            if state.terminal is not None:
                refuse("decision_terminal_duplicate", "a logical decision has one terminal disposition")
            if any(record[field] != state.proposal[field] for field in _IMMUTABLE_PROPOSAL_FIELDS):
                refuse("decision_payload_mutated", "a terminal disposition cannot rewrite its proposal")
            if status == "rejected":
                if record["supersedes"] is not None:
                    refuse("decision_supersedes_invalid", "a rejected decision cannot supersede another decision")
                state.status = "rejected"
            elif status == "accepted":
                target_id = record["supersedes"]
                if target_id is not None:
                    target = states.get(str(target_id))
                    if target is None or target.status != "accepted":
                        refuse(
                            "decision_supersedes_target_invalid",
                            "supersession requires an earlier currently accepted decision",
                        )
                    target.status = "superseded"
                state.status = "accepted"
                accepted_order.append(decision_id)
            else:
                refuse("decision_status_invalid", "decision status is not a permitted v0 event")
            state.terminal = record
            state.terminal_ordinal = ordinal
            seen_decision_ids.add(decision_id)

        current = []
        for decision_id in accepted_order:
            state = states[decision_id]
            if state.status != "accepted" or state.terminal is None or state.terminal_ordinal is None:
                continue
            record = state.terminal
            current.append(
                CurrentDecision(
                    decision_id=decision_id,
                    accepted_record_id=str(record["id"]),
                    ledger_ordinal=state.terminal_ordinal,
                    decision_digest=str(record["decision_digest"]),
                    scope=dict(record["scope"]),
                    statement=str(record["statement"]),
                    author_authority=str(record["author_authority"]),
                    source_artifact_ids=tuple(record["source_artifact_ids"]),
                    task_contract_id=record["task_contract_id"],
                    decided_by=str(record["decided_by"]),
                    supersedes=record["supersedes"],
                )
            )
        return cls(
            {decision_id: state.status for decision_id, state in states.items()},
            current,
        )

    def status_for(self, decision_id: object) -> str:
        if not isinstance(decision_id, str) or decision_id not in self._statuses:
            raise ProtocolRefusal("decision_unknown", "decision id is not present in this projection")
        return self._statuses[decision_id]

    def current_accepted(self) -> Tuple[CurrentDecision, ...]:
        return self._current


class DecisionRegister:
    """One explicit repository-context JSONL register beneath a supplied tenant root."""

    __slots__ = ("_authority", "_write_root", "_coordinate", "_document_resolver")

    def __setattr__(self, name: str, value: object) -> None:
        """Keep authority, write capability, and durable coordinate fixed after construction.

        The public display properties are deliberately non-authoritative: a
        subclass can override either one, so every operation below reads the
        private frozen coordinate directly.  Blocking reassignment here also
        prevents an ordinary caller from replacing any constructor-bound root
        capability or that canonical coordinate.
        """

        if name in ("_authority", "_write_root", "_coordinate", "_document_resolver") and hasattr(self, name):
            raise AttributeError("decision register binding is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        root: FloatiRoot,
        repository: str,
        *,
        document_resolver: Optional[DecisionSourceResolver] = None,
    ) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "a writable DecisionRegister requires an explicit FloatiRoot")
        if document_resolver is not None and not callable(document_resolver):
            raise ProtocolRefusal(
                "source_lookup_unavailable",
                "document resolver must be an injected read-only callable",
            )
        self._authority: _Authority = root
        self._write_root: Optional[FloatiRoot] = root
        self._document_resolver = document_resolver
        canonical_repository = validate_repository_coordinate(repository)
        self._coordinate = _DecisionCoordinate(
            canonical_repository,
            Path("repositories") / canonical_repository / "decisions.jsonl",
        )

    @classmethod
    def observe(
        cls,
        root: TenantObservation,
        repository: str,
        *,
        document_resolver: Optional[DecisionSourceResolver] = None,
    ) -> "DecisionRegister":
        """Create a strictly read-only register from an existing tenant observation."""

        if not isinstance(root, TenantObservation):
            raise ProtocolRefusal("observation_required", "decision observation requires a TenantObservation")
        if document_resolver is not None and not callable(document_resolver):
            raise ProtocolRefusal(
                "source_lookup_unavailable",
                "document resolver must be an injected read-only callable",
            )
        instance = cls.__new__(cls)
        instance._authority = root
        instance._write_root = None
        instance._document_resolver = document_resolver
        canonical_repository = validate_repository_coordinate(repository)
        instance._coordinate = _DecisionCoordinate(
            canonical_repository,
            Path("repositories") / canonical_repository / "decisions.jsonl",
        )
        return instance

    @property
    def repository(self) -> str:
        return self._coordinate.repository

    @property
    def relative_path(self) -> Path:
        return self._coordinate.relative_path

    @property
    def tenant_id(self) -> str:
        return self._authority.tenant_id

    def records(self) -> list[Dict[str, object]]:
        return read_records_snapshot(
            self._authority,
            self._coordinate.relative_path,
            allowed_kinds=DECISION_KINDS,
        )

    def _evidence_snapshot(self) -> _DecisionEvidence:
        """Read only the ruled ledger evidence, without ambient repository discovery."""

        run_records = read_records_snapshot(
            self._authority,
            _RUN_LEDGER_PATH,
            allowed_kinds=RUN_KINDS,
        )
        worker_records = read_records_snapshot(
            self._authority,
            _WORKER_RECEIPT_LEDGER_PATH,
            allowed_kinds=WORKER_KINDS,
        )
        return _DecisionEvidence(
            run_ids=frozenset(
                str(row["run_id"])
                for row in run_records
                if row["kind"] == "run_created"
            ),
            attempt_ids=frozenset(
                str(row["attempt_id"])
                for row in run_records
                if row["kind"] == "attempt_opened"
            ),
            task_contracts={
                str(row["id"]): dict(row)
                for row in run_records
                if row["kind"] == "task_contract"
            },
            acceptance_receipt_ids=frozenset(
                str(row["id"])
                for row in run_records
                if row["kind"] == "acceptance_receipt"
            ),
            worker_receipt_ids=frozenset(str(row["id"]) for row in worker_records),
        )

    def _project(
        self,
        records: Sequence[Dict[str, object]],
        *,
        integrity: bool,
        evidence: _DecisionEvidence,
    ) -> DecisionProjection:
        return DecisionProjection.from_records(
            records,
            repository=self._coordinate.repository,
            tenant_id=self._authority.tenant_id,
            integrity=integrity,
            source_resolver=lambda repository, source: evidence.resolve(
                repository,
                source,
                self._document_resolver,
            ),
            task_contracts=evidence.task_contracts,
        )

    def project(self) -> DecisionProjection:
        return self._project(
            self.records(),
            integrity=True,
            evidence=self._evidence_snapshot(),
        )

    def append(self, record: object) -> Dict[str, object]:
        """Append one physically ordered ruled proposal or terminal disposition."""

        candidate = _snapshot_decision_record(record)
        if self._write_root is None:
            raise ProtocolRefusal("write_root_required", "decision observations are physically read-only")
        candidate = validate_record(candidate, self._authority.tenant_id, DECISION_KINDS, integrity=False)
        if candidate["repository"] != self._coordinate.repository:
            raise ProtocolRefusal("decision_repository_mismatch", "record repository must equal this register coordinate")
        evidence = self._evidence_snapshot()

        def decide(existing: list[Dict[str, object]]):
            # An idempotent retry is never permission to ignore a later bad
            # durable frame. Existing replay failures are integrity failures;
            # candidate-only causal failures below remain protocol refusals.
            self._project(
                existing,
                integrity=True,
                evidence=evidence,
            )
            for prior in existing:
                if prior["id"] == candidate["id"]:
                    if prior == candidate:
                        return prior, None
                    raise ProtocolRefusal("duplicate_record_id", "record id cannot change its semantic payload")
            self._project(
                existing + [candidate],
                integrity=False,
                evidence=evidence,
            )
            return candidate, candidate

        return transact(
            self._write_root,
            self._coordinate.relative_path,
            decide,
            allowed_kinds=DECISION_KINDS,
        )

    def propose(
        self,
        *,
        timestamp: object,
        scope: object,
        statement: object,
        decided_by: object,
        author_authority: object,
        source_artifact_ids: object,
        task_contract_id: object = None,
        decision_id: object = None,
        record_id: object = None,
    ) -> Dict[str, object]:
        """Create one explicitly attributed proposal with durable source proof."""

        record_uuid = _propose_uuid_component(record_id, "record_id")
        decision_uuid = _propose_uuid_component(decision_id, "decision_id")
        while record_id is None and decision_id is None and record_uuid == decision_uuid:
            decision_uuid = uuid7_hex()
        source_artifact_ids = _propose_source_artifact_ids(source_artifact_ids)
        record = {
            "schema_version": 0,
            "id": "decision-record-" + record_uuid,
            "tenant_id": self._authority.tenant_id,
            "timestamp": timestamp,
            "kind": "decision_record",
            "repository": self._coordinate.repository,
            "decision_id": "decision-" + decision_uuid,
            "scope": scope,
            "statement": statement,
            "status": "proposed",
            "author_authority": author_authority,
            "source_artifact_ids": source_artifact_ids,
            "task_contract_id": task_contract_id,
            "decided_by": decided_by,
            "supersedes": None,
        }
        record["decision_digest"] = decision_digest(record)
        return self.append(record)

    def capsule(self) -> Dict[str, object]:
        return {
            "schema_version": 0,
            "kind": "handoff_capsule",
            "repository": self._coordinate.repository,
            "ledger": self._coordinate.relative_path.as_posix(),
            "entries": [entry.capsule_entry() for entry in self.project().current_accepted()],
        }

    def capsule_bytes(self) -> bytes:
        return _compact_ijson(self.capsule())
