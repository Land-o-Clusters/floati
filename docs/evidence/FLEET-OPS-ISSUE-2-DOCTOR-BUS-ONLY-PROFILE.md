# Issue #2 — doctor bus-only fleet profile

Status: **GATE REQUESTED — INCLUDES ONE UNPLANNED CORRECTNESS FIX, FLAGGED**

Date: 2026-08-18

## Identity and authority

- Node: `build lane` (builder seat, ad-hoc fleet-ops window)
- Worktree: `~/Projects/floati`
- Branch: `lane/fleet-ops-window`
- Predecessor commit (live exercise, gated DONE):
  `562f6782124232c5bc88dc8d3a78cc1cacbeedad`
- Governing issue: GitHub issue #2
- Dispatch authority: architect message `msg-01a017a911ef74a28650b7353f1dcec8`
  — "PROCEED ISSUE #2 as already ordered — the doctor profile builds against
  this real 2-active/4-retired roster."

## What was built

`--profile PROFILE` on `floati doctor`, and `profile=` on `Doctor`. Ruled
values are `bus-only` and `orchestration`. The profile is **declared, never
inferred**: doctor does not look at the root and guess.

Under `--profile bus-only`, **and only when the presence set is entirely
absent**, the liveness row becomes an `ok` finding
`registry_live_dirs_expected_absent` with no remediation, and the aggregate is
free to reach `healthy`.

Everything else is unchanged, each pinned by a test:

- **No profile named** → the existing `registry_live_dirs_mismatch` warning
  and `degraded` aggregate, byte-for-byte. No silent default change.
- **`--profile orchestration`** → the same warning. Orchestration fleets keep
  their testimony.
- **Partial absence under `bus-only`** → still a warning. This is the
  honest-absence law: absence is *stated*, never guessed. A fleet that
  declares "presence files are intentionally absent" while some presence files
  exist has not made a true statement, so doctor does not accept it.
- **A genuine presence match under `bus-only`** → still
  `registry_live_dirs_match`. The declaration never rewrites testimony that
  already agrees.
- **An unruled profile value** (`"bus only"`, `"BUS-ONLY"`, `"session"`, `""`)
  → typed `doctor_profile_invalid` refusal raised in the constructor, before
  any artifact is produced. Exit 20 at the CLI. A typo cannot silently select
  a lenient aggregate.

No schema changed. The profile adds no field to the doctor artifact, so the
`schemas/v0` and `schemas/v1` doctor contracts are untouched; the new finding
code matches their existing code pattern, and the CLI test validates a live
`bus-only` artifact against `schemas/v1/doctor-artifact.schema.json`.

## Copy ownership

Both new visible strings are unfilled placeholders registered for the
architect, plus one help slot:

| Key | Surface |
| --- | --- |
| `doctor.profile.invalid` | refusal detail |
| `doctor.live_dirs.expected_absent` | the ok row's detail |
| `help.doctor.profile` | the `--profile` line in `floati doctor --help` |

A test asserts the first two still contain `[[`. The `--profile PROFILE`
option token and the synopsis addition are machine command grammar, not prose;
the sentence describing them is the architect's and remains unwritten. The
issue's requested wording — "session-based fleet; presence files intentionally
absent" — was **not** typed in as the row detail, because row wording is
explicitly hers at build.

## The unplanned correctness fix — flagged for a scope ruling

Running the new profile against the real live root, exactly as sequenced,
surfaced a defect in the same statement issue #2 governs.

Doctor selected registered nodes with a naive per-row filter:

```
registered = {str(row["node_id"]) for row in registry if row["state"] == "active"}
```

On an append-only ledger a retired node's *original active row remains
forever*, so every retired node still counted as registered. Observed on the
live bus root immediately after the chartered retirement:

```
registered=['the architect', 'build lane', 'build lane', 'lane-puddle-relief',
            'build lane', 'the architect']
```

— six nodes, against a true roster of two active and four retired. The
`active_node_ids` projection was already correct; only doctor was wrong.

Doctor now folds latest-row-wins before selecting active nodes. After the fix,
the same live command reports:

```
registered=['build lane', 'the architect'] live_dirs=[]
```

RED first for this fix too: a new test asserting that a retired node with no
presence file is a match, not a mismatch, failed `0 != 35` before the change.

