"""Stable protocol failures used at Floati's filesystem boundary."""

from __future__ import annotations

import re
from typing import Mapping, Union


UNNAMED_REMEDY: dict[str, str] = {
    "kind": "none",
    "why": "no action was named for this refusal",
}

# Action text for the WS-I / M4 drill codes this row owns. Installer
# codes stay off this map so the install rows can name --ref / shadow
# at the emit sites without editing this map.
DRILL_REMEDIES: Mapping[str, str] = {
    "arguments_invalid": "retry the command supplying the exact flags or values named in detail",
    "ack_item_unknown": "pass --id of a message that inbox listed for this node and session",
    "solo_identity_ambiguous": "pass --as with the exact active node id",
    "authority_holder_mismatch": "act as the grant's exact holder",
    "work_unknown": "pass --id of a work item that work list shows",
    "work_duplicate_open": "use the existing work item or change its details",
    "work_already_claimed": "choose an open work item or coordinate with its current holder",
    "run_id_invalid": "pass --run as run-<uuid7>",
    # Shared idempotency-key validator; every verb that takes the flag serves it.
    "wake_idempotency_key_invalid": "pass --idempotency-key as printable text of 1 to 128 characters",
    "rollback_host_artifacts_differ": (
        "restore or re-trust the named host artifacts, then retry the rollback"
    ),
    "rollback_host_photograph_invalid": (
        "replace the host-wiring photograph with a regular JSON file, then retry the rollback"
    ),
}

# Eligibility finding, rather than a protocol refusal.
AUTHORITY_SUPERSEDED = "authority_superseded"

# Work notification findings leave the durable work and mail shapes unchanged.
WORK_ADD_UNNOTIFIED = "work_add_unnotified"
WORK_COMPLETE_UNNOTIFIED = "work_complete_unnotified"
WORK_NOTIFICATION_REFUSED = "work_notification_refused"
WORK_NOTIFICATION_PENDING = "work_notification_pending"
WORK_NOTIFICATION_INTENT_ORPHAN = "work_notification_intent_orphan"
WORK_NOTIFICATION_CONFLICT = "work_notification_conflict"
WORK_NOTIFICATION_UNREADABLE = "work_notification_unreadable"

