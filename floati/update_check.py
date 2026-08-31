"""Consent-gated signed release-index checks."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .signing import verify_minisign_paths
from .update_consent import (
    INSTALL_DIRECTORY,
    UpdateConsentLedger,
    _draft,
    _timestamp,
    _transact_jsonl,
    canonical_destination,
    load_update_trust,
    validate_idempotency_key,
    validate_update_channel,
)
from .update_ownership import require_standalone_ownership


ENVELOPE_MAX_BYTES = 128 * 1024
OBSERVATION_LEDGER = "update-observations.v0.jsonl"
_INDEX_FIELDS = {
    "schema_version",
    "channel_id",
    "index_version",
    "latest_version",
    "releases",
}
_RELEASE_FIELDS = {
    "version",
    "source_sha",
    "bundle_filename",
    "bundle_url",
    "bundle_sha256",
    "bundle_size",
}
_ENVELOPE_FIELDS = {
    "release-index.v0.json",
    "release-index.v0.json.minisig",
}
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_CHANNEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_APPLICATION_FIELDS = {
    "schema_version",
    "id",
    "kind",
    "destination",
    "channel",
    "consent_receipt_id",
    "public_key_sha256",
    "check_observation_id",
    "index_sha256",
    "signature_sha256",
    "index_version",
    "version",
    "bundle_filename",
    "bundle_url",
    "bundle_sha256",
    "bundle_size",
    "previous_source_sha",
    "source_sha",
    "verification_state",
    "request_count",
    "idempotency_key",
    "timestamp",
    "wiring_journal",
}


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise _DuplicateKey(key)
        value[key] = member
    return value


def _strict_json(payload: bytes, code: str, detail: str) -> object:
    try:
        return json.loads(payload, object_pairs_hook=_unique_object)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKey,
        ValueError,
    ) as exc:
        raise ProtocolRefusal(code, _draft(detail)) from exc


def decode_release_index_envelope(payload: bytes) -> tuple[bytes, bytes]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= ENVELOPE_MAX_BYTES:
        raise ProtocolRefusal(
            "update_envelope_invalid",
            _draft("update envelope must be bounded non-empty JSON bytes"),
        )
    value = _strict_json(
        payload,
        "update_envelope_invalid",
        "update envelope is not strict JSON",
    )
    if not isinstance(value, dict) or set(value) != _ENVELOPE_FIELDS:
        raise ProtocolRefusal(
            "update_envelope_invalid",
            _draft(
                "update envelope must contain only release-index.v0.json and release-index.v0.json.minisig"
            ),
        )
    decoded: list[bytes] = []
    for field in (
        "release-index.v0.json",
        "release-index.v0.json.minisig",
    ):
        member = value[field]
        if not isinstance(member, str) or not member:
            raise ProtocolRefusal(
                "update_envelope_invalid",
                _draft(f"update envelope field {field} is not base64 text"),
            )
        try:
            raw = base64.b64decode(member.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ProtocolRefusal(
                "update_envelope_invalid",
                _draft(f"update envelope field {field} is not strict base64"),
            ) from exc
        if not raw:
            raise ProtocolRefusal(
                "update_envelope_invalid",
                _draft(f"update envelope field {field} is empty"),
            )
        decoded.append(raw)
    return decoded[0], decoded[1]


def _invalid_index(detail: str) -> ProtocolRefusal:
    return ProtocolRefusal("update_index_invalid", _draft(detail))


def validate_release_index(payload: bytes) -> Dict[str, object]:
    if not isinstance(payload, bytes) or not 2 <= len(payload) <= 128 * 1024:
        raise _invalid_index("signed release index bytes are empty or exceed their bound")
    value = _strict_json(
        payload,
        "update_index_invalid",
        "signed release index is not strict JSON",
    )
    if not isinstance(value, dict) or set(value) != _INDEX_FIELDS:
        raise _invalid_index("signed release index has unexpected fields")
    if value["schema_version"] != 0 or isinstance(value["schema_version"], bool):
        raise _invalid_index("signed release index schema_version is unsupported")
    if (
        not isinstance(value["channel_id"], str)
        or _CHANNEL_ID.fullmatch(value["channel_id"]) is None
        or not isinstance(value["index_version"], str)
        or _VERSION.fullmatch(value["index_version"]) is None
        or not isinstance(value["latest_version"], str)
        or _VERSION.fullmatch(value["latest_version"]) is None
    ):
        raise _invalid_index("signed release index identifiers are invalid")
    releases = value["releases"]
    if not isinstance(releases, list) or not 1 <= len(releases) <= 64:
        raise _invalid_index("signed release catalog must contain between 1 and 64 entries")
    versions: set[str] = set()
    normalized: list[Dict[str, object]] = []
    for release in releases:
        if not isinstance(release, dict) or set(release) != _RELEASE_FIELDS:
            raise _invalid_index("signed release entry has unexpected fields")
        version = release["version"]
        filename = release["bundle_filename"]
        size = release["bundle_size"]
        if (
            not isinstance(version, str)
            or _VERSION.fullmatch(version) is None
            or version in versions
            or not isinstance(release["source_sha"], str)
            or _SOURCE_SHA.fullmatch(release["source_sha"]) is None
            or not isinstance(filename, str)
            or Path(filename).name != filename
            or filename in {"", ".", ".."}
            or not filename.endswith(".bundle")
            or not isinstance(release["bundle_sha256"], str)
            or _SHA256.fullmatch(release["bundle_sha256"]) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= 64 * 1024 * 1024
        ):
            raise _invalid_index("signed release entry is invalid or duplicated")
        validate_update_channel(release["bundle_url"])
        versions.add(version)
        normalized.append(dict(release))
    if value["latest_version"] not in versions:
        raise _invalid_index("latest_version does not select one exact release")
    return {
        "schema_version": 0,
        "channel_id": value["channel_id"],
        "index_version": value["index_version"],
        "latest_version": value["latest_version"],
        "releases": normalized,
    }


def verify_release_index(
    destination: Path, index: bytes, signature: bytes
) -> Dict[str, object]:
    selected = canonical_destination(destination)
    parsed = validate_release_index(index)
    trust = load_update_trust(selected)
    metadata = selected / INSTALL_DIRECTORY
    try:
        with tempfile.TemporaryDirectory(
            prefix=".update-index-verify-", dir=metadata
        ) as temporary:
            staging = Path(temporary).resolve(strict=True)
            artifact_name = Path("release-index.v0.json")
            signature_name = Path("release-index.v0.json.minisig")
            key_name = Path("public-key.pub")
            (staging / artifact_name).write_bytes(index)
            (staging / signature_name).write_bytes(signature)
            (staging / key_name).write_bytes(Path(trust["public_key"]).read_bytes())
            fact = verify_minisign_paths(
                staging,
                artifact_name,
                signature_name,
                key_name,
                version=str(parsed["index_version"]),
            )
    except ProtocolRefusal:
        raise
    except OSError as exc:
        raise DurabilityFailure(
            "storage_unavailable",
            _draft(f"release-index verification staging failed under {metadata}"),
        ) from exc
    if fact["state"] != "signature_verified":
        raise ProtocolRefusal(
            "update_signature_unverified",
            _draft("minisign is absent; no release-index observation was recorded"),
            remedy=_draft("install Minisign, then run a new explicit update check"),
        )
    return {"index": parsed, "verification": fact}


def _retain_verified_index(
    destination: Path, index: bytes, signature: bytes
) -> Dict[str, str]:
    index_digest = hashlib.sha256(index).hexdigest()
    signature_digest = hashlib.sha256(signature).hexdigest()
    retention_root = destination / INSTALL_DIRECTORY / "update-index"
    if retention_root.is_symlink() or (
        retention_root.exists() and not retention_root.is_dir()
    ):
        raise ProtocolRefusal(
            "update_index_storage_invalid",
            _draft(f"verified index storage parent is invalid at {retention_root}"),
        )
    try:
        retention_root.mkdir(exist_ok=True)
        if retention_root.resolve(strict=True) != retention_root:
            raise ProtocolRefusal(
                "update_index_storage_invalid",
                _draft(
                    f"verified index storage parent escapes the installation at {retention_root}"
                ),
            )
    except ProtocolRefusal:
        raise
    except OSError as exc:
        raise DurabilityFailure(
            "storage_unavailable",
            _draft(f"verified index storage parent is unavailable at {retention_root}"),
        ) from exc
    directory = retention_root / index_digest
    if directory.is_symlink():
        raise ProtocolRefusal(
            "update_index_storage_invalid",
            _draft(f"verified index storage is symlinked at {directory}"),
        )
    try:
        directory.mkdir(exist_ok=True)
        for name, payload in (
            ("release-index.v0.json", index),
            ("release-index.v0.json.minisig", signature),
        ):
            path = directory / name
            if path.exists():
                if path.is_symlink() or path.read_bytes() != payload:
                    raise ProtocolRefusal(
                        "update_index_storage_conflict",
                        _draft(f"verified index digest path conflicts at {path}"),
                    )
                continue
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                if os.write(descriptor, payload) != len(payload):
                    raise OSError("short verified-index write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except ProtocolRefusal:
        raise
    except OSError as exc:
        raise DurabilityFailure(
            "storage_unavailable",
            _draft(f"verified release-index bytes could not be retained at {directory}"),
        ) from exc
    return {
        "index_sha256": index_digest,
        "signature_sha256": signature_digest,
        "directory": str(directory.relative_to(destination)),
    }


def _validate_observation(row: Dict[str, object]) -> Dict[str, object]:
    required = {
        "schema_version",
        "id",
        "kind",
        "destination",
        "channel",
        "consent_receipt_id",
        "public_key_sha256",
        "index_sha256",
        "signature_sha256",
        "index_version",
        "latest_version",
        "observed_version",
        "installed_source_sha",
        "latest_source_sha",
        "bundle_filename",
        "bundle_url",
        "bundle_sha256",
        "bundle_size",
        "request_count",
        "state",
        "idempotency_key",
        "timestamp",
        "retained_index_directory",
    }
    if (
        set(row) != required
        or row.get("schema_version") != 0
        or row.get("kind") != "update_observation"
        or row.get("state") not in {"current", "available"}
        or row.get("request_count") != 1
        or not isinstance(row.get("id"), str)
        or not isinstance(row.get("idempotency_key"), str)
    ):
        raise IntegrityFailure(
            "update_observation_invalid",
            _draft("update observation ledger contains an invalid record"),
        )
    return row


def _validate_application(row: Dict[str, object]) -> Dict[str, object]:
    if (
        set(row) != _APPLICATION_FIELDS
        or row.get("schema_version") != 0
        or row.get("kind") != "update_application"
        or row.get("verification_state") != "signature_verified"
        or row.get("request_count") != 1
        or not isinstance(row.get("id"), str)
        or not isinstance(row.get("idempotency_key"), str)
        or not isinstance(row.get("check_observation_id"), str)
        or not isinstance(row.get("source_sha"), str)
        or _SOURCE_SHA.fullmatch(str(row.get("source_sha"))) is None
        or not isinstance(row.get("previous_source_sha"), str)
        or _SOURCE_SHA.fullmatch(str(row.get("previous_source_sha"))) is None
        or not isinstance(row.get("bundle_sha256"), str)
        or _SHA256.fullmatch(str(row.get("bundle_sha256"))) is None
    ):
        raise IntegrityFailure(
            "update_application_invalid",
            _draft("update observation ledger contains an invalid application record"),
        )
    return row


def validate_observation_ledger_row(
    row: Dict[str, object],
) -> Dict[str, object]:
    if row.get("kind") == "update_observation":
        return _validate_observation(row)
    if row.get("kind") == "update_application":
        return _validate_application(row)
    raise IntegrityFailure(
        "update_observation_invalid",
        _draft("update observation ledger contains an unknown record kind"),
    )


def check_for_updates(
    *,
    destination: Path,
    channel: str,
    entrypoint: Path,
    idempotency_key: str,
) -> Dict[str, object]:
    selected = canonical_destination(destination)
    selected_channel = validate_update_channel(channel)
    key = validate_idempotency_key(idempotency_key)
    ownership = require_standalone_ownership(selected, entrypoint=Path(entrypoint))
    consent_ledger = UpdateConsentLedger(selected)
    consent = consent_ledger.require_active(selected_channel)
    observations = selected / INSTALL_DIRECTORY / OBSERVATION_LEDGER

    def decide(rows: list[Dict[str, object]]):
        validated = [validate_observation_ledger_row(row) for row in rows]
        prior = next(
            (
                row
                for row in reversed(validated)
                if row["kind"] == "update_observation"
                and row["idempotency_key"] == key
            ),
            None,
        )
        coordinate = (
            str(selected),
            selected_channel,
            consent["id"],
            consent["public_key_sha256"],
        )
        if prior is not None:
            observed = (
                prior["destination"],
                prior["channel"],
                prior["consent_receipt_id"],
                prior["public_key_sha256"],
            )
            if observed != coordinate:
                raise ProtocolRefusal(
                    "update_check_idempotency_conflict",
                    _draft("update check key already names a different coordinate"),
                )
            return prior, None

        # Importing this module is itself gated by current consent and ownership.
        from . import update_transport

        envelope = update_transport.fetch_one_https(
            selected_channel, max_bytes=ENVELOPE_MAX_BYTES
        )
        index_bytes, signature_bytes = decode_release_index_envelope(envelope)
        verified = verify_release_index(selected, index_bytes, signature_bytes)
        # Consent is checked again before durable observation of network-derived bytes.
        current_consent = consent_ledger.require_active(selected_channel)
        if current_consent["id"] != consent["id"]:
            raise ProtocolRefusal(
                "update_consent_changed",
                _draft(
                    f"update consent changed during the check in {consent_ledger.path}"
                ),
                remedy=_draft("run a new explicit check under the current consent"),
            )
        retained = _retain_verified_index(selected, index_bytes, signature_bytes)
        index = verified["index"]
        releases = list(index["releases"])
        latest = next(
            release
            for release in releases
            if release["version"] == index["latest_version"]
        )
        installed_sha = str(ownership["source_sha"])
        installed_match = next(
            (
                release
                for release in releases
                if release["source_sha"] == installed_sha
            ),
            None,
        )
        state = "current" if latest["source_sha"] == installed_sha else "available"
        row: Dict[str, object] = {
            "schema_version": 0,
            "id": "update-observation-" + uuid7_hex(),
            "kind": "update_observation",
            "destination": str(selected),
            "channel": selected_channel,
            "consent_receipt_id": consent["id"],
            "public_key_sha256": consent["public_key_sha256"],
            "index_sha256": retained["index_sha256"],
            "signature_sha256": retained["signature_sha256"],
            "index_version": index["index_version"],
            "latest_version": index["latest_version"],
            "observed_version": (
                installed_match["version"] if installed_match is not None else None
            ),
            "installed_source_sha": installed_sha,
            "latest_source_sha": latest["source_sha"],
            "bundle_filename": latest["bundle_filename"],
            "bundle_url": latest["bundle_url"],
            "bundle_sha256": latest["bundle_sha256"],
            "bundle_size": latest["bundle_size"],
            "request_count": 1,
            "state": state,
            "idempotency_key": key,
            "timestamp": _timestamp(),
            "retained_index_directory": retained["directory"],
        }
        return row, row

    return _transact_jsonl(observations, decide)