**This is outside issue #2 as literally written**, and is surfaced rather than
buried. The judgment: the fix is three lines inside the exact statement the
profile modifies, and shipping the profile on a false roster would have made
the new `ok` row assert something untrue. If the architect prefers it split
into its own item, it is commit `7bd2fe8` alone and can be reverted
independently of the profile commit `8168de0`.

## RED / GREEN evidence

Exit codes captured with `echo "EXIT:$?"` on the command directly; no pipe
stands between any command and its recorded status.

RED (profile):

```
python3 -m unittest tests.test_doctor
Ran 17 tests — FAILED (failures=1, errors=18) — EXIT:1
```

Nine new tests were added to the existing `DoctorContractTests` fixture rather
than a new class. An earlier attempt put them in a subclass, which silently
re-ran all eight inherited doctor tests (25 runs for 17 tests); that was
reverted before implementation rather than shipped.

RED (fold fix): `AssertionError: 0 != 35 : a retired node with no presence
file is not a mismatch`.

GREEN:

- `tests.test_doctor`: `Ran 18 tests`, `OK`, EXIT:0
- focused source-scrub/corpus/name/capture gate: `Ran 33 tests`, `OK`, EXIT:0
- complete suite: `Ran 1513 tests`, `OK`, 0 failures, 0 errors, 0 skips,
  190.444 seconds, EXIT:0
- bundle self-test: `Ran 1513 tests`, `OK`, 183.797 seconds, EXIT:0
- bundle artifact:
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`
- `git diff --check`: EXIT:0

1513 = 1503 at the prior gate, plus 9 profile tests and 1 fold-fix test. No
test was deleted, skipped, or loosened. One test defect of my own was fixed
during the run: my directory-removal test used `rmdir` on a directory that
also holds a lock file, and was corrected to `shutil.rmtree` — a fault in the
test, not the product.

`bundle-manifest.v0.json` was regenerated twice, from
`floati.manifest._deployable_paths` plus real digests: once for the four
profile files, once for `floati/doctor.py` alone. No path was added or removed
either time, and no digest was typed by hand.

## Live-root observation, stated exactly

`doctor` is physically read-only and was run against `~/.floati-bus/the fleet`.
The live registry digest is `adebcaf3…` before and after every run —
unchanged.

The liveness plane behaves as designed on the real roster: without a profile,
`registry_live_dirs_mismatch` (warning); with `--profile bus-only`,
`registry_live_dirs_expected_absent` (ok).

**The live aggregate is still `degraded`, and this packet does not claim
otherwise.** Two unrelated findings hold it, both honest:

1. `deploy_currency_unavailable` — HEAD is `lane/fleet-ops-window`, not
   `origin/lane/hm0`. Expected mid-window; not a defect.
2. `installer_shadow` — `error` with no `--destination`; with
   `--destination ~/.local/share/floati` it becomes a `warning`, "Some PATH
   entries could not be read; shadow state unknown." That PATH-readability
   condition is the subject of issue #3 and is addressed there.

The aggregate reaching `healthy` under `bus-only` is proved in the test
fixture, where currency and shadow are controlled, not asserted on the live
root.

## Explicitly not claimed

- Not claimed: that the live bus root now reports `healthy`. It reports
  `degraded` for the two reasons above.
- Not claimed: any change to how liveness is *measured*. No presence file is
  written, read differently, or synthesized. The profile changes only how an
  entirely-absent presence set is reported.
- Not changed: the default. A root with no `--profile` produces exactly the
  artifact it produced before this change.
- Not authored: the row wording, the refusal detail, or the `--profile` help
  sentence. All three are placeholders awaiting the architect, together with
  the four `help.retire.*` strings and the root help command list still
  outstanding from issue #1.
- Not touched: `schemas/v0/doctor-artifact.schema.json` and
  `schemas/v1/doctor-artifact.schema.json`.
- Not written: any live-root mutation. Doctor made none, and no `retire`,
  `send`, or `work` command ran against the live root during this item.
- Not started: item #3 (installer-shadow docs).
- Out of scope by fence (R7) and untouched: the wake daemon, installer,
  publication checklist, and herdr. The stale installed bundle at
  `~/.local/share/floati` remains as the architect filed it — not this
  window's work.
- Standing: `stash@{0}` remains parked, per ruling.
- No network call was made beyond `git` and `gh` against the repository's own
  origin. No telemetry.