# Typeable remedies rebound ONLY on the send path. Shared verbs keep
# unnamed remedies unless EventLog.send / cli._send rebind them.
_WRITE_VALIDATION = (
    "do not retry send; the record being written failed validation — "
    "this is not a damaged ledger"
)
SEND_REFUSAL_REMEDIES: Mapping[str, str] = {
    "claude_inbox_evidence_invalid": _WRITE_VALIDATION,
    "claude_inbox_fallback_code_invalid": _WRITE_VALIDATION,
    "codex_wait_consent_missing": "run floati waiter arm with the exact seat coordinates named in detail before retrying",
    "codex_wait_workspace_map_invalid": "restore the exact workspace binding named in detail with floati waiter arm before retrying",
    "wake_session_invalid": "pass the exact safe session identifier named in detail before retrying",
    "address_preview_invalid": "run send with --preview, review and save the artifact, then pass --recipient-preview PATH",
    "archive_container_destination_invalid": "do not retry send; preserve the named archive destination and ask the coordinator to inspect automatic maintenance",
    "archive_events_invalid": "do not retry send; preserve the named archive events and ask the coordinator to inspect automatic maintenance",
    "archive_maintenance_required": "do not retry send; ask the coordinator to inspect the native automatic maintenance path",
    "archive_member_invalid": "do not retry send; preserve the named archive member and ask the coordinator to inspect automatic maintenance",
    "archive_root_invalid": "do not retry send; preserve the named archive root and ask the coordinator to inspect automatic maintenance",
    "authority_expired": "ask the coordinator to renew the exact expired authority before retrying send",
    "authority_lock_missing": "do not retry send; ask the coordinator to inspect the named authority lock",
    "authority_missing": "ask the coordinator to grant the exact missing authority before retrying send",
    "authority_released": "ask the coordinator for a current authority grant before retrying send",
    "cas_lock_timeout": "retry send once after the named authority lock is released",
    "epoch_carry_evidence_invalid": (
        "do not retry send; preserve the named archive proof source and ask "
        "the coordinator to inspect its exact epoch carry evidence"
    ),
    "epoch_marker_invalid": "do not retry send; preserve the named epoch marker and ask the coordinator to investigate",
    "epoch_roll_recovery_required": "ask the coordinator to reconcile the incomplete epoch roll before retrying send",
    "epoch_selected_member_changed": "do not retry send; preserve the named epoch member and ask the coordinator to investigate",
    "epoch_selected_member_not_regular": "do not retry send; preserve the named epoch member and ask the coordinator to investigate its file type",
    "epoch_selected_member_symlink": "do not retry send; ask the coordinator to restore the named epoch member without a symlink",
    "holder_mismatch": "retry send only as the exact holder named by the authority receipt",
    "unknown_node": "pass a registered active node named by the fleet registry, then retry send",
    "adhoc_dispatch_authentication_failed": (
        "do not retry send; preserve the named ad-hoc metadata and ask the coordinator "
        "to inspect its exact native authoring route"
    ),
    "native_dispatch_launcher_controller_only": (
        "Preserve the existing launcher records and ask the coordinator to inspect "
        "the authenticated send transaction before retrying."
    ),
    "cannot_speak": "pass --root as an absolute fleet path or set FLOATI_BUS_ROOT",
    "claim_artifact_path_invalid": "pass --claim artifact paths as contained repository-relative paths",
    "claim_artifacts_invalid": "pass --claim artifacts as the closed typed list",
    "claim_bank_invalid": "pass --claim whose bank matches a fetched remote ref",
    "claim_declared_invalid": "pass --claim declared fields that match --repo, --sha, and --doc",
    "claim_fields_invalid": "pass --claim with the closed delivery-claim field set",
    "claim_malformed": "pass --claim as strict JSON UTF-8",
    "claim_not_object": "pass --claim as one JSON object",
    "claim_path_invalid": "pass --claim as one regular JSON file path",
    "claim_repo_path_invalid": "pass --claim repo as one or two lexical segments",
    "claim_sha_invalid": "pass --claim sha as 40 lowercase hex characters",
    "claim_sha_mismatch": "pass --claim whose sha matches --sha",
    "claim_size_invalid": "pass --claim as a JSON file of at most 65536 bytes",
    "claim_unreadable": "pass --claim as a readable JSON file",
    "direct_home_missing": "pass --root as an existing direct-home fleet directory",
    "direct_home_not_directory": "pass --root as a directory, not a file",
    "direct_home_symlinked_entry": "pass --root that is not a symlink",
    "duplicate_record_id": "do not retry send; this ledger already holds that record id — restore or retire the root",
    "epoch_lock_timeout": "retry send once; the shared epoch lock was contended",
    "epoch_lock_upgrade_invalid": "do not retry send; the epoch lock cannot upgrade in this state — restart the command from a fresh process",
    "idempotency_conflict": "retry send with a fresh --idempotency-key, or replay the exact prior payload",
    "idempotency_key_invalid": "pass --idempotency-key as printable text of 1 to 128 characters",
    "journal_chain_fields_invalid": _WRITE_VALIDATION,
    "journal_prev_invalid": _WRITE_VALIDATION,
    "journal_seq_invalid": _WRITE_VALIDATION,
    "ledger_cursor_binding_mismatch": "do not retry send; this ledger cursor does not match the file — restore or retire the root",
    "ledger_kind_required": "do not retry send; this ledger named no allowed kinds — restore or retire the root",
    "ledger_record_limit": "do not retry send; this ledger is at its record cap — rotate or retire the root",
    "ledger_too_large": "do not retry send; this ledger exceeded its byte cap — restore or retire the root",
    "lock_order_invalid": "do not retry send; lock order inside this root is inverted — restore or retire the root",
    "namespace_root_layout_present": "pass --root of a direct-home fleet, not a tenants/ namespace layout",
    "node_lease_expired": "run floati seat board --as the sender --session <id> then retry send",
    "path_not_contained": "pass --root so every path stays inside that fleet directory",
    "prefix_digest_domain_invalid": "do not retry send; this ledger mixed digest domains — restore or retire the root",
    "recipient_unregistered": "pass --to as one of the registered nodes named in detail",
    "record_fields_invalid": _WRITE_VALIDATION,
    "record_id_invalid": _WRITE_VALIDATION,
    "record_kind_invalid": _WRITE_VALIDATION,
    "record_not_object": _WRITE_VALIDATION,
    "record_too_large": _WRITE_VALIDATION,
    "reply_to_invalid": "pass --reply-to as msg-<uuid7> of an existing envelope, or omit it",
    "repo_invalid": "pass --repo as one or two lexical segments",
    "root_not_absolute": "pass --root as an absolute path",
    "root_required": "pass --root as an absolute fleet path",
    "root_unavailable": "pass --root as a readable fleet directory",
    "sha_invalid": (
        "pass --sha as a 40- or 64-character lowercase Git object id "
        "(git rev-parse SHORT)"
    ),
    "node_invalid": "pass --from as a node id matching [a-z0-9][a-z0-9_-]*",
    "recipient_invalid": "pass --to as a node id matching [a-z0-9][a-z0-9_-]*",
    "sender_invalid": "pass --from as a node id matching [a-z0-9][a-z0-9_-]*",
    "coordinator_invalid": "pass a coordinator node id matching [a-z0-9][a-z0-9_-]*",
    "direct_home_tenant_invalid": (
        "pass --root whose last path component matches [a-z0-9][a-z0-9_-]*"
    ),
    "record_not_ijson": _WRITE_VALIDATION,
    "frame_bytes_required": (
        "do not retry send; framed input must be bytes — this is not a damaged ledger"
    ),
    "incomplete_frame": (
        "do not retry send; this ledger frame is truncated — restore or retire the root"
    ),
    "blank_frame": (
        "do not retry send; this ledger contains a blank frame — restore or retire the root"
    ),
    "duplicate_json_key": (
        "do not retry send; this ledger object repeats a member — restore or retire the root"
    ),
    "malformed_json": (
        "do not retry send; this ledger frame is not valid JSON — restore or retire the root"
    ),
    "tenant_invalid": "pass --root of a tenant id matching [a-z0-9][a-z0-9_-]*",
    "tenant_mismatch": "pass --root of the tenant that owns this ledger",
    "unknown_recipient": "pass --to as one of the registered nodes named in detail",
    "unknown_sender": "pass --from as one of the registered nodes named in detail",
    "workspace_identity_mismatch": "run send from the workspace declared for --from, or pass matching --root and --from",
    "worker_session_id_invalid": (
        "pass a worker session id as 1 to 512 printable non-control characters"
    ),
    "write_root_required": "pass --root of a writable fleet",
}

