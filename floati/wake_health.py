"""Stamped wake-path health projected from durable local evidence."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

from .delivery_health import (DELIVERY_STALL_RED_MINUTES, DeliveryHealthAnalyzer,
                              _receipt_item_times, oldest_unread_from_summary)
from .errors import IntegrityFailure, ProtocolRefusal
from .events import EventLog
from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES, read_records_snapshot
from .registry import Registry
from .root import FloatiRoot
from .wake_control import is_session_paused, validate_session_id


_PUBLICATION_EVENTS = frozenset({
    "followup_after_error", "followup_after_abort", "followup_mail_at_start",
    "followup_inbox_unreadable", "followup_mail", "followup_rearm",
    "followup_participation_refused",
    # NEEDS-YOU-OBSERVED-1: a declared seat woken for an observed needs-you item.
    "followup_observed_item",
})
_EMPTY_RESPONSE_EVENTS = frozenset({
    "exit_empty_participation", "exit_empty_aborted", "exit_empty_aborted_twice",
})
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


def _publication_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _journal_object(pairs: list[tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate journal field")
        result[key] = value
    return result


def _journal_constant(_value: str) -> object:
    raise ValueError("nonfinite journal value")


def _session_summaries(observations: list[tuple]) -> list[Dict[str, object]]:
    """Aggregate source rows without confusing chronology with append order."""
    grouped: Dict[str, list[tuple]] = {}
    for session, parsed, stamp, reference in observations:
        grouped.setdefault(session, []).append((parsed, reference["line"], stamp, reference))
    result = []
    for session, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: (row[0], row[1]))
        result.append({
            "session_id": session, "evidence_count": len(rows),
            "first_at": rows[0][2], "last_at": rows[-1][2],
            "first_record": rows[0][3], "last_record": rows[-1][3],
        })
    return result


def _cursor_journal_snapshot(root: FloatiRoot, relative: Path) -> tuple[str, bytes]:
    """Open the exact source without following any component's symlink."""
    directory = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(root.tenant_home, flags)
        for component in relative.parts[:-1]:
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return "unavailable", b""
            data = stream.read(MAX_LEDGER_BYTES + 1)
        if len(data) > MAX_LEDGER_BYTES:
            return "over_limit", b""
        return "complete_read", data
    except FileNotFoundError:
        return "absent", b""
    except OSError:
        return "unavailable", b""
    finally:
        if directory is not None:
            os.close(directory)


def _cursor_publications(root: FloatiRoot, node: str, start: datetime, end: datetime) -> Dict[str, object]:
    relative = Path("state/cursor-wait") / node / "journal.jsonl"
    coverage, data = _cursor_journal_snapshot(root, relative)
    result: Dict[str, object] = {
        "coverage": coverage, "source_path": relative.as_posix(),
        "legacy_row_count": 0, "invalid_row_count": 0, "sessions": [],
    }
    if coverage != "complete_read" or not data:
        return result
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
    if len(lines) > MAX_LEDGER_RECORDS or any(len(line) + 1 > MAX_RECORD_BYTES for line in lines):
        result["coverage"] = "over_limit"
        return result
    legacy = invalid = 0
    observations = []
    for line_number, raw in enumerate(lines, start=1):
        try:
            if line_number == len(lines) and not data.endswith(b"\n"):
                raise ValueError("incomplete row")
            row = json.loads(raw.decode("utf-8"), object_pairs_hook=_journal_object,
                parse_constant=_journal_constant)
            if not isinstance(row, dict) or row.get("node") != node:
                raise ValueError("foreign or malformed row")
            if "publication" not in row and "published_at" not in row:
                if not isinstance(row.get("event"), str) or _publication_time(row.get("ts")) is None:
                    raise ValueError("malformed legacy row")
                if row["event"] not in _EMPTY_RESPONSE_EVENTS:
                    legacy += 1
                continue
            if (row.get("publication") != "flushed" or not isinstance(row.get("event"), str)
                    or row["event"] not in _PUBLICATION_EVENTS):
                raise ValueError("unrecognized publication")
            session = validate_session_id(row.get("conversation_id"))
            published = _publication_time(row.get("published_at"))
            if published is None:
                raise ValueError("invalid publication time")
            reference: Dict[str, object] = {
                "line": line_number, "sha256": hashlib.sha256(raw).hexdigest(), "event": row["event"],
            }
            reason = row.get("reason_code")
            if reason is not None:
                if not isinstance(reason, str) or _REASON_CODE.fullmatch(reason) is None:
                    raise ValueError("invalid reason code")
                reference["reason_code"] = reason
            if start <= published <= end:
                observations.append((session, published, row["published_at"], reference))
        except (ValueError, UnicodeDecodeError, RecursionError, ProtocolRefusal):
            invalid += 1
    result.update({
        "coverage": "incomplete" if invalid else "complete_read",
        "legacy_row_count": legacy, "invalid_row_count": invalid,
        "sessions": _session_summaries(observations),
    })
    return result


