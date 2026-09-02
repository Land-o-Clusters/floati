"""Operator-declared harness executables for the live C-row suites.

Five suites used to assert that a named live binary exists on the host —
``/opt/homebrew/bin/{devin,herdr,t3,node}``, ``~/.local/bin/agy`` and the
ZCode entry script. Those are OPERATOR FACTS ABOUT ONE WORKSTATION, and
compiling them into the suite made them fail on both CI runners and on every
machine that is not that one.

⇒ A HOST FACT ASSERTED UNCONDITIONALLY IS A CLAIM ABOUT EVERY HOST. The claim
worth keeping is not *"devin is at X"* but *"the operator declared devin at X,
and here is what is true about X on this host."*

**This is not a second declaration mechanism.** FCD 20 R1a
(``floati/fcd20_conformance.py``) already made harness executables
operator-declared, and this module reuses its format exactly:

* a mapping ``row -> absolute path``; a ``null`` value means UNDECLARED and is
  skipped rather than raising,
* an unknown row is one typed refusal, so a typo is a named failure and never
  a silently undeclared row,
* every declared value is validated by the IMPORTED
  ``floati.fleet_update._explicit_executable`` — Policy A, not a second
  predicate, *a reimplementation is a second thing to keep correct*,
* an undeclared row projects to a candidate set of ZERO, which IS the typed
  absence the suites assert.

R1a takes its mapping from argv (``--<harness>-executable``). A test suite has
no argv, so the same mapping is read from one committed JSON file. That file is
the only thing this module adds to the house vocabulary.

⚠ **A SYMLINK CANNOT BE DECLARED.** Policy A refuses symlinks outright, and on
a standard Homebrew mac ``/opt/homebrew/bin/<x>`` is a Cellar symlink — so the
obvious spelling is refused and the declaration must name the RESOLVED target.
That is a known open product question, and it is NOT worked around here: the
validator is imported unchanged and nothing loosens it.

⚠ **NO PATH, EVER.** Nothing in this module searches. ``shutil.which`` does not
appear, no candidate list is consulted, and no package-manager prefix is
assumed. A path arrives from the operator's declaration or the row is absent.

The one transformation applied to a declared value is ``~`` expansion via
``Path.home()``. That is not discovery — ``Path.home()`` is not a search — and
it is required, because a declaration naming the operator's home must not spell
the operator's account name. The public exporter rewrites account names, and a
declaration carrying one would be rewritten into a declaration for somebody
else. See ``tests/operator_identity.py`` for the same reasoning applied to
source fences.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple

from floati.errors import ProtocolRefusal
from floati.fleet_update import _explicit_executable


DECLARATION_PATH = Path(__file__).resolve().parent / "harness_declarations.json"

#: The rows this file may declare. ZCode needs TWO artifacts — the node binary
#: and the entry script — so both are rows; a declaration covering only one
#: would leave the other hard-coded and the suite still host-bound.
ROWS: Tuple[str, ...] = (
    "devin",
    "herdr",
    "t3",
    "agy",
    "zcode-node",
    "zcode-entry",
)


def _code(row: str, suffix: str) -> str:
    return "harness_{0}_executable_{1}".format(row.replace("-", "_"), suffix)


def _remedy(row: str) -> str:
    return (
        "declare {0} in tests/harness_declarations.json with one absolute "
        "canonical executable path; a symlink is refused, so name the "
        "resolved target, and write a path under the operator's home as "
        "~/... so it carries no account name".format(row)
    )


def absence_codes(row: str) -> Tuple[str, ...]:
    """Every typed absence this module can produce for ``row``."""

    return (
        _code(row, "undeclared"),
        _code(row, "absent"),
        _code(row, "invalid"),
    )


@dataclass(frozen=True)
class HarnessAbsence:
    """One typed absence: WHY this host has no usable executable for a row."""

    row: str
    code: str
    detail: str
    remedy: str
    declared: Optional[str]

    def report(self) -> str:
        """One line naming the binary that was not found, for the suite log."""

        return "{0}: {1} [{2}] declared={3} — {4}".format(
            self.row,
            self.detail,
            self.code,
            "(nothing)" if self.declared is None else self.declared,
            self.remedy,
        )


@dataclass(frozen=True)
class HarnessResolution:
    """A declared executable, or the typed absence that stands in its place."""

    row: str
    executable: Optional[Path]
    candidates: Tuple[Path, ...]
    absence: Optional[HarnessAbsence]


def load_declarations(path: Optional[Path] = None) -> Mapping[str, object]:
    """Read the declaration file. A MISSING FILE IS A REFUSAL, not an empty map.

    Treating an unreadable file as "every row undeclared" would let a rename or
    a typo silently disarm all five suites while every one of them still
    printed OK. ⇒ AN INSTRUMENT THAT CANNOT FIND ITS INPUT MUST SAY SO.
    """

    source = DECLARATION_PATH if path is None else path
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProtocolRefusal(
            "harness_declarations_missing",
            "the harness declaration file is not readable at {0}".format(source),
            remedy=(
                "restore tests/harness_declarations.json; it is committed and "
                "required, and an absent file is not an empty declaration"
            ),
        ) from exc
    try:
        values = json.loads(raw)
    except ValueError as exc:
        raise ProtocolRefusal(
            "harness_declarations_invalid",
            "the harness declaration file is not one JSON object",
        ) from exc
    if not isinstance(values, dict):
        raise ProtocolRefusal(
            "harness_declarations_invalid",
            "harness declarations must be one mapping",
        )
    unknown = sorted(set(values) - set(ROWS))
    if unknown:
        raise ProtocolRefusal(
            "harness_declarations_invalid",
            "harness declarations contain an unknown row: {0}".format(
                ", ".join(unknown)
            ),
        )
    return values


def resolve(row: str, path: Optional[Path] = None) -> HarnessResolution:
    """Project one operator declaration into a candidate set of zero or one."""

    if row not in ROWS:
        raise ProtocolRefusal(
            "harness_declarations_invalid",
            "harness declarations contain an unknown row: {0}".format(row),
        )
    declared = load_declarations(path).get(row)
    if declared is None:
        return HarnessResolution(
            row=row,
            executable=None,
            candidates=(),
            absence=HarnessAbsence(
                row=row,
                code=_code(row, "undeclared"),
                detail=(
                    "the operator did not declare an executable for {0}".format(row)
                ),
                remedy=_remedy(row),
                declared=None,
            ),
        )
    if not isinstance(declared, str):
        raise ProtocolRefusal(
            "harness_declarations_invalid",
            "the declaration for {0} must be one path string".format(row),
        )
    # ``~`` expansion, never a search: Path.home() is not PATH.
    candidate = Path(declared).expanduser()
    try:
        selected = _explicit_executable(candidate, _code(row, "invalid"))
    except ProtocolRefusal as exc:
        # Declared but not usable here. Two different sentences: the host does
        # not have this file (the CI case), or it has something that is not one
        # canonical executable (an operator error — typically the Homebrew
        # symlink, which Policy A refuses).
        absent = not candidate.is_file()
        return HarnessResolution(
            row=row,
            executable=None,
            candidates=(),
            absence=HarnessAbsence(
                row=row,
                code=_code(row, "absent" if absent else "invalid"),
                detail=(
                    "declared {0} for {1} is not a file on this host".format(
                        candidate, row
                    )
                    if absent
                    else "declared {0} for {1} is not one canonical "
                    "executable: {2}".format(candidate, row, exc.detail)
                ),
                remedy=_remedy(row),
                declared=declared,
            ),
        )
    executable = Path(selected)
    return HarnessResolution(
        row=row,
        executable=executable,
        candidates=(executable,),
        absence=None,
    )


def live_executable_or_typed_absence(case, row: str) -> Optional[Path]:
    """Return the declared executable, or ASSERT the typed absence and report.

    The suite never skips. When no executable is declared and present, the test
    still runs and still asserts something true about this host: that the typed
    absence is well formed, names this row, and — when a path was declared —
    that the path really is not a file here. The binary that was not found is
    printed so a CI log names it.
    """

    resolution = resolve(row)
    if resolution.executable is not None:
        case.assertEqual(resolution.candidates, (resolution.executable,))
        case.assertIsNone(resolution.absence)
        return resolution.executable

    absence = resolution.absence
    case.assertIsNotNone(absence, "a resolution with no executable must be typed")
    case.assertEqual(absence.row, row)
    case.assertIn(absence.code, absence_codes(row))
    case.assertIn(row, absence.detail)
    case.assertTrue(absence.remedy)
    case.assertEqual(resolution.candidates, ())
    if absence.declared is None:
        case.assertEqual(absence.code, _code(row, "undeclared"))
    else:
        # The MEASURED host fact behind the absence, never an assumed one.
        candidate = Path(absence.declared).expanduser()
        case.assertIn(str(candidate), absence.detail)
        if absence.code == _code(row, "absent"):
            case.assertFalse(
                candidate.is_file(),
                "an 'absent' row must name a path that is not a file here",
            )
        else:
            case.assertEqual(absence.code, _code(row, "invalid"))
            case.assertTrue(
                candidate.is_file(),
                "an 'invalid' row names a file that is not one canonical "
                "executable; a path that is not a file is 'absent'",
            )
    print("TYPED ABSENCE " + absence.report())
    return None
