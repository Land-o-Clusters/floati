"""Closed version-one ad-hoc metadata; provider history is never receipt truth."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

from .errors import ProtocolRefusal

ADHOC_DIRECTORY = Path("adhoc")
PROVIDER_STATE_DIRECTORY = Path("provider-state")
PROFILE_RELATIVE = ADHOC_DIRECTORY / "profiles.jsonl"
JOURNAL_RELATIVE = ADHOC_DIRECTORY / "journal.jsonl"
PROFILE_DOMAIN = b"floati-adhoc-profile-v1\0"
UUID = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
COMMON = {"schema_version", "id", "tenant_id", "timestamp", "kind"}
PROFILE_FIELDS = COMMON | {"node_id", "node_lease_id", "adapter", "lane_opening_id",
    "repository", "result_recipient", "node_home", "seat_digest", "workspace",
    "git_common_dir", "git_admin_dir", "branch", "base_sha", "lease_expires_at",
    "provider_state", "filesystem_bindings", "idempotency_key", "digest"}
JOURNAL_FIELDS = COMMON | {"dispatch_id", "profile_id", "profile_digest", "envelope_id",
    "dispatch_digest", "dispatch", "actor", "acting_session_id", "idempotency_key", "phase", "evidence"}
NATIVE_BINDING_FIELDS = {"profile_id", "profile_digest", "dispatch_id", "dispatch_digest",
    "node_id", "node_lease_id", "work_item_id", "effect_binding"}
OPENED_FIELDS = COMMON | {"admission_id", "dispatch_id", "profile_id", "profile_digest",
    "dispatch_digest", "phase", "evidence"}
LIFECYCLE_PHASES = frozenset({"inbox_presented", "spawn_reserved", "provider_terminal",
    "process_closed", "work_completed", "result_published", "teardown_completed", "failed", "unknown"})
ADHOC_SPECS = {
    "adhoc_profile_record": ("adhoc-profile-", frozenset(PROFILE_FIELDS)),
    "adhoc_dispatch_record": ("adhoc-journal-", frozenset(JOURNAL_FIELDS)),
    "adhoc_native_thread_opened_record": ("adhoc-native-opened-", frozenset(OPENED_FIELDS)),
    "adhoc_lifecycle_record": ("adhoc-lifecycle-", frozenset(OPENED_FIELDS)),
}
ADHOC_KINDS = frozenset(ADHOC_SPECS)
ADHOC_RECORD_VERSIONS = dict.fromkeys(ADHOC_KINDS, (1,))
ADHOC_RECORD_VERSIONS["adhoc_dispatch_record"] = (1, 2)
DISPATCH_FIELDS = {"schema_version", "profile_id", "profile_digest", "node_id",
    "node_lease_id", "lane_opening_id", "work_item_id", "authority_subject",
    "authority_epoch", "operation", "permission", "requested_duration", "result_document", "result_recipient"}
BINDING_FIELDS = {"root", "node_home", "seat_file", "workspace", "git_common_dir", "git_admin_dir", "provider_state"}
CODEX_PERMISSION = {"mode": "on_request_deny_unattended", "approval_policy": "on-request",
    "reviewer": "user", "sandbox": "workspace-write", "network": False}
CLAUDE_PERMISSION = {"mode": "dontAsk", "tools": ["Read", "Write", "Edit"]}


def validate_native_permission(value, adapter=None, error=None):
    """Accept only the reviewed, explicit native authority translations."""
    error = refuse if error is None else error
    kind = None
    if type(value) is dict:
        if value == CODEX_PERMISSION and type(value.get("network")) is bool:
            kind = "codex"
        elif value == CLAUDE_PERMISSION:
            kind = "claude"
        elif value == {"mode": "tools_disabled"}:
            kind = "pi"
        elif set(value) == {"mode", "tools"} and value["mode"] == "allowlisted_tools":
            tools = value["tools"]
            if (type(tools) is list and 0 < len(tools) <= 3
                and all(type(tool) is str and tool in {"read", "write", "edit"} for tool in tools)
                and len(tools) == len(set(tools))):
                kind = "pi"
    if kind is None or (adapter is not None and kind != adapter):
        error("native_permission_unsupported", "permission requires an explicit reviewed translation for this adapter")
    return deepcopy(value)


def refuse(code, detail):
    raise ProtocolRefusal(code, detail, remedy="preserve the exact ad-hoc profile and receipts; repair the named binding before retrying")


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def profile_digest(record):
    return hashlib.sha256(PROFILE_DOMAIN + canonical_json({key: value for key, value
        in record.items() if key != "digest"})).hexdigest()


def dispatch_digest(dispatch):
    return hashlib.sha256(canonical_json(dispatch) + b"\n").hexdigest()


def _keys(value, fields, error, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        error("adhoc_dispatch_authentication_failed", label + " must have exactly its closed fields")


def _identifier(value, error, label):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        error("adhoc_dispatch_authentication_failed", label + " must be one identifier")


def _reference(value, prefix, error):
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + UUID, value):
        error("adhoc_dispatch_authentication_failed", "invalid " + prefix + "reference")


def _digest(value, error):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        error("adhoc_dispatch_authentication_failed", "expected one SHA256 digest")


def _timestamp(value, error):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z", value):
            raise ValueError()
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        error("adhoc_dispatch_authentication_failed", "timestamp must be a valid UTC timestamp")


def _path(value, error, *, relative=False):
    if not isinstance(value, str) or not value or any(unicodedata.category(c).startswith("C") for c in value) or len(value.encode()) > 4096:
        error("adhoc_binding_changed", "path must be bounded visible text")
    path = Path(value)
    if path.is_absolute() == relative or any(part in {".", ".."} for part in value.split("/")) or str(path) != value:
        error("adhoc_binding_changed", "path must be an exact normalized coordinate")


def validate_dispatch(value, error=refuse):
    version = value.get("schema_version") if type(value) is dict else None
    if type(version) is not int or version not in {1, 2}:
        error("adhoc_dispatch_authentication_failed", "dispatch version must be integer one or two")
    _keys(value, DISPATCH_FIELDS | ({"request_id"} if version == 2 else set()), error, "dispatch")
    if version == 2:
        _reference(value["request_id"], "msg-", error)
    for field, prefix in (("profile_id", "adhoc-profile-"), ("node_lease_id", "lease-"),
                          ("lane_opening_id", "lane-workspace-"), ("work_item_id", "work-")):
        _reference(value[field], prefix, error)
    _digest(value["profile_digest"], error)
    for field in ("node_id", "authority_subject", "result_recipient"):
        _identifier(value[field], error, field)
    if type(value["authority_epoch"]) is not int or not 1 <= value["authority_epoch"] <= 2**63 - 1:
        error("adhoc_dispatch_authentication_failed", "authority epoch must be a positive integer")
    if not isinstance(value["operation"], str) or value["operation"] not in {"start", "resume"}:
        error("adhoc_dispatch_authentication_failed", "operation must be explicit start or resume")
    duration = value["requested_duration"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or (isinstance(duration, float) and not math.isfinite(duration)) or duration <= 0:
        error("adhoc_dispatch_authentication_failed", "requested duration must be finite and positive")
    validate_native_permission(value["permission"], error=error)
    _path(value["result_document"], error, relative=True)
    return deepcopy(value)


def _native_coordinate(coordinate, error):
    _keys(coordinate, {"provider", "session_id", "session_file"}, error, "provider coordinate")
    provider, session = coordinate["provider"], coordinate["session_id"]
    try:
        session_bytes = len(session.encode("utf-8")) if type(session) is str else 0
    except UnicodeError:
        session_bytes = 513
    if (type(provider) is not str or provider not in {"codex_local", "claude_local", "pi_local"}
        or type(session) is not str or not session or len(session) > 512
        or any(unicodedata.category(c).startswith("C") for c in session)
        or session_bytes > 512):
        error("native_session_mismatch", "lifecycle requires bounded authenticated provider identity")
    if provider == "codex_local" and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", session) is None:
        error("native_session_mismatch", "lifecycle requires bounded authenticated provider identity")
    if provider == "pi_local":
        _path(coordinate["session_file"], error)
    elif coordinate["session_file"] is not None:
        error("native_session_mismatch", "lifecycle requires bounded authenticated provider identity")


def _references(values, prefix, error):
    if type(values) is not list or len(values) > 1000 or any(type(v) is not str for v in values):
        error("adhoc_dispatch_authentication_failed", "lifecycle references must be bounded unique native IDs")
    if len(values) != len(set(values)):
        error("adhoc_dispatch_authentication_failed", "lifecycle references must be bounded unique native IDs")
    for value in values:
        _reference(value, prefix, error)


def _visible(value, maximum, error):
    try:
        size = len(value.encode("utf-8")) if type(value) is str else 0
    except UnicodeError:
        size = maximum + 1
    if (type(value) is not str or not value or len(value) > maximum
        or any(unicodedata.category(c).startswith("C") and c not in "\n\t" for c in value)
        or size > maximum):
        error("adhoc_dispatch_authentication_failed", "lifecycle text exceeds its closed visible bound")


def _artifacts(values, error):
    if type(values) is not list or len(values) > 1000:
        error("adhoc_dispatch_authentication_failed", "lifecycle artifacts exceed their closed bound")
    for value in values:
        _keys(value, {"repo", "sha", "doc"}, error, "lifecycle artifact")
        _identifier(value["repo"], error, "repository")
        if type(value["sha"]) is not str or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["sha"]) is None:
            error("adhoc_dispatch_authentication_failed", "lifecycle artifact requires an exact Git SHA")
        _path(value["doc"], error, relative=True)


def _cleanup(value, error):
    _keys(value, {"adapter_pid", "registered_process_groups", "alive_after_cleanup", "isolation_backend"},
          error, "physical cleanup")
    if value["adapter_pid"] is not None and (type(value["adapter_pid"]) is not int or value["adapter_pid"] <= 0):
        error("adhoc_dispatch_authentication_failed", "physical cleanup requires bounded owned process metadata")
    for field in ("registered_process_groups", "alive_after_cleanup"):
        values = value[field]
        if (type(values) is not list or len(values) > 1000
            or any(type(v) is not int or v <= 0 for v in values) or len(values) != len(set(values))):
            error("adhoc_dispatch_authentication_failed", "physical cleanup requires bounded owned process metadata")
    if value["isolation_backend"] is not None:
        _visible(value["isolation_backend"], 128, error)


def _transport(evidence, error):
    from .worker_bootstrap_protocol import NATIVE_THREAD_BOUNDS
    bounds, value = evidence["bounds"], evidence["transport"]
    if (type(bounds) is not dict or bounds != NATIVE_THREAD_BOUNDS
        or any(type(v) is not int for v in bounds.values())):
        error("native_protocol_limit", "transport evidence requires the exact versioned native bounds")
    _keys(value, {"stdout_bytes", "stderr_bytes", "combined_bytes", "protocol_records",
                 "stdout_sha256", "stderr_sha256", "exit_code"}, error, "native transport")
    for field in ("stdout_bytes", "stderr_bytes", "combined_bytes", "protocol_records"):
        if type(value[field]) is not int or value[field] < 0:
            error("native_protocol_limit", "transport evidence requires bounded observed counters")
    if (value["combined_bytes"] != value["stdout_bytes"] + value["stderr_bytes"]
        or any(value[field] > bounds[field] for field in
               ("stdout_bytes", "stderr_bytes", "combined_bytes", "protocol_records"))
        or type(value["exit_code"]) is not int):
        error("native_protocol_limit", "transport evidence requires bounded observed counters")
    _digest(value["stdout_sha256"], error)
    _digest(value["stderr_sha256"], error)


def _workspace_pin(value, error):
    _keys(value, {"expected_head", "index_sha256"}, error, "reserved workspace pin")
    if type(value["expected_head"]) is not str or re.fullmatch(r"[0-9a-f]{40}", value["expected_head"]) is None:
        error("adhoc_binding_changed", "banking requires its exact reserved branch and index")
    _digest(value["index_sha256"], error)


def _identity_binding(value, error):
    _keys(value, {"path", "dev", "ino", "mode", "input_sha256", "output_sha256"},
          error, "Claude result identity binding")
    _path(value["path"], error, relative=True)
    if (type(value["dev"]) is not int or value["dev"] < 0
        or type(value["ino"]) is not int or value["ino"] <= 0
        or type(value["mode"]) is not str or value["mode"] not in {"100644", "100755"}):
        error("adhoc_binding_changed", "Claude result identity requires its exact owned inode and mode")
    _digest(value["input_sha256"], error)
    _digest(value["output_sha256"], error)


def _lifecycle_result(result, error, *, candidate=False):
    fields = {"doc", "blob_sha256", "provider_coordinate", "summary", "inbox_dispositions"}
    _keys(result, fields if candidate else fields | {"sha"},
          error, "banked result")
    if candidate:
        _path(result["doc"], error, relative=True)
    else:
        _artifacts([{"repo": "result", "sha": result["sha"], "doc": result["doc"]}], error)
    _digest(result["blob_sha256"], error)
    _native_coordinate(result["provider_coordinate"], error)
    _visible(result["summary"], 4096, error)
    dispositions = result["inbox_dispositions"]
    if type(dispositions) is not list or len(dispositions) > 1000:
        error("adhoc_result_disposition_invalid", "result dispositions must name the exact frozen batch")
    ids = []
    for disposition in dispositions:
        _keys(disposition, {"message_id", "disposition", "reason", "result_paths"}, error, "inbox disposition")
        _reference(disposition["message_id"], "msg-", error)
        ids.append(disposition["message_id"])
        if type(disposition["disposition"]) is not str or disposition["disposition"] not in {"acted", "unacted"}:
            error("adhoc_result_disposition_invalid", "result dispositions must name the exact frozen batch")
        _visible(disposition["reason"], 1024, error)
        paths = disposition["result_paths"]
        if (type(paths) is not list or len(paths) > 1000 or any(type(p) is not str for p in paths)
                or len(paths) != len(set(paths))):
            error("adhoc_result_disposition_invalid", "result dispositions must name the exact frozen batch")
        for path in paths:
            _path(path, error, relative=True)
    if len(ids) != len(set(ids)):
        error("adhoc_result_disposition_invalid", "result dispositions must name the exact frozen batch")


def validate_lifecycle_evidence(phase, evidence, error=refuse):
    if type(phase) is not str or phase not in LIFECYCLE_PHASES:
        error("adhoc_dispatch_authentication_failed", "lifecycle phase is outside its closed contract")
    fields = {
        "inbox_presented": {"message_ids", "presentation_ids", "session_id", "prompt_sha256"},
        "spawn_reserved": {"native_context", "workspace_pin"}, "provider_terminal": {"event"},
        "work_completed": {"transition_id", "ack_ids", "consumption_ids", "result"},
        "result_published": {"envelope_id"},
        "teardown_completed": {"resource_dispositions", "provider_state_bytes", "history_state", "live_login"},
        "failed": {"code", "cleanup"}, "unknown": {"code", "cleanup"},
    }
    if phase == "process_closed":
        stages = {"cleanup": {"stage", "worker_receipt_id", "cleanup", "artifacts"},
                  "banking_intent": {"stage", "workspace_pin", "result_candidate", "files"},
                  "result": {"stage", "result"}, "ack": {"stage", "ack_ids"},
                  "consume": {"stage", "consumption_ids"}}
        settlement_fields = {"stage", "head_sha", "remote_coordinate", "remote_ref", "remote_sha",
            "index_sha256_before", "index_sha256_after", "remote_evidence_digest"}
        stages.update({"index_settlement_intent": settlement_fields, "index_settled": settlement_fields})
        if type(evidence) is not dict or type(evidence.get("stage")) is not str or evidence["stage"] not in stages:
            error("adhoc_dispatch_authentication_failed", "lifecycle phase is outside its closed contract")
        expected = stages[evidence["stage"]]
        if evidence["stage"] == "cleanup" and ("transport" in evidence or "bounds" in evidence):
            expected = expected | {"transport", "bounds"}
        if evidence["stage"] == "banking_intent" and "identity_binding" in evidence:
            expected = expected | {"identity_binding"}
    else:
        expected = fields[phase]
        if phase == "inbox_presented" and type(evidence) is dict and "fleet_presence" in evidence:
            expected = expected | {"fleet_presence"}
        if phase == "teardown_completed" and type(evidence) is dict and "provenance" in evidence:
            expected = expected | {"provenance"}
    _keys(evidence, expected, error, "lifecycle evidence")
    if phase == "inbox_presented":
        _references(evidence["message_ids"], "msg-", error)
        _references(evidence["presentation_ids"], "delivery-", error)
        _visible(evidence["session_id"], 512, error)
        _digest(evidence["prompt_sha256"], error)
        if "fleet_presence" in evidence:
            _visible(evidence["fleet_presence"], 8192, error)
    elif phase == "spawn_reserved":
        _workspace_pin(evidence["workspace_pin"], error)
        from .worker_bootstrap_protocol import validate_native_thread_context
        try:
            validate_native_thread_context(evidence["native_context"])
        except ProtocolRefusal as exc:
            error(exc.code, "lifecycle reservation requires its exact native context")
    elif phase == "provider_terminal":
        event = evidence["event"]
        fields = {"schema_version", "event", "binding", "provider_coordinate", "outcome"}
        if type(event) is dict and "usage" in event:
            fields |= {"usage"}
        if type(event) is dict and event.get("outcome") == "failed":
            fields |= {"reason_code", "worker_failure"}
        _keys(event, fields, error, "native terminal")
        if (type(event["schema_version"]) is not int or event["schema_version"] != 1
            or event["event"] != "terminal" or type(event["outcome"]) is not str
            or event["outcome"] not in {"success", "failed"}):
            error("adhoc_dispatch_authentication_failed", "lifecycle terminal requires authenticated native testimony")
        if event["outcome"] == "failed":
            from .native_thread_protocol import _FAILURES
            if type(event["reason_code"]) is not str or _FAILURES.get(event["reason_code"]) != event["worker_failure"]:
                error("adhoc_dispatch_authentication_failed", "lifecycle terminal requires authenticated native testimony")
        _keys(event["binding"], NATIVE_BINDING_FIELDS, error, "terminal binding")
        _native_coordinate(event["provider_coordinate"], error)
        if "usage" in event:
            from .native_usage import validate_usage
            adapter = {"codex_local": "codex", "claude_local": "claude", "pi_local": "pi"}[
                event["provider_coordinate"]["provider"]]
            validate_usage(event["usage"], adapter, error)
    elif phase == "process_closed":
        if evidence["stage"] == "cleanup":
            _reference(evidence["worker_receipt_id"], "worker-receipt-", error)
            _cleanup(evidence["cleanup"], error)
            _artifacts(evidence["artifacts"], error)
            if "transport" in evidence:
                _transport(evidence, error)
        elif evidence["stage"] == "banking_intent":
            _workspace_pin(evidence["workspace_pin"], error)
            _lifecycle_result(evidence["result_candidate"], error, candidate=True)
            files = evidence["files"]
            if type(files) is not list or not 1 <= len(files) <= 32:
                error("adhoc_result_disposition_invalid", "banking intent requires the exact bounded result file set")
            paths = []
            for item in files:
                _keys(item, {"path", "sha256", "mode"}, error, "reserved result file")
                _path(item["path"], error, relative=True)
                if any(part.casefold() == ".git" for part in item["path"].split("/")):
                    error("adhoc_result_disposition_invalid", "banking intent requires ordinary project result files")
                _digest(item["sha256"], error)
                if type(item["mode"]) is not str or item["mode"] not in {"100644", "100755"}:
                    error("adhoc_result_disposition_invalid", "banking intent requires ordinary project result files")
                paths.append(item["path"])
            if paths != sorted(set(paths)):
                error("adhoc_result_disposition_invalid", "banking intent requires the exact bounded result file set")
            if "identity_binding" in evidence:
                _identity_binding(evidence["identity_binding"], error)
        elif evidence["stage"] == "result":
            _lifecycle_result(evidence["result"], error)
        elif evidence["stage"] in {"index_settlement_intent", "index_settled"}:
            if type(evidence["head_sha"]) is not str or re.fullmatch(r"[0-9a-f]{40}", evidence["head_sha"]) is None:
                error("adhoc_binding_changed", "index settlement requires one exact row HEAD")
            if evidence["remote_sha"] != evidence["head_sha"]:
                error("adhoc_binding_changed", "index settlement requires exact remote HEAD equality")
            for field in ("index_sha256_before", "index_sha256_after", "remote_evidence_digest"):
                _digest(evidence[field], error)
            _visible(evidence["remote_coordinate"], 1024, error)
            _visible(evidence["remote_ref"], 1024, error)
        elif evidence["stage"] == "ack":
            _references(evidence["ack_ids"], "ack-", error)
        else:
            _references(evidence["consumption_ids"], "consume-", error)
    elif phase == "work_completed":
        _reference(evidence["transition_id"], "transition-", error)
        _references(evidence["ack_ids"], "ack-", error)
        _references(evidence["consumption_ids"], "consume-", error)
        _lifecycle_result(evidence["result"], error)
    elif phase == "result_published":
        _reference(evidence["envelope_id"], "msg-", error)
    elif phase == "teardown_completed":
        resources = evidence["resource_dispositions"]
        _keys(resources, {"workspace", "lane_opening_id", "lane_closing_id", "node_lease_id",
                         "provider_state", "process", "work", "mail", "result"}, error, "teardown resources")
        if any(resources[field] != expected for field, expected in {
                "workspace": "closed", "provider_state": "retained", "process": "closed",
                "work": "completed", "mail": ("unknown_at_native_close" if "provenance" in evidence
                                               else "caught_up"), "result": "published"}.items()):
            error("adhoc_teardown_blocked", "teardown requires proved exact resource dispositions")
        _reference(resources["lane_opening_id"], "lane-workspace-", error)
        _reference(resources["lane_closing_id"], "lane-workspace-", error)
        _reference(resources["node_lease_id"], "lease-", error)
        if "provenance" in evidence:
            provenance = evidence["provenance"]
            _keys(provenance, {"kind", "lane_opening_id", "lane_closing_id", "observed_node_lease_id",
                "worker_receipt_id", "work_transition_id", "result_envelope_id", "acted_message_ids",
                "ack_receipt_ids", "consumption_receipt_ids"}, error, "historical teardown provenance")
            if (provenance["kind"] != "native_closing_recovery"
                or provenance["lane_opening_id"] != resources["lane_opening_id"]
                or provenance["lane_closing_id"] != resources["lane_closing_id"]):
                error("adhoc_teardown_blocked", "historical teardown provenance must name its exact native close")
            for field, prefix in (("lane_opening_id", "lane-workspace-"),
                    ("lane_closing_id", "lane-workspace-"), ("observed_node_lease_id", "lease-"),
                    ("worker_receipt_id", "worker-receipt-"), ("work_transition_id", "transition-"),
                    ("result_envelope_id", "msg-")):
                _reference(provenance[field], prefix, error)
            for field, prefix in (("acted_message_ids", "msg-"), ("ack_receipt_ids", "ack-"),
                    ("consumption_receipt_ids", "consume-")):
                _references(provenance[field], prefix, error)
        if type(evidence["provider_state_bytes"]) is not int or evidence["provider_state_bytes"] < 0 or evidence["history_state"] != "inactive" or evidence["live_login"] != {"status": "unmeasured", "credential_opt_in": "required_separately"}:
            error("adhoc_teardown_blocked", "teardown requires proved exact resource dispositions")
    else:
        _identifier(evidence["code"], error, "reason code")
        _cleanup(evidence["cleanup"], error)
    try:
        if len(canonical_json(evidence)) > 1_048_576:
            error("native_protocol_limit", "lifecycle metadata exceeds its closed byte bound")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        error("native_protocol_limit", "lifecycle metadata exceeds its closed byte bound")
    return deepcopy(evidence)


def validate_adhoc_record(record, tenant, error=refuse):
    kind = record.get("kind")
    prefix, fields = ADHOC_SPECS[kind]
    _keys(record, fields, error, kind)
    version = record["schema_version"]
    if type(version) is not int or version not in ADHOC_RECORD_VERSIONS[kind]:
        error("schema_version_invalid", "unsupported ad-hoc record version")
    if record["tenant_id"] != tenant:
        error("tenant_mismatch", "ad-hoc metadata belongs to another tenant")
    _identifier(record["tenant_id"], error, "tenant_id")
    _reference(record["id"], prefix, error)
    _timestamp(record["timestamp"], error)
    if kind == "adhoc_lifecycle_record":
        for field, reference in (("admission_id", "adhoc-journal-"),
                ("dispatch_id", "adhoc-dispatch-"), ("profile_id", "adhoc-profile-")):
            _reference(record[field], reference, error)
        for field in ("profile_digest", "dispatch_digest"):
            _digest(record[field], error)
        validate_lifecycle_evidence(record["phase"], record["evidence"], error)
        return deepcopy(record)
    if kind == "adhoc_native_thread_opened_record":
        for field, reference in (("admission_id", "adhoc-journal-"),
                ("dispatch_id", "adhoc-dispatch-"), ("profile_id", "adhoc-profile-")):
            _reference(record[field], reference, error)
        for field in ("profile_digest", "dispatch_digest"):
            _digest(record[field], error)
        if record["phase"] != "provider_opened":
            error("adhoc_dispatch_authentication_failed", "opened testimony has one exact phase")
        evidence = record["evidence"]
        _keys(evidence, {"binding", "provider_coordinate", "identity_phase"}, error, "opened evidence")
        if evidence["identity_phase"] != "pre_prompt":
            error("adhoc_dispatch_authentication_failed", "opened testimony requires a pre-prompt identity")
        binding = evidence["binding"]
        _keys(binding, NATIVE_BINDING_FIELDS, error, "native binding")
        for field in ("profile_id", "profile_digest", "dispatch_id", "dispatch_digest"):
            if binding[field] != record[field]:
                error("adhoc_dispatch_authentication_failed", "opened binding differs from its outer references")
        _identifier(binding["node_id"], error, "node_id")
        _reference(binding["node_lease_id"], "lease-", error)
        _reference(binding["work_item_id"], "work-", error)
        effect = binding["effect_binding"]
        _keys(effect, {"run_id", "item_id", "attempt_id"}, error, "effect binding")
        for field, prefix in (("run_id", "run-"), ("item_id", "work-"), ("attempt_id", "attempt-")):
            _reference(effect[field], prefix, error)
        if effect["item_id"] != binding["work_item_id"]:
            error("adhoc_dispatch_authentication_failed", "opened effect names another work item")
        coordinate = evidence["provider_coordinate"]
        _keys(coordinate, {"provider", "session_id", "session_file"}, error, "provider coordinate")
        provider, session = coordinate["provider"], coordinate["session_id"]
        if type(provider) is not str or provider not in {"codex_local", "pi_local"}:
            error("adhoc_dispatch_authentication_failed", "this provider cannot emit pre-prompt identity")
        if (type(session) is not str or not session or len(session) > 512
            or any(unicodedata.category(c).startswith("C") for c in session)
            or len(session.encode("utf-8")) > 512):
            error("adhoc_dispatch_authentication_failed", "provider session must be bounded native testimony")
        if provider == "codex_local":
            if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", session) is None or coordinate["session_file"] is not None:
                error("adhoc_dispatch_authentication_failed", "Codex opened identity requires UUIDv7 and no file")
        else:
            _path(coordinate["session_file"], error)
        return deepcopy(record)
    _identifier(record["idempotency_key"], error, "idempotency_key")
    if kind == "adhoc_profile_record":
        for field in ("node_id", "repository", "result_recipient"):
            _identifier(record[field], error, field)
        _reference(record["node_lease_id"], "lease-", error)
        _reference(record["lane_opening_id"], "lane-workspace-", error)
        if not isinstance(record["adapter"], str) or record["adapter"] not in {"codex", "claude", "pi"}:
            error("native_permission_unsupported", "profile adapter is outside the native family")
        for field in ("node_home", "workspace", "git_common_dir", "git_admin_dir"):
            _path(record[field], error)
        if not isinstance(record["branch"], str) or not record["branch"].startswith("codex/lane/" + record["node_id"] + "/") or any(c.isspace() or ord(c) < 32 for c in record["branch"]):
            error("adhoc_binding_changed", "branch must name the recorded node lane")
        if not isinstance(record["base_sha"], str) or re.fullmatch(r"[0-9a-f]{40}", record["base_sha"]) is None:
            error("adhoc_binding_changed", "base must be the exact recorded Git SHA")
        _timestamp(record["lease_expires_at"], error)
        _digest(record["seat_digest"], error)
        if record["provider_state"] != str(PROVIDER_STATE_DIRECTORY / record["id"]):
            error("adhoc_binding_changed", "provider slot must belong to this exact profile")
        _keys(record["filesystem_bindings"], BINDING_FIELDS, error, "filesystem bindings")
        for identity in record["filesystem_bindings"].values():
            _keys(identity, {"dev", "ino"}, error, "descriptor identity")
            if type(identity["dev"]) is not int or identity["dev"] < 0 or type(identity["ino"]) is not int or identity["ino"] <= 0:
                error("adhoc_binding_changed", "descriptor identities require nonboolean device and inode integers")
        _digest(record["digest"], error)
        if record["digest"] != profile_digest(record):
            error("adhoc_binding_changed", "profile content digest differs")
    else:
        for field, reference in (("dispatch_id", "adhoc-dispatch-"), ("profile_id", "adhoc-profile-"), ("envelope_id", "msg-")):
            _reference(record[field], reference, error)
        _digest(record["profile_digest"], error)
        _identifier(record["actor"], error, "actor")
        session = record["acting_session_id"]
        if not isinstance(session, str) or not session or any(unicodedata.category(c).startswith("C") for c in session) or len(session.encode()) > 512:
            error("adhoc_dispatch_authentication_failed", "acting session must be a bounded native identifier")
        dispatch = validate_dispatch(record["dispatch"], error)
        if dispatch["profile_id"] != record["profile_id"] or dispatch["profile_digest"] != record["profile_digest"] or dispatch["node_id"] != record["actor"]:
            error("adhoc_dispatch_authentication_failed", "outer admission and dispatch bindings differ")
        _digest(record["dispatch_digest"], error)
        if record["dispatch_digest"] != dispatch_digest(dispatch):
            error("adhoc_dispatch_authentication_failed", "canonical dispatch digest differs")
        if record["phase"] != "admitted":
            error("adhoc_dispatch_authentication_failed", "later native phases require separately reviewed evidence contracts")
        evidence = record["evidence"]
        evidence_keys = {"repository", "sha", "doc", "blob_sha256"}
        if version == 2:
            from .native_dispatch_launcher import KIND as launcher_kind
            from .records import validate_record

            evidence_keys.add("native_launcher")
            _keys(evidence, evidence_keys, error, "admission evidence")
            launcher = evidence["native_launcher"]
            try:
                validate_record(launcher, tenant, frozenset({launcher_kind}), integrity=False)
            except ProtocolRefusal as exc:
                error("adhoc_dispatch_authentication_failed", exc.detail)
            if ({"seq", "prev"}.intersection(launcher)
                    or launcher["message_id"] != record["envelope_id"]
                    or dispatch["operation"] != "start"
                    or datetime.fromisoformat(launcher["timestamp"].replace("Z", "+00:00"))
                    > datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))):
                error("adhoc_dispatch_authentication_failed", "launcher testimony differs from its exact admission")
        else:
            _keys(evidence, evidence_keys, error, "admission evidence")
        _identifier(evidence["repository"], error, "repository")
        if not isinstance(evidence["sha"], str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", evidence["sha"]) is None:
            error("adhoc_dispatch_authentication_failed", "admission requires exact Git SHA")
        _path(evidence["doc"], error, relative=True)
        _digest(evidence["blob_sha256"], error)
    return deepcopy(record)
