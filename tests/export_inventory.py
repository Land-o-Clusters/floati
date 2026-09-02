"""The population the retired-name fences walk: the exporter's include set.

R-N4 said "the whole tree". Am.3 narrowed it, and the narrowing is a DERIVATION
rather than a list: this module asks `scripts/export_public.py` to classify each
tracked path against the committed policy and keeps the ones no export excludes.
Two properties are load-bearing.

* **Never a hand-written path list, never a regex over path names.** A tuple in
  front of a fence is the exact defect NAME-2 exists to remove -- it is not
  absent, so nobody adds one, and it is not looking, so nobody is warned. When
  the policy changes, this population changes with it in the same commit.
* **Fail closed.** A path the exporter cannot classify (`unresolved`) is KEPT in
  the population. The exporter refuses such a path at export time; a fence that
  dropped it would go quiet on exactly the files nobody has ruled on yet.

**Am.4: the classification needs the PUBLISHED BASELINE, and leaving it empty
was not the strict reading.** `classify_path` consults `public_paths` BEFORE it
consults `new_private_prefixes`, so an empty baseline does not tighten the
population -- it silently drops every already-published document under a
now-private prefix. Measured: 831 files against the exporter's real 1,037, **206
exported files the fences never walked** (`docs/evidence/` 175, `docs/design/`
23, `docs/research/` 7, `docs/rulings/` 1). The baseline is therefore read from
`.github/public-export-baseline.v0.json`, a snapshot of the published tree at a
recorded public commit, and the population it produces is asserted equal to a
real projection's `included_paths` in the receipt.

The snapshot is sound between exports and only between exports: the published
tree changes only when an export runs, so the file is refreshed by whoever runs
one. A path added to the harbor after the snapshot still classifies correctly --
it is not in the baseline, so it falls through to the prefix rules, which is the
right answer for a path that has never been published.

The dated ops history the narrowing excludes is measured in
`docs/evidence/name-2c-widened-2026-09-02.md`, by policy class, and is
re-derivable in one command.

**In the public projection there is nothing to narrow.** The exporter and the
policy are themselves `private_only_paths`, so neither reaches the published
tree -- and there every tracked file is, by construction, an included file. The
include set of a tree that carries no export policy is that tree, so the
fallback below is an identity rather than a loosening. A tree carrying exactly
one of the two is neither case and raises, because a fence may not guess which
half is missing.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from typing import Iterable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPORTER_RELATIVE = "scripts/export_public.py"
POLICY_RELATIVE = ".github/public-export-policy.v0.json"
BASELINE_RELATIVE = ".github/public-export-baseline.v0.json"


def published_baseline(root: Path = REPOSITORY_ROOT) -> frozenset[str]:
    """Return the published tree's paths, as recorded by the last export.

    This is `classify_path`'s `public_paths`. It cannot be derived from the
    harbor -- "was this document public before its prefix became private" is a
    fact about the OTHER repository -- so it is a recorded snapshot, and the
    commit it was taken at is recorded beside it.
    """

    document = json.loads((root / BASELINE_RELATIVE).read_text(encoding="utf-8"))
    if document.get("schema_version") != 0 or isinstance(
        document.get("schema_version"), bool
    ):
        raise RuntimeError("published baseline schema_version must be 0")
    paths = document.get("public_paths")
    if (
        not isinstance(paths, list)
        or any(not isinstance(value, str) or not value for value in paths)
        or paths != sorted(set(paths))
    ):
        raise RuntimeError("published baseline must be a sorted unique string list")
    return frozenset(paths)


def export_policy_is_present(root: Path = REPOSITORY_ROOT) -> bool:
    """Return whether this tree carries BOTH halves of the classification."""

    present = {
        EXPORTER_RELATIVE: (root / EXPORTER_RELATIVE).is_file(),
        POLICY_RELATIVE: (root / POLICY_RELATIVE).is_file(),
        BASELINE_RELATIVE: (root / BASELINE_RELATIVE).is_file(),
    }
    if len(set(present.values())) != 1:
        raise RuntimeError(
            "export classification is partly present: "
            + " ".join(f"{name}={value}" for name, value in present.items())
        )
    return present[EXPORTER_RELATIVE]


def export_policy(root: Path = REPOSITORY_ROOT):
    """Return the committed export policy, loaded by the exporter's own reader."""

    exporter = importlib.import_module("scripts.export_public")
    return exporter.ExportPolicy.load(root / POLICY_RELATIVE)


def tracked_files(root: Path = REPOSITORY_ROOT) -> tuple[str, ...]:
    """Return the Git publication inventory, so the population cannot go stale."""

    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return tuple(
        entry.decode("utf-8", errors="replace")
        for entry in listing.split(b"\0")
        if entry
    )


def classify_inventory(
    relatives: Iterable[str], *, root: Path = REPOSITORY_ROOT
) -> tuple[str, ...]:
    """Return the paths no export excludes, in the order supplied.

    A pure function of path STRINGS: nothing here reads a file's content, so a
    control can classify a planted fixture path without planting anything.
    """

    ordered: Sequence[str] = tuple(relatives)
    if not export_policy_is_present(root):
        return tuple(ordered)
    exporter = importlib.import_module("scripts.export_public")
    policy = exporter.ExportPolicy.load(root / POLICY_RELATIVE)
    # `public_paths` is the PUBLISHED baseline, and it is consulted BEFORE
    # `new_private_prefixes`. Passing an empty set here read like the strict
    # choice and was the opposite: it dropped 206 already-published documents
    # from the population. The recorded snapshot is the baseline.
    baseline = published_baseline(root)
    return tuple(
        relative
        for relative in ordered
        if exporter.classify_path(relative, baseline, policy).disposition != "exclude"
    )


def export_include_set(root: Path = REPOSITORY_ROOT) -> tuple[str, ...]:
    """Return every tracked path this repository's own exporter would carry."""

    return classify_inventory(tracked_files(root), root=root)


def materialise_exposed_tree(
    destination: Path, root: Path = REPOSITORY_ROOT
) -> tuple[str, ...]:
    """Write the include set into `destination` AS THE EXPORT WOULD EXPOSE IT.

    The raw-containment scrub asks "does this byte sequence reach a reader", and
    for an exported file the bytes a reader gets are the ADAPTED ones: the
    exporter redacts recorded invocations, `--repo` values and module
    coordinates at exposure because a transcript is a photograph and is never
    rewritten at source (Am.1, Am.2). Scanning the harbor's raw bytes answers a
    question nobody is asking and reds on 12 documents the publication
    instrument passes.

    This is not the exporter's projection -- no renames, no structural contract,
    no public baseline diff. It is the include set with the text adaptation
    applied, which is the strict subset of the projection a name fence needs.

    In the projection there is no exporter and the tree is already adapted, so
    the files are copied through unchanged.
    """

    exporter = (
        importlib.import_module("scripts.export_public")
        if export_policy_is_present(root)
        else None
    )
    written: list[str] = []
    for relative in export_include_set(root):
        source = root / relative
        if not source.is_file() or source.is_symlink():
            continue
        data = source.read_bytes()
        if exporter is not None:
            try:
                data, _notes = exporter._adapt(relative, data)
            except Exception:  # pragma: no cover - a refusal is not a name finding
                pass
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(relative)
    return tuple(written)
