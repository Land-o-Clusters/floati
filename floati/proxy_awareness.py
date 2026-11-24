"""Typed awareness of a seat harness pointed at a local model proxy.

Awareness only (FLOATI-PROXY-AWARENESS-1): a loopback base URL in a
harness's own config or environment changes how that seat's limits and
failures behave, so doctor says so in one typed line. This module never
recommends, ships or routes through a proxy, and it never extracts key
material: every observed URL is reduced to scheme, host and port, and
only the allowlisted base-url fields of the two named non-credential
config surfaces are ever read. No user-controlled config text reaches
doctor output: source labels are a fixed vocabulary (harness + config
file kind + ordinal), never a provider key or any free-text field.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

MAX_CONFIG_BYTES = 1024 * 1024
FINDING_CODE = "model_proxy_detected"
URL_ENV_VARS = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "ZCODE_BASE_URL")
_LOOPBACK_HOSTS = frozenset({"localhost", "::1", "0.0.0.0"})


def _reduce_url(value: str) -> Optional[str]:
    """Reduce a base URL to scheme://host[:port]; userinfo and paths are dropped."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    location = f"[{host}]" if ":" in host else host
    if parsed.port:
        location = f"{location}:{parsed.port}"
    return f"{parsed.scheme}://{location}"


def _is_loopback(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _read_json(path: Path) -> Tuple[object, bool]:
    """Read a bounded JSON config; partial marks exists-but-unreadable."""
    try:
        if path.is_symlink() or not path.is_file():
            return None, False
        if path.stat().st_size > MAX_CONFIG_BYTES:
            return None, True
        return json.loads(path.read_text(encoding="utf-8")), False
    except (OSError, ValueError, UnicodeDecodeError):
        return None, True


def _mapping(value: object) -> Dict:
    return value if isinstance(value, dict) else {}


def _observations(home: Path, environment: Dict[str, str]) -> Tuple[List[Dict[str, str]], bool]:
    hits: List[Dict[str, str]] = []
    partial = False

    for variable in URL_ENV_VARS:
        reduced = _reduce_url(environment.get(variable) or "")
        if reduced:
            hits.append({"source": f"env:{variable}", "url": reduced})

    settings_path = home / ".claude" / "settings.json"
    settings, unreadable = _read_json(settings_path)
    partial = partial or unreadable
    base_url = _mapping(_mapping(settings).get("env")).get("ANTHROPIC_BASE_URL")
    reduced = _reduce_url(base_url if isinstance(base_url, str) else "")
    if reduced:
        hits.append({"source": "claude:settings.json", "url": reduced})

    zcode_path = home / ".zcode" / "cli" / "config.json"
    zcode, unreadable = _read_json(zcode_path)
    partial = partial or unreadable
    providers = _mapping(_mapping(zcode).get("provider"))
    # OPUS-RULED (2026-10-11): no user-controlled config text reaches doctor
    # output. The provider's own key is free text, so the source label is the
    # fixed vocabulary only - harness, config file kind, and the provider's
    # 1-based ordinal in sorted-key order (e.g. "zcode provider 2").
    for ordinal, name in enumerate(sorted(providers), start=1):
        base_url = _mapping(_mapping(providers[name]).get("options")).get("baseURL")
        reduced = _reduce_url(base_url if isinstance(base_url, str) else "")
        if reduced:
            hits.append({"source": f"zcode provider {ordinal}", "url": reduced})

    return hits, partial


def project_model_proxy_findings(
    home: Path, env: Optional[Dict[str, str]] = None
) -> List[Dict[str, object]]:
    """One typed doctor line when a harness source points at a loopback proxy."""
    from .doctor import _finding

    environment = dict(os.environ if env is None else env)
    hits, partial = _observations(home, environment)
    loopback = [hit for hit in hits if _is_loopback(hit["url"])]
    if not loopback:
        return []
    classification = 1 if len(loopback) == 1 else 2
    observed = ", ".join(f"{hit['source']} -> {hit['url']}" for hit in loopback)
    finding = _finding(
        FINDING_CODE,
        "info",
        "host",
        f"{len(loopback)} harness source(s) point at a local model proxy: {observed}. "
        "Awareness only: a local proxy changes how this seat's limits and failures "
        "behave; Floati never routes through one.",
        None,
    )
    finding["proxy_awareness"] = {
        "classification": classification,
        "sources": loopback,
        "coverage": "partial_read" if partial else "complete_read",
    }
    return [finding]
