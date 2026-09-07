"""Digest-backed tenant coordinate fence for living-document sweeps.

The publication/name fence must refuse private tenant coordinates without
carrying those coordinates in the exported tree. Fixed forbidden tokens are
therefore pinned as lowercase SHA-256 digests with byte lengths; the scanner
hashes candidate substrings and never spells the plaintext it forbids.

Operator-declared tokens live in ``.github/tenant-fence-declarations.v0.json``,
which export policy excludes from the public projection. Harbor tests verify
that file's digests against these pins; the public checkout scans from the pins
alone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Sequence

from .identity_fence import (
    HOME_PREFIX,
    OWNER_USERNAME,
    RETIRED_PRODUCT_NAME as RETIRED,
)

DECLARATION_RELATIVE = ".github/tenant-fence-declarations.v0.json"

PIN_SCHEME = "hmac-sha256"

#: Keyed digest pins. Each entry is (code, hmac_sha256_of_casefolded_bytes
#: under the operator key, byte_length). The digests are not bare hashes of
#: short tokens, so enumeration cannot recover what they name.
FORBIDDEN_TOKEN_DIGESTS: tuple[tuple[str, str, int], ...] = (
        ("host_model", "602459fc6deceee894729293cbb01f46dd320d74ac845a6b61dfc333ccc343e8", 6),
        ("private_fleet_id", "d94398c88de97f7cee82b3362d6b27a7f345245f2d783e169a372f61bd03a13a", 12),
        ("private_workspace_path", "5619b9f135b302cff90a45151a2faa05abd247a4cff236fe6b7836f8a24282a9", 17),
        ("private_bus_root", "605c099aa14103dfe0f4f4603abe28737754d22361af047774f31ee720524ef8", 25),
        ("absolute_product_path", "7fc58b0694f67dd86741311f94e15edb7bfca74f559ee2276cbb981b4d5c7c94", 17),
)

_ACCOUNT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(re.escape(HOME_PREFIX) + r"[^/\s`<>]+", re.IGNORECASE),
    re.compile(re.escape(OWNER_USERNAME), re.IGNORECASE),
)

_DERIVED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"\.{re.escape(RETIRED)}-bus/[a-z0-9](?:[a-z0-9_-]{{0,62}}[a-z0-9])?",
        re.IGNORECASE,
    ),
    re.compile(rf"/absolute/{re.escape(RETIRED)}", re.IGNORECASE),
)

_FLEET_ID_CODE = "private_fleet_id"
_FLEET_ID_DIGEST = next(
    digest for code, digest, _length in FORBIDDEN_TOKEN_DIGESTS if code == _FLEET_ID_CODE
)
_FLEET_ID_LENGTH = next(
    length for code, _digest, length in FORBIDDEN_TOKEN_DIGESTS if code == _FLEET_ID_CODE
)


def _digest_lookup() -> dict[tuple[str, int], str]:
    return {(digest, length): code for code, digest, length in FORBIDDEN_TOKEN_DIGESTS}


def hmac_digest(key: bytes, token: str) -> str:
    """The keyed digest a pin carries: HMAC-SHA256 of the casefolded bytes."""

    return hmac.new(key, token.casefold().encode("utf-8"), hashlib.sha256).hexdigest()


def operator_key(root: Path) -> bytes:
    """The harbor-only HMAC key, loaded from the private declaration.

    The projected tree carries no key: there the digest scan is inert
    (typed, never pretended), because matching a keyed pin without the
    key is exactly what Am.1 made impossible.
    """

    document = load_operator_declaration(Path(root))
    return str(document["key"]).encode("utf-8")


def _digest_token_hits(text: str, key: bytes | None) -> list[tuple[str, int]]:
    if key is None:
        return []
    folded = text.casefold()
    lookup = _digest_lookup()
    hits: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for _code, digest, length in FORBIDDEN_TOKEN_DIGESTS:
        if length > len(folded):
            continue
        for index in range(len(folded) - length + 1):
            chunk = folded[index : index + length]
            if len(chunk.encode("utf-8")) != length:
                continue
            code = lookup.get((hmac_digest(key, chunk), length))
            if code is None:
                continue
            seen_key = (code, index)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            hits.append((code, index))
    return hits


def tenant_violation_codes(text: str, *, key: bytes | None = None) -> tuple[str, ...]:
    """Return stable violation codes for one text surface.

    ``key`` is the operator key from the private declaration; without
    it only the keyless pattern half of the fence can speak.
    """

    codes = [code for code, _index in _digest_token_hits(text, key)]
    for pattern in _ACCOUNT_PATTERNS + _DERIVED_PATTERNS:
        if pattern.search(text) is not None:
            codes.append(f"derived:{pattern.pattern}")
    if _runtime_home_pattern().search(text) is not None:
        codes.append("runtime:home_path")
    return tuple(sorted(set(codes)))


def tenant_coordinate_found(text: str, *, key: bytes | None = None) -> bool:
    """Return whether ``text`` carries any forbidden tenant coordinate."""

    return bool(tenant_violation_codes(text, key=key))


def _runtime_home_pattern() -> re.Pattern[str]:
    return re.compile(re.escape(str(Path.home())), re.IGNORECASE)


def _replace_first_digest_token(
    text: str, code: str, replacement: str, key: bytes | None
) -> str:
    for hit_code, index in _digest_token_hits(text, key):
        if hit_code != code:
            continue
        length = next(
            entry_length
            for entry_code, _digest, entry_length in FORBIDDEN_TOKEN_DIGESTS
            if entry_code == code
        )
        return text[:index] + replacement + text[index + length :]
    return text


def prepare_tenant_scan_text(
    relative: str, text: str, *, key: bytes | None = None
) -> str:
    """Apply the living-doc scan's ruled exemptions without spelling private tokens."""

    if relative == "README.md":
        field = '"tenant_id":"'
        start = text.find(field)
        if start >= 0:
            value_start = start + len(field)
            segment = text.casefold()[value_start : value_start + _FLEET_ID_LENGTH]
            if (
                key is not None
                and len(segment) == _FLEET_ID_LENGTH
                and hmac_digest(key, segment) == _FLEET_ID_DIGEST
            ):
                return (
                    text[:value_start]
                    + "real-receipt"
                    + text[value_start + _FLEET_ID_LENGTH :]
                )
    if relative == "docs/PUBLICATION-CHECKLIST.md":
        return _replace_first_digest_token(
            text, _FLEET_ID_CODE, "preserved-live-coordinate", key
        )
    return text


