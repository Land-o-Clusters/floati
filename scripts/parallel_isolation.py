"""Fresh-process test scratch isolation and exact-class serial hazards.

No tests or product modules are imported here. The launcher can capture its
original HOME and user-site code path before constructing child environments.
The launcher owns worker-root creation and removal; this helper only prepares
directories within that already owned root.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


CLAUDE_MESSAGING_VARIABLES = (
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
)


def require_clean_test_environment(environ) -> None:
    """Refuse leaked native-session coordinates without disclosing values."""
    if any(name in environ for name in CLAUDE_MESSAGING_VARIABLES):
        raise ValueError("test_environment_contains_claude_messaging")


def _platform_temp_root() -> Path:
    # TMPDIR is private DATA, not the platform boundary. A fresh nested child
    # sees a different gettempdir(); inheriting it here grows every generation.
    return Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp").resolve(strict=True)


def worker_tmp_budget() -> int:
    """Bytes available before the longest multiprocessing socket suffix.

    Both platforms need a trailing NUL in sockaddr_un.sun_path. Python's
    multiprocessing listener adds these two generated components to TMPDIR.
    """
    capacities = {'darwin': 104, 'linux': 108}
    if sys.platform not in capacities:
        raise ValueError('parallel_unix_socket_platform_unsupported')
    suffix = '/pymp-xxxxxxxx/listener-xxxxxxxx'
    return capacities[sys.platform] - 1 - len(os.fsencode(suffix))


def _ordinary_owned_directory(candidate: Path, root: Path) -> None:
    if not candidate.is_absolute() or os.path.normpath(str(candidate)) != str(candidate):
        raise ValueError("worker root must be an absolute normalized path")
    if candidate == root or root not in candidate.parents:
        raise ValueError("worker root must be strictly below the real platform temporary root")
    for component in (*reversed(candidate.parents), candidate):
        metadata = component.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("worker root must have only ordinary directory components")
    if candidate.stat().st_uid != os.getuid():
        raise ValueError("worker root must be owned by the current uid")


def make_worker_environment(worker_root: Path, base_env: dict, user_site: str) -> dict:
    """Prepare isolated HOME/XDG/tmp and preserve original user-site imports.

    On Darwin TMPDIR deliberately names the \x2ftmp alias, preserving the native
    signing probe's symlink ancestor. Fixture directories use only canonical
    paths. On other hosts tempfile.gettempdir() in the fresh child selects its
    worker TMPDIR, so the fixture override is strictly beneath that directory.
    """

    platform_root = _platform_temp_root()
    worker_root = Path(worker_root)
    _ordinary_owned_directory(worker_root, platform_root)
    temporary = worker_root / "tmp"
    fixtures = (worker_root if sys.platform == "darwin" else temporary) / "fixtures"
    home = fixtures / "prepin-home"
    directories = (temporary, fixtures, home, home / ".config", home / ".local",
                   home / ".local" / "share", home / ".cache")
    for directory in directories:
        # Existing scratch entries are validated before reuse. Never follow an
        # inherited symlink while preparing a process's supposedly private tree.
        if not directory.exists() and not directory.is_symlink():
            directory.mkdir(mode=0o700)
        _ordinary_owned_directory(directory, platform_root)
    env = {name: value for name, value in base_env.items()
           if name not in CLAUDE_MESSAGING_VARIABLES}
    temporary_spelling = str(temporary)
    if sys.platform == "darwin":
        temporary_spelling = str(Path("\x2ftmp") / temporary.relative_to("\x2fprivate/tmp"))
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "TMPDIR": temporary_spelling,
        "TMP": temporary_spelling,
        "TEMP": temporary_spelling,
        "FLOATI_TEST_TEMP_ROOT": str(fixtures),
    })
    carried = env.get("PYTHONPATH", "")
    if user_site and user_site not in carried.split(os.pathsep):
        env["PYTHONPATH"] = os.pathsep.join(part for part in (carried, user_site) if part)
    return env


_EXACT_CLASS = re.compile(r"(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*\Z")


def apply_serial_rules(census: dict, rules: dict) -> dict:
    """Promote intact scheduling units without changing native selection order.

    Every rule must name an exact class actually present in discovery. A stale
    inventory cannot silently stop protecting the shared checkout or product
    work parent. Multiple class hazards and preexisting fixture reasons remain
    visible on the whole unit.
    """

    if not isinstance(rules, dict):
        raise ValueError("serial rules must map exact class names to resource reasons")
    units = census["units"]
    discovered = {class_name for unit in units for class_name in unit["classes"]}
    for class_name, reason in rules.items():
        if not isinstance(class_name, str) or not _EXACT_CLASS.fullmatch(class_name):
            raise ValueError(f"serial rule must name an exact class: {class_name!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"serial rule must state a concrete resource reason: {class_name}")
        if class_name not in discovered:
            raise ValueError(f"stale serial rule names an undiscovered class: {class_name}")
    classified = copy.deepcopy(census)
    for unit in classified["units"]:
        reasons = [unit["serial_reason"]] if unit.get("serial_reason") else []
        reasons.extend(f"{class_name}: {rules[class_name]}" for class_name in unit["classes"]
                       if class_name in rules)
        unit["serial_reason"] = "; ".join(reasons) if reasons else None
    return classified
