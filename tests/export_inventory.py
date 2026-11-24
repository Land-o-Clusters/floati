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

**BASELINE-1 Am.1: the snapshot is checked against the published tip when one
is available.** "Sound between exports" is a statement about the world, and the
fences had no way to notice when it stopped being true -- a baseline older than
the published tree drops already-published documents from this population, and
the fences then pass on a smaller world, which is the direction nobody catches.
`published_baseline` therefore runs a drift check whenever the rehearsal
declares its fresh public clone in `FLOATI_PUBLIC_ROOT`: the recorded
`public_commit` must be an ancestor of that clone's tip, else
`public_export_baseline_stale`. The check is deliberately weaker than currency,
which only a regeneration can establish -- but a NON-ancestor recording is
always wrong, and the two ways this goes wrong in practice (a tip that moved
past the recording; a recording taken against a different history) are both
non-ancestors. A declared root that is not a clone refuses rather than going
quiet; an undeclared root is the ordinary case and reads the file as before.
`scripts/regen_public_baseline.py` is the regeneration the refusal names.

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

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPORTER_RELATIVE = "scripts/export_public.py"
POLICY_RELATIVE = ".github/public-export-policy.v0.json"
BASELINE_RELATIVE = ".github/public-export-baseline.v0.json"
REGENERATOR_MODULE = "scripts.regen_public_baseline"
PUBLIC_ROOT_ENV = "FLOATI_PUBLIC_ROOT"


def declared_public_root() -> Path | None:
    """The rehearsal's public clone, when the operator declared one.

    Undeclared is the ordinary case and means "no clone available"; declared but
    empty is an operator mistake and is not read as a declaration.
    """

    value = os.environ.get(PUBLIC_ROOT_ENV)
    if value is None or not value.strip():
        return None
    return Path(value)


def assert_baseline_current(
    root: Path = REPOSITORY_ROOT, public_root: Path | None = None
) -> str | None:
    """RED when the recorded public commit is not an ancestor of the clone's tip.

    Returns the tip it checked against, or `None` when no public clone is
    available. The refusal is the regenerator's own typed one, so the remedy the
    caller sees names the command that fixes it.
    """

    clone = Path(public_root) if public_root is not None else declared_public_root()
    if clone is None:
        return None
    regenerator = importlib.import_module(REGENERATOR_MODULE)
    document = json.loads((root / BASELINE_RELATIVE).read_text(encoding="utf-8"))
    return regenerator.assert_public_commit_current(
        clone, document.get("public_commit"), "HEAD"
    )


def published_baseline(root: Path = REPOSITORY_ROOT) -> frozenset[str]:
    """Return the published tree's paths, as recorded by the last export.

    This is `classify_path`'s `public_paths`. It cannot be derived from the
    harbor -- "was this document public before its prefix became private" is a
    fact about the OTHER repository -- so it is a recorded snapshot, and the
    commit it was taken at is recorded beside it.

    When a public clone is available the snapshot is checked for drift HERE,
    where the fences read it, rather than in a check beside it that a fence run
    would not reach.
    """

    assert_baseline_current(root)
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


def classify_inventory_excluded(
    relatives: Iterable[str], *, root: Path = REPOSITORY_ROOT
) -> tuple[str, ...]:
    """NET-FENCE-1-F2: the private_only twin of `classify_inventory`.

    The same classifier call, the complementary half: the paths an export
    EXCLUDES. A projection-aware pin that compares only included halves
    goes quiet on exactly the files the policy hides, so the excluded
    half needs its own pin - and that pin needs this to classify it. In
    a policy-less projection the identity fallback excludes nothing, so
    the excluded half is empty there and a private pin is a typed skip.
    """

    ordered: Sequence[str] = tuple(relatives)
    if not export_policy_is_present(root):
        return tuple()
    exporter = importlib.import_module("scripts.export_public")
    policy = exporter.ExportPolicy.load(root / POLICY_RELATIVE)
    baseline = published_baseline(root)
    return tuple(
        relative
        for relative in ordered
        if exporter.classify_path(relative, baseline, policy).disposition == "exclude"
    )


def export_include_set(root: Path = REPOSITORY_ROOT) -> tuple[str, ...]:
    """Return every tracked path this repository's own exporter would carry."""

    return classify_inventory(tracked_files(root), root=root)


