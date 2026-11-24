"""Lightweight shared node/session validation for root, wake and hook boundaries."""
import re

IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_SESSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,255})$")
_RESERVED = frozenset({"all", "global"})


def validate_identifier(value, field="tenant"):
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        from .errors import ProtocolRefusal
        raise ProtocolRefusal(
            f"{field}_invalid", f"{field} must match {IDENTIFIER_PATTERN.pattern}",
            "pass --from/--to as a node id matching [a-z0-9][a-z0-9_-]*",
        )
    return value


def validate_session_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or _SESSION.fullmatch(value) is None
        or value.casefold() in _RESERVED
    ):
        from .errors import ProtocolRefusal
        raise ProtocolRefusal(
            "wake_session_invalid",
            "wake control requires one exact terminal-safe session id; global and wildcard selectors are forbidden",
        )
    return value
