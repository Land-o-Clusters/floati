"""Refuse a release whose version is spelled four different ways.

RELEASING.md: "The tag, the changelog heading, `pyproject.toml`, and
`__version__` must be four spellings of the same value; the release check
refuses otherwise."
This module is that check. Before it existed the sentence was true of
nobody — a promise a document made and no instrument kept. The fourth
spelling joined last: `pyproject.toml` carries `[project] version`, the
value the packaging tools read, and the check compared the other three
while never opening the file (RELEASE-CHECK-PYPROJECT-1).

The ordinary check accepts Unreleased notes between releases and dated
notes on a release candidate. Naming a tag adds the tag spelling to the
comparison and requires dated notes.

Errors are typed strings in the manifest module's style — a caller prints
them, and each one names the specific disagreement rather than "invalid".
The same gate verifies the cut named by the release being prepared. Existing
tags are checked from their own trees; preparation pins cannot authorize release.
"""

from __future__ import annotations

import io
import re
import subprocess
import tempfile
from html import unescape
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import unquote

from .git_process import fixed_git_command, fixed_git_environment
from .readme_claims import readme_claim_census, readme_claim_findings


VERSION_ASSIGNMENT = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
CHANGELOG_HEADING = re.compile(
    r"^##[ \t]+\[([^\]\n]+)\](?:[ \t]*[—–-][ \t]*(.+?))?[ \t]*$", re.MULTILINE
)
TAG_SPELLING = re.compile(r"^v(.+)$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UNRELEASED = "Unreleased"
PYPROJECT_TABLE = re.compile(r"^\[project\][ \t]*(?:#.*)?$", re.MULTILINE)
NEXT_TABLE = re.compile(r"^\[", re.MULTILINE)
PYPROJECT_VERSION = re.compile(r'^version[ \t]*=[ \t]*"([^"]*)"[ \t]*$', re.MULTILINE)
PRIVATE_REPOSITORY_LINK = re.compile(
    r"\bfloati-harbor/blob\b|(?<![\w.-])"
    r"(?:(?:www\.|api\.|codeload\.|raw\.)?github\.com|raw\.githubusercontent\.com)"
    r"(?::\d+)?[:/](?:repos/)?land-o-clusters/floati-harbor(?:\.git)?"
    r"""(?=$|[/#?\s>\)\]"'`])""",
    re.IGNORECASE,
)


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


def pyproject_version(repo_root) -> Optional[str]:
    """The literal `version` the `[project]` table spells, read as text.

    Bounded like the exporter's release-route reader: a flat, double-quoted
    `version = "X.Y.Z"` line inside `[project]`. A dynamic version, single
    quotes or a form this reader does not parse read as unreadable and
    refuse — the question is what the file spells, not what a TOML engine
    would compute.
    """

    path = Path(repo_root) / "pyproject.toml"
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    table = PYPROJECT_TABLE.search(source)
    if table is None:
        return None
    body = source[table.end():]
    following = NEXT_TABLE.search(body)
    if following is not None:
        body = body[: following.start()]
    match = PYPROJECT_VERSION.search(body)
    return match.group(1) if match else None


def changelog_heading(repo_root) -> Optional[Tuple[str, str]]:
    """The first release heading, including Keep a Changelog's `[Unreleased]`.

    First, because Keep a Changelog puts the newest section at the top and a
    release is always about the newest one.
    """

    path = Path(repo_root) / "CHANGELOG.md"
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = CHANGELOG_HEADING.search(source)
    return (match.group(1), match.group(2) or UNRELEASED) if match else None


def readme_claim_receipt_findings(repo_root) -> List[str]:
    """Dead README links, unlinked claim units and stale reviewed exceptions.

    Claims are sentences or cells, outside comments and fences. A link only
    discharges its own unit; every other claim needs an exact digest in the
    reviewed allowlist. Both kinds of finding enter the existing release gate.
    """

    return readme_claim_findings(repo_root)


def private_repository_link_findings(repo_root) -> List[str]:
    """Report private repository references in the two public release documents."""
    findings = []
    for name in ("CHANGELOG.md", "README.md"):
        try:
            source = (Path(repo_root) / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(source.splitlines(), 1):
            if PRIVATE_REPOSITORY_LINK.search(unquote(unescape(line))):
                findings.append("release_private_repository_link:%s:%d" % (name, number))
    return findings


def ledger_contract_findings(repo_root, *, historical_tag=None, observations=None) -> List[str]:
    """Require a covering cut in preparation notes or a declared tag's notes.

    The contract tool ships in the public projection. Its check verifies the
    cut pointer, history, schema bytes and runtime contract slices, including
    fixture-backed ratifications. A prepared inventory is useful during
    development but cannot authorize a release.
    """
    try:
        source = (Path(repo_root) / "CHANGELOG.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []  # The heading check names the missing document.
    heading = CHANGELOG_HEADING.search(source)
    if heading is None:
        return []  # The heading check names the missing entry.
    body = source[heading.end():]
    following = re.search(r"^##[ \t]+", body, re.MULTILINE)
    if following is not None:
        body = body[:following.start()]
    declarations = re.findall(r"^Ledger contract:.*$", body, re.MULTILINE)
    historical_absence = historical_tag is not None and not declarations
    if historical_absence:
        if observations is not None:
            observations.append("ledger_contract_declaration_absent:%s" % historical_tag)
        if not (Path(repo_root) / "bundle/ledger-contract-current.json").exists():
            return []  # This tree predates cuts; do not infer a contract.

    try:
        from scripts import ledger_contract
    except ImportError:
        return ["ledger_contract_checker_unavailable"]

    try:
        result = ledger_contract.check(Path(repo_root))
    except ledger_contract.ContractRefusal as exc:
        return ["%s:%s" % (exc.code, exc.detail)]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return ["ledger_contract_document_or_io_invalid:%s" % exc]
    if result["classification"] != "unchanged":
        return ["ledger_contract_cut_required"]
    if historical_absence:
        return []  # Verify an available cut without inventing a declaration.

    if len(declarations) != 1:
        return ["ledger_contract_notes_invalid"]
    declaration = re.fullmatch(
        r"Ledger contract: `(c7\.(?:0|[1-9][0-9]*))`\.[ \t]*", declarations[0]
    )
    if declaration is None:
        return ["ledger_contract_notes_invalid"]
    named, latest = declaration.group(1), result["version"]
    if named != latest:
        return ["ledger_contract_version_mismatch:notes=%s,latest=%s" % (named, latest)]
    return []


def _write_tag_snapshot(root, commit, snapshot, environment):
    """Read raw Git blobs, without archive attributes or checkout filters."""
    def git(*arguments, input_data=None):
        return subprocess.run(
            fixed_git_command("/usr/bin/git", root, arguments), input=input_data,
            capture_output=True, timeout=60, check=True, env=environment,
        ).stdout

    entries = []
    for record in git("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, blob = metadata.decode("ascii").split()
        path = Path(raw_path.decode("utf-8"))
        if (kind != "blob" or mode not in ("100644", "100755")
                or path.is_absolute() or ".." in path.parts):
            raise ValueError("tag tree contains a nonordinary path")
        entries.append((path, mode, blob))
    data = io.BytesIO(git("cat-file", "--batch", input_data=(
        "".join(blob + "\n" for _, _, blob in entries).encode("ascii")
    )))
    for path, mode, blob in entries:
        found, kind, size = data.readline().decode("ascii").split()
        if found != blob or kind != "blob" or int(size) < 0:
            raise ValueError("tag blob identity mismatch")
        content = data.read(int(size))
        if len(content) != int(size) or data.read(1) != b"\n":
            raise ValueError("tag blob framing mismatch")
        destination = snapshot / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(int(mode, 8) & 0o777)
    if data.read():
        raise ValueError("tag blob batch has trailing bytes")


def check_release(repo_root, *, tag: Optional[str] = None, observations=None) -> List[str]:
    """Check an existing tag's immutable snapshot, or the tree being prepared.

    Informational historical absences are separate from the error list. A tag
    that does not exist yet is a pre-cut check of the supplied working tree.
    """
    root = Path(repo_root).resolve()
    if tag is not None and TAG_SPELLING.fullmatch(tag) and (root / ".git").exists():
        try:
            environment = fixed_git_environment("/usr/bin/git")
            exists = subprocess.run(
                fixed_git_command("/usr/bin/git", root, (
                    "show-ref", "--verify", "--quiet", "refs/tags/%s" % tag)),
                capture_output=True, timeout=30, check=False, env=environment,
            )
            if exists.returncode not in (0, 1):
                return ["tag_tree_unreadable:%s" % tag]
            if exists.returncode == 0:
                resolved = subprocess.run(
                    fixed_git_command("/usr/bin/git", root, (
                        "rev-parse", "--verify", "refs/tags/%s^{commit}" % tag)),
                    capture_output=True, timeout=30, check=True, env=environment,
                )
                with tempfile.TemporaryDirectory(prefix="release-tag-") as temporary:
                    snapshot = Path(temporary)
                    _write_tag_snapshot(root, resolved.stdout.decode("ascii").strip(),
                                        snapshot, environment)
                    return _check_release_tree(snapshot, tag=tag, historical=True,
                                               observations=observations)
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            return ["tag_tree_unreadable:%s" % tag]
    return _check_release_tree(root, tag=tag, observations=observations)


def _check_release_tree(repo_root, *, tag=None, historical=False, observations=None) -> List[str]:
    """Return every disagreement between the four spellings, typed.

    Without a tag, compare the readable spellings for either Unreleased
    or dated notes. A named tag requires dated notes. A spelling
    that cannot be read is typed and cited, never filled in. A README claim
    whose receipt link is not a file in this tree, or whose own unit has no
    receipt or reviewed exception, is named and refused.
    The latest cut must cover the tree and be named by the current entry.
    """

    errors: List[str] = []

    version = module_version(repo_root)
    if version is None:
        errors.append("version_unreadable")

    heading = changelog_heading(repo_root)
    if heading is None:
        errors.append("changelog_heading_missing")

    pyproject = pyproject_version(repo_root)
    if pyproject is None:
        errors.append("pyproject_version_unreadable")

    tag_version: Optional[str] = None
    if tag is not None:
        match = TAG_SPELLING.match(tag)
        if match is None:
            errors.append("tag_malformed:%s" % tag)
        else:
            tag_version = match.group(1)

    errors.extend(private_repository_link_findings(repo_root))
    errors.extend(readme_claim_receipt_findings(repo_root))
    errors.extend(ledger_contract_findings(
        repo_root, historical_tag=tag if historical else None,
        observations=observations,
    ))

    if version is None or heading is None:
        return errors

    changelog_version, changelog_date = heading
    spellings = [version]
    if changelog_version != UNRELEASED:
        spellings.append(changelog_version)
    if tag_version is not None:
        spellings.append(tag_version)
    if pyproject is not None:
        spellings.append(pyproject)
    if len(set(spellings)) != 1:
        errors.append(
            "spelling_mismatch:module=%s,changelog=%s,tag=%s,pyproject=%s"
            % (
                version,
                changelog_version,
                tag_version if tag_version else version,
                pyproject if pyproject is not None else "unreadable",
            )
        )

    if UNRELEASED in (changelog_version, changelog_date):
        if tag is not None:
            errors.append("changelog_date_unreleased:%s" % changelog_version)
    elif not ISO_DATE.match(changelog_date):
        errors.append("changelog_date_malformed:%s" % changelog_date)

    return errors


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Refuse a release whose version is spelled four different ways."
    )
    parser.add_argument("--repository-root", default=".")
    parser.add_argument(
        "--tag",
        default=None,
        help="an existing tag to verify, or the tag being cut",
    )
    arguments = parser.parse_args(argv)

    observations: List[str] = []
    errors = check_release(arguments.repository_root, tag=arguments.tag, observations=observations)
    for observation in observations:
        print("RELEASE OBSERVATION: %s" % observation, file=sys.stderr)
    for error in errors:
        print("RELEASE CHECK: %s" % error, file=sys.stderr)
    if errors:
        print(
            "RELEASE CHECK: release metadata, README receipts, and the latest "
            "cut ledger contract must agree (RELEASING.md).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