# HARBOR-SPEED-R-C: one adaptation per distinct input per process.
#
# Every name fence and every projection leg (test_public_name_fence twice,
# test_source_scrub, test_name_sweep, test_no_listener_fence,
# test_fixture_placement, test_public_export_baseline, test_copy_ledger)
# re-ran the exporter's `_adapt` over the whole include set: 1,401 files, 668
# of them Python sources fully tokenized and untokenized, ~5 s per build at
# harbor main 1fa68e48. The adaptation is a pure function of
#
#   (the path, the file's bytes, the policy, the published baseline,
#    and the exporter code that is loaded in this process),
#
# so it is memoised under exactly that key. Everything that can change during
# a run is part of the key and is RE-READ ON EVERY CALL: the include set (git
# ls-files and classification), each file's bytes (sha256), the policy file's
# bytes, and the baseline (which also keeps the BASELINE-1 drift check on every
# call). The loaded code is fingerprinted by the identity of every global in
# the exporter and the modules its adaptation reaches; a re-bound or patched
# global starts a fresh memo. An adaptation that refuses is never memoised, so
# each caller still sees the refusal its own way (raw bytes in the lenient
# helper, a propagated error in the strict one). Values are immutable bytes.
_ADAPTATION_MODULES = (
    "scripts.export_public",
    "scripts.public_name_fence",
    "floati.identity_fence",
)
_ADAPTATION_MEMO: dict[str, object] = {"fingerprint": None, "entries": {}}


def _adaptation_fingerprint() -> tuple[tuple[tuple[str, int], ...], list[object]]:
    keys: list[tuple[str, int]] = []
    held: list[object] = []
    for name in _ADAPTATION_MODULES:
        module = sys.modules[name]
        for attribute, value in sorted(vars(module).items()):
            keys.append((f"{name}.{attribute}", id(value)))
            held.append(value)
    return tuple(keys), held


def _adapted_bytes_source(
    root: Path, exporter: object, policy: object, baseline: set[str]
) -> Callable[[str, bytes], bytes]:
    """Return `adapt(relative, data) -> bytes`, memoised under a content key."""

    fingerprint, held = _adaptation_fingerprint()
    if _ADAPTATION_MEMO["fingerprint"] != fingerprint:
        # Holding the globals keeps their ids from being reused while the
        # fingerprint that names them is live.
        _ADAPTATION_MEMO.update(fingerprint=fingerprint, held=held, entries={})
    entries = _ADAPTATION_MEMO["entries"]
    assert isinstance(entries, dict)
    context = (
        hashlib.sha256((root / POLICY_RELATIVE).read_bytes()).hexdigest(),
        hashlib.sha256("\0".join(sorted(baseline)).encode("utf-8")).hexdigest(),
    )

    def adapt(relative: str, data: bytes) -> bytes:
        key = (relative, hashlib.sha256(data).hexdigest(), context)
        cached = entries.get(key)
        if cached is not None:
            return data if cached is _UNCHANGED else cached
        adapted, _notes = exporter._adapt(  # type: ignore[attr-defined]
            relative,
            data,
            policy=policy,
            public_paths=baseline,
        )
        entries[key] = _UNCHANGED if adapted == data else bytes(adapted)
        return adapted

    return adapt


_UNCHANGED = object()


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
    policy = export_policy(root) if exporter is not None else None
    baseline = set(published_baseline(root)) if exporter is not None else None
    adapt = (
        _adapted_bytes_source(root, exporter, policy, baseline)
        if exporter is not None and baseline is not None
        else None
    )
    written: list[str] = []
    for relative in export_include_set(root):
        source = root / relative
        if not source.is_file() or source.is_symlink():
            continue
        data = source.read_bytes()
        if adapt is not None:
            try:
                data = adapt(relative, data)
            except Exception:  # pragma: no cover - a refusal is not a name finding
                pass
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(relative)
    return tuple(written)


def materialise_adapted_tree(
    destination: Path, root: Path = REPOSITORY_ROOT
) -> tuple[str, ...]:
    """Write the include set into `destination` with adaptation REFUSING LOUDLY.

    `materialise_exposed_tree` serves the name fences, where a file whose
    adaptation refuses is written raw and the scrub reads the raw bytes; a
    projection leg built that way would go quiet on exactly the drift this
    helper exists to catch. Here an adaptation refusal PROPAGATES: a leg whose
    projection cannot be built as the exporter builds it must error, not
    silently fall back to the raw bytes the leg exists to stop trusting.

    Like the lenient helper this is not the exporter's full projection -- no
    renames, no structural contract, no public baseline diff -- and in a
    policy-less tree there is nothing to adapt, so the files copy through
    unchanged.
    """

    exporter = (
        importlib.import_module("scripts.export_public")
        if export_policy_is_present(root)
        else None
    )
    policy = export_policy(root) if exporter is not None else None
    baseline = set(published_baseline(root)) if exporter is not None else None
    adapt = (
        _adapted_bytes_source(root, exporter, policy, baseline)
        if exporter is not None and baseline is not None
        else None
    )
    written: list[str] = []
    for relative in export_include_set(root):
        source = root / relative
        if not source.is_file() or source.is_symlink():
            continue
        data = source.read_bytes()
        if adapt is not None:
            data = adapt(relative, data)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(relative)
    return tuple(written)
