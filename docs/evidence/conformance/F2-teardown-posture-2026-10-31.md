# Worker teardown posture — retained observations

DRAFT-OPUS, 2026-10-31. Source reviewed:
`870542bc7241b3b78bd780f1e5355497d796ba3a`.
This receipt summarizes existing native observations. No new provider run,
owner demonstration or measurement was performed for this document.

## What the cells mean

Worker teardown necessity requires a paired native observation: close a
worker without teardown, then boot the next session of that same node and
record the prior claim, consent, hook and pending-mail residue. Compare an
explicit-teardown control. None of the observations below establishes that
omitting teardown is safe. Those README cells remain
`NOT_YET_MEASURED: worker_omission_pair_missing`.

An architect always performs wind-down to preserve its context checkpoint;
this is an operating requirement, independent of worker cleanup necessity.
The README's absent cells make no claim of successful or unsupported cleanup.

## Claude

The 2026-10-26 headless demonstration at source `858ba877` recorded boot,
identical-receipt repeat, unrooted refusal and explicit teardown passing.
Its release mail was swallowed by boot's first hold and required explicit
acknowledgment before teardown. The later inbox path changes that mail path;
this historical demonstration does not qualify the new idle path.

Source testimony: the `F2-NODE-TURNOVER-1` ledger entry records this native
demonstration. The worker omission/next-session comparison is still owed.

## Cursor

The retained native run on 2026-10-31 records explicit teardown completion
and consent disarm at **21:25:05Z**, after the first hold and subsequent
project hold. Sources are the retained seat-board, stop-hold,
codex-wait-consent and seat-teardown journals and the native project
transcript. Boot's first hold was
`stop-hold-01a193b502c17070b74af7e0b5a84bd3`; consent was
`codex-wait-consent-01a193b500db7584be479509fc14524e`.

The source audit at `870542bc` separates a user-hook foreign-cwd refusal
from the project holds and later completed teardown. Its launch-directory
fix still requires a live re-sit. Completion after an explicit teardown
does not measure what would remain if that teardown were omitted.

## zcode

The 2026-10-26 native demonstration recorded boot, identical-receipt repeat
and unrooted refusal passing, followed by teardown refusing on the hosting
zcode processes. The host classifier cure subsequently landed at `602b7a1e`.
Its test evidence is fixture/native-parser coverage and explicitly does not
claim live teardown acceptance after the cure.

Source testimony: the `F2-NODE-TURNOVER-1` ledger entry records the native
refusal and landed cure. Live resume evidence establishes resume only.
Neither establishes worker teardown necessity or post-fix acceptance.

## Codex and the next observations

The desktop re-sit and worker omission comparison remain unmeasured here.
Use fresh native sessions in an explicit scratch account and declared seat.
For each harness, retain the exact source SHA, native version, actual session
IDs and predecessor receipts. Close the worker without teardown, boot the
next session, record whether board, arm, verified hook and first hold
complete, then repeat with explicit teardown as a control. Keep transcripts
outside the seat repository so the clean/banked-work gate stays real.
Native sign-in and credential issuance remain owner-run.

A resulting cell can be replaced by one linked receipt edit. The claim
census treats a teardown verdict as a claim even without a number; only
the exact reason-bearing `NOT_YET_MEASURED` cell is an absence. No new code
is needed to fill it, and removing its receipt fails the release check.
