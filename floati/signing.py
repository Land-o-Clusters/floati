"""Minisign shell-out for bounded offline artifact testimony."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .errors import DurabilityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .root import FloatiRoot, validate_identifier


_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_MAX_SIGNATURE_BYTES = 64 * 1024
_MINISIGN_TIMEOUT_SECONDS = 30.0


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise _DuplicateKey(key)
        value[key] = member
    return value


def _binding(
    artifact: Path,
    version: str,
    journal_id: Optional[str],
    through_seq: Optional[int],
) -> Dict[str, object]:
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ProtocolRefusal(
            "signature_version_invalid",
            "signature version must be one bounded release identifier",
        )
    if (journal_id is None) != (through_seq is None):
        raise ProtocolRefusal(
            "signature_journal_binding_incomplete",
            "journal_id and through_seq must be supplied together",
        )
    value: Dict[str, object] = {
        "filename": artifact.name,
        "version": version,
    }
    if journal_id is not None:
        value["journal_id"] = validate_identifier(journal_id, "journal_id")
        if (
            not isinstance(through_seq, int)
            or isinstance(through_seq, bool)
            or not 1 <= through_seq <= 2**63 - 1
        ):
            raise ProtocolRefusal(
                "signature_through_seq_invalid",
                "signature through_seq is outside its journal bounds",
            )
        value["through_seq"] = through_seq
    return value


def _comment(binding: Dict[str, object]) -> str:
    return json.dumps(
        binding,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _minisign() -> Optional[Path]:
    selected = shutil.which("minisign")
    if selected is None:
        return None
    try:
        resolved = Path(selected).resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return resolved


def _ordinary(path: Path, code: str, detail: str) -> Path:
    try:
        if path.is_symlink() or path.resolve(strict=True) != path or not path.is_file():
            raise OSError("not one canonical ordinary file")
    except OSError as exc:
        raise ProtocolRefusal(code, detail) from exc
    return path


def _directory_relative(directory: Path, relative: Path) -> Path:
    base = Path(directory)
    if not base.is_absolute() or base.is_symlink() or not base.is_dir():
        raise ProtocolRefusal(
            "signature_root_invalid",
            "signature verification requires one absolute ordinary directory",
        )
    candidate = Path(relative)
    if candidate.is_absolute() or any(
        part in ("", ".", "..") for part in candidate.parts
    ):
        raise ProtocolRefusal(
            "path_not_contained",
            "path must remain relative to the explicit signature root",
        )
    resolved_base = base.resolve(strict=True)
    resolved = (resolved_base / candidate).resolve(strict=False)
    try:
        resolved.relative_to(resolved_base)
    except ValueError as exc:
        raise ProtocolRefusal(
            "path_not_contained",
            "path escapes the explicit signature root",
        ) from exc
    return resolved


def _environment() -> Dict[str, str]:
    return {
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DurabilityFailure(
            "storage_unavailable", f"{path.name} could not be measured"
        ) from exc


def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            check=False,
            env=_environment(),
            timeout=_MINISIGN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProtocolRefusal(
            "signature_tool_timeout", "minisign did not finish within 30 seconds"
        ) from exc
    except OSError as exc:
        raise ProtocolRefusal(
            "signature_tool_unavailable", "the resolved minisign executable could not run"
        ) from exc


def sign_minisign(
    root: FloatiRoot,
    artifact_relative: Path,
    signature_relative: Path,
    *,
    secret_key: Path,
    version: str,
    journal_id: Optional[str] = None,
    through_seq: Optional[int] = None,
) -> Dict[str, object]:
    """Create one Minisign signature; key generation is deliberately absent."""

    artifact = _ordinary(
        root.resolve_relative(artifact_relative),
        "signature_artifact_missing",
        "the selected artifact is not one ordinary file inside the root",
    )
    signature = root.resolve_relative(signature_relative)
    if signature == artifact:
        raise ProtocolRefusal(
            "signature_path_invalid", "a signature cannot replace its artifact"
        )
    key = Path(secret_key)
    if not key.is_absolute():
        raise ProtocolRefusal(
            "signature_key_path_invalid", "the release secret key path must be absolute"
        )
    key = _ordinary(
        key,
        "signature_key_unavailable",
        "the release secret key is not one canonical ordinary file",
    )
    try:
        key.relative_to(root.tenant_home)
    except ValueError:
        pass
    else:
        raise ProtocolRefusal(
            "signature_key_location_invalid",
            "a release secret key must remain outside the explicit Floati root",
        )
    binding = _binding(artifact, version, journal_id, through_seq)
    tool = _minisign()
    if tool is None:
        raise ProtocolRefusal(
            "signature_tool_absent", "install minisign before signing an artifact"
        )
    temporary = signature.with_name("." + signature.name + ".tmp-" + uuid7_hex())
    try:
        signature.parent.mkdir(parents=True, exist_ok=True)
        completed = _run(
            [
                os.fspath(tool),
                "-S",
                "-s",
                os.fspath(key),
                "-x",
                os.fspath(temporary),
                "-t",
                _comment(binding),
                "-m",
                os.fspath(artifact),
            ]
        )
        if completed.returncode != 0:
            raise ProtocolRefusal(
                "signature_signing_failed",
                "minisign did not create the requested detached signature",
            )
        if temporary.is_symlink() or not temporary.is_file():
            raise ProtocolRefusal(
                "signature_output_invalid", "minisign did not create one ordinary signature"
            )
        size = temporary.stat().st_size
        if not 1 <= size <= _MAX_SIGNATURE_BYTES:
            raise ProtocolRefusal(
                "signature_output_invalid", "the detached signature is outside its size bound"
            )
        os.replace(temporary, signature)
        descriptor = os.open(signature.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except ProtocolRefusal:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DurabilityFailure(
            "storage_unavailable", "the detached signature could not be stored"
        ) from exc
    return {
        "state": "signature_signed",
        "tool": "minisign",
        "artifact": str(artifact_relative),
        "signature": str(signature_relative),
        "binding": binding,
        "artifact_sha256": _digest(artifact),
        "signature_sha256": _digest(signature),
    }


def verify_minisign(
    root: FloatiRoot,
    artifact_relative: Path,
    signature_relative: Path,
    public_key_relative: Path,
    *,
    version: str,
    journal_id: Optional[str] = None,
    through_seq: Optional[int] = None,
) -> Dict[str, object]:
    """Verify files beneath one explicit Floati root."""

    return verify_minisign_paths(
        root.tenant_home,
        artifact_relative,
        signature_relative,
        public_key_relative,
        version=version,
        journal_id=journal_id,
        through_seq=through_seq,
    )


def verify_minisign_paths(
    directory: Path,
    artifact_relative: Path,
    signature_relative: Path,
    public_key_relative: Path,
    *,
    version: str,
    journal_id: Optional[str] = None,
    through_seq: Optional[int] = None,
) -> Dict[str, object]:
    """Verify exact bytes beneath one explicit ordinary directory."""

    artifact = _ordinary(
        _directory_relative(directory, artifact_relative),
        "signature_artifact_missing",
        "the selected artifact is not one ordinary file inside the root",
    )
    signature = _ordinary(
        _directory_relative(directory, signature_relative),
        "signature_missing",
        "the selected detached signature is not one ordinary file inside the root",
    )
    public_key = _ordinary(
        _directory_relative(directory, public_key_relative),
        "signature_public_key_missing",
        "the pinned Minisign public key is not one ordinary file inside the root",
    )
    expected = _binding(artifact, version, journal_id, through_seq)
    tool = _minisign()
    if tool is None:
        return {
            "state": "signature_unverified",
            "reason": "tool_absent",
            "tool": "minisign",
            "artifact": str(artifact_relative),
            "signature": str(signature_relative),
            "public_key": str(public_key_relative),
            "binding": expected,
        }
    completed = _run(
        [
            os.fspath(tool),
            "-V",
            "-H",
            "-Q",
            "-p",
            os.fspath(public_key),
            "-x",
            os.fspath(signature),
            "-m",
            os.fspath(artifact),
        ]
    )
    if completed.returncode != 0:
        raise ProtocolRefusal(
            "signature_invalid", "minisign refused the artifact or detached signature"
        )
    try:
        text = completed.stdout.decode("utf-8")
        if len(text.splitlines()) != 1 or not text.endswith("\n"):
            raise ValueError("trusted comment is not one complete line")
        observed = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, _DuplicateKey, ValueError) as exc:
        raise ProtocolRefusal(
            "signature_binding_invalid",
            "the verified trusted comment is not one strict JSON binding",
        ) from exc
    if not isinstance(observed, dict) or observed != expected:
        raise ProtocolRefusal(
            "signature_binding_mismatch",
            "the signed filename/version/journal binding does not match the request",
        )
    return {
        "state": "signature_verified",
        "tool": "minisign",
        "artifact": str(artifact_relative),
        "signature": str(signature_relative),
        "public_key": str(public_key_relative),
        "binding": expected,
        "artifact_sha256": _digest(artifact),
        "signature_sha256": _digest(signature),
        "public_key_sha256": _digest(public_key),
    }
