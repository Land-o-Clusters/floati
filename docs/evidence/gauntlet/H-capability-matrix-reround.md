# H — capability-matrix re-round (derivability ≠ capability)

**Seat:** grok · **Root:** puddle-fleet · **Date:** 2026-08-28
**Spec correction:** `docs/design/capability-matrix-spec-2026-08-28.md` @
`b9e5d2ec857e77498d9d047b672a8373b137cf1e` (cherry-picked onto this lane;
conflict resolved by keeping the Claude re-measure refusal section already
on main tip plus the first-seeding correction).

## Order

Architect envelope `msg-01a046b2aafc7bd78bc7200ad9b11184`: one re-round.
T1 proves derivability, not shipped auto-turnover. All
`auto_turnover.value` → `—` until tide T2–T4 land. The five class-A
derivable surfaces keep their tide receipts seated and gain
`derivable: "A"`. Compaction column header must name the harness's own
native compact verb.

## Diff applied

| change | proof |
|---|---|
| 19/19 `auto_turnover` cells are `—` | dataset scan |
| five `derivable: "A"` (codex/cli, claude/cli, opencode/cli, grok/cli, pi/cli) | same records; receipts unchanged (`T1-depth2.md` ×2, `T1-tide-survey.md` ×3) |
| compaction column title `native compact verb` | `docs/capability-matrix.v0.json` columns[] |
| renderer pins that header via `COMPACTION_HEADER` | `scripts/capability-matrix-render.py` |
| 133 records; zero uncited non-dashes | renderer + scan |

Ship-day flip for auto-turnover is one value change (`—` → `auto`) on those
five records; receipts already seated.

## Not done here

No README swap. No product source. No tide T2–T4 work.
