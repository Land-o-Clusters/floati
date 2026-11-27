# Releasing

## Versions

Floati uses semantic versioning, applied honestly. While the version starts
with `0.`, minor versions may change anything; we will not pretend otherwise.
**1.0.0 means the receipt vocabulary is frozen** — the schemas under
`schemas/` stop changing shape except by addition. That is the only promise
1.0 will make, and it is the one that matters for a product whose output is
records.

`floati/__init__.py` holds the version. The tag, the changelog heading,
`pyproject.toml`, and `__version__` must be four spellings of the same
value; the release check refuses otherwise. Versions are numbers, not names.

Run the ordinary check on both Unreleased notes and dated release candidates.
Supply a tag to verify all four version spellings.

    python3 -m floati.release                   # Unreleased notes or a dated candidate
    python3 -m floati.release --tag v0.1.0       # the tag's tree, or a pre-cut check

The first runs two ways: as `tests/test_release_check.py` inside the canonical
suite, so a drift goes red on every seat and every branch; and as a step in
`release-gate.yml`, which is gated to the public repository and therefore runs
on the exported projection. Before cutting a new tag, rename `[Unreleased]`
to the release version and date, and make the four version spellings agree.
You can leave a fresh, empty `[Unreleased]` section immediately above the
dated entry.
Run the second mode by hand before cutting the tag. When that tag already
exists, the check reads an isolated snapshot of the tag's own tree, including
its metadata, README receipts, release notes and contract cut. Later working
tree changes do not describe the tag.

Preparing a release also requires the latest cut ledger contract. The release check
reuses `scripts/ledger_contract.py check` to verify the current cut, its
history, every governed schema byte and runtime contract source slice.
Preparation pins alone do not authorize release. Put exactly one declaration
in `CHANGELOG.md` under Keep a Changelog's `## [Unreleased]`, on its own line:

```text
Ledger contract: `c7.16`.
```

Name the latest cut in that declaration. Keep it with those notes when they
become a dated release. Never add it to an entry that has already shipped.
An existing tag with no declaration reports
`ledger_contract_declaration_absent:<tag>` as an informational observation;
that absence does not fail the old tag or invent a contract version for it.
A tag with a declaration must name the cut covering its own tree, even if
the working tree has a newer cut. Malformed or duplicate declarations fail.

If the check reports
`ledger_contract_recut_required`, assess the change against that cut with
preserved-record fixtures. A proven schema loosening may be ratified with
those fixtures; other contract changes require a new cut. Preparation is
not a substitute for ratification or a cut.

## A release is a receipt

Every release consists of:

- an annotated tag `vX.Y.Z` whose message is that version's changelog section;
- a `CHANGELOG.md` section, written by hand — this project's history is prose,
  and no generator writes honest prose;
- the release receipt: the bundle manifest SHA-256 of the released tree, the
  full test-suite result (count, duration, platform), and links to the
  conformance receipts behind the README's harness table.

## Verifying a release

You do not have to trust a release announcement, because the verbs ship with
the product:

    floati install --source <clone> --destination <dir> --ref vX.Y.Z
    floati doctor --root <fleet> --source <clone> --ref vX.Y.Z

Install deploys exactly the manifest at the tag; doctor tells you whether what
is on your disk still matches it, file by file.

## Cadence

There is no release schedule. A release happens when the changelog says
something worth reading. Promising a cadence we would not keep is the kind of
claim this project exists to avoid.
