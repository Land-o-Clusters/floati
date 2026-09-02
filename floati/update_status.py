"""Physically read-only doctor projection of local update testimony."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict

from .artifact_subject import artifact_subject
from .copy import register
from .errors import IntegrityFailure
from .update_check import OBSERVATION_LEDGER, validate_observation_ledger_row
from .update_consent import (
    CONSENT_LEDGER,
    INSTALL_DIRECTORY,
    UpdateConsentLedger,
    _read_jsonl,
)
from .update_ownership import observe_install_ownership


OWNERSHIP_DETAIL = register(
    "doctor.update.ownership",
    "update ownership state={state} source_sha={source_sha}",
    "Doctor update finding",
)
CONSENT_DETAIL = register(
    "doctor.update.consent",
    "update consent state={state} receipt={receipt} coordinate={coordinate} epoch={epoch} predecessor={predecessor}",
    "Doctor update finding",
)
CONSENT_INVALID_DETAIL = register(
    "doctor.update.consent_invalid",
    "update consent ledger names another installation",
    "Doctor update finding",
)
OBSERVATION_INVALID_DETAIL = register(
    "doctor.update.observation_invalid",
    "update observation ledger names another installation",
    "Doctor update finding",
)
APPLICATION_INVALID_DETAIL = register(
    "doctor.update.application_invalid",
    "update application does not link one local signed check",
    "Doctor update finding",
)
NEVER_CHECKED_DETAIL = register(
    "doctor.update.never_checked",
    "updates have never been checked for this installation",
    "Doctor update finding",
)
LAST_CHECK_DETAIL = register(
    "doctor.update.last_check",
    "last update check receipt={receipt} timestamp={timestamp} version={version} source_sha={source_sha} signature=verified",
    "Doctor update finding",
)
NEVER_APPLIED_DETAIL = register(
    "doctor.update.never_applied",
    "no update has been applied or rolled back for this installation",
    "Doctor update finding",
)
LAST_APPLY_DETAIL = register(
    "doctor.update.last_apply",
    "last update target receipt={receipt} version={version} previous_source_sha={previous_source_sha} source_sha={source_sha} check={check} wiring_journal={wiring_journal}",
    "Doctor update finding",
)


def _finding(code: str, subject: Path, detail: str) -> Dict[str, object]:
    return {
        "code": code,
        "severity": "ok",
        "subject": artifact_subject(subject),
        "detail": detail,
        "remediation": None,
    }


def _coordinate_digest(row: Dict[str, object]) -> str:
    coordinate = {
        "destination": row["destination"],
        "channel": row["channel"],
        "public_key_sha256": row["public_key_sha256"],
    }
    encoded = json.dumps(
        coordinate,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def project_update_findings(
    destination: Path, *, entrypoint: Path
) -> list[Dict[str, object]]:
    """Return local update facts without creating a lock or receipt."""

    selected = Path(destination)
    metadata = selected / INSTALL_DIRECTORY
    ownership = observe_install_ownership(selected, entrypoint=entrypoint)
    findings = [
        _finding(
            "update_ownership",
            metadata / "manifest.v0.json",
            OWNERSHIP_DETAIL.format(
                state=ownership["state"],
                source_sha=ownership["source_sha"],
            ),
        )
    ]

    consent_path = metadata / CONSENT_LEDGER
    consent_rows = [
        UpdateConsentLedger._validate_row(row) for row in _read_jsonl(consent_path)
    ]
    if any(row["destination"] != str(selected) for row in consent_rows):
        raise IntegrityFailure(
            "update_consent_record_invalid",
            CONSENT_INVALID_DETAIL,
        )
    if consent_rows:
        consent = consent_rows[-1]
        consent_detail = CONSENT_DETAIL.format(
            state=consent["state"],
            receipt=consent["id"],
            coordinate=_coordinate_digest(consent),
            epoch=consent["epoch"],
            predecessor=consent["predecessor_receipt_id"] or "none",
        )
    else:
        consent_detail = CONSENT_DETAIL.format(
            state="never_consented",
            receipt="none",
            coordinate="none",
            epoch="none",
            predecessor="none",
        )
    findings.append(_finding("update_consent", consent_path, consent_detail))

    observations_path = metadata / OBSERVATION_LEDGER
    observations = [
        validate_observation_ledger_row(row) for row in _read_jsonl(observations_path)
    ]
    checks = [row for row in observations if row["kind"] == "update_observation"]
    applications = [
        row for row in observations if row["kind"] == "update_application"
    ]
    if any(row["destination"] != str(selected) for row in checks):
        raise IntegrityFailure(
            "update_observation_invalid",
            OBSERVATION_INVALID_DETAIL,
        )
    check_ids = {row["id"] for row in checks}
    if any(
        row["destination"] != str(selected)
        or row["check_observation_id"] not in check_ids
        for row in applications
    ):
        raise IntegrityFailure(
            "update_application_invalid",
            APPLICATION_INVALID_DETAIL,
        )
    if checks:
        checked = checks[-1]
        check_detail = LAST_CHECK_DETAIL.format(
            receipt=checked["id"],
            timestamp=checked["timestamp"],
            version=checked["observed_version"],
            source_sha=checked["latest_source_sha"],
        )
    else:
        check_detail = NEVER_CHECKED_DETAIL
    findings.append(_finding("update_last_check", observations_path, check_detail))

    if applications:
        applied = applications[-1]
        apply_detail = LAST_APPLY_DETAIL.format(
            receipt=applied["id"],
            version=applied["version"],
            previous_source_sha=applied["previous_source_sha"],
            source_sha=applied["source_sha"],
            check=applied["check_observation_id"],
            wiring_journal=applied["wiring_journal"],
        )
    else:
        apply_detail = NEVER_APPLIED_DETAIL
    findings.append(_finding("update_last_apply", observations_path, apply_detail))
    return findings
