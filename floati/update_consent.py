"""Destination-bound consent for explicit update checks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional, Tuple
from urllib.parse import urlsplit

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex


INSTALL_DIRECTORY = ".floati-install"
CONSENT_LEDGER = "update-consent.v0.jsonl"
_MAX_LEDGER_BYTES = 4 * 1024 * 1024
_MAX_RECORD_BYTES = 16 * 1024
_LOCK_TIMEOUT_SECONDS = 1.0
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RECORD_FIELDS = {
    "schema_version",
    "id",
    "kind",
    "destination",
    "channel",
    "public_key",
    "public_key_sha256",
    "epoch",
    "operation",
    "state",
    "predecessor_receipt_id",
    "idempotency_key",
    "timestamp",
}


def _draft(detail: str) -> str:
    # Voice pass 2026-08-29: shipped copy carries no provenance marker.
    return detail


def canonical_destination(destination: Path) -> Path:
    selected = Path(destination)
    try:
        if (
            not selected.is_absolute()
            or selected.is_symlink()
            or not selected.is_dir()
            or selected.resolve(strict=True) != selected
        ):
            raise OSError("not one canonical ordinary directory")
    except OSError as exc:
        raise ProtocolRefusal(
            "update_destination_invalid",
            _draft("update destination must be one canonical absolute directory"),
        ) from exc
    metadata = selected / INSTALL_DIRECTORY
    if metadata.is_symlink() or not metadata.is_dir():
        raise ProtocolRefusal(
            "update_install_metadata_missing",
            _draft(f"installed metadata is missing at {metadata}"),
            remedy=_draft("reinstall with the governed standalone installer"),
        )
    return selected


def validate_update_channel(channel: object) -> str:
    if not isinstance(channel, str) or not 1 <= len(channel) <= 2048:
        raise ProtocolRefusal(
            "update_channel_invalid",
            _draft("update channel must be one bounded exact HTTPS URL"),
        )
    try:
        parts = urlsplit(channel)
        port = parts.port
    except ValueError as exc:
        raise ProtocolRefusal(
            "update_channel_invalid",
            _draft("update channel is not a valid HTTPS URL"),
        ) from exc
    if (
        parts.scheme != "https"
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or not parts.path.startswith("/")
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in channel)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ProtocolRefusal(
            "update_channel_invalid",
            _draft(
                "update channel must be exact HTTPS with a host and path, without credentials or a fragment"
            ),
        )
    return channel


def validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise ProtocolRefusal(
            "idempotency_key_invalid",
            _draft("idempotency key must be terminal-safe and between 1 and 128 bytes"),
        )
    return value


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _durability(exc: OSError, path: Path) -> DurabilityFailure:
    return DurabilityFailure(
        "storage_unavailable",
        _draft(f"{path} could not be read or written: {exc.strerror or str(exc)}"),
    )


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    if path.is_symlink() or lock.is_symlink() or path.parent.is_symlink():
        raise ProtocolRefusal(
            "update_ledger_invalid",
            _draft(f"update ledger path is symlinked at {path}"),
        )
    try:
        handle = lock.open("a+b")
    except OSError as exc:
        raise _durability(exc, lock) from exc
    with handle:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProtocolRefusal(
                        "update_ledger_lock_timeout",
                        _draft(f"update ledger lock remained contended at {lock}"),
                    ) from exc
                time.sleep(0.01)
            except OSError as exc:
                raise _durability(exc, lock) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_jsonl(path: Path) -> list[Dict[str, object]]:
    if not path.exists():
        return []
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise _durability(exc, path) from exc
    if len(payload) > _MAX_LEDGER_BYTES:
        raise IntegrityFailure(
            "update_ledger_too_large", _draft(f"update ledger exceeds its bound at {path}")
        )
    if payload and not payload.endswith(b"\n"):
        raise IntegrityFailure(
            "update_ledger_incomplete", _draft(f"update ledger has an incomplete line at {path}")
        )
    rows: list[Dict[str, object]] = []
    seen: set[str] = set()
    for number, raw in enumerate(payload.splitlines(), start=1):
        if not raw or len(raw) + 1 > _MAX_RECORD_BYTES:
            raise IntegrityFailure(
                "update_ledger_record_invalid",
                _draft(f"update ledger line {number} is empty or oversized at {path}"),
            )
        try:
            row = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityFailure(
                "update_ledger_record_invalid",
                _draft(f"update ledger line {number} is not strict JSON at {path}"),
            ) from exc
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise IntegrityFailure(
                "update_ledger_record_invalid",
                _draft(f"update ledger line {number} has no record identity at {path}"),
            )
        if row["id"] in seen:
            raise IntegrityFailure(
                "update_ledger_record_invalid",
                _draft(f"update ledger repeats record {row['id']} at {path}"),
            )
        seen.add(str(row["id"]))
        rows.append(dict(row))
    return rows


def _append_jsonl(path: Path, row: Dict[str, object]) -> None:
    encoded = (
        json.dumps(
            row,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    if len(encoded) > _MAX_RECORD_BYTES:
        raise ProtocolRefusal(
            "update_ledger_record_invalid", _draft("update ledger record exceeds its bound")
        )
    try:
        existed = path.exists()
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short update ledger write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if not existed:
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    except OSError as exc:
        raise _durability(exc, path) from exc


def _transact_jsonl(
    path: Path,
    decide: Callable[
        [list[Dict[str, object]]], Tuple[Dict[str, object], Optional[Dict[str, object]]]
    ],
) -> Dict[str, object]:
    with _locked(path):
        rows = _read_jsonl(path)
        result, record = decide(rows)
        if record is not None:
            _append_jsonl(path, record)
        return result


def load_update_trust(destination: Path) -> Dict[str, object]:
    selected = canonical_destination(destination)
    trust = selected / "trust"
    metadata = trust / "keys.json"
    if trust.is_symlink() or metadata.is_symlink() or not metadata.is_file():
        raise ProtocolRefusal(
            "update_trust_unprovisioned",
            _draft(f"installed trust metadata is missing at {metadata}"),
            remedy=_draft("reinstall Floati after the public trust files are provisioned"),
        )
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(
            "update_trust_unprovisioned",
            _draft(f"installed trust metadata is unreadable at {metadata}"),
            remedy=_draft("reinstall Floati after verifying trust/keys.json"),
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "keys"}
        or value.get("format") != "floati-trust-keys.v1"
        or not isinstance(value.get("keys"), list)
    ):
        raise ProtocolRefusal(
            "update_trust_unprovisioned",
            _draft(f"installed trust metadata has an unsupported shape at {metadata}"),
            remedy=_draft("reinstall Floati after verifying trust/keys.json"),
        )
    active = [
        row
        for row in value["keys"]
        if isinstance(row, dict) and row.get("status") == "active"
    ]
    if len(active) != 1 or set(active[0]) != {
        "key_id",
        "public_key_file",
        "valid_from",
        "valid_to",
        "status",
        "transition",
    }:
        raise ProtocolRefusal(
            "update_trust_unprovisioned",
            _draft(f"installed trust metadata must name one active key at {metadata}"),
            remedy=_draft("reinstall Floati after verifying trust/keys.json"),
        )
    filename = active[0].get("public_key_file")
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or filename in {"", ".", ".."}
    ):
        raise ProtocolRefusal(
            "update_trust_unprovisioned",
            _draft(f"installed trust metadata names an invalid key file at {metadata}"),
            remedy=_draft("reinstall Floati after verifying trust/keys.json"),
        )
    public_key = trust / filename
    try:
        if (
            public_key.is_symlink()
            or not public_key.is_file()
            or public_key.resolve(strict=True) != public_key
        ):
            raise OSError("not one canonical ordinary file")
        digest = hashlib.sha256(public_key.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProtocolRefusal(
            "update_trust_unprovisioned",
            _draft(f"installed public key is unavailable at {public_key}"),
            remedy=_draft("reinstall Floati after verifying trust/keys.json"),
        ) from exc
    return {
        "metadata": metadata,
        "public_key": public_key,
        "public_key_relative": f"trust/{filename}",
        "public_key_sha256": digest,
        "key_id": active[0]["key_id"],
    }


class UpdateConsentLedger:
    """Append and project consent for one exact installation destination."""

    def __init__(self, destination: Path) -> None:
        self.destination = canonical_destination(destination)
        self.path = self.destination / INSTALL_DIRECTORY / CONSENT_LEDGER

    @staticmethod
    def _validate_row(row: Dict[str, object]) -> Dict[str, object]:
        if (
            set(row) != _RECORD_FIELDS
            or row.get("schema_version") != 0
            or row.get("kind") != "update_consent_receipt"
            or row.get("operation") not in {"consent", "revoke"}
            or row.get("state") not in {"active", "revoked"}
            or not isinstance(row.get("destination"), str)
            or not isinstance(row.get("channel"), str)
            or not isinstance(row.get("epoch"), int)
            or isinstance(row.get("epoch"), bool)
            or not isinstance(row.get("public_key"), str)
            or not isinstance(row.get("public_key_sha256"), str)
            or _SHA256.fullmatch(str(row.get("public_key_sha256"))) is None
            or not isinstance(row.get("idempotency_key"), str)
            or not isinstance(row.get("id"), str)
            or not isinstance(row.get("timestamp"), str)
        ):
            raise IntegrityFailure(
                "update_consent_record_invalid",
                _draft("update consent ledger contains an invalid record"),
            )
        return row

    def _rows(self) -> list[Dict[str, object]]:
        with _locked(self.path):
            return [self._validate_row(row) for row in _read_jsonl(self.path)]

    def _remedy(self, channel: str) -> str:
        return _draft(
            "run floati update consent "
            f"--destination {self.destination} --channel {channel} "
            "--epoch NEXT --idempotency-key KEY"
        )

    def consent(
        self, *, channel: str, epoch: int, idempotency_key: str
    ) -> Dict[str, object]:
        selected_channel = validate_update_channel(channel)
        key = validate_idempotency_key(idempotency_key)
        if not isinstance(epoch, int) or isinstance(epoch, bool) or not 1 <= epoch <= 2**63 - 1:
            raise ProtocolRefusal(
                "update_consent_epoch_invalid",
                _draft("update consent epoch must be a positive bounded integer"),
            )
        trust = load_update_trust(self.destination)

        def decide(rows: list[Dict[str, object]]):
            validated = [self._validate_row(row) for row in rows]
            prior_key = next(
                (row for row in reversed(validated) if row["idempotency_key"] == key),
                None,
            )
            expected = (
                str(self.destination),
                selected_channel,
                trust["public_key_sha256"],
                epoch,
                "consent",
            )
            if prior_key is not None:
                observed = (
                    prior_key["destination"],
                    prior_key["channel"],
                    prior_key["public_key_sha256"],
                    prior_key["epoch"],
                    prior_key["operation"],
                )
                if observed != expected:
                    raise ProtocolRefusal(
                        "update_consent_idempotency_conflict",
                        _draft("update consent key already names different content"),
                    )
                return prior_key, None
            latest_epoch = max(
                (int(row["epoch"]) for row in validated), default=0
            )
            if epoch <= latest_epoch:
                raise ProtocolRefusal(
                    "update_consent_epoch_stale",
                    _draft(
                        f"update consent epoch must exceed {latest_epoch} in {self.path}"
                    ),
                    remedy=self._remedy(selected_channel),
                )
            row: Dict[str, object] = {
                "schema_version": 0,
                "id": "update-consent-" + uuid7_hex(),
                "kind": "update_consent_receipt",
                "destination": str(self.destination),
                "channel": selected_channel,
                "public_key": trust["public_key_relative"],
                "public_key_sha256": trust["public_key_sha256"],
                "epoch": epoch,
                "operation": "consent",
                "state": "active",
                "predecessor_receipt_id": validated[-1]["id"] if validated else None,
                "idempotency_key": key,
                "timestamp": _timestamp(),
            }
            return row, row

        return _transact_jsonl(self.path, decide)

    def revoke(self, *, channel: str, idempotency_key: str) -> Dict[str, object]:
        selected_channel = validate_update_channel(channel)
        key = validate_idempotency_key(idempotency_key)

        def decide(rows: list[Dict[str, object]]):
            validated = [self._validate_row(row) for row in rows]
            prior_key = next(
                (row for row in reversed(validated) if row["idempotency_key"] == key),
                None,
            )
            if prior_key is not None:
                if (
                    prior_key["operation"] == "revoke"
                    and prior_key["destination"] == str(self.destination)
                    and prior_key["channel"] == selected_channel
                ):
                    return prior_key, None
                raise ProtocolRefusal(
                    "update_consent_idempotency_conflict",
                    _draft("update revoke key already names different content"),
                )
            matches = [
                row
                for row in validated
                if row["destination"] == str(self.destination)
                and row["channel"] == selected_channel
            ]
            if not matches or matches[-1]["state"] != "active":
                raise ProtocolRefusal(
                    "update_consent_missing",
                    _draft(
                        f"no active consent for destination {self.destination} and channel {selected_channel} in {self.path}"
                    ),
                    remedy=self._remedy(selected_channel),
                )
            active = matches[-1]
            row: Dict[str, object] = {
                **active,
                "id": "update-consent-" + uuid7_hex(),
                "operation": "revoke",
                "state": "revoked",
                "predecessor_receipt_id": active["id"],
                "idempotency_key": key,
                "timestamp": _timestamp(),
            }
            return row, row

        return _transact_jsonl(self.path, decide)

    def require_active(self, channel: str) -> Dict[str, object]:
        selected_channel = validate_update_channel(channel)
        rows = [self._validate_row(row) for row in _read_jsonl(self.path)]
        matches = [
            row
            for row in rows
            if row["destination"] == str(self.destination)
            and row["channel"] == selected_channel
        ]
        if not matches:
            code = "update_consent_mismatch" if rows else "update_consent_missing"
            raise ProtocolRefusal(
                code,
                _draft(
                    f"active consent for destination {self.destination} and channel {selected_channel} is missing from {self.path}"
                ),
                remedy=self._remedy(selected_channel),
            )
        current = matches[-1]
        if current["state"] != "active":
            raise ProtocolRefusal(
                "update_consent_revoked",
                _draft(
                    f"consent is revoked for destination {self.destination} and channel {selected_channel} in {self.path}"
                ),
                remedy=self._remedy(selected_channel),
            )
        trust = load_update_trust(self.destination)
        if (
            current["public_key"] != trust["public_key_relative"]
            or current["public_key_sha256"] != trust["public_key_sha256"]
        ):
            raise ProtocolRefusal(
                "update_trust_changed",
                _draft(
                    f"installed trust changed after consent; inspect {trust['metadata']} and trust/key-transition-*"
                ),
                remedy=_draft(
                    "complete the governed key transition, then consent again for the exact destination and channel"
                ),
            )
        return current

    def status(self, channel: str) -> Dict[str, object]:
        selected_channel = validate_update_channel(channel)
        rows = self._rows()
        matches = [
            row
            for row in rows
            if row["destination"] == str(self.destination)
            and row["channel"] == selected_channel
        ]
        if not matches:
            return {
                "state": "never_consented",
                "destination": str(self.destination),
                "channel": selected_channel,
                "ledger": str(self.path),
            }
        return dict(matches[-1])
