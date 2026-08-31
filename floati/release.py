"""Refuse a release whose version is spelled three different ways.

RELEASING.md: "The tag, the changelog heading, and `__version__` must be
three spellings of the same value; the release check refuses otherwise."
This module is that check. Before it existed the sentence was true of
nobody — a promise a document made and no instrument kept.

The two modes are deliberate. On a pull request there is no tag and the
changelog heading is still `Unreleased`, so only the pair is compared; that
is the state the repository lives in between releases and it is not an
error. Name a tag and the release rules apply: the tag must spell the same
value with a `v`, and the heading must carry a date rather than a promise.

Errors are typed strings in the manifest module's style — a caller prints
them, and each one names the specific disagreement rather than "invalid".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple


VERSION_ASSIGNMENT = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
CHANGELOG_HEADING = re.compile(r"^##\s+\[([^\]]+)\]\s*[—–-]\s*(.+?)\s*$", re.MULTILINE)
TAG_SPELLING = re.compile(r"^v(.+)$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UNRELEASED = "Unreleased"


def module_version(repo_root) -> Optional[str]:
    """The value `floati/__init__.py` assigns, read as text rather than imported.

    Read, not imported: the question is what the file *spells*, and importing
    a package to ask what it says lets a re-export or a computed value answer
    for it.
    """

    path = Path(repo_root) / "floati" / "__init__.py"
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = VERSION_ASSIGNMENT.search(source)
    return match.group(1) if match else None


def changelog_heading(repo_root) -> Optional[Tuple[str, str]]:
    """The first `## [version] — date` heading in CHANGELOG.md, as (version, date).

    First, because Keep a Changelog puts the newest section at the top and a
    release is always about the newest one.
    """

    path = Path(repo_root) / "CHANGELOG.md"
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = CHANGELOG_HEADING.search(source)
    return (match.group(1), match.group(2)) if match else None


def check_release(repo_root, *, tag: Optional[str] = None) -> List[str]:
    """Return every disagreement between the three spellings, typed.

    With `tag=None` only the module and the changelog are compared, and an
    `Unreleased` heading is correct. With a tag named, the release rules
    apply and an undated heading is a refusal.
    """

    errors: List[str] = []

    version = module_version(repo_root)
    if version is None:
        errors.append("version_unreadable")

    heading = changelog_heading(repo_root)
    if heading is None:
        errors.append("changelog_heading_missing")

    tag_version: Optional[str] = None
    if tag is not None:
        match = TAG_SPELLING.match(tag)
        if match is None:
            errors.append("tag_malformed:%s" % tag)
        else:
            tag_version = match.group(1)

    if version is None or heading is None:
        return errors

    changelog_version, changelog_date = heading
    spellings = [version, changelog_version]
    if tag_version is not None:
        spellings.append(tag_version)
    if len(set(spellings)) != 1:
        errors.append(
            "spelling_mismatch:module=%s,changelog=%s,tag=%s"
            % (version, changelog_version, tag_version if tag_version else version)
        )

    if tag is not None:
        if changelog_date == UNRELEASED:
            errors.append("changelog_date_unreleased:%s" % changelog_version)
        elif not ISO_DATE.match(changelog_date):
            errors.append("changelog_date_malformed:%s" % changelog_date)

    return errors


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Refuse a release whose version is spelled three different ways."
    )
    parser.add_argument("--repository-root", default=".")
    parser.add_argument(
        "--tag",
        default=None,
        help="the tag being cut, e.g. v0.1.0; omit to check only the pair",
    )
    arguments = parser.parse_args(argv)

    errors = check_release(arguments.repository_root, tag=arguments.tag)
    for error in errors:
        print("RELEASE CHECK: %s" % error, file=sys.stderr)
    if errors:
        print(
            "RELEASE CHECK: the tag, the changelog heading, and __version__ "
            "must be three spellings of the same value (RELEASING.md).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
