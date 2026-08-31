"""One-request HTTPS transport for explicit update checks."""

from __future__ import annotations

import http.client
import ssl
from urllib.parse import urlsplit

from .errors import ProtocolRefusal
from .update_consent import _draft, validate_update_channel


def fetch_one_https(channel: str, *, max_bytes: int) -> bytes:
    """Perform exactly one HTTPS GET without redirects or retries."""

    selected = validate_update_channel(channel)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ProtocolRefusal(
            "update_envelope_bound_invalid",
            _draft("update response bound must be one positive integer"),
        )
    parts = urlsplit(selected)
    host = parts.hostname
    assert host is not None
    target = parts.path
    if parts.query:
        target += "?" + parts.query
    connection = http.client.HTTPSConnection(
        host,
        port=parts.port,
        timeout=30,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("GET", target, body=None, headers={})
        response = connection.getresponse()
        if 300 <= response.status <= 399:
            raise ProtocolRefusal(
                "update_redirect_refused",
                _draft(
                    f"update channel {selected} returned redirect status {response.status}; no redirect was followed"
                ),
                remedy=_draft("consent to the final exact HTTPS channel explicitly"),
            )
        if response.status != 200:
            raise ProtocolRefusal(
                "update_http_status",
                _draft(
                    f"update channel {selected} returned HTTP status {response.status}; no retry was attempted"
                ),
                remedy=_draft("verify the exact consented channel and run a new explicit check"),
            )
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ProtocolRefusal(
                "update_envelope_too_large",
                _draft(f"update envelope from {selected} exceeds {max_bytes} bytes"),
                remedy=_draft("publish an index envelope within the stated size bound"),
            )
        return payload
    except ProtocolRefusal:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise ProtocolRefusal(
            "update_transport_failed",
            _draft(
                f"the one allowed request to {selected} failed; no retry was attempted"
            ),
            remedy=_draft("verify the exact HTTPS channel and run a new explicit check"),
        ) from exc
    finally:
        connection.close()
