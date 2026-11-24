"""Deterministic double for the closed TOML subset used by seat fixtures.

This is not a product parser or an optional-dependency fallback. Every fixture
uses the same subset on every interpreter; unsupported input raises ValueError
through the unchanged product decoder and validation wrapper.
"""
import builtins
import json
import re
from types import SimpleNamespace
from unittest import mock


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_DECODER = json.JSONDecoder()


def _table_path(text):
    parts = []
    position = 0
    while position < len(text):
        if text[position] == '"':
            value, position = _DECODER.raw_decode(text, position)
            if not isinstance(value, str) or not value:
                raise ValueError("fixture table key must be a nonempty string")
        else:
            match = _BARE_KEY.match(text, position)
            if match is None:
                raise ValueError("unsupported fixture table key")
            value, position = match.group(), match.end()
        parts.append(value)
        if position == len(text):
            break
        if text[position] != "." or position + 1 == len(text):
            raise ValueError("malformed fixture dotted table")
        position += 1
    if not ((len(parts) == 2 and parts[0] == "projects") or
            (len(parts) == 3 and parts[:2] == ["hooks", "state"])):
        raise ValueError("unsupported fixture table shape")
    return tuple(parts)


def fixture_loads(text):
    """Read only fixture string/bool assignments and project/hook tables."""
    if not isinstance(text, str):
        raise ValueError("fixture TOML input must be decoded text")
    if any((ord(char) < 32 and char not in "\t\n\r") or ord(char) == 127 for char in text):
        raise ValueError("unsupported fixture control character")
    if "\r" in text.replace("\r\n", "\n"):
        raise ValueError("unsupported fixture line ending")
    document, declared_tables = {}, set()
    current = document
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if not line.endswith("]") or line.startswith("[["):
                raise ValueError("malformed fixture table at line " + str(number))
            path = _table_path(line[1:-1])
            if path in declared_tables:
                raise ValueError("duplicate fixture table")
            declared_tables.add(path)
            current = document
            for part in path:
                if part not in current:
                    current[part] = {}
                if not isinstance(current[part], dict):
                    raise ValueError("fixture scalar/table collision")
                current = current[part]
            continue
        key, separator, encoded = line.partition("=")
        key, encoded = key.strip(), encoded.strip()
        if not separator or _BARE_KEY.fullmatch(key) is None:
            raise ValueError("unsupported fixture assignment at line " + str(number))
        value = json.loads(encoded)
        if type(value) not in (str, bool):
            raise ValueError("unsupported fixture value")
        if key in current:
            raise ValueError("duplicate fixture key or scalar/table collision")
        current[key] = value
    return document


def install_fixture_toml(testcase, *, module="tomllib"):
    """Replace only the product's parser imports for this test's lifetime."""
    if module not in ("tomllib", "tomli"):
        raise ValueError("fixture parser branch must be tomllib or tomli")
    delegate = builtins.__import__
    parser = SimpleNamespace(loads=fixture_loads)
    def imported(name, globals=None, locals=None, fromlist=(), level=0):
        if (name in ("tomllib", "tomli") and level == 0 and globals is not None
                and globals.get("__name__") == "floati.seat_harness_config"):
            if name != module:
                raise ImportError("controlled fixture selects " + module)
            return parser
        return delegate(name, globals, locals, fromlist, level)
    patcher = mock.patch("builtins.__import__", side_effect=imported)
    testcase.addCleanup(patcher.stop)
    patcher.start()
