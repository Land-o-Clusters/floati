"""Pure replay projection for append-only registered thread testimony."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import _transact_thread_observation_records, read_records
from .records import (
    THREAD_OBSERVATION_KINDS,
    thread_observation_digest,
    validate_record,
)
from .root import FloatiRoot
from .thread_source import CodexLocalThreadSource, ThreadReadResult


class ThreadObservationProjection:
    """Replay thread attachment testimony without sampling clocks or providers."""

    def __init__(self) -> None:
        self._attachments: Dict[str, Dict[str, object]] = {}
        self._provider_ids: Dict[Tuple[str, str], str] = {}
        self._record_ids: set[str] = set()
        self._tenant_id: str | None = None

    @classmethod
    def from_records(
        cls, records: Sequence[Mapping[str, object]]
    ) -> "ThreadObservationProjection":
        projection = cls()
        for source in records:
            tenant_id = source.get("tenant_id")
            if not isinstance(tenant_id, str):
                raise IntegrityFailure(
                    "tenant_invalid", "thread observation tenant must be a string"
                )
            if projection._tenant_id is None:
                projection._tenant_id = tenant_id
            record = validate_record(
                dict(source),
                projection._tenant_id,
                THREAD_OBSERVATION_KINDS,
                integrity=True,
            )
            projection._apply(record)
        return projection

    def _apply(self, record: Dict[str, object]) -> None:
        record_id = str(record["id"])
        if record_id in self._record_ids:
            raise IntegrityFailure(
                "thread_record_duplicate",
                "a physical thread testimony identity may appear only once",
            )
        self._record_ids.add(record_id)
        kind = record["kind"]
        if kind == "thread_attachment_registered":
            attachment_id = str(record["id"])
            coordinate = (str(record["provider"]), str(record["provider_thread_id"]))
            if attachment_id in self._attachments or coordinate in self._provider_ids:
                raise IntegrityFailure(
                    "thread_attachment_duplicate",
                    "a provider thread may have only one physical attachment",
                )
            self._provider_ids[coordinate] = attachment_id
            self._attachments[attachment_id] = {
                "attachment": deepcopy(record),
                "latest_observation": None,
                "detachment": None,
            }
            return

        attachment_id = str(record["attachment_id"])
        current = self._attachments.get(attachment_id)
        if current is None:
            raise IntegrityFailure(
                "thread_attachment_missing",
                "thread testimony requires a prior exact attachment",
            )
        attachment = current["attachment"]
        if not isinstance(attachment, dict) or any(
            record[field] != attachment[field]
            for field in ("provider", "provider_thread_id")
        ):
            raise IntegrityFailure(
                "thread_attachment_mismatch",
                "thread testimony must repeat the exact provider coordinate",
            )
        if kind == "thread_observation_recorded":
            if current["detachment"] is not None:
                raise IntegrityFailure(
                    "thread_attachment_detached",
                    "thread testimony cannot follow detachment",
                )
            latest = current["latest_observation"]
            if (
                isinstance(latest, dict)
                and latest.get("observation_digest") == record["observation_digest"]
            ):
                raise IntegrityFailure(
                    "thread_observation_duplicate",
                    "an unchanged observation may have only one physical row",
                )
            current["latest_observation"] = deepcopy(record)
            return
        if current["detachment"] is not None:
            raise IntegrityFailure(
                "thread_attachment_detached",
                "thread attachments may be detached only once",
            )
        current["detachment"] = deepcopy(record)

    def attachment(self, attachment_id: str) -> Dict[str, object]:
        if not isinstance(attachment_id, str) or attachment_id not in self._attachments:
            raise ProtocolRefusal(
                "thread_attachment_missing", "thread attachment does not exist"
            )
        return deepcopy(self._attachments[attachment_id])

    def attachments(self) -> List[Dict[str, object]]:
        return [
            deepcopy(self._attachments[attachment_id])
            for attachment_id in sorted(self._attachments)
        ]


def _current_time(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ProtocolRefusal(
            "time_invalid", "thread testimony requires an aware UTC-compatible time"
        )
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ThreadObservationLedger:
    """Controller-owned durable testimony for registered provider threads."""

    relative_path = Path("thread-observations/records.jsonl")

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal(
                "root_required", "thread observation ledger requires a writable FloatiRoot"
            )
        self.root = root
        self.__owner: Optional[object] = None
        self.__capability: Optional[object] = None

    def records(self) -> List[Dict[str, object]]:
        return deepcopy(
            read_records(
                self.root,
                self.relative_path,
                allowed_kinds=set(THREAD_OBSERVATION_KINDS),
            )
        )

    def project(self) -> ThreadObservationProjection:
        return ThreadObservationProjection.from_records(self.records())

    def append(self, _record: Mapping[str, object]) -> Dict[str, object]:
        raise ProtocolRefusal(
            "thread_observer_only",
            "thread testimony requires controller-owned append authority",
        )

    def _capability_for(self, owner: object) -> object:
        if type(owner) is not ThreadObserver or getattr(owner, "ledger", None) is not self:
            raise ProtocolRefusal(
                "thread_observer_only",
                "thread testimony capability requires the exact controller ledger",
            )
        if self.__owner is None:
            self.__owner = owner
            self.__capability = object()
        elif self.__owner is not owner:
            raise ProtocolRefusal(
                "thread_observer_only", "thread ledger already has one controller owner"
            )
        assert self.__capability is not None
        return self.__capability

    def _transact(
        self,
        capability: object,
        decide: Callable[
            [List[Dict[str, object]]],
            Tuple[Dict[str, object], Optional[Dict[str, object]]],
        ],
    ) -> Dict[str, object]:
        if capability is not self.__capability or self.__owner is None:
            raise ProtocolRefusal(
                "thread_observer_only", "thread rows require controller-owned authority"
            )
        return deepcopy(
            _transact_thread_observation_records(self.root, decide)
        )


_THREAD_OBSERVATION_LEDGER_APPEND_CODE = ThreadObservationLedger._transact.__code__


class ThreadObserver:
    """Register, observe, and detach explicit Codex-local thread coordinates."""

    def __init__(self, root: FloatiRoot, *, codex_executable: object = None) -> None:
        self._initialize(root, CodexLocalThreadSource(codex_executable))

    @classmethod
    def _for_test(
        cls, root: FloatiRoot, source: CodexLocalThreadSource
    ) -> "ThreadObserver":
        if type(source) is not CodexLocalThreadSource:
            raise ProtocolRefusal(
                "thread_source_invalid", "test observer requires the exact local source"
            )
        observer = cls.__new__(cls)
        observer._initialize(root, source)
        return observer

    def _initialize(self, root: FloatiRoot, source: CodexLocalThreadSource) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "thread observer requires a writable root")
        self.root = root
        self.source = source
        self.ledger = ThreadObservationLedger(root)
        self.__capability = self.ledger._capability_for(self)

    def _subject_exists(
        self,
        subject_kind: str,
        work_item_id: str,
        run_id: Optional[str],
        attempt_id: Optional[str],
    ) -> None:
        from .work import WorkLog

        WorkLog(self.root).show(work_item_id)
        if subject_kind == "work_item":
            return
        from .runtruth import RunLedger

        if run_id is None or attempt_id is None:
            raise ProtocolRefusal(
                "thread_subject_invalid", "attempt attachment requires run and attempt"
            )
        run = RunLedger(self.root).project().run(run_id)
        state = run["attempts"].get(attempt_id)
        if (
            work_item_id not in run["item_ids"]
            or not isinstance(state, dict)
            or state.get("opened", {}).get("item_id") != work_item_id
        ):
            raise ProtocolRefusal(
                "thread_subject_missing",
                "thread attachment requires the exact durable run item attempt relation",
            )

    def _register(
        self,
        *,
        subject_kind: str,
        work_item_id: str,
        provider_thread_id: str,
        registered_by: str,
        run_id: Optional[str],
        attempt_id: Optional[str],
        now: Optional[datetime],
    ) -> Dict[str, object]:
        from .registry import Registry

        Registry(self.root).require_active(registered_by)
        current = _current_time(now)
        candidate: Dict[str, object] = {
            "schema_version": 1,
            "id": "thread-attachment-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "thread_attachment_registered",
            "provider": "codex_local",
            "provider_thread_id": provider_thread_id,
            "subject_kind": subject_kind,
            "work_item_id": work_item_id,
            "registered_by": registered_by,
            "registered_at_testimony": _timestamp(current),
        }
        if subject_kind == "attempt":
            candidate["run_id"] = run_id
            candidate["attempt_id"] = attempt_id
        candidate = validate_record(
            candidate,
            self.root.tenant_id,
            THREAD_OBSERVATION_KINDS,
            integrity=False,
        )

        semantic_fields = (
            "provider",
            "provider_thread_id",
            "subject_kind",
            "work_item_id",
            "registered_by",
            "run_id",
            "attempt_id",
        )

        def decide(
            existing: List[Dict[str, object]],
        ) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
            projection = ThreadObservationProjection.from_records(existing)
            Registry(self.root).require_active(registered_by)
            self._subject_exists(
                subject_kind, work_item_id, run_id, attempt_id
            )
            for row in projection.attachments():
                attachment = row["attachment"]
                assert isinstance(attachment, dict)
                if (
                    attachment["provider"] == candidate["provider"]
                    and attachment["provider_thread_id"]
                    == candidate["provider_thread_id"]
                ):
                    if all(
                        attachment.get(field) == candidate.get(field)
                        for field in semantic_fields
                    ):
                        return deepcopy(attachment), None
                    raise ProtocolRefusal(
                        "thread_attachment_conflict",
                        "provider thread coordinate already has a different attachment",
                    )
            return deepcopy(candidate), candidate

        return self.ledger._transact(self.__capability, decide)

    def register_work_item(
        self,
        work_item_id: str,
        provider_thread_id: str,
        registered_by: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        return self._register(
            subject_kind="work_item",
            work_item_id=work_item_id,
            provider_thread_id=provider_thread_id,
            registered_by=registered_by,
            run_id=None,
            attempt_id=None,
            now=now,
        )

    def register_attempt(
        self,
        run_id: str,
        work_item_id: str,
        attempt_id: str,
        provider_thread_id: str,
        registered_by: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        return self._register(
            subject_kind="attempt",
            work_item_id=work_item_id,
            provider_thread_id=provider_thread_id,
            registered_by=registered_by,
            run_id=run_id,
            attempt_id=attempt_id,
            now=now,
        )

    @staticmethod
    def _observation_candidate(
        attachment: Mapping[str, object],
        result: ThreadReadResult,
        current: datetime,
    ) -> Dict[str, object]:
        observed = result.observation_outcome == "observed"
        flags = None if result.active_flags is None else list(result.active_flags)
        if flags is None:
            attention = "unknown"
        elif not flags:
            attention = "none"
        elif len(flags) == 2:
            attention = "multiple"
        else:
            attention = flags[0]
        evidence_class = "measured" if observed else "unknown"
        candidate: Dict[str, object] = {
            "schema_version": 1,
            "id": "thread-observation-" + uuid7_hex(),
            "tenant_id": attachment["tenant_id"],
            "timestamp": _timestamp(current),
            "kind": "thread_observation_recorded",
            "attachment_id": attachment["id"],
            "provider": attachment["provider"],
            "provider_thread_id": attachment["provider_thread_id"],
            "provider_status": {
                "value": result.provider_status,
                "evidence_class": evidence_class,
            },
            "active_flags": {"value": flags, "evidence_class": evidence_class},
            "provider_updated_at": {
                "value": result.provider_updated_at,
                "evidence_class": evidence_class,
            },
            "attention": {
                "value": attention,
                "evidence_class": "derived" if observed else "unknown",
            },
            "observation_outcome": result.observation_outcome,
            "observation_reason": result.observation_reason,
            "observation_digest": "",
            "observed_at_testimony": _timestamp(current),
        }
        candidate["observation_digest"] = thread_observation_digest(candidate)
        return validate_record(
            candidate,
            str(attachment["tenant_id"]),
            THREAD_OBSERVATION_KINDS,
            integrity=False,
        )

    def observe(
        self, attachment_id: str, *, now: Optional[datetime] = None
    ) -> Dict[str, object]:
        before = self.ledger.project().attachment(attachment_id)
        if before["detachment"] is not None:
            raise ProtocolRefusal(
                "thread_attachment_detached",
                "detached thread attachments cannot be observed",
            )
        attachment = before["attachment"]
        assert isinstance(attachment, dict)
        result = self.source.read(str(attachment["provider_thread_id"]))
        candidate = self._observation_candidate(
            attachment, result, _current_time(now)
        )

        def decide(
            existing: List[Dict[str, object]],
        ) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
            current = ThreadObservationProjection.from_records(existing).attachment(
                attachment_id
            )
            if current["attachment"] != attachment:
                raise ProtocolRefusal(
                    "thread_attachment_changed",
                    "thread attachment changed during observation",
                )
            if current["detachment"] is not None:
                raise ProtocolRefusal(
                    "thread_attachment_detached",
                    "detached thread attachments cannot be observed",
                )
            latest = current["latest_observation"]
            if (
                isinstance(latest, dict)
                and latest["observation_digest"] == candidate["observation_digest"]
            ):
                return deepcopy(latest), None
            return deepcopy(candidate), candidate

        return self.ledger._transact(self.__capability, decide)

    def detach(
        self,
        attachment_id: str,
        detached_by: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        from .registry import Registry

        Registry(self.root).require_active(detached_by)
        before = self.ledger.project().attachment(attachment_id)
        attachment = before["attachment"]
        assert isinstance(attachment, dict)
        current = _current_time(now)
        candidate = validate_record(
            {
                "schema_version": 1,
                "id": "thread-attachment-detached-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": _timestamp(current),
                "kind": "thread_attachment_detached",
                "attachment_id": attachment_id,
                "provider": attachment["provider"],
                "provider_thread_id": attachment["provider_thread_id"],
                "detached_by": detached_by,
                "detached_at_testimony": _timestamp(current),
            },
            self.root.tenant_id,
            THREAD_OBSERVATION_KINDS,
            integrity=False,
        )

        def decide(
            existing: List[Dict[str, object]],
        ) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
            Registry(self.root).require_active(detached_by)
            state = ThreadObservationProjection.from_records(existing).attachment(
                attachment_id
            )
            if state["attachment"] != attachment:
                raise ProtocolRefusal(
                    "thread_attachment_changed",
                    "thread attachment changed during detachment",
                )
            detached = state["detachment"]
            if isinstance(detached, dict):
                if detached["detached_by"] == detached_by:
                    return deepcopy(detached), None
                raise ProtocolRefusal(
                    "thread_detachment_conflict",
                    "thread attachment was detached by a different actor",
                )
            return deepcopy(candidate), candidate

        return self.ledger._transact(self.__capability, decide)
