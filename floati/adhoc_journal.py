"""Immutable authenticated admission and exclusive physical-row ownership.

Native lifecycle phases remain closed until their owning producers supply the
reviewed receipt contracts. An unfinished admitted owner is never released.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .adhoc_profiles import AdhocProfiles, current_time, timestamp
from .adhoc_records import (JOURNAL_RELATIVE, canonical_json, dispatch_digest,
    refuse, validate_dispatch, validate_lifecycle_evidence)
from .bus_epoch import epoch_guard
from .car_fence import car_acceptance_guard
from .errors import IntegrityFailure, ProtocolRefusal
from .events import EventLog
from .git_process import fixed_git_command, fixed_git_environment
from .ids import uuid7_hex
from .jsonl import read_records, read_records_snapshot, _transact_adhoc_records
from .planes import AuthorityGrantStore
from .records import validate_record
from .root import FloatiRoot, validate_identifier
from .work import WorkLog

JOURNAL_KINDS = {"adhoc_dispatch_record", "adhoc_native_thread_opened_record", "adhoc_lifecycle_record"}
MAX_DISPATCH_BYTES = 65536


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _git(repository, *arguments, limit=MAX_DISPATCH_BYTES):
    try:
        result = subprocess.run(fixed_git_command("/usr/bin/git", repository, arguments),
            env=fixed_git_environment("/usr/bin/git"), capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProtocolRefusal("adhoc_dispatch_authentication_failed", "exact local Git inspection is unavailable") from exc
    if result.returncode or len(result.stdout) > limit or len(result.stderr) > MAX_DISPATCH_BYTES:
        refuse("adhoc_dispatch_authentication_failed", "exact Git dispatch inspection failed or exceeded its bound")
    return result.stdout


def _blob(repository, sha, doc):
    tree = _git(repository, "ls-tree", sha, "--", doc)
    if not tree.startswith((b"100644 blob ", b"100755 blob ")) or tree.count(b"\n") != 1:
        refuse("adhoc_dispatch_authentication_failed", "dispatch must be one exact ordinary Git blob")
    size = _git(repository, "cat-file", "-s", sha + ":" + doc)
    try:
        if not 0 < int(size.strip()) <= MAX_DISPATCH_BYTES:
            raise ValueError()
    except ValueError:
        refuse("adhoc_dispatch_authentication_failed", "dispatch Git blob exceeds its bound")
    raw = _git(repository, "cat-file", "blob", sha + ":" + doc)
    if len(raw) != int(size.strip()):
        refuse("adhoc_dispatch_authentication_failed", "dispatch Git blob size changed")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        return validate_dispatch(parsed), hashlib.sha256(raw).hexdigest()
    except (ValueError, UnicodeError, TypeError, OverflowError) as exc:
        raise ProtocolRefusal("adhoc_dispatch_authentication_failed", "dispatch must be bounded strict unique-key JSON") from exc


def _physical_row(profile):
    identities = profile["filesystem_bindings"]
    return tuple((identities[field]["dev"], identities[field]["ino"])
                 for field in ("workspace", "git_common_dir", "git_admin_dir"))


def _lifecycle_key(row):
    return (row["phase"], row["evidence"].get("stage") if row["phase"] == "process_closed" else None)


def _history(rows, dispatch_id):
    return [row for row in rows if row["kind"] == "adhoc_lifecycle_record" and row["dispatch_id"] == dispatch_id]


def _phases(rows):
    return {_lifecycle_key(row): row["evidence"] for row in rows}


def _released(rows):
    phases = _phases(rows)
    cleanup = phases.get(("process_closed", "cleanup"), {}).get("cleanup")
    return (cleanup is not None and not cleanup["alive_after_cleanup"]
            and ("result_published", None) in phases
            and not any((phase, None) in phases for phase in ("failed", "unknown")))


def sender_request(root, dispatch):
    """Resolve the peer from the exact request pinned by a coordinator dispatch.

    A version-one dispatch retains its original whole-mailbox semantics. Version
    two gives a sender a private governed profile, including its own writable
    row and provider slot; a peer request grants no launch or work authority.
    """
    if dispatch["schema_version"] == 1:
        return None
    events = EventLog(root)
    frames = events.event_records()
    requests = [row for row in frames if row["kind"] == "message_envelope"
                and row["id"] == dispatch["request_id"]]
    if (len(requests) != 1 or any(row["kind"] == "message_retracted"
            and row["retracted_message_id"] == dispatch["request_id"] for row in frames)):
        refuse("adhoc_sender_binding_changed", "sender request is unavailable or retracted")
    request = requests[0]
    if (request["recipient"] != dispatch["node_id"]
        or request["sender"] != dispatch["result_recipient"]
        or request.get("worker_session_id") is not None):
        refuse("adhoc_sender_binding_changed", "sender request must address this served node and receive its answer")
    return request


def _validate_lifecycle_order(row, prior, admission, opened, error):
    evidence, phase = row["evidence"], row["phase"]
    phases = _phases(prior)
    key = _lifecycle_key(row)
    if key in phases or any((p, None) in phases for p in ("failed", "unknown", "teardown_completed")):
        error("adhoc_dispatch_authentication_failed", "lifecycle history is duplicate or out of order")
    predecessors = {
        ("inbox_presented", None): None,
        ("spawn_reserved", None): ("inbox_presented", None),
        ("provider_terminal", None): ("spawn_reserved", None),
        ("process_closed", "cleanup"): ("spawn_reserved", None),
        ("process_closed", "banking_intent"): ("process_closed", "cleanup"),
        ("process_closed", "result"): ("process_closed", "banking_intent"),
        ("process_closed", "index_settlement_intent"): ("process_closed", "result"),
        ("process_closed", "index_settled"): ("process_closed", "index_settlement_intent"),
        ("process_closed", "ack"): ("process_closed", "index_settled"),
        ("process_closed", "consume"): ("process_closed", "ack"),
        ("work_completed", None): ("process_closed", "consume"),
        ("result_published", None): ("work_completed", None),
        ("teardown_completed", None): ("result_published", None),
        ("failed", None): ("spawn_reserved", None),
        ("unknown", None): ("spawn_reserved", None),
    }
    predecessor = predecessors[key]
    if predecessor is not None and predecessor not in phases:
        error("adhoc_dispatch_authentication_failed", "lifecycle history is duplicate or out of order")
    if phase == "inbox_presented" and prior:
        error("adhoc_dispatch_authentication_failed", "lifecycle history is duplicate or out of order")
    if phase == "provider_terminal" and ("process_closed", "cleanup") in phases:
        error("adhoc_dispatch_authentication_failed", "lifecycle history is duplicate or out of order")
    dispatch = admission["dispatch"]
    if phase == "spawn_reserved":
        context = evidence["native_context"]
        if (any(context[field] != admission[field] for field in
                ("profile_id", "profile_digest", "dispatch_id", "dispatch_digest"))
            or any(context[field] != dispatch[field] for field in
                   ("node_id", "node_lease_id", "work_item_id", "operation", "permission"))):
            error("adhoc_dispatch_authentication_failed", "lifecycle evidence differs from its immutable admission")
    if phase == "provider_terminal":
        from .native_thread_protocol import validate_native_event
        context = phases[("spawn_reserved", None)]["native_context"]
        provider = evidence["event"]["provider_coordinate"]["provider"]
        adapter = {"codex_local": "codex", "claude_local": "claude", "pi_local": "pi"}[provider]
        try:
            validate_native_event(context, evidence["event"], adapter_kind=adapter,
                opened_coordinate=None if opened is None else opened["evidence"]["provider_coordinate"],
                terminal_seen=False)
        except ProtocolRefusal as exc:
            error(exc.code, "lifecycle terminal differs from its authenticated native identity")
    if phase == "process_closed" and evidence["stage"] in {"banking_intent", "result"}:
        terminal = phases.get(("provider_terminal", None), {}).get("event")
        cleanup = phases[("process_closed", "cleanup")]["cleanup"]
        batch = phases[("inbox_presented", None)]["message_ids"]
        result = evidence["result_candidate"] if evidence["stage"] == "banking_intent" else evidence["result"]
        if (terminal is None or terminal["outcome"] != "success" or cleanup["alive_after_cleanup"]
            or cleanup["adapter_pid"] is None or cleanup["isolation_backend"] is None
            or result["provider_coordinate"] != terminal["provider_coordinate"]
            or result["doc"] != dispatch["result_document"]):
            error("adhoc_dispatch_authentication_failed", "lifecycle result lacks authenticated terminal and physical cleanup proof")
        if set(batch) != {d["message_id"] for d in result["inbox_dispositions"]}:
            error("adhoc_result_disposition_invalid", "result dispositions must name the exact frozen batch")
        if evidence["stage"] == "banking_intent":
            reservation = phases[("spawn_reserved", None)]["workspace_pin"]
            paths = {result["doc"]} | {path for d in result["inbox_dispositions"] for path in d["result_paths"]}
            if (evidence["workspace_pin"] != reservation or {f["path"] for f in evidence["files"]} != paths
                or next(f["sha256"] for f in evidence["files"] if f["path"] == result["doc"]) != result["blob_sha256"]):
                error("adhoc_binding_changed", "banking intent differs from its exact reserved result and workspace")
            if "identity_binding" in evidence:
                binding = evidence["identity_binding"]
                selected = next(f for f in evidence["files"] if f["path"] == result["doc"])
                if (terminal["provider_coordinate"]["provider"] != "claude_local"
                    or binding["path"] != result["doc"] or binding["mode"] != selected["mode"]
                    or binding["output_sha256"] != result["blob_sha256"]
                    or binding["input_sha256"] == binding["output_sha256"]):
                    error("adhoc_binding_changed", "Claude identity intent differs from its authenticated terminal and result")
        else:
            candidate = phases[("process_closed", "banking_intent")]["result_candidate"]
            if {field: result[field] for field in candidate} != candidate:
                error("adhoc_binding_changed", "banked result differs from its immutable banking intent")
    if phase == "work_completed":
        if (evidence["result"] != phases[("process_closed", "result")]["result"]
            or evidence["ack_ids"] != phases[("process_closed", "ack")]["ack_ids"]
            or evidence["consumption_ids"] != phases[("process_closed", "consume")]["consumption_ids"]):
            error("adhoc_dispatch_authentication_failed", "lifecycle completion differs from its frozen result and receipts")
    if phase == "process_closed" and evidence["stage"] in {"index_settlement_intent", "index_settled"}:
        reservation = phases[("spawn_reserved", None)]["workspace_pin"]
        result = phases[("process_closed", "result")]["result"]
        if (evidence["head_sha"] != result["sha"]
            or evidence["remote_sha"] != result["sha"]
            or evidence["index_sha256_before"] != reservation["index_sha256"]
            or evidence["stage"] == "index_settled" and evidence != dict(
                phases[("process_closed", "index_settlement_intent")], stage="index_settled")):
            error("adhoc_binding_changed", "index settlement differs from its exact reservation and banked result")


class AdhocJournal:
    relative_path = JOURNAL_RELATIVE

    def __init__(self, root):
        if not isinstance(root, FloatiRoot):
            refuse("root_required", "ad-hoc journal requires a validated writable root")
        self.root = root
        self.profiles = AdhocProfiles(root)
        self.__owner = None
        self.__capability = None

    def records(self):
        rows = read_records(self.root, self.relative_path, allowed_kinds=JOURNAL_KINDS)
        self._project_history(rows)
        return rows

    @staticmethod
    def _project_history(rows):
        """Join testimony to immutable admission without changing its replay shape."""
        admissions, opened, keys, lifecycle = {}, {}, set(), {}
        def malformed(detail):
            raise IntegrityFailure("adhoc_dispatch_authentication_failed", detail)
        for row in rows:
            dispatch_id = row["dispatch_id"]
            if row["kind"] == "adhoc_dispatch_record":
                if dispatch_id in admissions or row["idempotency_key"] in keys:
                    malformed("ad-hoc admission history repeats a dispatch or key")
                admissions[dispatch_id] = row
                keys.add(row["idempotency_key"])
                continue
            admission = admissions.get(dispatch_id)
            if admission is None:
                malformed("opened testimony requires one prior admission and one exact identity")
            if row["admission_id"] != admission["id"] or any(
                row[field] != admission[field] for field in
                ("profile_id", "profile_digest", "dispatch_digest")):
                malformed("native testimony refers to another immutable admission")
            if row["kind"] == "adhoc_lifecycle_record":
                def invalid(code, detail):
                    raise IntegrityFailure(code, detail)
                prior = lifecycle.setdefault(dispatch_id, [])
                _validate_lifecycle_order(row, prior, admission, opened.get(dispatch_id), invalid)
                prior.append(row)
                continue
            if dispatch_id in opened:
                malformed("opened testimony requires one prior admission and one exact identity")
            binding = row["evidence"]["binding"]
            dispatch = admission["dispatch"]
            if any(binding[field] != dispatch[field] for field in
                   ("profile_id", "profile_digest", "node_id", "node_lease_id", "work_item_id")):
                malformed("opened native binding differs from its authenticated dispatch")
            opened[dispatch_id] = row
        return admissions, opened

    def _capability_for(self, owner):
        if type(owner) is not AdhocJournalController or getattr(owner, "journal", None) is not self:
            refuse("adhoc_dispatch_authentication_failed", "journal capability requires the exact reciprocal writer")
        if self.__owner is None:
            self.__owner = owner
            self.__capability = object()
        elif self.__owner is not owner:
            refuse("adhoc_dispatch_busy", "journal already has one exact writer owner")
        return self.__capability

    def _append(self, capability, dispatch_id, phase, evidence, *, now=None):
        if capability is not self.__capability or self.__owner is None:
            refuse("adhoc_dispatch_authentication_failed", "journal append requires private writer capability")
        from .adhoc_threads import AdhocThreadController, _ADHOC_THREAD_RECORD_CODE
        caller, producer = sys._getframe(1), sys._getframe(2)
        controller = producer.f_locals.get("self")
        if (caller.f_code is not _ADHOC_CONTROLLER_APPEND_CODE or caller.f_locals.get("self") is not self.__owner
            or producer.f_code is not _ADHOC_THREAD_RECORD_CODE or type(controller) is not AdhocThreadController
            or controller.writer is not self.__owner or controller.journal is not self or controller.root is not self.root):
            refuse("adhoc_dispatch_authentication_failed", "lifecycle append requires its exact reciprocal thread controller")
        evidence = validate_lifecycle_evidence(phase, evidence)
        current = current_time(now)
        with epoch_guard(self.root), car_acceptance_guard(self.root):
            def decide(rows):
                admissions, opened = self._project_history(rows)
                admission = admissions.get(dispatch_id)
                if admission is None:
                    refuse("adhoc_dispatch_authentication_failed", "lifecycle evidence requires one exact immutable admission")
                prior = _history(rows, dispatch_id)
                key = (phase, evidence.get("stage") if phase == "process_closed" else None)
                for old in prior:
                    if _lifecycle_key(old) == key:
                        if phase == "spawn_reserved":
                            refuse("adhoc_spawn_unknown", "a saved launch reservation cannot authorize another process")
                        if old["evidence"] != evidence:
                            refuse("adhoc_dispatch_authentication_failed", "lifecycle replay differs from its frozen evidence")
                        return deepcopy(old), None
                record = {"schema_version": 1, "kind": "adhoc_lifecycle_record",
                    "id": "adhoc-lifecycle-" + uuid7_hex(), "tenant_id": self.root.tenant_id,
                    "timestamp": timestamp(current), "admission_id": admission["id"],
                    **{field: admission[field] for field in
                       ("dispatch_id", "profile_id", "profile_digest", "dispatch_digest")},
                    "phase": phase, "evidence": evidence}
                _validate_lifecycle_order(record, prior, admission, opened.get(dispatch_id), refuse)
                self._authenticate_lifecycle(record, prior, admission, controller, rows)
                return deepcopy(record), record
            return self._transact_lifecycle(decide)

    def lifecycle(self, dispatch_id):
        self.show(dispatch_id)
        return deepcopy(_history(self.records(), dispatch_id))

    def _authenticate_lifecycle(self, record, prior, admission, controller, rows):
        """Join each phase to its native owner, never to a caller's receipt bag."""
        from .cursor import SparseCursor
        from .message_consumption import MessageConsumption
        from .workers import WorkerReceipts
        from .records import WAKE_HOLD_KINDS
        phase, evidence = record["phase"], record["evidence"]
        dispatch, phases = admission["dispatch"], _phases(prior)
        profiles = [p for p in self.profiles.records() if p["id"] == admission["profile_id"]]
        if len(profiles) != 1 or profiles[0]["digest"] != admission["profile_digest"]:
            refuse("adhoc_binding_changed", "lifecycle evidence differs from its immutable profile")
        profile = profiles[0]
        node, work = profile["node_id"], dispatch["work_item_id"]

        def exact(records, identity):
            matches = [r for r in records if r["id"] == identity]
            if len(matches) != 1:
                refuse("adhoc_dispatch_authentication_failed", "lifecycle reference lacks one exact native receipt")
            return matches[0]

        def workspace_proof():
            from .adhoc_workspace import BorrowedWorkspace
            owner = getattr(controller, "_workspace_owner", None)
            if (type(owner) is not BorrowedWorkspace or owner.root is not self.root
                or owner._pin is None or owner._pin["profile"] != profile):
                refuse("adhoc_binding_changed", "banking evidence requires its exact descriptor-owned workspace")
            owner._check_pinned_files()
            return owner, {"expected_head": owner._pin["head"],
                           "index_sha256": hashlib.sha256(owner._pin["index"]).hexdigest()}

        if phase == "inbox_presented":
            messages = {r["id"]: r for r in EventLog(self.root).event_records()
                        if r["kind"] == "message_envelope"}
            if evidence["session_id"] != admission["acting_session_id"]:
                refuse("adhoc_dispatch_authentication_failed", "frozen inbox belongs to another acting session")
            for identity in evidence["message_ids"]:
                message = messages.get(identity)
                if message is None or message["recipient"] != node or message.get("worker_session_id") is not None:
                    refuse("adhoc_dispatch_authentication_failed", "frozen inbox includes foreign or session-plane mail")
            request = sender_request(self.root, dispatch)
            if request is not None and set(evidence["message_ids"]) != {request["id"], admission["envelope_id"]}:
                refuse("adhoc_sender_binding_changed", "served startup must contain only its exact peer request and coordinator dispatch")
            deliveries = read_records_snapshot(self.root, SparseCursor(self.root)._delivery_relative_path_for(node),
                                               allowed_kinds=WAKE_HOLD_KINDS)
            delivered = []
            for identity in evidence["presentation_ids"]:
                delivery = exact(deliveries, identity)
                if delivery["kind"] not in {"delivery_receipt", "delivered_unpresented"} or delivery["recipient"] != node:
                    refuse("adhoc_dispatch_authentication_failed", "frozen inbox lacks its exact native presentation")
                delivered.extend(delivery["item_ids"])
            if set(delivered) != set(evidence["message_ids"]):
                refuse("adhoc_dispatch_authentication_failed", "frozen inbox lacks its exact native presentation")
        elif phase == "spawn_reserved":
            context = evidence["native_context"]
            _workspace, pin = workspace_proof()
            if evidence["workspace_pin"] != pin:
                refuse("adhoc_binding_changed", "reservation differs from its original descriptor-owned branch and index")
            from .adhoc_records import validate_native_permission
            validate_native_permission(context["permission"], profile["adapter"])
            if (context["workspace_binding"] != {"profile_id": profile["id"],
                    "lane_opening_id": profile["lane_opening_id"], "node_lease_id": profile["node_lease_id"],
                    "workspace": profile["workspace"]}
                or context["provider_state_binding"] != {"path": str(self.root.tenant_home / profile["provider_state"]),
                    **profile["filesystem_bindings"]["provider_state"]}):
                refuse("adhoc_binding_changed", "lifecycle reservation differs from its exact borrowed bindings")
            coordinate = context["provider_coordinate"]
            previous = [r for r in rows if r["kind"] == "adhoc_dispatch_record"
                        and r["profile_id"] == profile["id"] and r["dispatch_id"] != admission["dispatch_id"]]
            if context["operation"] == "resume":
                if not previous:
                    refuse("native_session_unmaterialized", "resume requires a released durable native coordinate")
                history = _history(rows, previous[-1]["dispatch_id"])
                terminal = _phases(history).get(("provider_terminal", None), {}).get("event")
                if not _released(history) or terminal is None or terminal["provider_coordinate"] != coordinate:
                    refuse("native_session_mismatch", "resume differs from its released durable native coordinate")
            elif coordinate is not None or any(
                    not _released(_history(rows, row["dispatch_id"])) for row in previous):
                refuse("native_session_mismatch", "fresh start requires released predecessors and no provider coordinate")
        elif phase == "provider_terminal":
            provider = {"codex": "codex_local", "claude": "claude_local", "pi": "pi_local"}[profile["adapter"]]
            if evidence["event"]["provider_coordinate"]["provider"] != provider:
                refuse("native_session_mismatch", "native terminal belongs to another profile adapter")
            owner = getattr(controller, "_execution_owner", None)
            from .workers import WorkerRunner
            if (type(owner) is not WorkerRunner or owner.root is not self.root
                    or evidence["event"] not in owner.last_native_events):
                refuse("adhoc_dispatch_authentication_failed", "native terminal lacks its original process owner's testimony")
        elif phase == "process_closed":
            stage = evidence["stage"]
            if stage in {"index_settlement_intent", "index_settled"}:
                from .adhoc_workspace import BorrowedWorkspace
                owner = getattr(controller, "_workspace_owner", None)
                if (type(owner) is not BorrowedWorkspace or owner.root is not self.root
                    or owner._pin is None or owner._pin["profile"] != profile
                    or evidence["remote_ref"] != "refs/heads/" + profile["branch"]
                    or owner._index_settlement_attestation(admission["dispatch_id"], stage) != evidence):
                    refuse("adhoc_binding_changed", "index settlement lacks its exact native workspace owner")
            elif stage == "cleanup":
                receipt = exact(WorkerReceipts(self.root).records(), evidence["worker_receipt_id"])
                if (receipt["node_id"] != node or receipt["work_item_id"] != work
                    or receipt["adapter"] != profile["adapter"] or receipt["transition"] not in {"bind_artifact", "degrade"}
                    or receipt["artifact_bindings"] != evidence["artifacts"]):
                    refuse("adhoc_dispatch_authentication_failed", "physical cleanup lacks its exact native worker receipt")
                owner = getattr(controller, "_execution_owner", None)
                from .workers import WorkerRunner
                if (type(owner) is not WorkerRunner or owner.root is not self.root
                        or owner.last_process_audit != evidence["cleanup"]):
                    refuse("adhoc_dispatch_authentication_failed", "physical cleanup differs from its original process owner")
                if "transport" in evidence and getattr(owner, "last_native_transport", None) != evidence["transport"]:
                    refuse("adhoc_dispatch_authentication_failed", "transport evidence differs from its original process owner")
            elif stage == "banking_intent":
                workspace, pin = workspace_proof()
                if evidence["workspace_pin"] != pin:
                    refuse("adhoc_binding_changed", "banking intent differs from its original descriptor-owned branch and index")
                for item in evidence["files"]:
                    mode, data = workspace._result(item["path"])
                    identity = evidence.get("identity_binding")
                    if identity is not None and item["path"] == identity["path"]:
                        from .adhoc_result_identity import prepare_claude_result_identity
                        output, observed = prepare_claude_result_identity(workspace, item["path"], data,
                            phases[("provider_terminal", None)]["event"]["provider_coordinate"])
                        candidate = evidence["result_candidate"]
                        expected = canonical_json({"schema_version": 1, "dispatch_digest": admission["dispatch_digest"],
                            **{field: candidate[field] for field in ("provider_coordinate", "summary", "inbox_dispositions")}}) + b"\n"
                        if observed != identity or output != expected or hashlib.sha256(output).hexdigest() != item["sha256"]:
                            refuse("adhoc_binding_changed", "Claude identity intent differs from its exact descriptor-owned draft")
                    elif mode != item["mode"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                        refuse("adhoc_binding_changed", "banking intent differs from its exact descriptor-owned result files")
                workspace._check_pinned_files()
            elif stage == "result":
                result = evidence["result"]
                intent = phases[("process_closed", "banking_intent")]
                workspace, pin = workspace_proof()
                if intent["workspace_pin"] != pin:
                    refuse("adhoc_binding_changed", "banked result differs from its original descriptor-owned branch and index")
                _lanes, repositories = self.profiles.lanes._declarations()
                repository = repositories[profile["repository"]]["path"]
                sha, doc = result["sha"], result["doc"]
                parent = _git(repository, "rev-list", "--parents", "-n", "1", sha).decode("ascii").strip()
                if parent != sha + " " + pin["expected_head"]:
                    refuse("adhoc_binding_changed", "banked result must have the exact reserved parent commit")
                changed = _git(repository, "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-z",
                               "-r", pin["expected_head"], sha, limit=1_048_576)
                expected_paths = {item["path"] for item in intent["files"]}
                try:
                    changed_paths = {path.decode("utf-8", "strict") for path in changed.split(b"\0") if path}
                except UnicodeError as exc:
                    raise ProtocolRefusal("adhoc_binding_changed", "banked result contains a foreign project path") from exc
                if not changed_paths.issubset(expected_paths):
                    refuse("adhoc_binding_changed", "banked result contains paths outside its immutable banking intent")
                for item in intent["files"]:
                    tree_entry = _git(repository, "ls-tree", "-z", sha, "--", item["path"])
                    if (not tree_entry.startswith((item["mode"] + " blob ").encode("ascii"))
                        or tree_entry.count(b"\0") != 1 or b"\t" not in tree_entry
                        or tree_entry.split(b"\t", 1)[1] != item["path"].encode("utf-8") + b"\0"):
                        refuse("adhoc_binding_changed", "banked result file mode differs from its immutable banking intent")
                    blob_name = sha + ":" + item["path"]
                    try:
                        blob_size = int(_git(repository, "cat-file", "-s", blob_name).strip())
                    except ValueError as exc:
                        raise ProtocolRefusal("adhoc_binding_changed", "banked result file size is malformed") from exc
                    if not 0 <= blob_size <= 16 * 1024 * 1024:
                        refuse("adhoc_binding_changed", "banked result file exceeds its ordinary file bound")
                    blob = _git(repository, "cat-file", "blob", blob_name, limit=16 * 1024 * 1024)
                    if len(blob) != blob_size or hashlib.sha256(blob).hexdigest() != item["sha256"]:
                        refuse("adhoc_binding_changed", "banked result file differs from its immutable banking intent")
                if _git(repository, "rev-parse", "refs/heads/" + profile["branch"]).decode("ascii").strip() != sha:
                    refuse("adhoc_binding_changed", "owned branch differs from its exact banked result commit")
                tree = _git(repository, "ls-tree", sha, "--", doc)
                if not tree.startswith((b"100644 blob ", b"100755 blob ")) or tree.count(b"\n") != 1:
                    refuse("adhoc_dispatch_authentication_failed", "banked result must be one exact ordinary Git blob")
                size = _git(repository, "cat-file", "-s", sha + ":" + doc)
                try:
                    if not 0 < int(size.strip()) <= 1_048_576:
                        raise ValueError()
                    raw = _git(repository, "cat-file", "blob", sha + ":" + doc, limit=1_048_576)
                    parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                except (ValueError, UnicodeError) as exc:
                    raise ProtocolRefusal("adhoc_dispatch_authentication_failed", "banked result must be bounded strict unique-key JSON") from exc
                expected = {"schema_version": 1, "dispatch_digest": admission["dispatch_digest"],
                    **{field: result[field] for field in ("provider_coordinate", "summary", "inbox_dispositions")}}
                if len(raw) != int(size.strip()) or hashlib.sha256(raw).hexdigest() != result["blob_sha256"] or parsed != expected or type(parsed.get("schema_version")) is not int:
                    refuse("adhoc_dispatch_authentication_failed", "banked result differs from its exact immutable blob")
                for disposition in result["inbox_dispositions"]:
                    for path in disposition["result_paths"]:
                        named = _git(repository, "ls-tree", sha, "--", path)
                        if not named.startswith((b"100644 blob ", b"100755 blob ")) or named.count(b"\n") != 1:
                            refuse("adhoc_result_disposition_invalid", "disposition path lacks its exact ordinary result blob")
            elif stage == "ack":
                result = phases[("process_closed", "result")]["result"]
                _lanes, repositories = self.profiles.lanes._declarations()
                repository = repositories[profile["repository"]]["path"]
                if not _git(repository, "branch", "-r", "--contains", result["sha"]).strip():
                    refuse("adhoc_result_unbanked", "result requires durable remote-tracking containment before acknowledgment")
                acted = {d["message_id"] for d in result["inbox_dispositions"] if d["disposition"] == "acted"}
                cursor = SparseCursor(self.root)
                acks = read_records_snapshot(self.root, cursor._relative_path_for(node), allowed_kinds={"ack_receipt"})
                selected = []
                for identity in evidence["ack_ids"]:
                    ack = exact(acks, identity)
                    if (ack["recipient"] != node or ack.get("acting_session_id") != result["provider_coordinate"]["session_id"]
                        or ack.get("node_lease_id") != profile["node_lease_id"]):
                        refuse("adhoc_dispatch_authentication_failed", "result ACK differs from its authenticated provider and lease")
                    selected.extend(ack["item_ids"])
                if set(selected) != acted or len(selected) != len(set(selected)):
                    refuse("adhoc_result_disposition_invalid", "result ACK must name only the exact saved acted subset")
            else:
                result = phases[("process_closed", "result")]["result"]
                acted = {d["message_id"] for d in result["inbox_dispositions"] if d["disposition"] == "acted"}
                consumptions = MessageConsumption(self.root).records(node)
                selected = []
                for identity in evidence["consumption_ids"]:
                    consumption = exact(consumptions, identity)
                    if (consumption["recipient"] != node or consumption["worker_session_id"] is not None
                        or consumption["acting_session_id"] != result["provider_coordinate"]["session_id"]
                        or consumption["work_item_id"] != work
                        or consumption["ack_receipt_id"] not in phases[("process_closed", "ack")]["ack_ids"]):
                        refuse("adhoc_dispatch_authentication_failed", "result consumption differs from its exact saved ACK and work")
                    selected.append(consumption["message_id"])
                if set(selected) != acted or len(selected) != len(set(selected)):
                    refuse("adhoc_result_disposition_invalid", "result consumption must name only the exact saved acted subset")
        elif phase == "work_completed":
            log = WorkLog(self.root)
            transition = exact(read_records_snapshot(self.root, log.relative_path,
                allowed_kinds={"work_item", "work_transition"}), evidence["transition_id"])
            result = evidence["result"]
            paths = [result["doc"]]
            for disposition in result["inbox_dispositions"]:
                for path in disposition["result_paths"]:
                    if path not in paths:
                        paths.append(path)
            paths = [result["doc"], *sorted(set(paths) - {result["doc"]})]
            expected_artifacts = [{"repo": profile["repository"], "sha": result["sha"], "doc": path} for path in paths]
            if (transition["kind"] != "work_transition" or transition["work_item_id"] != work
                or transition["action"] != "complete" or transition["actor"] != node
                or transition["authority_subject"] != dispatch["authority_subject"]
                or transition["authority_epoch"] != dispatch["authority_epoch"]
                or transition["artifact_bindings"] != expected_artifacts):
                refuse("adhoc_dispatch_authentication_failed", "completion lacks its exact native work transition")
        elif phase == "result_published":
            envelope = exact(EventLog(self.root).event_records(), evidence["envelope_id"])
            result = phases[("process_closed", "result")]["result"]
            if (envelope["kind"] != "message_envelope" or envelope["sender"] != node
                or envelope["recipient"] != profile["result_recipient"]
                or envelope.get("reply_to") != dispatch.get("request_id", admission["envelope_id"])
                or envelope["repo"] != profile["repository"] or envelope["sha"] != result["sha"]
                or envelope["doc"] != result["doc"] or envelope.get("worker_session_id") is not None):
                refuse("adhoc_dispatch_authentication_failed", "publication lacks its exact native result envelope")
        elif phase == "teardown_completed":
            resources = evidence["resource_dispositions"]
            lanes = self.profiles.lanes
            native = lanes._records(node)
            lanes._open_records(node)
            closing = exact(native, resources["lane_closing_id"])
            opening = exact(native, resources["lane_opening_id"])
            if (resources["lane_opening_id"] != profile["lane_opening_id"]
                or resources["node_lease_id"] != profile["node_lease_id"]
                or closing["state"] != "closed" or closing["opened_record_id"] != profile["lane_opening_id"]
                or closing["node_id"] != node or closing["closed_by"] != node or closing["why"] is not None
                or opening["state"] != "open" or opening["node_id"] != node
                or opening["repo"] != profile["repository"] or opening["branch"] != profile["branch"]
                or opening["base_sha"] != profile["base_sha"]
                or any(closing[field] != opening[field] for field in
                       ("node_id", "repo", "branch", "base_sha", "path", "row", "opened_at"))):
                refuse("adhoc_teardown_blocked", "teardown lacks the exact native lane closing proof")
            provenance = evidence.get("provenance")
            if provenance is None:
                if Path(profile["workspace"]).exists() or Path(profile["workspace"]).is_symlink():
                    refuse("adhoc_teardown_blocked", "teardown lacks the exact native lane closing proof")
            else:
                from .adhoc_threads import _inspect_closed_successor
                _inspect_closed_successor(lanes, profile, opening)
                cleanup = phases[("process_closed", "cleanup")]
                result = phases[("process_closed", "result")]["result"]
                completed = phases[("work_completed", None)]
                published = phases[("result_published", None)]
                ack_ids = phases[("process_closed", "ack")]["ack_ids"]
                consumption_ids = phases[("process_closed", "consume")]["consumption_ids"]
                acted = [item["message_id"] for item in result["inbox_dispositions"]
                         if item["disposition"] == "acted"]
                leases = [row for row in self.profiles.registry.known_records()
                          if row["kind"] == "node_lease" and row["node_id"] == node]
                original_lease = exact(leases, profile["node_lease_id"])
                expected = {"kind": "native_closing_recovery", "lane_opening_id": opening["id"],
                    "lane_closing_id": closing["id"], "observed_node_lease_id": leases[-1]["id"],
                    "worker_receipt_id": cleanup["worker_receipt_id"],
                    "work_transition_id": completed["transition_id"],
                    "result_envelope_id": published["envelope_id"], "acted_message_ids": acted,
                    "ack_receipt_ids": ack_ids, "consumption_receipt_ids": consumption_ids}
                if (provenance != expected or original_lease["workspace"] != profile["node_home"]
                    or original_lease["expires_at"] != profile["lease_expires_at"]
                    or cleanup["cleanup"]["alive_after_cleanup"]
                    or cleanup["cleanup"]["adapter_pid"] is None
                    or cleanup["cleanup"]["isolation_backend"] is None):
                    refuse("adhoc_teardown_blocked", "historical teardown differs from its exact saved native provenance")
                receipt = exact(WorkerReceipts(self.root).records(), provenance["worker_receipt_id"])
                if (receipt["node_id"] != node or receipt["work_item_id"] != work
                    or receipt["adapter"] != profile["adapter"] or receipt["transition"] != "bind_artifact"):
                    refuse("adhoc_teardown_blocked", "historical teardown cleanup belongs to another native worker")
                cursor = SparseCursor(self.root)
                acks = read_records_snapshot(self.root, cursor._relative_path_for(node), allowed_kinds={"ack_receipt"})
                ack_for_message = {}
                selected = []
                for identity in ack_ids:
                    receipt = exact(acks, identity)
                    if (receipt["recipient"] != node or receipt.get("acting_session_id") != result["provider_coordinate"]["session_id"]
                        or receipt.get("node_lease_id") != profile["node_lease_id"]):
                        refuse("adhoc_teardown_blocked", "historical teardown ACK differs from its exact provider and lease")
                    selected.extend(receipt["item_ids"])
                    ack_for_message.update({message: identity for message in receipt["item_ids"]})
                if set(selected) != set(acted) or len(selected) != len(set(selected)):
                    refuse("adhoc_teardown_blocked", "historical teardown ACK differs from its saved acted subset")
                selected = []
                consumptions = MessageConsumption(self.root).records(node)
                for identity in consumption_ids:
                    receipt = exact(consumptions, identity)
                    if (receipt["recipient"] != node or receipt["worker_session_id"] is not None
                        or receipt["acting_session_id"] != result["provider_coordinate"]["session_id"]
                        or receipt["work_item_id"] != work
                        or receipt["ack_receipt_id"] != ack_for_message.get(receipt["message_id"])):
                        refuse("adhoc_teardown_blocked", "historical teardown consumption differs from its exact native ACK")
                    selected.append(receipt["message_id"])
                if set(selected) != set(acted) or len(selected) != len(set(selected)):
                    refuse("adhoc_teardown_blocked", "historical teardown consumption differs from its saved acted subset")
        elif phase in {"failed", "unknown"}:
            owner = getattr(controller, "_execution_owner", None)
            from .workers import WorkerRunner
            saved_cleanup = phases.get(("process_closed", "cleanup"), {}).get("cleanup")
            if evidence["cleanup"] != saved_cleanup and (
                    type(owner) is not WorkerRunner or owner.root is not self.root
                    or evidence["cleanup"] != owner.last_process_audit):
                refuse("adhoc_dispatch_authentication_failed", "failure cleanup differs from its original process owner")

    def show(self, dispatch_id):
        rows = [row for row in self.records() if row["kind"] == "adhoc_dispatch_record"
                and row["dispatch_id"] == dispatch_id]
        if len(rows) != 1:
            refuse("adhoc_dispatch_authentication_failed", "dispatch does not name one exact admitted history")
        return deepcopy(rows[0])

    def _authenticate(self, profile, binding, envelope_id, dispatch, actor):
        from .native_dispatch_launcher import KIND as launcher_kind

        governance = self.profiles.registry.governance()
        events = EventLog(self.root).event_records()
        envelopes = [row for row in events
                     if row["kind"] == "message_envelope" and row["id"] == envelope_id]
        if governance is None or "dispatch_bounded_work" not in governance.coordinator_authority or len(envelopes) != 1:
            refuse("adhoc_dispatch_authentication_failed", "dispatch envelope is not exact live authenticated metadata")
        envelope = envelopes[0]
        if envelope["sender"] != governance.coordinator or envelope["recipient"] != profile["node_id"] or envelope["repo"] != profile["repository"] or envelope.get("worker_session_id") is not None:
            refuse("adhoc_dispatch_authentication_failed", "dispatch is not the coordinator's exact node-mailbox repository envelope")
        repository = binding["repository_path"]
        if not _git(repository, "branch", "-r", "--contains", envelope["sha"]).strip():
            refuse("adhoc_dispatch_authentication_failed", "dispatch SHA has no banked remote-tracking reference")
        parsed, raw_digest = _blob(repository, envelope["sha"], envelope["doc"])
        if canonical_json(parsed) != canonical_json(dispatch):
            refuse("adhoc_dispatch_authentication_failed", "supplied dispatch differs from the exact authenticated Git object")
        checks = {"profile_id": profile["id"], "profile_digest": profile["digest"],
            "node_id": profile["node_id"], "node_lease_id": profile["node_lease_id"],
            "lane_opening_id": profile["lane_opening_id"], "result_recipient": profile["result_recipient"]}
        if actor != profile["node_id"] or any(dispatch[key] != value for key, value in checks.items()):
            refuse("adhoc_dispatch_authentication_failed", "dispatch and exact profile coordinates differ")
        from .adhoc_records import validate_native_permission
        validate_native_permission(dispatch["permission"], profile["adapter"])
        request = sender_request(self.root, dispatch)
        if request is not None and request["repo"] != profile["repository"]:
            refuse("adhoc_sender_binding_changed", "sender request belongs to another repository")
        evidence = {"repository": profile["repository"], "sha": envelope["sha"],
                    "doc": envelope["doc"], "blob_sha256": raw_digest}
        if profile["adapter"] == "codex" and dispatch["operation"] == "start":
            launcher = next((row for row in events if row["kind"] == launcher_kind
                             and row["message_id"] == envelope_id), None)
            if launcher is not None:
                evidence["native_launcher"] = {key: value for key, value in launcher.items()
                                               if key not in {"seq", "prev"}}
        return evidence

    def _require_work(self, profile, binding, dispatch, actor, current, *, allow_claimed=False):
        import shlex

        replacement = shlex.join(["floati", "work", "add", "--root", str(self.root.path),
            "--title", "replacement for " + dispatch["work_item_id"], "--owner", actor,
            "--adhoc-profile", profile["id"]])
        try:
            item = WorkLog(self.root).show(dispatch["work_item_id"])[0]
            expected = {"row": binding["row"], "repository": profile["repository"], "branch": profile["branch"]}
            borrowed = {"profile_id": profile["id"], "lane_opening_id": profile["lane_opening_id"],
                "node_lease_id": profile["node_lease_id"], "workspace": profile["workspace"]}
            matches_row = (item.get("adhoc_binding") == borrowed and item.get("workspace") == profile["workspace"]
                if item.get("adhoc_binding") is not None else item.get("car_binding") == expected)
            state_matches = item["state"] == "open" or (
                allow_claimed and item["state"] == "claimed" and item.get("holder") == actor
                and item.get("authority_subject") == dispatch["authority_subject"]
                and item.get("authority_epoch") == dispatch["authority_epoch"])
            if item["owner"] != actor or not state_matches or not matches_row:
                raise ValueError("exact open work row differs")
        except (ProtocolRefusal, ValueError, KeyError) as exc:
            raise ProtocolRefusal("adhoc_dispatch_authentication_failed",
                "work ownership failed: dispatch requires an exact open work item owned by "
                f"{actor} and bound to profile {profile['id']}; create replacement work, then have "
                "the coordinator bank and send a new dispatch using its returned work ID; "
                "the original work and dispatch are immutable", replacement) from exc

        try:
            grant = AuthorityGrantStore(self.root).exact_tail(dispatch["authority_subject"])
        except ProtocolRefusal as exc:
            if exc.code != "authority_missing":
                raise
            grant = None
        if (grant is not None and grant["state"] == "active" and grant["holder"] == actor
            and grant["epoch"] == dispatch["authority_epoch"]
            and current < datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))):
            return

        # A stale dispatch may use an already-current grant, but an expired or
        # revoked coordinate can only be replaced by the next authority epoch.
        epoch = 1 if grant is None else grant["epoch"] + 1
        if (grant is not None and grant["state"] == "active" and grant["holder"] == actor
            and current < datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))):
            epoch = grant["epoch"]
        from .admin_registry import RegistryAdminBackend
        try:
            architect = RegistryAdminBackend(self.root).current_architect()["node_id"]
        except ProtocolRefusal as exc:
            raise ProtocolRefusal("adhoc_dispatch_authentication_failed",
                "authority failed: the exact dispatch authority is inactive or differs; "
                "an active architect must grant the replacement coordinate; " + exc.detail,
                exc.remedy) from exc
        remedy = shlex.join(["floati", "grant", "--root", str(self.root.path),
            "--as", architect, "--holder", actor, "--subject", dispatch["authority_subject"],
            "--epoch", str(epoch)])
        detail = (f"authority failed: work ownership is valid, but subject {dispatch['authority_subject']} "
            f"requires active holder {actor} at dispatch epoch {dispatch['authority_epoch']}; "
            f"have architect {architect} run the remedy for epoch {epoch}")
        if epoch != dispatch["authority_epoch"]:
            detail += "; then have the coordinator bank and send a new dispatch using that epoch; do not retry the old envelope"
        raise ProtocolRefusal("adhoc_dispatch_authentication_failed", detail, remedy)

    def admit(self, profile_id, envelope_id, dispatch, *, actor, acting_session_id, idempotency_key, now=None):
        current = current_time(now)
        validate_identifier(idempotency_key, "idempotency_key")
        validate_dispatch(dispatch)
        old = [row for row in self.records() if row["kind"] == "adhoc_dispatch_record"
               and row["idempotency_key"] == idempotency_key]
        replay = bool(old)
        with epoch_guard(self.root), car_acceptance_guard(self.root):
            with self.profiles.validated(profile_id, now=current, allow_expired=replay, require_clean=not replay) as (profile, binding):
                evidence = self._authenticate(profile, binding, envelope_id, dispatch, actor)
                immutable = {"profile_id": profile_id, "profile_digest": profile["digest"],
                    "envelope_id": envelope_id, "dispatch_digest": dispatch_digest(dispatch),
                    "dispatch": deepcopy(dispatch), "actor": actor, "acting_session_id": acting_session_id,
                    "idempotency_key": idempotency_key, "evidence": evidence}

                def decide(rows):
                    admissions, _opened = self._project_history(rows)
                    for row in admissions.values():
                        if row["idempotency_key"] == idempotency_key:
                            if any(row[field] != value for field, value in immutable.items()):
                                refuse("adhoc_dispatch_key_conflict", "admission key was reused with a different immutable dispatch")
                            return deepcopy(row), None
                    profiles = {row["id"]: row for row in self.profiles.records()}
                    previous_profile = []
                    request = sender_request(self.root, dispatch)
                    if request is not None and request["id"] not in EventLog(self.root).unacked_ids(actor):
                        refuse("adhoc_request_conflict", "a fresh served dispatch requires its pending peer request")
                    for row in admissions.values():
                        owner = profiles.get(row["profile_id"])
                        if owner is None:
                            refuse("adhoc_dispatch_busy", "unresolved admission retains its exclusive owner")
                        history = _history(rows, row["dispatch_id"])
                        if row["actor"] == actor:
                            # These immutable fields were authenticated at the
                            # original admission. Later request retraction must
                            # not erase conversation ownership or prevent an
                            # unrelated sender from using its own profile.
                            prior_request_id = row["dispatch"].get("request_id")
                            same_profile = row["profile_id"] == profile_id
                            if same_profile and (request is None) != (prior_request_id is None):
                                refuse("adhoc_sender_binding_changed", "legacy and sender-bound conversations cannot share a profile")
                            if request is not None and prior_request_id is not None:
                                same_sender = request["sender"] == row["dispatch"]["result_recipient"]
                                if request["id"] == prior_request_id:
                                    refuse("adhoc_request_conflict", "this peer request already has its durable dispatch; replay its exact key")
                                if same_profile != same_sender:
                                    refuse("adhoc_sender_binding_changed", "each sender must retain its own exact profile and provider history")
                        if row["profile_id"] == profile_id:
                            previous_profile.append(row)
                        if (row["profile_id"] == profile_id or _physical_row(owner) == _physical_row(profile)) and not _released(history):
                            refuse("adhoc_dispatch_busy", "another dispatch owns this profile/provider/physical row")
                    if dispatch["operation"] == "resume":
                        if not previous_profile:
                            refuse("native_session_unmaterialized", "resume requires a released durable native coordinate")
                        terminal = _phases(_history(rows, previous_profile[-1]["dispatch_id"])).get(("provider_terminal", None), {}).get("event")
                        if terminal is None:
                            refuse("native_session_unmaterialized", "resume requires a released durable native coordinate")
                    self._require_work(profile, binding, dispatch, actor, current)
                    record = {"schema_version": 2 if "native_launcher" in evidence else 1,
                        "id": "adhoc-journal-" + uuid7_hex(),
                        "tenant_id": self.root.tenant_id, "timestamp": timestamp(current),
                        "kind": "adhoc_dispatch_record", "dispatch_id": "adhoc-dispatch-" + uuid7_hex(),
                        **immutable, "phase": "admitted"}
                    validate_record(record, self.root.tenant_id, frozenset(JOURNAL_KINDS), integrity=False)
                    return deepcopy(record), record
                return self._transact_admission(decide)

    def _transact_admission(self, decide):
        caller = sys._getframe(1)
        if caller.f_code is not _ADHOC_JOURNAL_ADMIT_CODE or caller.f_locals.get("self") is not self:
            refuse("adhoc_dispatch_authentication_failed", "admission transaction requires its authenticated route")
        return _transact_adhoc_records(self.root, decide)

    def _transact_provider_opened(self, decide):
        from .native_thread_protocol import NativeThreadProtocol, _NATIVE_PROTOCOL_ACCEPT_CODE
        caller = sys._getframe(1)
        owner = caller.f_locals.get("self")
        if (caller.f_code is not _NATIVE_PROTOCOL_ACCEPT_CODE
            or type(owner) is not NativeThreadProtocol or owner.journal is not self):
            refuse("adhoc_dispatch_authentication_failed", "opened transaction requires its exact authenticated native owner")
        return _transact_adhoc_records(self.root, decide)

    def _transact_lifecycle(self, decide):
        caller = sys._getframe(1)
        if caller.f_code is not _ADHOC_JOURNAL_APPEND_CODE or caller.f_locals.get("self") is not self:
            refuse("adhoc_dispatch_authentication_failed", "lifecycle transaction requires its exact authenticated producer")
        return _transact_adhoc_records(self.root, decide)


