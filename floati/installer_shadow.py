"""Read-only installer-shadow observation over an explicitly supplied PATH."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Mapping, Optional, Union


FOUND_EXIT = 0
AFFIRMATIVE_NONE_EXIT = 20
UNKNOWN_EXIT = 21
CANNOT_SPEAK_EXIT = 22

_OUTCOME_EXITS = {
    "found": FOUND_EXIT,
    "affirmative_none": AFFIRMATIVE_NONE_EXIT,
    "unknown": UNKNOWN_EXIT,
    "cannot_speak": CANNOT_SPEAK_EXIT,
}

_COLD_READ = {
    "found": "A floati ahead of the installed copy answered first on PATH.",
    "affirmative_none": "Every PATH entry was checked; the installed floati answers first.",
    "unknown": "Some PATH entries could not be read; shadow state unknown.",
    "cannot_speak": "No installer destination named; the shadow check could not run.",
}


def resolve_installer_destination(
    explicit_destination: Optional[Union[Path, str]],
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[Union[Path, str]]:
    """Apply the ruled destination precedence without inferring a location."""

    if explicit_destination is not None:
        return explicit_destination
    source = os.environ if environ is None else environ
    return source.get("FLOATI_INSTALL_DESTINATION")


def observation_exit_code(artifact: Mapping[str, object]) -> int:
    """Return the closed TD5 exit for one observation artifact."""

    outcome = artifact.get("outcome")
    if not isinstance(outcome, str) or outcome not in _OUTCOME_EXITS:
        return CANNOT_SPEAK_EXIT
    return _OUTCOME_EXITS[outcome]


def observe_installer_shadow(
    explicit_destination: Optional[Union[Path, str]],
    *,
    path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    source_script: Optional[Union[Path, str]] = None,
) -> dict[str, Any]:
    """Make one presenter observation through the sole ruled destination resolver."""

    return enumerate_installer_shadow(
        resolve_installer_destination(explicit_destination, environ=environ),
        path=path,
        environ=environ,
        source_script=source_script,
    )


def enumerate_installer_shadow(
    destination: Optional[Union[Path, str]],
    *,
    path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    source_script: Optional[Union[Path, str]] = None,
) -> dict[str, Any]:
    """Observe only resolvable ``floati`` entries preceding the installer destination.

    This is intentionally a filesystem observation, not a command lookup or
    execution path.  A partial PATH scan is never promoted to affirmative-none.
    """

    authoritative, has_installed_command, failure_outcome, blocked_entry = _authoritative_directory(
        destination
    )
    if failure_outcome is not None:
        return _artifact(failure_outcome, blocked_entry=blocked_entry)
    if authoritative is None:
        return _artifact("unknown", blocked_entry=_coordinate_label(destination))

    source = os.environ if environ is None else environ
    supplied_path = source.get("PATH") if path is None else path
    if not isinstance(supplied_path, str) or not supplied_path:
        return _artifact("unknown", blocked_entry="PATH")

    excluded_source = _resolved_regular_file(
        source_script if source_script is not None else _loaded_source_script()
    )
    roots: list[str] = []
    found: list[dict[str, str]] = []
    skipped_entries: list[str] = []
    authoritative_seen = False

    for raw_entry in supplied_path.split(os.pathsep):
        entry, entry_outcome = _path_entry(raw_entry)
        if entry is None:
            if entry_outcome == "missing":
                skipped_entries.append(raw_entry)
                continue
            return _blocked_path_artifact(roots, found, raw_entry, skipped_entries)
        roots.append(str(entry))
        if has_installed_command and entry == authoritative:
            authoritative_seen = True

        candidate = entry / "floati"
        try:
            candidate_stat = candidate.stat()
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError):
            return _blocked_path_artifact(roots, found, raw_entry, skipped_entries)
        if not stat.S_ISREG(candidate_stat.st_mode):
            continue
        try:
            resolved_candidate = candidate.resolve(strict=True)
            payload = resolved_candidate.read_bytes()
        except (OSError, ValueError, UnicodeError):
            return _blocked_path_artifact(roots, found, raw_entry, skipped_entries)
        if authoritative_seen or resolved_candidate == excluded_source:
            continue
        found.append(
            {
                "path": str(resolved_candidate),
                "digest": hashlib.sha256(payload).hexdigest(),
            }
        )

    if has_installed_command and not authoritative_seen:
        return _artifact(
            "unknown",
            roots,
            found,
            blocked_entry=str(authoritative),
            skipped_entries=skipped_entries,
        )
    if found:
        return _artifact(
            "found",
            roots,
            found,
            skipped_entries=skipped_entries,
        )
    return _artifact(
        "affirmative_none",
        roots,
        found,
        skipped_entries=skipped_entries,
    )


def _artifact(
    outcome: str,
    enumerated_roots: Optional[list[str]] = None,
    found: Optional[list[dict[str, str]]] = None,
    *,
    blocked_entry: Optional[str] = None,
    skipped_entries: Optional[list[str]] = None,
    remedy: Optional[str] = None,
) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "outcome": outcome,
        "enumerated_roots": [] if enumerated_roots is None else enumerated_roots,
        "found": [] if found is None else found,
    }
    if blocked_entry is not None:
        artifact["blocked_entry"] = blocked_entry
    if skipped_entries:
        artifact["skipped_entries"] = skipped_entries
    if remedy is not None:
        artifact["remedy"] = remedy
    if outcome in _COLD_READ:
        artifact["reason"] = _COLD_READ[outcome]
    return artifact


def _authoritative_directory(
    value: Optional[Union[Path, str]],
) -> tuple[Optional[Path], bool, Optional[str], Optional[str]]:
    """Derive the command directory without making a future install impossible."""

    if value is None or value == "":
        return None, False, "cannot_speak", None
    try:
        candidate = Path(value)
    except (OSError, TypeError, ValueError, UnicodeError):
        return None, False, "unknown", _coordinate_label(value)
    try:
        is_symlink = candidate.is_symlink()
    except OSError:
        return None, False, "unknown", _coordinate_label(value)
    if not candidate.is_absolute() or is_symlink:
        return None, False, "unknown", _coordinate_label(value)
    try:
        bundle_mode = candidate.lstat().st_mode
    except FileNotFoundError:
        return _prospective_directory(candidate)
    except (OSError, ValueError, UnicodeError):
        return None, False, "unknown", _coordinate_label(value)
    if not stat.S_ISDIR(bundle_mode):
        return None, False, "unknown", _coordinate_label(value)

    command_directory = candidate / "scripts"
    try:
        command_directory_mode = command_directory.lstat().st_mode
    except FileNotFoundError:
        return _prospective_directory(candidate)
    except (OSError, ValueError, UnicodeError):
        return None, False, "unknown", str(command_directory)
    if stat.S_ISLNK(command_directory_mode) or not stat.S_ISDIR(command_directory_mode):
        return None, False, "unknown", str(command_directory)

    command = command_directory / "floati"
    try:
        command_mode = command.lstat().st_mode
    except FileNotFoundError:
        return _prospective_directory(candidate)
    except (OSError, ValueError, UnicodeError):
        return None, False, "unknown", str(command)
    if stat.S_ISLNK(command_mode) or not stat.S_ISREG(command_mode):
        return None, False, "unknown", str(command)
    try:
        return command_directory.resolve(strict=True), True, None, None
    except (OSError, ValueError, UnicodeError):
        return None, False, "unknown", str(command_directory)


def _prospective_directory(
    candidate: Path,
) -> tuple[Optional[Path], bool, Optional[str], Optional[str]]:
    """A lexical future bundle root is valid only until the observer sees a fault."""

    try:
        return (candidate / "scripts").resolve(strict=False), False, None, None
    except (OSError, ValueError, UnicodeError):
        return None, False, "unknown", str(candidate)


def _coordinate_label(value: object) -> str:
    try:
        return str(value)
    except (TypeError, ValueError, UnicodeError):
        return "destination"


def _path_entry(raw_entry: str) -> tuple[Optional[Path], str]:
    if not raw_entry:
        return None, "Checked PATH entries in order until an empty entry could not be enumerated."
    try:
        candidate = Path(raw_entry)
    except (OSError, TypeError, ValueError, UnicodeError):
        return None, "Checked PATH entries in order until one entry could not be read."
    if not candidate.is_absolute():
        return None, "Checked PATH entries in order until one entry was not absolute."
    try:
        candidate.lstat()
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, UnicodeError):
        return None, "unreadable"
    try:
        resolved = candidate.resolve(strict=True)
        mode = resolved.stat().st_mode
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, UnicodeError):
        return None, "unreadable"
    if not stat.S_ISDIR(mode):
        return None, "Checked PATH entries in order until one entry was not a directory."
    return resolved, ""


def _blocked_path_artifact(
    roots: list[str],
    found: list[dict[str, str]],
    blocked_entry: str,
    skipped_entries: list[str],
) -> dict[str, Any]:
    return _artifact(
        "unknown",
        roots,
        found,
        blocked_entry=blocked_entry,
        skipped_entries=skipped_entries,
        remedy=f"Fix or drop PATH entry {blocked_entry}, or pass a clean PATH.",
    )


def _resolved_regular_file(value: Optional[Union[Path, str]]) -> Optional[Path]:
    if value is None:
        return None
    try:
        candidate = Path(value).resolve(strict=True)
        if not candidate.is_file():
            return None
        return candidate
    except (OSError, TypeError, ValueError, UnicodeError):
        return None


def _loaded_source_script() -> Path:
    """Name this loaded bundle's build launcher only; never derive a destination from it."""

    return Path(__file__).resolve().parents[1] / "scripts" / "floati"
