# Releasing

## Versions

Floati uses semantic versioning, applied honestly. While the version starts
with `0.`, minor versions may change anything; we will not pretend otherwise.
**1.0.0 means the receipt vocabulary is frozen** — the schemas under
`schemas/` stop changing shape except by addition. That is the only promise
1.0 will make, and it is the one that matters for a product whose output is
records.

`floati/__init__.py` holds the version. The tag, the changelog heading, and
`__version__` must be three spellings of the same value; the release check
refuses otherwise. Versions are numbers, not names.

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