class AdhocJournalController:
    """Narrow journal writer composed by the later native thread controller."""
    def __init__(self, journal):
        if type(journal) is not AdhocJournal:
            refuse("adhoc_dispatch_authentication_failed", "writer requires an exact AdhocJournal")
        self.journal = journal
        self.__capability = journal._capability_for(self)

    def admit(self, *args, **kwargs):
        return self.journal.admit(*args, **kwargs)

    def append(self, dispatch_id, phase, evidence, *, now=None):
        return self.journal._append(self.__capability, dispatch_id, phase, evidence, now=now)


_ADHOC_JOURNAL_ADMISSION_CODE = AdhocJournal._transact_admission.__code__
_ADHOC_JOURNAL_ADMIT_CODE = AdhocJournal.admit.__code__
_ADHOC_JOURNAL_OPENED_CODE = AdhocJournal._transact_provider_opened.__code__
_ADHOC_JOURNAL_APPEND_CODE = AdhocJournal._append.__code__
_ADHOC_JOURNAL_LIFECYCLE_CODE = AdhocJournal._transact_lifecycle.__code__
_ADHOC_CONTROLLER_APPEND_CODE = AdhocJournalController.append.__code__


def active_dispatch_references(root):
    """Closed metadata seam for the last-hunk epoch dependency composition.

    Every current row is unfinished. No future phase is treated as release
    proof, and no provider slot file or history name is enumerated here.
    """
    journal = AdhocJournal(root)
    references = []
    all_rows = journal.records()
    rows = [row for row in all_rows if row["kind"] == "adhoc_dispatch_record"
            and not _released(_history(all_rows, row["dispatch_id"]))]
    if not rows:
        return references
    governance = journal.profiles.registry.governance()
    for row in rows:
        if governance is None or "dispatch_bounded_work" not in governance.coordinator_authority:
            refuse("adhoc_dispatch_authentication_failed", "active admission has no declared dispatch coordinator")
        with journal.profiles.validated(row["profile_id"], allow_expired=True) as (profile, _binding):
            if row["profile_digest"] != profile["digest"] or any(
                row["dispatch"][field] != profile[expected] for field, expected in (
                    ("node_id", "node_id"), ("node_lease_id", "node_lease_id"),
                    ("lane_opening_id", "lane_opening_id"), ("result_recipient", "result_recipient"))) or row["evidence"]["repository"] != profile["repository"]:
                refuse("adhoc_binding_changed", "active admission profile coordinates differ")
            references.append({"dispatch_id": row["dispatch_id"], "profile_id": row["profile_id"],
                "profile_digest": row["profile_digest"], "envelope_id": row["envelope_id"],
                "node_id": profile["node_id"], "coordinator": governance.coordinator,
                "repository": profile["repository"], "sha": row["evidence"]["sha"], "doc": row["evidence"]["doc"],
                "frozen_envelope_ids": list(_phases(_history(all_rows, row["dispatch_id"])).get(
                    ("inbox_presented", None), {}).get("message_ids", []))})
    return references
