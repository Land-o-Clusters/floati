"""Generated-artifact source-name scrub used by the full selftest."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List

from .identity_fence import RETIRED_PRODUCT_NAME


# The scrub vocabulary. Every token is hex-built for the same reason: a scanner
# that spells the string it forbids becomes a finding of itself. The code beside
# each token is what a caller reports, so a fence can say WHICH name fired
# rather than folding two different remediations into one word.
_PRIVATE_PROJECT_NAME = bytes.fromhex("5369676e616c4372616674")
_RETIRED_PRODUCT_NAME = RETIRED_PRODUCT_NAME.encode("ascii")
FORBIDDEN_NAMES = (
    ("private_project_name", _PRIVATE_PROJECT_NAME),
    ("retired_product_name", _RETIRED_PRODUCT_NAME),
)
FORBIDDEN_CODES = tuple(code for code, _token in FORBIDDEN_NAMES)
_EXCLUDED_NAMES = frozenset((".git", "__pycache__", "HM0_BRIEF.md"))
_MAX_HISTORY_BYTES = 16 * 1024 * 1024

# The three coordinate FORMS the product itself still emits, hex-built from the
# token beside them. They are not the word: they are an on-disk directory
# prefix the pre-rename product wrote (floati/storage_identity.py refuses a
# workspace on exactly this prefix, and floati/adapters/codex_live.py reads
# it), a retired schema-extension prefix, and a retired schema origin. Evidence
# that documents a migration honestly has to quote them.
_LEGACY_DIRECTORY_PREFIX = b"." + _RETIRED_PRODUCT_NAME
_LEGACY_EXTENSION_PREFIX = b"x-" + _RETIRED_PRODUCT_NAME + b"-"
_LEGACY_SCHEMA_ORIGIN = b"https://" + _RETIRED_PRODUCT_NAME + b".dev/schemas/"

# A SITE allowlist, shaped like scripts/public_name_fence.py's seat-name one:
# exact code, exact path, exact literal form, and a PINNED COUNT.
#
# The pin is what makes this an allowance rather than a hole. A file listed
# here is cleared only when every occurrence of the token in it is one of the
# enumerated forms AND each form occurs exactly the pinned number of times, so
# the fence still refuses the bare word in these very files, refuses a
# thirteenth coordinate appearing, and refuses the twelfth disappearing. The
# counts were derived from the fence's own RED output at this tree, never
# typed from a summary.
RETIRED_NAME_SITE_ALLOWLIST = (
    (
        "retired_product_name",
        "docs/evidence/HM05-DOGFOOD.md",
        _LEGACY_DIRECTORY_PREFIX,
        12,
        "bus-root directory the pre-rename product created, quoted by a dogfood transcript",
    ),
    (
        "retired_product_name",
        "docs/evidence/FL4.5-FLOATI-INTERNAL-RENAME.md",
        _LEGACY_DIRECTORY_PREFIX,
        8,
        "the legacy artifact names the rename record exists to say are refused",
    ),
    (
        "retired_product_name",
        "docs/evidence/FL4.5-FLOATI-INTERNAL-RENAME.md",
        _LEGACY_EXTENSION_PREFIX,
        1,
        "the retired schema-extension prefix, recorded as the thing replaced",
    ),
    (
        "retired_product_name",
        "docs/evidence/FL4.5-FLOATI-INTERNAL-RENAME.md",
        _LEGACY_SCHEMA_ORIGIN,
        2,
        "the retired schema origin, recorded as a measured gate failure",
    ),
    (
        "retired_product_name",
        "docs/evidence/HM1B-LIVE-WORKERS.md",
        _LEGACY_DIRECTORY_PREFIX,
        2,
        "the worker transcript directory the pre-rename adapter wrote",
    ),
)


def _site_allowance_clears(code: str, relative: str, token: bytes, lowered: bytes) -> bool:
    """Return True when every hit in one file is an enumerated, pinned coordinate.

    False is the safe answer: an unlisted file, a pinned count that moved, or a
    single occurrence the enumerated forms do not account for all leave the file
    a finding. An allowance may narrow WHICH hits are tolerated; it may never
    turn a file into one the scanner stops reading.
    """

    rows = [
        row
        for row in RETIRED_NAME_SITE_ALLOWLIST
        if row[0] == code and row[1] == relative
    ]
    if not rows:
        return False
    covered = 0
    for _code, _path, form, expected, _reason in rows:
        seen = lowered.count(form.lower())
        if seen != expected:
            return False
        covered += seen
    return lowered.count(token) == covered


def _selected(codes: Iterable[str] | None) -> tuple[tuple[str, bytes], ...]:
    """Return the requested vocabulary, refusing a code that does not exist."""

    if codes is None:
        return tuple((code, token.lower()) for code, token in FORBIDDEN_NAMES)
    wanted = tuple(codes)
    unknown = sorted(set(wanted) - set(FORBIDDEN_CODES))
    if unknown:
        raise ValueError(f"unknown scrub vocabulary code: {unknown[0]}")
    return tuple(
        (code, token.lower()) for code, token in FORBIDDEN_NAMES if code in wanted
    )


def scan_generated_tree_by_code(
    root: Path,
    codes: Iterable[str] | None = None,
    paths: Iterable[str] | None = None,
) -> Dict[str, List[str]]:
    """Return, per vocabulary code, the generated files carrying that name.

    One walk over the publication inventory, so a caller wanting to name the
    token that fired does not pay for a second read of every tracked file.

    `paths` narrows the POPULATION to an explicit repository-relative list. It
    is how R-N4 Am.3's fence walks the exporter's include set instead of the
    whole tracked tree, and it is deliberately a supplied list rather than a
    rule this module implements: the classification belongs to the exporter and
    the policy it reads, and a product module may not grow a second opinion
    about what is published. Callers that omit it get the whole inventory,
    which is what the exporter itself wants when it scans a projected tree.
    """

    base = Path(root).resolve()
    selected = _selected(codes)
    hits: Dict[str, List[str]] = {code: [] for code, _token in selected}
    for path in _inventory(base, paths):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        lowered = data.lower()
        relative = path.relative_to(base).as_posix()
        for code, token in selected:
            if token in lowered and not _site_allowance_clears(
                code, relative, token, lowered
            ):
                hits[code].append(relative)
    return {code: sorted(found) for code, found in hits.items()}


def _inventory(base: Path, paths: Iterable[str] | None) -> Iterable[Path]:
    """Return the files to read: the whole tracked tree, or a supplied narrowing."""

    if paths is None:
        return _files(base)
    resolved: List[Path] = []
    for value in paths:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("tracked source scrub path is invalid")
        resolved.append(base / relative)
    return resolved


def scan_generated_tree(
    root: Path,
    codes: Iterable[str] | None = None,
    paths: Iterable[str] | None = None,
) -> List[str]:
    """Return repository-relative generated files containing a forbidden name."""

    found = scan_generated_tree_by_code(root, codes, paths)
    return sorted({path for values in found.values() for path in values})


def scan_git_history_notes(root: Path) -> List[str]:
    """Return commit-message and Git-note coordinates containing the private name."""

    base = Path(root).resolve()
    messages = _git(
        base,
        "log",
        "--all",
        "--max-count=10000",
        "--format=format:%H%x00%B%x00",
    )
    fields = messages.split(b"\0")
    # R-N4 puts the retired name in front of TWO scanners by name: this tree
    # scan's sibling and the public-product-source scan. It deliberately does
    # not name this one, and the difference is a measured fact rather than an
    # oversight: the published tree is a recut whose whole history is 16
    # commits with no hit, while the harbor this is developed in carries 2183
    # commits, nine of whose messages spell the name. Widening this scanner
    # would therefore RED on harbor history that no export can reach and no
    # remediation short of history surgery can clear — a fence reporting on a
    # repository the ruling is not about. Recorded here so the narrower
    # vocabulary reads as the scope it is.
    vocabulary = _selected(("private_project_name",))
    hits: List[str] = []
    for index in range(0, len(fields) - 1, 2):
        sha = fields[index].decode("ascii", errors="replace").strip()
        body = fields[index + 1].lower()
        if sha and any(token in body for _code, token in vocabulary):
            hits.append(f"{sha}:commit-message")

    refs = _git(base, "for-each-ref", "--format=%(refname)", "refs/notes")
    for raw_ref in refs.splitlines():
        ref = raw_ref.decode("utf-8", errors="replace").strip()
        if not ref:
            continue
        listing = _git(base, "notes", f"--ref={ref}", "list")
        for line in listing.splitlines():
            parts = line.decode("ascii", errors="replace").split()
            if len(parts) != 2:
                continue
            object_sha = parts[1]
            note = _git(base, "notes", f"--ref={ref}", "show", object_sha).lower()
            if any(token in note for _code, token in vocabulary):
                hits.append(f"{ref}:{object_sha}:note")
    return sorted(hits)


def _git(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", *arguments],
            cwd=root,
            env=_git_environment(),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("history-note scrub unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("history-note scrub unavailable")
    if len(result.stdout) > _MAX_HISTORY_BYTES:
        raise RuntimeError("history-note scrub exceeds bounded output")
    return result.stdout


def _files(root: Path) -> Iterable[Path]:
    tracked = _tracked_files(root)
    if tracked is not None:
        yield from tracked
        return
    for path in root.rglob("*"):
        if any(part in _EXCLUDED_NAMES for part in path.relative_to(root).parts):
            continue
        if path.is_file() and not path.is_symlink():
            yield path


def _tracked_files(root: Path) -> List[Path] | None:
    """Return the Git publication inventory, or None for a non-repository fixture."""

    try:
        probe = subprocess.run(
            ["git", "--no-replace-objects", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            env=_git_environment(),
            check=False,
            capture_output=True,
            timeout=30,
        )
        if probe.returncode != 0:
            if (root / ".git").exists():
                raise RuntimeError("tracked source scrub unavailable")
            return None
        if probe.stdout.strip() != b"true":
            raise RuntimeError("tracked source scrub requires a work tree")
        result = subprocess.run(
            ["git", "--no-replace-objects", "ls-files", "-z", "--cached"],
            cwd=root,
            env=_git_environment(),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("tracked source scrub unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("tracked source scrub unavailable")
    if len(result.stdout) > _MAX_HISTORY_BYTES:
        raise RuntimeError("tracked source scrub exceeds bounded output")
    paths: List[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("tracked source scrub path is invalid")
        if any(part in _EXCLUDED_NAMES for part in relative.parts):
            continue
        path = root / relative
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    return paths


def _git_environment() -> dict[str, str]:
    """Use the addressed checkout, never caller-injected Git coordinates."""

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_PAGER": "cat"})
    return environment
