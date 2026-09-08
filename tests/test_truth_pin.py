"""TRUTH-PIN-1: the public outbound-path claim is derived, not trusted.

Half A derives the counted outbound paths from the no-listener fence's
own census and asserts README.md and docs/TRUTH-GUARANTEES.md agree on
the count and the per-path consent class. Half B refuses an enumerated
list of false assertions on those two pages only. Residual: Half B is a
sentinel list; a novel phrasing of the same lie passes it. Half A is
the instrument. The pages' freehand prose is still a person's job.

FOR WHOEVER WRITES THOSE TWO PAGES NEXT — the rule, so you never learn it
from a red. Half A has to FIND the number, and no derivation can read every
English sentence, so a bounded family of shapes carries the claim. Put the
number in a sentence about the outbound census, in any ONE of these:

    "... exactly four ..."     "... all four ..."     "... just four ..."
    "... four outbound paths ..."        (the number next to the noun)
    "... four ... the complete census ..."
    digits work everywhere a word does: "exactly 4"

Anything else reads as NO CLAIM and the fence says so by name. Measured
rewrites that DO NOT carry the count, so you can see the edge:
"the outbound census has four members" - "outbound paths numbering four" -
"only four, and that is the whole outbound census" (it wants the literal
"complete census"). Two census sentences that disagree also read as no
claim, deliberately: a page that says two different numbers about one
subject has an ambiguity a fence may not resolve for it.

THIS LIST IS A CONTRACT WITH THE WRITER, NOT A PIN ON THE PROSE. Everything
around the number is free. If you need a shape that is not here, widen the
family - do not reword the page to satisfy the regex, and do not delete the
sentence. A fence over prose that is not written down is a copy pin that has
not been caught yet.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_no_listener_fence import counted_outbound_paths, measure_network_surface


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
README = REPOSITORY_ROOT / "README.md"
TRUTH = REPOSITORY_ROOT / "docs" / "TRUTH-GUARANTEES.md"

_CARDINALS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

# Subject-derived count (Am.2 F1/F2): numbers only in outbound-census sentences.
_CENSUS_SUBJECT = re.compile(
    r"outbound\s+path|counted\s+outbound|outbound\s+census",
    re.IGNORECASE,
)

_COUNT_IN_SUBJECT = (
    re.compile(
        r"\b(?:exactly|all|just)\s+"
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b"
        r"[^.]*\bcomplete census\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(?:counted\s+)?outbound(?:\s+path)?s?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:there are|are)\s+"
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(?:outbound\s+paths?\s+in\s+total|counted\s+outbound)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\boutbound\s+census\s+names\s+"
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        re.IGNORECASE,
    ),
)

_PATH_NEEDLES = {
    "floati/adapters/herdr.py": ("herdr",),
    "floati/adapters/t3.py": ("t3",),
    "floati/update_transport.py:http.client": ("update",),
    "floati/gh_process.py:read_github_issue:gh issue view": ("gh", "github"),
}

# Derived presence needles: bare two-char/generic substrings are not a test (F3).
_PRESENCE_NEEDLES = {
    "floati/adapters/herdr.py": ("herdr",),
    "floati/adapters/t3.py": ("t3",),
    "floati/update_transport.py:http.client": (
        "update_transport.py",
        "https fetch for updates",
        "the update fetch",
    ),
    "floati/gh_process.py:read_github_issue:gh issue view": (
        "gh issue view",
        "`gh` executable",
        "intake adopt --source github",
    ),
}

_CLASS_MARKERS = {
    "target_bound_arm": ("target-bound",),
    "channel_bound_consent": ("channel-bound",),
    "typed_no_receipt": (
        "no consent receipt of its own",
        "does not yet have its own consent receipt",
        "has no consent receipt",
    ),
}

_FORBIDDEN_ASSERTIONS = (
    re.compile(r"\btelemetry is on\b", re.IGNORECASE),
    re.compile(r"\banalytics are enabled\b", re.IGNORECASE),
    re.compile(r"\bwe collect\b", re.IGNORECASE),
    re.compile(r"\bsends usage\b", re.IGNORECASE),
    re.compile(r"\btelemetry is on by default\b", re.IGNORECASE),
)


def _parse_cardinal(token: str) -> int | None:
    lowered = token.lower()
    if lowered.isdigit():
        return int(lowered)
    return _CARDINALS.get(lowered)


def _count_in_census_sentence(sentence: str) -> int | None:
    if not _CENSUS_SUBJECT.search(sentence):
        return None
    for pattern in _COUNT_IN_SUBJECT:
        match = pattern.search(sentence)
        if match is not None:
            return _parse_cardinal(match.group(1))
    return None


def page_claimed_count(text: str) -> int | None:
    counts: list[int] = []
    for sentence in _sentences(text):
        count = _count_in_census_sentence(sentence)
        if count is not None:
            counts.append(count)
    if not counts:
        return None
    if len(set(counts)) == 1:
        return counts[0]
    return None


def path_named_on_page(text: str, path_id: str) -> bool:
    derived = _PRESENCE_NEEDLES.get(path_id) or ()
    bare = _PATH_NEEDLES.get(path_id) or ()
    for sentence in _sentences(text):
        folded = sentence.casefold()
        if any(needle.casefold() in folded for needle in derived):
            return True
        if any(needle.casefold() in folded for needle in bare):
            if any(
                marker in folded
                for markers in _CLASS_MARKERS.values()
                for marker in markers
            ):
                return True
    return False


def page_names_bound_classes(text: str) -> bool:
    folded = text.casefold()
    return "target-bound" in folded or "channel-bound" in folded


def _sentences(text: str) -> tuple[str, ...]:
    chunk = " ".join(text.split())
    return tuple(part.strip() for part in re.split(r"[.;]", chunk) if part.strip())


def page_class_for_path(text: str, path_id: str) -> str | None:
    needles = _PATH_NEEDLES.get(path_id) or ()
    hits: set[str] = set()
    for sentence in _sentences(text):
        folded = sentence.casefold()
        if not any(needle.casefold() in folded for needle in needles):
            continue
        for cls, markers in _CLASS_MARKERS.items():
            if any(marker in folded for marker in markers):
                hits.add(cls)
    if len(hits) == 1:
        return next(iter(hits))
    return None


def forbidden_assertion_hits(text: str) -> tuple[str, ...]:
    return tuple(
        pattern.pattern
        for pattern in _FORBIDDEN_ASSERTIONS
        if pattern.search(text)
    )


def truth_pin_findings(
    readme: str,
    truth: str,
    *,
    surface: tuple | None = None,
) -> list[str]:
    census = surface or measure_network_surface(Path("floati"))
    paths = counted_outbound_paths(census)
    findings: list[str] = []
    named = ", ".join(path_id for path_id, _cls in paths)
    for text, label in ((readme, "README.md"), (truth, "docs/TRUTH-GUARANTEES.md")):
        claimed = page_claimed_count(text)
        if claimed != len(paths):
            if claimed is None:
                findings.append(
                    f"{label}: claimed None outbound paths, census has "
                    f"{len(paths)} ({named}) -- no sentence about the outbound "
                    "census carries a number in a shape this fence reads, or two "
                    "such sentences disagree; see the accepted shapes in this "
                    "module's docstring"
                )
            else:
                findings.append(
                    f"{label}: claimed {claimed} outbound paths, census has "
                    f"{len(paths)} ({named})"
                )
        for path_id, cls in paths:
            if not path_named_on_page(text, path_id):
                findings.append(f"{label}: missing path {path_id}")
                continue
            observed = page_class_for_path(text, path_id)
            if cls == "typed_no_receipt":
                folded = text.casefold()
                if observed != cls and not any(
                    marker in folded for marker in _CLASS_MARKERS[cls]
                ):
                    findings.append(
                        f"{label}: consent class for {path_id} is {observed}, "
                        f"census has {cls}"
                    )
            elif page_names_bound_classes(text) and observed != cls:
                findings.append(
                    f"{label}: consent class for {path_id} is {observed}, "
                    f"census has {cls}"
                )
        for hit in forbidden_assertion_hits(text):
            findings.append(f"{label}: forbidden assertion {hit}")
    return findings


def _move_outbound_claim_to_end(readme: str, truth: str) -> tuple[str, str]:
    """Green-control rewrite: relocate the census claim to the file tail."""

    readme_lines = readme.splitlines(keepends=True)
    claim_start = next(
        i
        for i, line in enumerate(readme_lines)
        if "the outbound paths are" in line.casefold()
    )
    claim_end = claim_start
    while claim_end < len(readme_lines) and not readme_lines[claim_end].startswith("## "):
        claim_end += 1
    claim_block = readme_lines[claim_start:claim_end]
    body = readme_lines[:claim_start] + readme_lines[claim_end:]
    moved_readme = "".join(body + ["\n"] + claim_block)
    moved_truth = (
        truth.replace(
            "There are exactly four\ncounted outbound paths:",
            "The outbound census names four paths:",
            1,
        )
    )
    return moved_readme, moved_truth


class TruthPinTests(unittest.TestCase):
    def test_pages_agree_with_the_fence_census(self) -> None:
        findings = truth_pin_findings(
            README.read_text(encoding="utf-8"),
            TRUTH.read_text(encoding="utf-8"),
        )
        self.assertEqual([], findings)
        paths = counted_outbound_paths(measure_network_surface(Path("floati")))
        self.assertEqual(4, len(paths), paths)
        classes = {cls for _path, cls in paths}
        self.assertEqual(
            {"target_bound_arm", "channel_bound_consent", "typed_no_receipt"},
            classes,
        )

    def test_a_fifth_outbound_path_reds_and_names_the_new_path(self) -> None:
        planted = REPOSITORY_ROOT / "floati" / "_truth_pin_planted_egress.py"
        self.addCleanup(lambda: planted.exists() and planted.unlink())
        planted.write_text("import http.client\n", encoding="utf-8")
        findings = truth_pin_findings(
            README.read_text(encoding="utf-8"),
            TRUTH.read_text(encoding="utf-8"),
        )
        named = [row for row in findings if "census has 5" in row and "http.client" in row]
        self.assertTrue(named, findings)
        self.assertTrue(
            any("floati/_truth_pin_planted_egress.py:http.client" in row for row in findings),
            findings,
        )

    def test_a_moved_page_number_reds_with_the_census_still(self) -> None:
        readme = README.read_text(encoding="utf-8").replace("exactly four", "exactly five", 1)
        findings = truth_pin_findings(
            readme,
            TRUTH.read_text(encoding="utf-8"),
        )
        self.assertTrue(
            any(row.startswith("README.md: claimed 5 outbound paths, census has 4") for row in findings),
            findings,
        )

    def test_inverted_telemetry_sentence_is_refused(self) -> None:
        readme = README.read_text(encoding="utf-8").replace(
            "No telemetry, ever.",
            "Telemetry is on by default.",
            1,
        )
        findings = truth_pin_findings(
            readme,
            TRUTH.read_text(encoding="utf-8"),
        )
        self.assertTrue(
            any("README.md: forbidden assertion" in row for row in findings),
            findings,
        )

    def test_voice_only_number_rewrites_stay_green(self) -> None:
        """F1 green-control SET: five rewrites, same census, different shape."""

        readme = README.read_text(encoding="utf-8")
        truth = TRUTH.read_text(encoding="utf-8")
        variants = (
            (
                readme.replace("exactly four:", "four, and that is the complete census:", 1),
                truth.replace(
                    "exactly four\ncounted outbound paths:",
                    "four, and that is the complete census of outbound paths:",
                    1,
                ),
            ),
            (
                readme.replace("exactly four:", "all four:", 1),
                truth.replace(
                    "exactly four\ncounted outbound paths:",
                    "all four counted outbound paths:",
                    1,
                ),
            ),
            (
                readme.replace("exactly four:", "just four:", 1),
                truth.replace(
                    "exactly four\ncounted outbound paths:",
                    "just four counted outbound paths:",
                    1,
                ),
            ),
            (
                readme.replace("exactly four:", "four outbound paths in total:", 1),
                truth.replace(
                    "There are exactly four",
                    "There are four outbound paths in total",
                    1,
                ),
            ),
            _move_outbound_claim_to_end(readme, truth),
        )
        for index, (rewritten_readme, rewritten_truth) in enumerate(variants):
            with self.subTest(rewrite=index):
                self.assertNotEqual(rewritten_readme, readme)
                findings = truth_pin_findings(rewritten_readme, rewritten_truth)
                self.assertEqual([], findings)

    def test_f2_unrelated_exactly_number_does_not_steal_claim(self) -> None:
        readme = "Puddle ships exactly one binary.\n\n" + README.read_text(encoding="utf-8")
        findings = truth_pin_findings(
            readme,
            TRUTH.read_text(encoding="utf-8"),
        )
        self.assertEqual([], findings)

    def test_f3_presence_requires_path_and_class_in_one_sentence(self) -> None:
        decoy = "The update system is fine. We use github for issues."
        findings = truth_pin_findings(decoy, decoy)
        falsely_present = [
            path_id
            for path_id in (
                "floati/update_transport.py:http.client",
                "floati/gh_process.py:read_github_issue:gh issue view",
            )
            if not any(f"missing path {path_id}" in row for row in findings)
        ]
        self.assertEqual([], falsely_present, findings)

    def test_inverted_consent_classes_reds(self) -> None:
        truth = (
            TRUTH.read_text(encoding="utf-8")
            .replace("target-bound", "@@SWAP_TARGET@@")
            .replace("channel-bound", "target-bound")
            .replace("@@SWAP_TARGET@@", "channel-bound")
        )
        self.assertNotEqual(truth, TRUTH.read_text(encoding="utf-8"))
        findings = truth_pin_findings(
            README.read_text(encoding="utf-8"),
            truth,
        )
        class_hits = [
            row
            for row in findings
            if "consent class" in row and "docs/TRUTH-GUARANTEES.md" in row
        ]
        self.assertTrue(class_hits, findings)

    def test_perturbation_reverts_exactly(self) -> None:
        before_readme = README.read_bytes()
        before_truth = TRUTH.read_bytes()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            copy = Path(temporary) / "README.md"
            copy.write_bytes(before_readme)
            copy.write_text(
                copy.read_text(encoding="utf-8").replace("exactly four", "exactly nine", 1),
                encoding="utf-8",
            )
            self.assertNotEqual(before_readme, copy.read_bytes())
        self.assertEqual(before_readme, README.read_bytes())
        self.assertEqual(before_truth, TRUTH.read_bytes())
