# WAVE 2 R1 — documented headless invocations

**Status:** IMPLEMENTED — local verification complete; live surface remains unclaimed.

**Dispatch:** `msg-01a045c7f6a07b8185ab9fcb407d2a9e` from `the architect`.

**Branch:** `lane/ws-b-wave2`, rooted at fetched `origin/main`.

**Implementation commit:** `73df7b6f0df948fe97e884e36e1bcd33dd77d913`.

## Change

The three researched profile declarations now carry the exact documented entry
points and a primary vendor citation:

| Adapter | Declared arguments | Citation |
| --- | --- | --- |
| OpenCode | `run` | `https://opencode.ai/docs/cli/` |
| Cline | `--json` | `https://docs.cline.bot/usage/cli-overview` |
| Grok Build | `-p` | `https://docs.x.ai/build/cli/headless-scripting` |

Cursor and Pi Observation remain explicitly empty because the cited research
does not establish a safe invocation for either profile. The default
`/opt/homebrew/bin/grok-build` path remains absent and was not renamed to the
separately installed `grok` executable; a caller may provide that executable
as the already-ruled explicit override.

## RED and GREEN

- RED: `python3 -m unittest -v tests.test_headless_invocations` at the pre-change tree — 3 failures, one for each researched tuple still being empty.
- GREEN: the same command after the change — 3 tests, 0 failures, 0 errors.
- GREEN regression: `python3 -m unittest -v tests.test_roster_adapters tests.test_roster_parity_battery` — 22 tests, 0 failures, 0 errors.
- Pre-commit `tests.test_source_scrub` — 8 tests, 0 failures, 0 errors.
- `git diff --check` — clean.

`surface_verified` remains false because this row cited documentation but did
not claim a live provider execution. `floati/cli.py` was untouched.