def load_operator_declaration(root: Path) -> dict[str, object]:
    """Load the harbor-only operator declaration after schema validation."""

    path = Path(root).resolve() / DECLARATION_RELATIVE
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 0 or isinstance(
        document.get("schema_version"), bool
    ):
        raise RuntimeError("tenant fence declaration schema_version must be 0")
    tokens = document.get("forbidden_tokens")
    if not isinstance(tokens, list) or not tokens:
        raise RuntimeError("tenant fence declaration must list forbidden_tokens")
    if document.get("scheme") != PIN_SCHEME:
        raise RuntimeError(
            f"tenant fence declaration scheme must be {PIN_SCHEME}"
        )
    if not isinstance(document.get("key"), str) or not document["key"]:
        raise RuntimeError("tenant fence declaration must carry a non-empty key")
    for entry in tokens:
        if not isinstance(entry, dict):
            raise RuntimeError("tenant fence declaration entry must be an object")
        for field in ("code", "digest", "length", "token"):
            if field not in entry:
                raise RuntimeError(f"tenant fence declaration entry missing {field}")
    return document


def declaration_matches_public_pins(root: Path) -> bool:
    """Return whether the operator file's keyed digests match the exported pins."""

    document = load_operator_declaration(root)
    key = str(document["key"]).encode("utf-8")
    pinned = {
        (code, digest, length)
        for code, digest, length in FORBIDDEN_TOKEN_DIGESTS
    }
    observed: set[tuple[str, str, int]] = set()
    for entry in document["forbidden_tokens"]:
        code = entry["code"]
        digest = entry["digest"]
        length = entry["length"]
        token = entry["token"]
        if not isinstance(code, str) or not isinstance(digest, str):
            return False
        if not isinstance(length, int) or length <= 0:
            return False
        if not isinstance(token, str) or len(token.casefold().encode("utf-8")) != length:
            return False
        if hmac_digest(key, token) != digest:
            return False
        observed.add((code, digest, length))
    return observed == pinned
