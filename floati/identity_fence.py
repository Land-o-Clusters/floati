"""Shared governed identity values for installed and publication fences."""

from __future__ import annotations

import re


_SLASH = bytes.fromhex("2f").decode("ascii")
_PRIVATE = bytes.fromhex("70726976617465").decode("ascii")
_VAR = bytes.fromhex("766172").decode("ascii")
_TMP = bytes.fromhex("746d70").decode("ascii")
_FOLDERS = bytes.fromhex("666f6c64657273").decode("ascii")

HOME_PREFIX = bytes.fromhex("2f55736572732f").decode("ascii")
TMP_PREFIX = _SLASH + _TMP
PRIVATE_TMP_PREFIX = _SLASH + _PRIVATE + TMP_PREFIX
PRIVATE_VAR_TMP_PREFIX = _SLASH + _PRIVATE + _SLASH + _VAR + TMP_PREFIX
VAR_FOLDERS_PREFIX = _SLASH + _VAR + _SLASH + _FOLDERS
GOVERNED_TEMP_FENCES = (
    ("private_tmp_path", PRIVATE_TMP_PREFIX),
    ("private_var_tmp_path", PRIVATE_VAR_TMP_PREFIX),
    ("var_folders_path", VAR_FOLDERS_PREFIX),
    ("tmp_path", TMP_PREFIX),
)
GOVERNED_TEMP_PREFIXES = tuple(prefix for _code, prefix in GOVERNED_TEMP_FENCES)
OWNER_USERNAME = bytes.fromhex("63687269736d656e656e64657a").decode("ascii")
HOME_PATTERN = re.compile(re.escape(HOME_PREFIX) + r"[A-Za-z0-9._-]+")

# The retired repository name. It is a governed token for the same reason the
# others here are: a fence that spells the string it forbids reports itself as
# a finding. Every shipped use of this name is a hash-domain salt, a persisted
# field value, or an on-disk coordinate the product reads — values that must
# keep their exact bytes — so the convention is to build the token, never to
# rewrite it. Consumers: floati/scrub.py's vocabulary and the tests that sweep
# for the name.
RETIRED_PRODUCT_NAME = bytes.fromhex("736c6970776179").decode("ascii")
RETIRED_PRODUCT_SHORT_NAME = bytes.fromhex("736c6970").decode("ascii")


# A governed prefix is a POSITION, not a substring. The shortest governed
# prefix also occurs INSIDE a longer, ungoverned system path, and a plain
# str.replace rewrote the middle of it — which turned a shipped CLI example
# into nonsense and published it. A match counts only where a path can begin:
# at the start of the text, or after a character that cannot be part of a path
# component. (This file spells no governed token literally; every one is built
# from hex above, and a test enforces that.)
_PATH_COMPONENT_CHARACTER = re.compile(r"[A-Za-z0-9_.~-]")
_LONGEST_FIRST_TEMP_PREFIXES = tuple(
    sorted(GOVERNED_TEMP_PREFIXES, key=len, reverse=True)
)


def redact_governed_temp_prefixes(text, replacement="<temp>", prefixes=None):
    """Replace every governed temporary prefix that BEGINS a path.

    `replacement` is either the literal text to substitute, or a callable
    taking the matched prefix and returning its replacement — the exporter's
    Python-literal escape needs the prefix itself to build one. `prefixes`
    narrows the set, so a caller that must report WHICH fence fired can ask
    about one at a time.

    Longest prefix first, so a longer governed prefix is consumed whole rather
    than reduced to its parent plus a redacted tail.
    """

    selected = _LONGEST_FIRST_TEMP_PREFIXES if prefixes is None else tuple(
        sorted(prefixes, key=len, reverse=True)
    )
    for prefix in selected:
        pieces = []
        index = 0
        while True:
            found = text.find(prefix, index)
            if found < 0:
                pieces.append(text[index:])
                break
            preceding = text[found - 1] if found else ""
            if preceding and _PATH_COMPONENT_CHARACTER.fullmatch(preceding):
                pieces.append(text[index : found + len(prefix)])
            else:
                pieces.append(text[index:found])
                pieces.append(
                    replacement(prefix) if callable(replacement) else replacement
                )
            index = found + len(prefix)
        text = "".join(pieces)
    return text
