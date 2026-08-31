"""Read-only projection of explicit Floati installation ownership."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Dict, Mapping

from .errors import ProtocolRefusal
from .storage_identity import INSTALL_METADATA_DIRECTORY


_ENTRYPOINT = "scripts/floati"
_METADATA_NAME = "manifest.v0.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_OWNERSHIP_FIELDS = {
    "kind",
    "destination",
    "entrypoint",
    "entrypoint_sha256",
    "manager",
    "remedy",
}


def validate_install_ownership(value: object) -> Dict[str, object]:
    """Return one strict schema-v1 ownership record or refuse it."""

    if not isinstance(value, dict) or set(value) != _OWNERSHIP_FIELDS:
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install ownership metadata has an unexpected shape",
        )
    record = dict(value)
    kind = record["kind"]
    destination = record["destination"]
    entrypoint = record["entrypoint"]
    digest = record["entrypoint_sha256"]
    manager = record["manager"]
    remedy = record["remedy"]
    if kind not in {"floati_standalone", "package_manager", "unknown"}:
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install owner kind is unsupported",
        )
    if (
        not isinstance(destination, str)
        or not Path(destination).is_absolute()
        or str(Path(destination)) != destination
    ):
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install owner destination is not one canonical absolute path",
        )
    if not isinstance(entrypoint, str):
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install owner entrypoint is invalid",
        )
    relative = PurePosixPath(entrypoint)
    if (
        entrypoint != _ENTRYPOINT
        or relative.is_absolute()
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install owner entrypoint is outside the Floati bundle",
        )
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install owner entrypoint digest is invalid",
        )
    if kind == "floati_standalone":
        valid_owner = manager is None and remedy is None
    elif kind == "package_manager":
        valid_owner = (
            isinstance(manager, str)
            and bool(manager)
            and isinstance(remedy, str)
            and remedy.strip()
        )
    else:
        valid_owner = (
            manager is None
            and isinstance(remedy, str)
            and remedy.strip()
        )
    if not valid_owner:
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install owner manager/remedy binding is invalid",
        )
    return record


def _metadata(destination: Path) -> Dict[str, object]:
    metadata = destination / INSTALL_METADATA_DIRECTORY / _METADATA_NAME
    if metadata.parent.is_symlink() or metadata.is_symlink() or not metadata.is_file():
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "exact install ownership metadata is unavailable",
        )
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install ownership metadata is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install ownership metadata is not one object",
        )
    return payload


def _source_sha(payload: Mapping[str, object]) -> str:
    value = payload.get("source_sha")
    if not isinstance(value, str) or _SOURCE_SHA.fullmatch(value) is None:
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install ownership source SHA is invalid",
        )
    return value


def _mismatch(source_sha: str, reason: str) -> Dict[str, object]:
    return {"state": "mismatch", "reason": reason, "source_sha": source_sha}


def observe_install_ownership(
    destination: Path, *, entrypoint: Path
) -> Dict[str, object]:
    """Project installed ownership without guessing or opening a process."""

    selected = Path(destination)
    if (
        not selected.is_absolute()
        or selected.is_symlink()
        or not selected.is_dir()
    ):
        raise ProtocolRefusal(
            "update_ownership_destination_invalid",
            "install destination must be one absolute ordinary directory",
        )
    selected = selected.resolve(strict=True)
    payload = _metadata(selected)
    source_sha = _source_sha(payload)
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool):
        schema_version = None
    if schema_version == 0:
        if set(payload) != {"schema_version", "source_ref", "source_sha", "files"}:
            raise ProtocolRefusal(
                "update_ownership_metadata_invalid",
                "legacy install ownership metadata has an unexpected shape",
            )
        return {
            "state": "unknown",
            "reason": "legacy_receipt",
            "source_sha": source_sha,
        }
    if schema_version != 1 or set(payload) != {
        "schema_version",
        "source_ref",
        "source_sha",
        "files",
        "ownership",
    }:
        raise ProtocolRefusal(
            "update_ownership_metadata_invalid",
            "install ownership schema is unsupported",
        )
    ownership = validate_install_ownership(payload["ownership"])
    if ownership["destination"] != str(selected):
        return _mismatch(source_sha, "destination_mismatch")
    expected_entrypoint = selected / _ENTRYPOINT
    candidate = Path(entrypoint)
    try:
        ordinary = (
            candidate.is_absolute()
            and not candidate.is_symlink()
            and candidate.is_file()
            and candidate.resolve(strict=True) == candidate
        )
    except OSError:
        ordinary = False
    if not ordinary or candidate != expected_entrypoint:
        return _mismatch(source_sha, "entrypoint_mismatch")
    try:
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return _mismatch(source_sha, "entrypoint_unreadable")
    if digest != ownership["entrypoint_sha256"]:
        return _mismatch(source_sha, "entrypoint_digest_mismatch")
    return {"state": ownership["kind"], "source_sha": source_sha, **ownership}


def require_standalone_ownership(
    destination: Path, *, entrypoint: Path
) -> Dict[str, object]:
    """Require exact standalone ownership before any future transport step."""

    fact = observe_install_ownership(destination, entrypoint=entrypoint)
    if fact["state"] == "floati_standalone":
        return fact
    if fact["state"] == "package_manager":
        raise ProtocolRefusal(
            "update_package_manager_owned",
            "the installed Floati bundle is owned by a package manager",
            remedy=str(fact["remedy"]),
        )
    raise ProtocolRefusal(
        "update_ownership_" + str(fact["state"]),
        "standalone update ownership could not be proved",
        remedy="reinstall with the governed standalone installer",
    )