SEND_EXPLICIT_NONE: Mapping[str, dict[str, str]] = {
    "attempt_binding_invalid": {
        "kind": "none",
        "why": "library-only field; CLI send has no flag for it",
    },
    "attempt_binding_session_mismatch": {
        "kind": "none",
        "why": "library-only field; CLI send has no flag for it",
    },
    "attempt_id_invalid": {
        "kind": "none",
        "why": "library-only field; CLI send has no flag for it",
    },
    "time_invalid": {
        "kind": "none",
        "why": "library-only now=; CLI send uses the clock",
    },
}

DOCTOR_CURRENCY_REMEDY = "run doctor --source from a git working tree git can inspect"
DEPLOY_CURRENCY_REMEDY = "check the tag out and name it with --ref"

RefusalRemedy = Union[str, dict[str, Union[str, int]]]


def bind_refusal_remedy(code: str, remedy: RefusalRemedy | None) -> RefusalRemedy:
    """Populate every refusal's remedy; JSON null is not a default."""

    if isinstance(remedy, str) and remedy.strip():
        return remedy
    if (
        code in {"ledger_lock_timeout", "consumption_snapshot_contention"}
        and isinstance(remedy, dict)
        and set(remedy) == {"kind", "action", "delay_seconds", "holder"}
        and remedy.get("kind") == "transient"
        and remedy.get("action") == "retry_once"
        and type(remedy.get("delay_seconds")) is int
        and 1 <= remedy["delay_seconds"] <= 3600
        and isinstance(remedy.get("holder"), str)
        and re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", remedy["holder"])
    ):
        return dict(remedy)
    if (
        isinstance(remedy, dict)
        and remedy.get("kind") == "none"
        and str(remedy.get("why") or "").strip()
    ):
        return {"kind": "none", "why": str(remedy["why"])}
    mapped = DRILL_REMEDIES.get(code)
    if mapped is not None:
        return mapped
    return dict(UNNAMED_REMEDY)


def send_path_remedy(code: str, remedy: RefusalRemedy | None) -> RefusalRemedy:
    """Overlay send-only remedies after the global binder has run."""

    if isinstance(remedy, str) and remedy.strip():
        return remedy
    if (
        code == "ledger_lock_timeout"
        and isinstance(remedy, dict)
        and set(remedy) == {"kind", "action", "delay_seconds", "holder"}
        and remedy.get("kind") == "transient"
    ):
        return remedy
    if (
        isinstance(remedy, dict)
        and remedy.get("kind") == "none"
        and str(remedy.get("why") or "").strip()
        and str(remedy.get("why")) != UNNAMED_REMEDY["why"]
    ):
        return {"kind": "none", "why": str(remedy["why"])}
    mapped = SEND_REFUSAL_REMEDIES.get(code)
    if mapped is not None:
        return mapped
    explicit = SEND_EXPLICIT_NONE.get(code)
    if explicit is not None:
        return dict(explicit)
    return bind_refusal_remedy(code, remedy)


def bind_send_refusal_remedy(exc: ProtocolRefusal) -> ProtocolRefusal:
    rebound = send_path_remedy(exc.code, exc.remedy)
    if rebound == exc.remedy:
        return exc
    return ProtocolRefusal(exc.code, exc.detail, rebound)


class FloatiError(RuntimeError):
    """Base error carrying a stable machine-readable reason code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ProtocolRefusal(FloatiError):
    """A requested operation was rejected before its primary mutation."""

    def __init__(
        self,
        code: str,
        detail: str,
        remedy: RefusalRemedy | None = None,
    ) -> None:
        super().__init__(code, detail)
        self.remedy = bind_refusal_remedy(code, remedy)


class IntegrityFailure(FloatiError):
    """Durable evidence is malformed or internally inconsistent."""


class DurabilityFailure(FloatiError):
    """A filesystem failure interrupted or prevented durable evidence access."""


class SnapshotRefusal(FloatiError):
    """Derived snapshot state could not be reconciled with its authority."""
