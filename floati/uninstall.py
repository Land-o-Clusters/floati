"""Manifest-exact, data-retaining removal of an installed Floati bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Sequence, Tuple

from .errors import ProtocolRefusal
from .storage_identity import INSTALL_METADATA_DIRECTORY


_METADATA_RELATIVE = PurePosixPath(INSTALL_METADATA_DIRECTORY, "manifest.v0.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_DATA_NOTICE = (
    "Bus roots and ledgers are retained; uninstall removes tool files only."
)


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_regular(path: Path, code: str, detail: str) -> Tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProtocolRefusal(code, detail) from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            raise ProtocolRefusal(code, detail)
        chunks: List[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), identity
    finally:
        os.close(descriptor)


def _owned_tool_path(relative: PurePosixPath) -> bool:
    value = relative.as_posix()
    return (
        value == "LICENSE"
        or value in {"scripts/floati", "scripts/floati-codex-wait"}
        or value.startswith("floati/")
        or value.startswith("schemas/")
        or value.startswith("bundle/")
        or value.startswith("roles/shipped/")
    )


def _validate_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise ProtocolRefusal("uninstall_manifest_invalid", "owned path must be text")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
        or not _owned_tool_path(relative)
    ):
        raise ProtocolRefusal(
            "uninstall_manifest_invalid",
            f"manifest path is outside the Floati tool bundle: {value}",
        )
    return relative


def _path_has_symlink(destination: Path, relative: PurePosixPath) -> bool:
    current = destination
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


class UninstallWriter:
    """Remove only unchanged files named by exact install ownership metadata."""

    def __init__(self, destination: os.PathLike[str] | str, *, dry_run: bool = False) -> None:
        self.destination_arg = destination
        self.dry_run = dry_run

    def _destination(self) -> Path:
        destination = Path(self.destination_arg).expanduser()
        if not destination.is_absolute():
            raise ProtocolRefusal(
                "uninstall_destination_absolute_required",
                "destination must be absolute",
            )
        if destination.is_symlink():
            raise ProtocolRefusal(
                "uninstall_destination_symlinked",
                "destination must not be a symlink",
            )
        if not destination.is_dir():
            raise ProtocolRefusal(
                "uninstall_destination_missing",
                "destination must be an existing installation directory",
            )
        if not isinstance(self.dry_run, bool):
            raise ProtocolRefusal("uninstall_mode_invalid", "dry_run must be boolean")
        return destination.resolve()

    def _load_manifest(
        self, destination: Path
    ) -> Tuple[List[Dict[str, str]], Dict[str, os.stat_result], Dict[str, Any]]:
        metadata_directory = destination / INSTALL_METADATA_DIRECTORY
        metadata = destination.joinpath(*_METADATA_RELATIVE.parts)
        if metadata_directory.is_symlink() or metadata.is_symlink():
            raise ProtocolRefusal(
                "uninstall_manifest_invalid",
                "install ownership metadata must not be a symlink",
            )
        if not metadata.is_file():
            raise ProtocolRefusal(
                "uninstall_manifest_missing",
                "exact install ownership metadata is required; nothing was removed",
            )
        raw, metadata_identity = _read_regular(
            metadata,
            "uninstall_manifest_invalid",
            "install ownership metadata is not a readable regular file",
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolRefusal(
                "uninstall_manifest_invalid",
                "install ownership metadata is unreadable",
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "source_ref", "source_sha", "files"
        }:
            raise ProtocolRefusal(
                "uninstall_manifest_invalid",
                "install ownership metadata has an unexpected shape",
            )
        if payload["schema_version"] != 0 or isinstance(payload["schema_version"], bool):
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install ownership schema is unsupported"
            )
        if not isinstance(payload["source_ref"], str) or not payload["source_ref"]:
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install source ref is invalid"
            )
        if not isinstance(payload["source_sha"], str) or not _SOURCE_SHA.fullmatch(
            payload["source_sha"]
        ):
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install source SHA is invalid"
            )
        if not isinstance(payload["files"], list) or not payload["files"]:
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install ownership file set is empty"
            )

        entries: List[Dict[str, str]] = []
        identities: Dict[str, os.stat_result] = {}
        seen = set()
        for item in payload["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise ProtocolRefusal(
                    "uninstall_manifest_invalid", "install ownership entry is invalid"
                )
            relative = _validate_relative(item["path"])
            relative_text = relative.as_posix()
            digest = item["sha256"]
            if relative_text in seen:
                raise ProtocolRefusal(
                    "uninstall_manifest_invalid", f"duplicate owned path: {relative_text}"
                )
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ProtocolRefusal(
                    "uninstall_manifest_invalid", f"invalid owned digest: {relative_text}"
                )
            if _path_has_symlink(destination, relative):
                raise ProtocolRefusal(
                    "uninstall_manifest_mismatch",
                    f"owned path traverses or is a symlink: {relative_text}",
                )
            path = destination.joinpath(*relative.parts)
            current, identity = _read_regular(
                path,
                "uninstall_manifest_mismatch",
                f"owned file is missing or not regular: {relative_text}",
            )
            if _digest_bytes(current) != digest:
                raise ProtocolRefusal(
                    "uninstall_manifest_mismatch",
                    f"owned file digest changed: {relative_text}",
                )
            seen.add(relative_text)
            entries.append({"path": relative_text, "sha256": digest})
            identities[relative_text] = identity

        if [entry["path"] for entry in entries] != sorted(seen):
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install ownership paths are not ordered"
            )
        metadata_digest = _digest_bytes(raw)
        metadata_receipt = {
            "path": _METADATA_RELATIVE.as_posix(),
            "sha256": metadata_digest,
        }
        identities[_METADATA_RELATIVE.as_posix()] = metadata_identity
        return entries, identities, metadata_receipt

    @staticmethod
    def _same_identity(
        path: Path, expected: os.stat_result, expected_digest: str
    ) -> bool:
        try:
            payload, current = _read_regular(
                path,
                "uninstall_manifest_mismatch",
                "owned file changed before removal",
            )
        except ProtocolRefusal:
            return False
        return (
            stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino, current.st_size)
            == (expected.st_dev, expected.st_ino, expected.st_size)
            and _digest_bytes(payload) == expected_digest
        )

    @staticmethod
    def _foreign_files(destination: Path, owned: Sequence[str]) -> List[str]:
        owned_set = set(owned)
        foreign: List[str] = []
        for path in destination.rglob("*"):
            relative = path.relative_to(destination).as_posix()
            if relative.startswith(INSTALL_METADATA_DIRECTORY + "/"):
                continue
            if relative in owned_set:
                continue
            if path.is_file() or path.is_symlink():
                foreign.append(relative)
        return sorted(foreign)

    @staticmethod
    def _remove_empty_owned_directories(destination: Path, entries: Sequence[str]) -> None:
        candidates = set()
        for value in entries:
            current = PurePosixPath(value).parent
            while current.parts and current != PurePosixPath("."):
                candidates.add(current)
                current = current.parent
        for relative in sorted(candidates, key=lambda value: len(value.parts), reverse=True):
            path = destination.joinpath(*relative.parts)
            try:
                path.rmdir()
            except OSError:
                continue

    def run(self) -> Dict[str, Any]:
        destination = self._destination()
        entries, identities, metadata_receipt = self._load_manifest(destination)
        owned_paths = [entry["path"] for entry in entries]
        receipts = [*entries, metadata_receipt]
        foreign = self._foreign_files(destination, owned_paths)
        if self.dry_run:
            return {
                "destination": str(destination),
                "dry_run": True,
                "removal_receipts": receipts,
                "removed_count": 0,
                "foreign_preserved": foreign,
                "data_retention_notice": _DATA_NOTICE,
            }

        for entry in entries:
            relative = PurePosixPath(entry["path"])
            path = destination.joinpath(*relative.parts)
            if not self._same_identity(
                path, identities[entry["path"]], entry["sha256"]
            ):
                raise ProtocolRefusal(
                    "uninstall_manifest_mismatch",
                    f"owned file identity changed before removal: {entry['path']}",
                )
            path.unlink()

        metadata = destination.joinpath(*_METADATA_RELATIVE.parts)
        if not self._same_identity(
            metadata,
            identities[_METADATA_RELATIVE.as_posix()],
            metadata_receipt["sha256"],
        ):
            raise ProtocolRefusal(
                "uninstall_manifest_mismatch",
                "install ownership metadata changed before removal",
            )
        metadata.unlink()
        try:
            (destination / INSTALL_METADATA_DIRECTORY).rmdir()
        except OSError:
            pass
        self._remove_empty_owned_directories(destination, owned_paths)
        return {
            "destination": str(destination),
            "dry_run": False,
            "removal_receipts": receipts,
            "removed_count": len(receipts),
            "foreign_preserved": foreign,
            "data_retention_notice": _DATA_NOTICE,
        }


def _handle(args: argparse.Namespace) -> Tuple[str, Dict[str, Any], int]:
    evidence = UninstallWriter(args.destination, dry_run=args.dry_run).run()
    return "ok", evidence, 0


def register_cli(commands: argparse._SubParsersAction) -> None:
    """Register the dark command; the integration train calls this once from cli.py."""
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--destination", required=True)
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument("--json", action="store_true")
    uninstall.set_defaults(handler=_handle)
