# Issue #3 — installer-shadow check requires the install scripts dir on the scanned PATH

Status: **GATE REQUESTED — DOCS SLOTS OPENED; THE PROSE ITSELF IS THE ARCHITECT'S**

Date: 2026-08-18

## Identity and authority

- Node: `build lane` (builder seat, ad-hoc fleet-ops window)
- Worktree: `~/Projects/floati`
- Branch: `lane/fleet-ops-window`
- Predecessor commit (issue #2, gated DONE):
  `8253630...`
- Governing issue: GitHub issue #3
- Dispatch authority: architect message `msg-01a017bc90b271b1b85d686b182b8a64`
  — "GO ISSUE #3 (docs: installer-shadow check requires the install scripts
  dir on scanned PATH) — docs-only."

## The honest problem with delivering this item

Issue #3 asks for two things: a line in the doctor help text, and a short
operator note in the living docs. **Both are architect-owned prose.**

- The doctor help page is a registered copy entry (`help.doctor`) and appears
  verbatim in `docs/COPY-LEDGER.md`.
- `docs/DESIGN.md` is in `LIVING_PUBLIC_DOCS` in `tests/test_name_sweep.py`,
  the governed public-prose set.

So this lane cannot write the sentences the issue asks for without violating
the standing copy law. What was delivered instead, per that law: the two
slots are **opened in the exact right places**, as unfilled placeholders, and
pinned by tests so they cannot be quietly dropped or quietly filled by a
non-architect hand. The requirement is stated in this evidence file, in the
architect's queue, and in the tests — but not yet in the product's voice.

**This item is therefore only half-closable by me.** It closes when the
architect writes the two strings.

| Key | Where it renders |
| --- | --- |
| `help.doctor.installer_shadow_path` | `floati doctor --help`, options block |
| `design.doctor.installer_shadow_path` | `docs/DESIGN.md`, beside the other doctor paragraphs |

The `DESIGN.md` slot was placed immediately after the existing
`--gateway-config` doctor paragraph, which is where doctor's per-family
behavior is already described. The paragraph above it, which enumerates what
doctor checks (root identity, registry/liveness agreement, manifest, currency,
symlink entries, consumption coordinate), does not mention the installer-shadow
family at all — reported, not edited, because that sentence is hers.

## The behavior the prose must describe, now pinned by tests

Rather than leave the requirement as an unverified claim in a document, the
two conditions the note must state are now characterized by tests against the
real observer, so the eventual prose cannot drift from the code:

1. **The authoritative entry must be seen.** A scan over a PATH that omits the
   install destination's `scripts/` dir returns `unknown` with `blocked_entry`
   naming that dir — not a shadow finding, not an all-clear. Source:
   `floati/installer_shadow.py`, the `has_installed_command and not
   authoritative_seen` branch.
2. **Partial scans are never promoted.** An unreadable PATH entry yields
   `unknown`, and adding the authoritative entry alongside an unreadable one
   still yields `unknown`. An incomplete scan never becomes
   `affirmative_none`.

Both read as failures on first encounter but are scan-input conditions, which
is exactly why the note is needed.

## RED / GREEN evidence

Exit codes captured with `echo "EXIT:$?"` on the command directly.

RED:

```
python3 -m unittest tests.test_installer_shadow tests.test_name_sweep
Ran 24 tests — FAILED (failures=4) — EXIT:1
```

- three documentation-contract tests: neither slot existed;
- one behavior test: `test_scan_omitting_the_destination_scripts_dir_is_unknown_not_a_shadow`.

That fourth failure was **my test being wrong, not the product**: I asserted
the unresolved temporary path while `blocked_entry` correctly reports the
resolved one (`/private/var/...` on macOS). The expectation was corrected to
`.resolve()`; the product was not touched. Recorded rather than quietly
adjusted, because it means only three of the four RED signals were real
absences.

GREEN:

- `tests.test_installer_shadow`: `Ran 13 tests`, `OK`, EXIT:0
- `tests.test_installer_shadow` + `test_name_sweep` + `test_copy_ledger`:
  `Ran 27 tests`, `OK`, EXIT:0
- focused source-scrub/corpus/name/capture gate: `Ran 36 tests`, `OK`, EXIT:0
- complete suite: `Ran 1518 tests`, `OK`, 0 failures, 0 errors, 0 skips,
  173.176 seconds, EXIT:0
- bundle self-test: `Ran 1518 tests`, `OK`, 174.986 seconds, EXIT:0
- bundle artifact:
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`
- `git diff --check`: EXIT:0

1518 = 1513 at the prior gate, plus 2 behavior tests and 3 documentation
tests. **The focused gate count moved from 33 to 36** because the three
documentation tests live in `tests/test_name_sweep.py`, which is one of its
four modules. Flagged so the change in a long-cited constant is not mistaken
for drift.

`bundle-manifest.v0.json` was regenerated for `floati/helptext.py` alone; no
path added or removed, no digest typed by hand.

## Explicitly not claimed

- **Not claimed: that issue #3 is closed.** The two slots are open and
  pinned; the sentences do not exist yet. Closure needs the architect's copy.
- Not changed: any behavior. `floati/installer_shadow.py` and
  `floati/doctor.py`'s shadow handling were not modified. The only code file
  touched is `floati/helptext.py`, and only to add a placeholder line.
- Not edited: the `docs/DESIGN.md` paragraph that enumerates doctor's check
  families, which omits the installer-shadow family. That is a second copy
  item for the architect, reported here rather than changed.
- Not written: any operator guidance in my own words in a living public doc.
- Not created: a new install guide or migration guide. Neither exists in this
  repository, and inventing a document structure for the architect's prose
  would be a larger decision than this item carries.
- Not touched: `~/.local/share/floati`. It is still behind the repo and still
  lacks the `retire` verb, as the architect filed.
- Not written: anything to the live bus root during this item.
- Standing: `stash@{0}` remains parked.
- No network call beyond `git` and `gh` against the repository's own origin.
  No telemetry.