class WakeHealthProjection:
    """Join claim, waiter, breaker, pause, and unread facts for one node."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def stop_waiting(self, node_id: str, now: datetime) -> Dict[str, object]:
        """Observe one current native Stop holder, independently of turn completion.

        An open ledger row or a live session process alone is insufficient.
        The acquired holder must name this exact invocation, still have its
        original native authority and budget, and survive a stable reread.
        No lock, receipt, registration, or lifecycle event is created here.
        """
        from .codex_wait_contract import resolve_participant
        from .codex_wait_liveness import (
            classify_holder, holder_testimony_relative, read_holder_testimony,
        )
        from .errors import FloatiError
        from .recipient_mechanisms import _maps, _native, _paused, _read
        from .stop_hold import StopHoldLedger

        current = self._current(now)
        result = {"state": "UNKNOWN", "reason": "current_stop_holder_not_proven",
            "turn_ended": {"state": "UNKNOWN", "reason": "stop_is_not_turn_completion"},
            "sources": []}
        try:
            snapshots = []
            registry = Registry(self.root)
            registry_path = self.root.tenant_home / registry.relative_path
            snapshots.append((registry_path, _read(registry_path, with_identity=True)))
            registration = registry.require_active(node_id)
            node = registration["node_id"]
            if registration["role"].casefold() != "codex":
                result["reason"] = "codex_stop_holder_not_applicable"
                return result
            maps = _maps(self.root, node, snapshots=snapshots)
            hold_relative = StopHoldLedger.relative(node)
            hold_path = self.root.tenant_home / hold_relative
            snapshots.append((hold_path, _read(hold_path, with_identity=True)))
            rows = StopHoldLedger(self.root).read(node)
            starts = [row for row in rows if row["phase"] == "start"]
            if not starts:
                return result
            start = starts[-1]
            result["sources"] = [{"path": hold_relative.as_posix(), "record_id": start["id"]}]
            if start["harness"] != "codex" or any(row["phase"] == "exit"
                    and row["invocation_id"] == start["invocation_id"] for row in rows):
                return result
            began = datetime.fromisoformat(start["started_at"].replace("Z", "+00:00"))
            if not began <= current < began + timedelta(seconds=start["deadline_seconds"]):
                result["reason"] = "stop_hold_outside_native_budget"
                return result
            workspace = Path(start["workspace"])
            if workspace not in maps:
                return result
            participant = resolve_participant(self.root.tenant_home, workspace)
            if participant is None or participant.binding.node_id != node or (
                    participant.binding.workspace != workspace):
                return result
            claim, consent = _native(self.root, node, workspace, snapshots=snapshots)
            if claim is None or consent is None or claim["state"] != "armed" or (
                    consent["state"] != "armed"
                    or claim["acting_session_id"] != start["acting_session_id"]
                    or claim["consent_receipt_id"] != consent["id"]
                    or consent["id"] != start["consent_receipt_id"]
                    or start["deadline_seconds"] > consent["wait_deadline_seconds"]):
                result["reason"] = "stop_hold_native_authority_changed"
                return result
            for row in (claim, consent):
                if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) > current:
                    return result
            if _paused(self.root, node, start["acting_session_id"]):
                result["reason"] = "stop_holder_paused"
                return result
            holder_relative = holder_testimony_relative(node)
            holder_path = self.root.tenant_home / holder_relative
            holder_snapshot = _read(holder_path, 4096, with_identity=True)
            # Reject duplicate fields/nonfinite numbers before the compatible
            # legacy process reader consumes this exact ordinary snapshot.
            json.loads(holder_snapshot[0].decode("utf-8"),
                object_pairs_hook=_journal_object, parse_constant=_journal_constant)
            testimony = read_holder_testimony(self.root, node)
            if testimony is None or testimony.invocation_id != start["invocation_id"] or (
                    testimony.session_id != start["acting_session_id"]):
                result["reason"] = "stop_holder_invocation_unproven"
                return result
            if classify_holder(testimony) != "live":
                result["reason"] = "stop_holder_process_not_live"
                return result
            if _read(holder_path, 4096, with_identity=True) != holder_snapshot or (
                    _paused(self.root, node, start["acting_session_id"])) or any(
                    _read(path, with_identity=True) != snapshot for path, snapshot in snapshots):
                result["reason"] = "stop_holder_evidence_changed"
                return result
            deadline = began + timedelta(seconds=start["deadline_seconds"])
            result.update(state="STOP_WAITING", reason="current_native_stop_holder_live",
                hold_deadline_at=deadline.isoformat().replace("+00:00", "Z"),
                next_check_at=min(deadline, current + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"))
            result["sources"].extend([
                {"path": "registry/entries.jsonl", "record_id": registration["id"]},
                {"path": "receipts/codex-wait-session/" + node + ".jsonl", "record_id": claim["id"]},
                {"path": "receipts/codex-wait-consent/" + node + ".jsonl", "record_id": consent["id"]},
                {"path": holder_relative.as_posix(), "record_id": start["invocation_id"]},
            ])
            return result
        except (FloatiError, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            result["reason"] = "stop_holder_evidence_unavailable"
            return result

    @staticmethod
    def _current(now: datetime) -> datetime:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("wake health observation requires an aware datetime")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _stamp(now: datetime) -> str:
        return now.isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _latest(rows: list[Dict[str, object]]) -> Optional[Dict[str, object]]:
        return rows[-1] if rows else None

    def _attempts(self, node: str) -> list[Dict[str, object]]:
        rows = read_records_snapshot(
            self.root, Path("receipts/wakes") / f"{node}.jsonl",
            allowed_kinds={"wake_attempt_receipt"},
        )
        if any(row["node_id"] != node for row in rows):
            raise IntegrityFailure("wake_health_receipt_invalid", "wake receipt node does not match its ledger")
        return rows

    def _attempt_observation(self, node: str) -> tuple[list[Dict[str, object]], Optional[Dict[str, object]]]:
        """Unavailable history is an observation, never a valid legacy row."""
        try:
            return self._attempts(node), None
        except IntegrityFailure as failure:
            if failure.code != "decision_receipt_id_invalid":
                raise
            relative = Path("receipts/wakes") / f"{node}.jsonl"
            path = self.root.resolve_relative(relative)
            # Native decoding validates the common record ID before this field
            # and qualifies its failure with this exact ledger coordinate.
            # Copy only a governed ID, never the defective value or raw detail.
            prefix = f"ledger {path}: record "
            record_id = None
            if failure.detail.startswith(prefix):
                candidate, separator, _ = failure.detail[len(prefix):].partition(
                    ": kind wake_attempt_receipt:"
                )
                if separator and re.fullmatch(
                    r"wake-attempt-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}", candidate
                ):
                    record_id = candidate
            return [], {
                "code": "wake_receipt_legacy_unreadable",
                "ledger": relative.as_posix(), "record_id": record_id,
                "source_code": failure.code,
            }

    def recent_sessions(self, node_id: str, now: datetime) -> Dict[str, object]:
        """Fresh, read-only hour evidence; historical activity is not liveness."""
        current = self._current(now)
        node = Registry(self.root).resolve_node_id(node_id, field="node")
        attempts, read_finding = self._attempt_observation(node)
        return self._recent_sessions(node, current, attempts, read_finding=read_finding)

    def _recent_sessions(self, node: str, current: datetime, attempts: list[Dict[str, object]],
                         *, read_finding: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        start = current - timedelta(hours=1)
        observations = []
        for line_number, row in enumerate(attempts, start=1):
            stamp = str(row["timestamp"])
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if row["outcome"] == "woke" and start <= parsed <= current:
                observations.append((str(row["acting_session_id"]), parsed, stamp, {
                    "line": line_number, "receipt_id": row["id"],
                }))
        return {
            "window_seconds": 3600,
            "window_start": start.isoformat().replace("+00:00", "Z"),
            "window_end": current.isoformat().replace("+00:00", "Z"),
            "native_woke": {
                "coverage": "unavailable" if read_finding is not None else "recorded_receipts_only",
                "source_path": (Path("receipts/wakes") / f"{node}.jsonl").as_posix(),
                "sessions": _session_summaries(observations),
            },
            "cursor_local_publication": _cursor_publications(self.root, node, start, current),
        }

    def seat_attention(self, node_id: str, now: datetime, *, mail=None) -> Dict[str, object]:
        """Observe overdue mail without inferring turn completion or deafness.

        The 15-minute threshold is the inherited DELIVERY_STALL_RED_MINUTES
        ruled judgment, not a measured turn duration or consent hold budget.
        A START since the oldest waiting mail suffices; it claims no delivery.
        """
        from .stop_hold import StopHoldLedger

        if mail is None:
            from .mail_summary import observe
            from .send_prefix import _CACHE_ERRORS
            observed = observe(self.root)
            if observed is not None:
                try:
                    return self.seat_attention(node_id, now, mail=observed)
                except _CACHE_ERRORS:
                    pass
        if mail is False:
            mail = None

        current = self._current(now)
        registration = Registry(self.root).require_active(node_id)
        node = str(registration["node_id"])
        result = {
            "state": "not_applicable", "reason": "harness_has_no_stop_hook",
            "node_id": node, "observed_at": self._stamp(current),
            "threshold_minutes": DELIVERY_STALL_RED_MINUTES,
            "threshold_basis": "inherited ruled judgment; not a measured turn duration",
            "undelivered_count": 0, "oldest_waiting_at": None,
            "oldest_waiting_age_seconds": None, "last_hold_started_at": None,
            "hold_evidence_state": "not_read", "next_check_at": None,
            "detail": "", "remedy": "",
            "sources": [{"path": "registry/entries.jsonl", "record_id": registration["id"]}],
        }
        if str(registration["role"]).casefold() in {"worker", "smoke"}:
            return result
        if mail is None:
            events, _unknown, _skew = EventLog(self.root)._compatible_event_records_with_skew(snapshot=True)
            delivered = set(_receipt_item_times(self.root, "deliveries", node))
            pending = [row for row in events if row["kind"] == "message_envelope"
                       and row["recipient"] == node and row["id"] not in delivered]
            pending_count = len(pending)
            oldest = min(pending, key=lambda row: str(row["timestamp"])) if pending else None
        else:
            observed_mail = mail.node(node)
            pending_count = observed_mail["undelivered_count"]
            oldest = observed_mail["oldest_waiting"]
        result.update(state="not_overdue", reason="no_waiting_mail", undelivered_count=pending_count)
        if oldest is None:
            return result
        waiting_at = datetime.fromisoformat(str(oldest["timestamp"]).replace("Z", "+00:00"))
        age = max(0.0, (current - waiting_at).total_seconds())
        result.update(oldest_waiting_at=oldest["timestamp"], oldest_waiting_age_seconds=age)
        result["sources"].extend([
            {"path": "events.jsonl", "record_id": oldest["id"]},
            {"path": f"receipts/deliveries/{node}.jsonl", "record_id": None},
            {"path": str(StopHoldLedger.relative(node)), "record_id": None},
        ])
        try:
            rows = StopHoldLedger(self.root).read(node)
        except (OSError, IntegrityFailure, ProtocolRefusal):
            result["hold_evidence_state"] = "unknown"
        else:
            result["hold_evidence_state"] = "readable"
            starts = [row for row in rows if row["phase"] == "start"
                      and datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")) <= current]
            if starts:
                latest = max(starts, key=lambda row: row["started_at"])
                result["last_hold_started_at"] = latest["started_at"]
                result["sources"][-1]["record_id"] = latest["id"]
                if datetime.fromisoformat(latest["started_at"].replace("Z", "+00:00")) >= waiting_at:
                    result["reason"] = "hold_started_since_waiting_mail"
                    return result
        if age <= DELIVERY_STALL_RED_MINUTES * 60:
            result.update(reason="fresh_waiting_mail",
                next_check_at=(waiting_at + timedelta(minutes=DELIVERY_STALL_RED_MINUTES,
                                                      milliseconds=1)).isoformat().replace("+00:00", "Z"))
            return result
        result.update(state="unknown", reason="overdue_without_hold_start"
            if result["hold_evidence_state"] == "readable" else "hold_evidence_unreadable")
        last = result["last_hold_started_at"] or (
            "unknown (hold testimony unreadable)" if result["hold_evidence_state"] == "unknown"
            else "none on record")
        result["detail"] = (
            f"{node}: {pending_count} undelivered, oldest waiting {int(age // 60)}m; "
            f"last hold start {last}; threshold {DELIVERY_STALL_RED_MINUTES}m (inherited ruled judgment). "
            "Floati cannot tell a long-running turn from skipped Stop."
        )
        result["remedy"] = f"Look at {node}'s seat for a pending approval card or a stopped turn."
        return result

    def fact(self, node_id: str, now: datetime, *, mail=None) -> Dict[str, object]:
        if mail is None:
            from .mail_summary import observe
            from .send_prefix import _CACHE_ERRORS
            observed = observe(self.root)
            if observed is not None:
                try:
                    return self.fact(node_id, now, mail=observed)
                except _CACHE_ERRORS:
                    pass
        if mail is False:
            mail = None
        current = self._current(now)
        node = Registry(self.root).resolve_node_id(node_id, field="node")
        sessions = read_records_snapshot(
            self.root,
            Path("receipts/codex-wait-session") / f"{node}.jsonl",
            allowed_kinds={"codex_wait_session_receipt"},
        )
        claim = self._latest(sessions)
        claim_session = (
            str(claim["acting_session_id"])
            if claim is not None and claim.get("state") == "armed"
            else None
        )
        attempts, read_finding = self._attempt_observation(node)
        last_attempt = self._latest([row for row in attempts if row.get("outcome") == "woke"])
        last_seen_session = (
            None if last_attempt is None else str(last_attempt["acting_session_id"])
        )
        exits = read_records_snapshot(
            self.root,
            Path("receipts/wake-waiter-exit") / f"{node}.jsonl",
            allowed_kinds={"wake_waiter_exit_receipt"},
        )
        last_exit = self._latest(exits)
        if last_exit is None:
            waiter_exit_reason = None
            waiter_receipt_age_minutes = None
        else:
            from .wake_exit import WAKE_EXIT_REASONS
            waiter_exit_reason = str(last_exit["reason_code"])
            if waiter_exit_reason not in WAKE_EXIT_REASONS:
                # Every reason is named in the one vocabulary; none is passed through unread.
                raise IntegrityFailure("reason_code_invalid", "waiter exit reason is not a governed value")
            try:
                stamp = datetime.fromisoformat(
                    str(last_exit["timestamp"]).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError as exc:
                raise IntegrityFailure(
                    "wake_health_receipt_invalid",
                    "waiter exit receipt timestamp is unavailable",
                ) from exc
            waiter_receipt_age_minutes = max(
                0, int((current - stamp).total_seconds() // 60)
            )

        breaker_path = self.root.resolve_relative(
            Path("state/codex-wait") / node / "breaker.json"
        )
        try:
            breaker = json.loads(breaker_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            hits = []
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityFailure(
                "wake_health_breaker_invalid", "waiter breaker evidence is malformed"
            ) from exc
        else:
            if not isinstance(breaker, dict) or not isinstance(breaker.get("hits"), list):
                raise IntegrityFailure(
                    "wake_health_breaker_invalid", "waiter breaker evidence is malformed"
                )
            hits = [
                float(value) for value in breaker["hits"]
                if isinstance(value, (int, float))
                and 0 <= current.timestamp() - float(value) < 60
            ]
        breaker_state = "open" if len(hits) > 20 else "closed"
        pause_state = (
            "unknown" if claim_session is None
            else "paused" if is_session_paused(self.root, node, claim_session)
            else "active"
        )
        if mail is None:
            events, _unrecognized_kinds, _skew = (
                EventLog(self.root)._compatible_event_records_with_skew(snapshot=True)
            )
            health = DeliveryHealthAnalyzer.analyze(
                events=events, root=self.root, nodes=[node], now=current
            ).by_node[node]
            oldest_unread = health.oldest_unread
        else:
            oldest_unread = oldest_unread_from_summary(node, mail.node(node), current)

        entrypoint = Path(__file__).resolve().parents[1] / "scripts" / "floati"
        if read_finding is not None:
            state = "unknown"
        elif pause_state == "paused":
            state = "paused"
        elif breaker_state == "open":
            state = "breaker_open"
        elif oldest_unread is not None and (
            claim_session is None or last_seen_session != claim_session
        ):
            state = "stale_claim_with_unread_mail"
        elif oldest_unread is not None:
            state = "unread_mail"
        else:
            state = "healthy"
        workspace = None if claim is None else claim.get("workspace")
        if workspace is None:
            from .codex_wait_contract import WORKSPACE_MAP_RELATIVE, resolve_workspace_binding
            try:
                mapping = json.loads(self.root.resolve_relative(WORKSPACE_MAP_RELATIVE).read_text())
                candidates = [row.get("workspace") for row in mapping.get("mappings", [])
                              if isinstance(row, dict) and row.get("node_id") == node]
                valid = [path for path in candidates if isinstance(path, str)
                         and resolve_workspace_binding(self.root.path, Path(path)) is not None]
                workspace = valid[0] if len(valid) == 1 else None
            except (OSError, ValueError, AttributeError):
                workspace = None
        if workspace is None:
            remedy = "declare one exact workspace and acting session for this node, then run floati seat board with --session and --take-over for that coordinate; after success present the verified takeover_report"
        else:
            key = "board-notice-" + hashlib.sha256((node + "\0" + str(workspace) + "\0" + self._stamp(current)).encode()).hexdigest()[:32]
            remedy = (
                "Acting session is not known; after success present the verified takeover_report; add --session with your explicitly declared session ID to: "
                + shlex.join([str(entrypoint), "seat", "board", "--root", str(self.root.path),
                    "--as", node, "--workspace", str(workspace), "--idempotency-key", key,
                    "--take-over"])
            )
        if read_finding is not None:
            remedy = (
                "Retain " + str(read_finding["ledger"]) + " unchanged and inspect an owned clone. "
                "Rehearse any quarantine with the actual Stop waiter before live repair."
            )
        fact = {
            "schema_version": 1,
            "node_id": node,
            "state": state,
            "observed_at": self._stamp(current),
            "claim_session": claim_session,
            "last_seen_session": last_seen_session,
            "recent_sessions": self._recent_sessions(node, current, attempts, read_finding=read_finding),
            "waiter_exit_reason": waiter_exit_reason,
            "waiter_receipt_age_minutes": waiter_receipt_age_minutes,
            "breaker_state": breaker_state,
            "pause_state": pause_state,
            "oldest_unread": oldest_unread,
            "documented_entrypoint": str(entrypoint),
            "documented_entrypoint_resolves": entrypoint.is_file(),
            "remedy": remedy,
        }
        if read_finding is not None:
            fact["read_finding"] = read_finding
        return fact
