"""Exact-byte journal chaining, checkpoints, and rollback testimony."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .ids import uuid7_hex
from .jsonl import read_records, transact, transact_exact_frame
from .records import validate_record
from .registry import utc_now
from .root import FloatiRoot, validate_identifier


CHECKPOINT_FORMAT = "floati-journal-checkpoint-v1"
_CHECKPOINT_FIELDS = frozenset(
    {"format", "journal_id", "through_seq", "head_sha256", "byte_length"}
)
_STATE_KINDS = frozenset({"journal_checkpoint_state"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class JournalChain:
    """Opt one existing JSONL journal into chain-forward v-next records."""

    def __init__(
        self,
        root: FloatiRoot,
        relative_path: Path,
        *,
        journal_id: str,
        allowed_kinds: Iterable[str],
    ) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "journal chaining requires one writable root")
        self.root = root
        self.relative_path = Path(relative_path)
        self.path = root.resolve_relative(self.relative_path)
        self.journal_id = validate_identifier(journal_id, "journal_id")
        kinds = frozenset(allowed_kinds)
        if not kinds or any(not isinstance(kind, str) or not kind for kind in kinds):
            raise ProtocolRefusal(
                "journal_kinds_invalid", "journal chaining requires a bounded known-kind set"
            )
        self.allowed_kinds = kinds

    @staticmethod
    def _digest(line: bytes) -> str:
        return hashlib.sha256(line).hexdigest()

    def _genesis(self) -> str:
        return hashlib.sha256(self.journal_id.encode("utf-8")).hexdigest()

    def append(self, record: Mapping[str, object]) -> Dict[str, object]:
        if not isinstance(record, Mapping):
            raise ProtocolRefusal("record_not_object", "chained journal record must be an object")
        if "seq" in record or "prev" in record:
            raise ProtocolRefusal(
                "journal_chain_caller_fields",
                "the journal controller, never the caller, assigns seq and prev",
            )

        def decide(
            existing: list[Dict[str, object]], exact_lines: tuple[bytes, ...]
        ) -> tuple[Dict[str, object], Dict[str, object]]:
            if any("seq" in row for row in existing):
                measured = self._verify_rows(existing, exact_lines)
                if measured["through_seq"] != existing[-1]["seq"]:
                    raise IntegrityFailure(
                        "journal_chain_interrupted",
                        "a legacy line appears after the chain boundary",
                    )
                seq = int(measured["through_seq"]) + 1
                prev = self._digest(exact_lines[-1])
            else:
                seq = 1
                prev = self._digest(exact_lines[-1]) if exact_lines else self._genesis()
            candidate = dict(record, seq=seq, prev=prev)
            return candidate, candidate

        return transact_exact_frame(
            self.root,
            self.relative_path,
            decide,
            allowed_kinds=set(self.allowed_kinds),
        )

    def _decode(self, data: bytes) -> tuple[list[Dict[str, object]], tuple[bytes, ...]]:
        if data and not data.endswith(b"\n"):
            raise ProtocolRefusal(
                "journal_truncated_tail", "journal ends with an incomplete exact line"
            )
        try:
            decoded = decode_frames(data)
        except FrameError as exc:
            code = (
                "journal_truncated_tail"
                if exc.code == "incomplete_frame"
                else "journal_frame_malformed"
            )
            raise ProtocolRefusal(code, exc.detail) from exc
        rows: list[Dict[str, object]] = []
        seen = set()
        for raw in decoded:
            try:
                row = validate_record(
                    raw,
                    self.root.tenant_id,
                    self.allowed_kinds,
                    integrity=True,
                )
            except IntegrityFailure as exc:
                raise ProtocolRefusal("journal_record_invalid", exc.detail) from exc
            if row["id"] in seen:
                raise ProtocolRefusal(
                    "journal_record_duplicate", "journal repeats a durable record id"
                )
            seen.add(row["id"])
            rows.append(row)
        return rows, tuple(data.split(b"\n")[:-1])

    def _verify_rows(
        self,
        rows: list[Dict[str, object]],
        exact_lines: tuple[bytes, ...],
    ) -> Dict[str, object]:
        first = next((index for index, row in enumerate(rows) if "seq" in row), None)
        if first is None:
            raise ProtocolRefusal(
                "journal_chain_absent", "journal has no v-next seq/prev boundary"
            )
        if any("seq" in row or "prev" in row for row in rows[:first]):
            raise ProtocolRefusal(
                "journal_chain_fields_invalid", "pre-chain records carry partial chain fields"
            )
        if any("seq" not in row or "prev" not in row for row in rows[first:]):
            raise ProtocolRefusal(
                "journal_chain_interrupted", "a legacy record appears after the chain boundary"
            )
        expected_seq = 1
        expected_prev = self._digest(exact_lines[first - 1]) if first else self._genesis()
        for index in range(first, len(rows)):
            row = rows[index]
            seq = int(row["seq"])
            if seq != expected_seq:
                code = (
                    "journal_seq_gap" if seq > expected_seq and expected_seq > 1
                    else "journal_seq_out_of_order"
                )
                raise ProtocolRefusal(code, f"journal expected seq {expected_seq}, found {seq}")
            if row["prev"] != expected_prev:
                raise ProtocolRefusal(
                    "journal_prev_mismatch",
                    f"journal seq {seq} does not hash its preceding exact line",
                )
            expected_seq += 1
            expected_prev = self._digest(exact_lines[index])
        return {
            "legacy_prefix_lines": first,
            "chain_start_line": first + 1,
            "through_seq": expected_seq - 1,
            "head_sha256": self._digest(exact_lines[-1]),
        }

    def _read_data(self) -> bytes:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise ProtocolRefusal("journal_missing", "the selected journal does not exist") from exc
        except OSError as exc:
            raise DurabilityFailure("storage_unavailable", "journal could not be read") from exc
        if not data:
            raise ProtocolRefusal("journal_empty", "the selected journal is empty")
        return data

    def _measure(self, data: bytes) -> Dict[str, object]:
        rows, exact_lines = self._decode(data)
        measured = self._verify_rows(rows, exact_lines)
        measured["byte_length"] = len(data)
        return measured

    @staticmethod
    def _checkpoint(value: object, journal_id: str) -> Dict[str, object]:
        if not isinstance(value, dict) or frozenset(value) != _CHECKPOINT_FIELDS:
            raise ProtocolRefusal(
                "journal_checkpoint_invalid", "checkpoint fields do not match the v1 contract"
            )
        if value.get("format") != CHECKPOINT_FORMAT:
            raise ProtocolRefusal(
                "journal_checkpoint_format_invalid", "checkpoint format is not supported"
            )
        if value.get("journal_id") != journal_id:
            raise ProtocolRefusal(
                "journal_checkpoint_id_mismatch", "checkpoint belongs to another journal"
            )
        through = value.get("through_seq")
        length = value.get("byte_length")
        if not isinstance(through, int) or isinstance(through, bool) or not 1 <= through <= 2**63 - 1:
            raise ProtocolRefusal(
                "journal_checkpoint_seq_invalid", "checkpoint through_seq is out of bounds"
            )
        if not isinstance(length, int) or isinstance(length, bool) or not 1 <= length <= 64 * 1024 * 1024:
            raise ProtocolRefusal(
                "journal_checkpoint_length_invalid", "checkpoint byte_length is out of bounds"
            )
        head = value.get("head_sha256")
        if not isinstance(head, str) or _SHA256.fullmatch(head) is None:
            raise ProtocolRefusal(
                "journal_checkpoint_head_invalid", "checkpoint head digest is malformed"
            )
        return dict(value)

    @staticmethod
    def _durable_replace(path: Path, payload: bytes) -> None:
        temporary = path.with_name("." + path.name + ".tmp-" + uuid7_hex())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise DurabilityFailure(
                "storage_unavailable", "checkpoint could not be durably written"
            ) from exc

    def write_checkpoint(self, output_relative: Path) -> Dict[str, object]:
        data = self._read_data()
        measured = self._measure(data)
        checkpoint: Dict[str, object] = {
            "format": CHECKPOINT_FORMAT,
            "journal_id": self.journal_id,
            "through_seq": measured["through_seq"],
            "head_sha256": measured["head_sha256"],
            "byte_length": len(data),
        }
        output = self.root.resolve_relative(Path(output_relative))
        if output == self.path:
            raise ProtocolRefusal(
                "journal_checkpoint_path_invalid", "checkpoint cannot replace its journal"
            )
        self._durable_replace(output, encode_frame(checkpoint))
        return checkpoint

    def read_checkpoint(self, relative: Path) -> Dict[str, object]:
        path = self.root.resolve_relative(Path(relative))
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ProtocolRefusal(
                "journal_checkpoint_unreadable", "checkpoint file could not be read"
            ) from exc
        try:
            decoded = decode_frames(data)
        except FrameError as exc:
            raise ProtocolRefusal("journal_checkpoint_invalid", exc.detail) from exc
        if len(decoded) != 1:
            raise ProtocolRefusal(
                "journal_checkpoint_invalid", "checkpoint file must contain one exact line"
            )
        return self._checkpoint(decoded[0], self.journal_id)

    def _state_relative(self) -> Path:
        digest = hashlib.sha256(self.journal_id.encode("utf-8")).hexdigest()
        return Path("receipts/journal-checkpoints") / (digest + ".jsonl")

    def _states(self) -> list[Dict[str, object]]:
        return read_records(
            self.root, self._state_relative(), allowed_kinds=set(_STATE_KINDS)
        )

    def verify(
        self,
        checkpoint_value: Mapping[str, object],
        *,
        historical: bool = False,
    ) -> Dict[str, object]:
        checkpoint = self._checkpoint(dict(checkpoint_value), self.journal_id)
        data = self._read_data()
        self._measure(data)
        length = int(checkpoint["byte_length"])
        if length > len(data) or not data[:length].endswith(b"\n"):
            raise ProtocolRefusal(
                "journal_checkpoint_length_mismatch",
                "checkpoint byte_length is not an exact journal prefix",
            )
        prefix = data[:length]
        measured = self._measure(prefix)
        if measured["through_seq"] != checkpoint["through_seq"]:
            raise ProtocolRefusal(
                "journal_checkpoint_seq_mismatch",
                "checkpoint through_seq does not match its exact prefix",
            )
        if measured["head_sha256"] != checkpoint["head_sha256"]:
            raise ProtocolRefusal(
                "journal_head_mismatch", "checkpoint head does not match its exact prefix"
            )
        through = int(checkpoint["through_seq"])
        if historical:
            highest = max(
                (int(row["through_seq"]) for row in self._states()), default=0
            )
        else:
            checkpoint_line = encode_frame(checkpoint)[:-1]

            def accept(
                states: list[Dict[str, object]],
            ) -> tuple[int, Optional[Dict[str, object]]]:
                highest = max(
                    (int(row["through_seq"]) for row in states), default=0
                )
                if through < highest:
                    raise ProtocolRefusal(
                        "journal_rollback_suspected",
                        f"checkpoint seq {through} is below accepted seq {highest}",
                    )
                if through == highest:
                    return highest, None
                state: Dict[str, object] = {
                    "schema_version": 0,
                    "id": "journal-checkpoint-state-" + uuid7_hex(),
                    "tenant_id": self.root.tenant_id,
                    "timestamp": utc_now(),
                    "kind": "journal_checkpoint_state",
                    "journal_id": self.journal_id,
                    "through_seq": through,
                    "head_sha256": checkpoint["head_sha256"],
                    "byte_length": length,
                    "checkpoint_sha256": self._digest(checkpoint_line),
                }
                return through, state

            highest = transact(
                self.root,
                self._state_relative(),
                accept,
                allowed_kinds=set(_STATE_KINDS),
            )
        legacy = int(measured["legacy_prefix_lines"])
        legacy_subject = "line is" if legacy == 1 else "lines are"
        statement = (
            "Verified exact-byte continuity through the selected checkpoint; "
            f"{legacy} pre-chain {legacy_subject} anchored without "
            "seq/prev testimony. This checkpoint alone cannot prove freshness or authenticity."
        )
        return {
            "state": "verified",
            "journal_id": self.journal_id,
            "through_seq": through,
            "head_sha256": measured["head_sha256"],
            "byte_length": length,
            "legacy_prefix_lines": legacy,
            "chain_start_line": measured["chain_start_line"],
            "highest_accepted_seq": highest,
            "historical": historical,
            "scope_statement": statement,
        }
