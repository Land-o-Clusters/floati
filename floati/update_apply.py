"""Verified one-path application and rollback of signed Floati bundles."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Dict, Sequence

from .deploy import DeploymentWriter
from .errors import IntegrityFailure, ProtocolRefusal
from .fleet_update import _explicit_executable
from .ids import uuid7_hex
from .manifest import verify_manifest
from .update_check import (
    OBSERVATION_LEDGER,
    validate_release_index,
    validate_observation_ledger_row,
)
from .update_consent import (
    INSTALL_DIRECTORY,
    UpdateConsentLedger,
    _draft,
    _timestamp,
    _transact_jsonl,
    canonical_destination,
    validate_idempotency_key,
    validate_update_channel,
)
from .update_ownership import (
    observe_install_ownership,
    require_standalone_ownership,
)


_BUNDLE_MAX_BYTES = 64 * 1024 * 1024
_GIT_CANDIDATES = ("/usr/bin/git", "/bin/git")


def _select_git_executable(explicit: str | Path | None = None) -> str:
    code = "update_git_unavailable"
    if explicit is not None:
        try:
            return _explicit_executable(explicit, code)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                code,
                _draft(
                    f"Git executable is unavailable at operator-declared path: {explicit}"
                ),
            ) from exc
    for candidate in _GIT_CANDIDATES:
        path = Path(candidate)
        if not path.exists() and not path.is_symlink():
            continue
        try:
            return _explicit_executable(candidate, code)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                code,
                _draft(f"Git executable candidate is not usable: {candidate}"),
            ) from exc
    raise ProtocolRefusal(
        code,
        _draft(
            "Git executable is absent from fixed candidates: "
            + ", ".join(_GIT_CANDIDATES)
        ),
    )


def _same_identity(path: Path, expected: tuple[int, int]) -> bool:
    try:
        fact = path.stat()
    except OSError:
        return False
    return not path.is_symlink() and (fact.st_dev, fact.st_ino) == expected


def _retained_index(
    destination: Path, observation: Dict[str, object]
) -> tuple[bytes, bytes]:
    relative = observation["retained_index_directory"]
    if not isinstance(relative, str):
        raise IntegrityFailure(
            "update_observation_invalid",
            _draft("update observation retained-index coordinate is invalid"),
        )
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise IntegrityFailure(
            "update_observation_invalid",
            _draft("update observation retained-index coordinate escapes the installation"),
        )
    directory = destination.joinpath(*pure.parts)
    try:
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or directory.resolve(strict=True) != directory
        ):
            raise OSError("not one canonical retained-index directory")
        index_path = directory / "release-index.v0.json"
        signature_path = directory / "release-index.v0.json.minisig"
        if (
            index_path.is_symlink()
            or signature_path.is_symlink()
            or not index_path.is_file()
            or not signature_path.is_file()
        ):
            raise OSError("retained index members are unavailable")
        index = index_path.read_bytes()
        signature = signature_path.read_bytes()
    except OSError as exc:
        raise IntegrityFailure(
            "update_index_storage_invalid",
            _draft(f"retained verified release-index bytes are unavailable at {directory}"),
        ) from exc
    if (
        hashlib.sha256(index).hexdigest() != observation["index_sha256"]
        or hashlib.sha256(signature).hexdigest() != observation["signature_sha256"]
    ):
        raise IntegrityFailure(
            "update_index_storage_invalid",
            _draft("retained verified release-index bytes no longer match their observation"),
        )
    return index, signature


def _git(
    executable: str, arguments: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    executable_directory = str(Path(executable).resolve().parent)
    try:
        return subprocess.run(
            [executable, *arguments],
            cwd=cwd,
            env={
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": "/var/empty",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": os.pathsep.join(
                    dict.fromkeys((executable_directory, "/usr/bin", "/bin"))
                ),
                "XDG_CONFIG_HOME": "/var/empty",
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtocolRefusal(
            "update_bundle_invalid",
            _draft("Git could not inspect the downloaded bundle"),
        ) from exc


def _require_git_success(
    completed: subprocess.CompletedProcess[str], code: str
) -> None:
    if completed.returncode != 0:
        raw = completed.stderr.strip() or completed.stdout.strip()
        detail = "Git refused the bundle: " + raw if raw else "Git refused the bundle"
        raise ProtocolRefusal(code, _draft(detail))


def _stage_bundle(
    bundle: bytes, source_sha: str, *, git_executable: str
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    git = git_executable
    temporary = tempfile.TemporaryDirectory(prefix="floati-update-apply-")
    root = Path(temporary.name).resolve(strict=True)
    bundle_path = root / "release.bundle"
    checkout = root / "checkout"
    verifier = root / "verifier.git"
    try:
        bundle_path.write_bytes(bundle)
        _require_git_success(
            _git(git, ("init", "--quiet", "--bare", str(verifier)), cwd=root),
            "update_bundle_invalid",
        )
        _require_git_success(
            _git(git, ("-C", str(verifier), "bundle", "verify", str(bundle_path)), cwd=root),
            "update_bundle_invalid",
        )
        _require_git_success(
            _git(git, ("clone", "--quiet", "--no-checkout", str(bundle_path), str(checkout)), cwd=root),
            "update_bundle_invalid",
        )
        resolved = _git(
            git,
            ("-C", str(checkout), "rev-parse", "--verify", f"{source_sha}^{{commit}}"),
            cwd=root,
        )
        if resolved.returncode != 0 or resolved.stdout.strip() != source_sha:
            raise ProtocolRefusal(
                "update_bundle_source_mismatch",
                _draft("downloaded bundle does not contain the signed source commit"),
            )
        _require_git_success(
            _git(git, ("-C", str(checkout), "checkout", "--quiet", "--detach", source_sha), cwd=root),
            "update_bundle_source_mismatch",
        )
        head = _git(
            git,
            ("-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=root,
        )
        if head.returncode != 0 or head.stdout.strip() != source_sha:
            raise ProtocolRefusal(
                "update_bundle_source_mismatch",
                _draft("staged checkout does not equal the signed source commit"),
            )
        errors = verify_manifest(checkout, git_executable=git)
        if errors:
            raise ProtocolRefusal(
                "update_bundle_manifest_invalid",
                _draft("; ".join(errors)),
            )
    except Exception:
        temporary.cleanup()
        raise
    return temporary, checkout


def apply_update(
    *,
    destination: Path,
    channel: str,
    entrypoint: Path,
    version: str,
    idempotency_key: str,
    git_executable: str | Path | None = None,
) -> Dict[str, object]:
    """Apply or roll back through the same retained-index and bundle checks."""

    selected = canonical_destination(destination)
    selected_channel = validate_update_channel(channel)
    key = validate_idempotency_key(idempotency_key)
    if not isinstance(version, str) or not version or len(version) > 128:
        raise ProtocolRefusal(
            "update_version_invalid", _draft("update version must be one exact identifier")
        )
    selected_entrypoint = Path(entrypoint)
    ownership = require_standalone_ownership(
        selected, entrypoint=selected_entrypoint
    )
    consent_ledger = UpdateConsentLedger(selected)
    consent = consent_ledger.require_active(selected_channel)
    stat = selected.stat()
    destination_identity = (stat.st_dev, stat.st_ino)
    observations = selected / INSTALL_DIRECTORY / OBSERVATION_LEDGER

    def decide(rows: list[Dict[str, object]]):
        validated = [validate_observation_ledger_row(row) for row in rows]
        prior = next(
            (
                row
                for row in reversed(validated)
                if row["kind"] == "update_application"
                and row["idempotency_key"] == key
            ),
            None,
        )
        coordinate = (str(selected), selected_channel, version)
        if prior is not None:
            observed = (prior["destination"], prior["channel"], prior["version"])
            if observed != coordinate:
                raise ProtocolRefusal(
                    "update_apply_idempotency_conflict",
                    _draft("update apply key already names a different coordinate"),
                )
            return prior, None

        check = next(
            (
                row
                for row in reversed(validated)
                if row["kind"] == "update_observation"
                and row["destination"] == str(selected)
                and row["channel"] == selected_channel
                and row["consent_receipt_id"] == consent["id"]
                and row["public_key_sha256"] == consent["public_key_sha256"]
            ),
            None,
        )
        if check is None:
            raise ProtocolRefusal(
                "update_check_missing",
                _draft("no signed update check exists for the current consent coordinate"),
                remedy=_draft("run a new explicit update check before apply"),
            )
        index_bytes, signature_bytes = _retained_index(selected, check)
        index = validate_release_index(index_bytes)
        release = next(
            (row for row in index["releases"] if row["version"] == version),
            None,
        )
        if release is None:
            raise ProtocolRefusal(
                "update_version_unavailable",
                _draft(f"signed release index does not contain version {version}"),
            )

        selected_git = _select_git_executable(git_executable)

        # The transport module remains unreachable until ownership, consent,
        # retained signature evidence, and the requested release all verify.
        from . import update_transport

        try:
            bundle = update_transport.fetch_one_https(
                str(release["bundle_url"]), max_bytes=_BUNDLE_MAX_BYTES
            )
        except ProtocolRefusal as exc:
            if exc.code == "update_envelope_too_large":
                raise ProtocolRefusal(
                    "update_bundle_size_mismatch",
                    _draft("downloaded bundle exceeds the signed bundle size bound"),
                ) from exc
            raise
        if len(bundle) != release["bundle_size"]:
            raise ProtocolRefusal(
                "update_bundle_size_mismatch",
                _draft("downloaded bundle size does not equal the signed bundle size"),
            )
        digest = hashlib.sha256(bundle).hexdigest()
        if digest != release["bundle_sha256"]:
            raise ProtocolRefusal(
                "update_bundle_digest_mismatch",
                _draft("downloaded bundle digest does not equal the signed bundle digest"),
            )

        temporary, staged = _stage_bundle(
            bundle,
            str(release["source_sha"]),
            git_executable=selected_git,
        )
        try:
            try:
                current_consent = consent_ledger.require_active(selected_channel)
            except ProtocolRefusal as exc:
                raise ProtocolRefusal(
                    "update_consent_changed",
                    _draft("update consent changed after bundle download"),
                    remedy=_draft("run a new explicit check under the current consent"),
                ) from exc
            if current_consent["id"] != consent["id"]:
                raise ProtocolRefusal(
                    "update_consent_changed",
                    _draft("update consent changed after bundle download"),
                    remedy=_draft("run a new explicit check under the current consent"),
                )
            try:
                current_ownership = observe_install_ownership(
                    selected, entrypoint=selected_entrypoint
                )
            except ProtocolRefusal as exc:
                raise ProtocolRefusal(
                    "update_ownership_changed",
                    _draft("installation ownership changed after bundle download"),
                ) from exc
            if current_ownership != ownership:
                raise ProtocolRefusal(
                    "update_ownership_changed",
                    _draft("installation ownership changed after bundle download"),
                )
            if not _same_identity(selected, destination_identity):
                raise ProtocolRefusal(
                    "update_destination_changed",
                    _draft("update destination identity changed after bundle download"),
                )

            installer_path = os.pathsep.join(
                (str(selected / "scripts"), str(Path(selected_git).parent))
            )
            outcome = DeploymentWriter(
                staged,
                selected,
                "update",
                ref="HEAD",
                committed_tree=True,
                installer_path=installer_path,
                git_executable=selected_git,
            ).run()
            receipt: Dict[str, object] = {
                "schema_version": 0,
                "id": "update-application-" + uuid7_hex(),
                "kind": "update_application",
                "destination": str(selected),
                "channel": selected_channel,
                "consent_receipt_id": consent["id"],
                "public_key_sha256": consent["public_key_sha256"],
                "check_observation_id": check["id"],
                "index_sha256": check["index_sha256"],
                "signature_sha256": check["signature_sha256"],
                "index_version": index["index_version"],
                "version": version,
                "bundle_filename": release["bundle_filename"],
                "bundle_url": release["bundle_url"],
                "bundle_sha256": digest,
                "bundle_size": len(bundle),
                "previous_source_sha": ownership["source_sha"],
                "source_sha": release["source_sha"],
                "verification_state": "signature_verified",
                "request_count": 1,
                "idempotency_key": key,
                "timestamp": _timestamp(),
                "wiring_journal": outcome["wiring_journal"],
            }
            return receipt, receipt
        finally:
            temporary.cleanup()

    return _transact_jsonl(observations, decide)
