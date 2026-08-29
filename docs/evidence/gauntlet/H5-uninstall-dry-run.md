# H5 — uninstall residue audit (dry-run)

**Family:** uninstall-dry-run. Capture sha256 `8b01eaeff030e4ec3de568c09b25fb28303fccc0e5d0448f1ae9c1d25aada149`.
**Trunk:** `c4dd4a164328f91407e4103562a0e6308d573f73`
**Scratch destination:** `.../h20260828004028/install-dest`
**Law:** `--dry-run` only. `--purge` not used. Live fleet root not passed.

## Journaled fixture

Two journal entries: shipped `install-dest/shipped.txt` (file) and the scratch bus root (`bus_root`, preserved). Dry-run exit 0. Owned file **21 bytes before and after** (`owned_unchanged: true`).

Plan includes `WILL REMOVE` for the shipped file and `PRESERVED` for the bus root. Nothing was deleted.

## Empty destination (no journal)

exit 0. `journal entries: 0`, `WILL REMOVE: (nothing)`. Unresolved line: `no wiring journal at .../empty-install/.floati-install/wiring-journal.v1.jsonl; nothing attributable (uninstall never globs)`.

## Defects filed (not fixed)

1. **Missing-journal copy skew.** Contract text is "no manifest found; nothing attributable". Render labels that case under `UNRESOLVED (journal corrupt at offset — fail-closed)` even though the journal is absent, not corrupt. Not repaired.

**Verdict: PASS** (dry-run; residue plan emitted; no mutation)
