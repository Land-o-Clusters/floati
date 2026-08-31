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
