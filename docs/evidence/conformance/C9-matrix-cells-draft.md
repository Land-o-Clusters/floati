# C9 draft — README matrix cells from C0+C1 (grok, 2026-08-27)

**Row:** C9 format draft. **Not** a README edit (WS-F places cells). Filler while holding for Car 4, per the architect `msg-01a0454701847b89a1c894bf35759d9a`.
**C0 receipt:** `docs/evidence/conformance/C0-machine-harness-inventory.md` (gated PASS).
**C1 receipt:** `docs/evidence/conformance/C1-codex-conformance-live.md` @ `48794c3c78074244c31d39850d680ab38025f5ba` (gated PASS, `surface_verified: true`).
**Inventory bound:** C0. **No cell says more than its row receipt.**

C0 is not a harness cell. It only decides whether a later row may claim `live` / `surface_verified` or must stamp `BATTERY-ONLY`.

## Cell grammar (paste into README `| harness | status |`)

README today has two columns. The status cell carries the receipt link. A harness row is emitted only after that harness's WS-C row is gated PASS.

```markdown
| <harness> | [<stamp>](<receipt-relpath>) |
```

| token | meaning | when |
|---|---|---|
| `<harness>` | exact roster name: `codex` · `claude` · `opencode` · `cursor` · `cline` · `grok-build` · `pi` · `herdr` | always |
| `<stamp>` | `live` | gated PASS **and** `surface_verified: true` in the same row doc (real binary receipt in-doc) |
| `<stamp>` | `BATTERY-ONLY` | gated PASS, no real binary on the machine (C0 NOT PRESENT, or C0 PRESENT but live invocation not earned) |
| `<receipt-relpath>` | repo-relative path of that row's evidence doc | always |

Do not put executable paths, version strings, suite counts, or findings in the cell. Those live in the linked receipt.

## Filled cells (completed rows only)

```markdown
| harness | status |
|---|---|
| codex | [live](docs/evidence/conformance/C1-codex-conformance-live.md) |
```

That is the entire current matrix. C0 does not add a row.

## Drop-in slots (not cells yet — C2..C8 unrun)

C0 live-eligible vs battery-only is a prediction, not a status. Replace `SLOT` with the grammar above when that row is gated PASS. Receipt paths are the brief's one-doc-per-row layout.

| row | harness | C0 binary | predicted stamp if that row later PASSes | receipt path when written |
|---|---|---|---|---|
| C2 | claude | PRESENT `/opt/homebrew/bin/claude` | `live` only if C2 names the launched executable and includes a real-binary receipt | `docs/evidence/conformance/C2-claude-conformance.md` |
| C3 | opencode | PRESENT `/opt/homebrew/bin/opencode` | `live` only with a real-binary receipt in-doc | `docs/evidence/conformance/C3-opencode-conformance.md` |
| C4 | cursor | PRESENT `/opt/homebrew/bin/cursor-agent` (Homebrew `--version` exits 1; local copy `--version` exits 0) | `live` only if C4 receipts the executable it actually launched | `docs/evidence/conformance/C4-cursor-conformance.md` |
| C5 | cline | NOT PRESENT | `BATTERY-ONLY` unless a binary appears after C0 | `docs/evidence/conformance/C5-cline-conformance.md` |
| C6 | grok-build | NOT PRESENT | `BATTERY-ONLY` unless a binary appears after C0 | `docs/evidence/conformance/C6-grok-build-conformance.md` |
| C7 | pi | NOT PRESENT | `BATTERY-ONLY` unless a binary appears after C0 | `docs/evidence/conformance/C7-pi-conformance.md` |
| C8 | herdr | NOT PRESENT | `BATTERY-ONLY` unless a binary appears after C0 | `docs/evidence/conformance/C8-herdr-conformance.md` |

After C8, this doc's "Filled cells" block is the collected C9 payload the architect pastes. Until then, only the `codex` line is paste-ready.

## Mechanical append

When row Cn is gated PASS, append exactly one markdown table row to the filled block, using only `<stamp>` values from that row's evidence (`live` or `BATTERY-ONLY`) and that row's doc path. Do not edit README from this seat.
