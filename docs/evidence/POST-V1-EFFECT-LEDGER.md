# Post-v1 Effect ledger evidence

Date: 2026-08-13

Branch: `codex/effect-ledger`

Frozen comparison base: `62bcef5391e4192291f027e5b4dc6e5bed45858c`

Task 8 input and tested Git HEAD: `a0bb9c6c0768d2f1a8352d1ee50e667bdd2d19e1`

Whole-review consolidated-fix input HEAD:
`1c075d4ab8075a7aa11a9e40611667122533adf6`

This document describes the candidate formed by that exact Git HEAD plus the
six Task 8 paths named below. A commit cannot contain its own object ID, so the
single Task 8 commit ID is recorded in the Task 8 report and whole-branch review
request, not guessed inside the commit. No push, publication, installation,
activation, release, merge, live GitHub action, or deployment occurred.

The final whole-branch review then confirmed four Important gaps. One
consolidated RED-first fix wave changes the production, test, design, manifest,
and evidence paths described below. Its commit cannot contain its own object ID;
the exact final ID is reported from Git after the one commit, never predicted in
this evidence file.

## Exact tracked scope

The complete candidate delta from the frozen comparison base contains these 88
tracked paths. Task 8 itself changes only `bundle-manifest.v0.json`, the four
gauntlet/manifest test modules, and this evidence document.

```text
AGENTS.md
bundle-manifest.v0.json
docs/COPY-LEDGER.md
docs/evidence/POST-V1-EFFECT-LEDGER.md
docs/superpowers/plans/2026-08-11-effect-ledger.md
docs/superpowers/plans/2026-08-12-effect-worker-descriptor-prelude.md
docs/superpowers/plans/2026-08-12-effect-worker-exec-bootstrap.md
docs/superpowers/plans/2026-08-12-effect-worker-isolation.md
docs/superpowers/plans/2026-08-13-effect-reconciliation-observer.md
docs/superpowers/specs/2026-08-11-effect-ledger-design.md
docs/superpowers/specs/2026-08-12-effect-worker-exec-bootstrap-design.md
docs/superpowers/specs/2026-08-13-effect-reconciliation-observer-design.md
schemas/v1/compensation-executed-record.schema.json
schemas/v1/compensation-proposed-record.schema.json
schemas/v1/effect-acknowledged-record.schema.json
schemas/v1/effect-confirmed-record.schema.json
schemas/v1/effect-dispatched-record.schema.json
schemas/v1/effect-failed-record.schema.json
schemas/v1/effect-intent-record.schema.json
schemas/v1/effect-reconciled-record.schema.json
schemas/v1/effect-record-common.schema.json
schemas/v1/effect-status-artifact.schema.json
schemas/v1/effect-unknown-record.schema.json
schemas/v1/run-result-accepted-record.schema.json
slip/adapters/claude.py
slip/adapters/codex_live.py
slip/adapters/pi.py
slip/approvals.py
slip/cli.py
slip/copy.py
slip/effect_reconciliation_exec.py
slip/effect_reconciliation_observer.py
slip/effect_reconciliation_protocol.py
slip/effects.py
slip/helptext.py
slip/jsonl.py
slip/policy.py
slip/projection.py
slip/records.py
slip/run_limits.py
slip/runtruth.py
slip/sequencer.py
slip/spawn_groups.py
slip/supervisor.py
slip/tui.py
slip/tui_render.py
slip/worker_adapter_runtime.py
slip/worker_bootstrap.py
slip/worker_bootstrap_protocol.py
slip/worker_errors.py
slip/worker_exec.py
slip/worker_isolation.py
slip/workers.py
tests/schema_validation.py
tests/test_approvals.py
tests/test_claude_adapter.py
tests/test_cli.py
tests/test_codex_live_adapter.py
tests/test_copy_ledger.py
tests/test_effect_cli.py
tests/test_effect_controller.py
tests/test_effect_reconciliation.py
tests/test_effect_reconciliation_exec.py
tests/test_effect_reconciliation_observer.py
tests/test_effect_reconciliation_protocol.py
tests/test_effects.py
tests/test_gauntlet_concurrency.py
tests/test_gauntlet_crash.py
tests/test_gauntlet_fuzz.py
tests/test_manifest.py
tests/test_outcomes.py
tests/test_pi_adapter.py
tests/test_projection.py
tests/test_record_validation.py
tests/test_run_limits.py
tests/test_runtruth.py
tests/test_schemas.py
tests/test_sequencer.py
tests/test_sequencer_epoch.py
tests/test_snapshot_board.py
tests/test_spawn_groups.py
tests/test_supervisor.py
tests/test_tui_controls.py
tests/test_tui_render.py
tests/test_worker_bootstrap.py
tests/test_worker_bootstrap_protocol.py
tests/test_worker_isolation.py
tests/test_workers.py
```

No file below `schemas/v0`, `bundle/c7.1`, or `bundle/c7.2` differs from the
exact frozen base.

## RED and fix chronology

All implementation tasks used tests before production changes. The detailed
command transcripts, counts, and review rounds remain in the SDD task reports;
this section records the whole candidate chronology, including non-green
history rather than replacing it with the final result.

1. Task 1 defined the closed Effect record and schema family. Its initial tests
   failed with three missing-contract import errors. Review found schema/runtime
   effect-type-to-target-kind drift; a 30-failure matrix was captured before
   commit `41e82cce2a45ce03bd75d4c2c71ca9e1c0d8f00a` closed it. Task 1's scoped
   review was clean.
2. Task 2 added the canonical append-only Effect ledger. Missing production
   support produced two errors; malformed-prefix, transaction-lock, restart,
   and collision REDs then exposed one malformed-prefix failure, a two-failure/
   one-error transaction bank, and one collision failure. Review fixed exact
   confirmation matching, separate compensation proof, unique reconciliation
   evidence, and process-death retry in `d6817e438f475a3f8b7fac2304a8aafe6ecb3bc8`.
   The scoped review was clean. Two manifest failures remained deliberately
   deferred.
3. Task 3 added `EffectController` authority and immutable Run/approval binding.
   The required RED had eight failures. Four review rounds closed protected-path
   aliases, managed-lock bypass, mutable policy/class dispatch, stateful mapping
   and module-binding attacks, detached Run inputs, racy policy references, and
   public method metadata in commits `f742ecb48a051ccf02ef39d85e6ab81806d1e587`,
   `d6a4741cd0a471f4bc141ea1fc521aab0d96be59`,
   `6ee451c7bcb0888fcfb934768e64803c7298e863`, and
   `01dc544ccde66b3a3662334f4096d97fa6923d19`. One managed-pytest attempt was
   refused before Python with exit 64; stdlib unittest supplied the authoritative
   RED/GREEN evidence. Scoped review ended clean; manifest failures remained.
4. Task 4's private-pipe RED had six missing-behavior errors and its crash RED
   had one. Four Python-only hardening rounds closed three findings but repeatedly
   demonstrated that same-UID forked Python state could mutate the authority
   fence. The fifth round stopped at the architecture boundary instead of
   claiming a seal. The approved amendment rejected cooperative fork authority
   and required a fresh exec plus OS-enforced Effect-ledger write denial.
   Isolation, prepared-workspace identity, descriptor-bound bootstrap sources,
   closed bootstrap framing, loader/environment scrubbing, provider process-group
   ownership, TERM/grace/KILL ordering, and reap identity were implemented in the
   commit sequence `bc86298e` through `b08f8f09`. Host-gated isolation controls
   sometimes printed sandbox initialization denials; the supported-backend
   callbacks remained zero on this host. Historical complete banks still had
   manifest/install deferrals and the preserved 12-writer contention intermittent.
   The whole Effect Worker isolation/exec scoped review ended clean; it did not
   create supported-host kernel proof.
5. Task 5 began with nine missing reconciliation-module errors, four missing
   compensation-method errors, five missing controller-integration errors, and
   one mutable-adapter binding error. Review first exposed eight mutable-binding
   failures. The approved reconciliation amendment replaced an impossible
   in-process Python seal with a descriptor/digest-bound fresh-exec read-only
   observer. Its protocol, observer, launcher, and controller cutover were each
   RED-first and independently reviewed. Subsequent rounds closed result
   cross-product validation, lazy Git fetch mutation, descendant process cleanup,
   request-coordinate evidence scope, dot-segment/noncanonical coordinates,
   repository ancestor symlinks, and child environment leakage in commits through
   `80d1c5b03559c17ae56b21c657dd1d17b22d6d57`. A 259-test affected run had two
   Worker-prelude timeouts; both isolated controls passed and a fresh 259-test
   rerun passed. The reconciliation sub-plan and Task 5 scoped review ended clean.
6. Task 6 bound Run v1 acceptance to current Effect truth and budgets. The RED
   ran ten tests in 0.686s: its lawful v0/no-effect control passed while nine
   features produced ten failure/error reports. Review rounds closed overtaken
   same-ID retry, guarded cross-ledger snapshots, terminal-current-evidence
   revalidation, partial-spend ranking, and managed Sequencer cache/durable/
   non-batched retry bypasses through
   `4b25edddf1179880842c610e3aef2976312ef5ad`. One broad run had five loaded
   observer timeout subcases; isolated and fresh banks recovered. Another run
   exposed a real `acceptance.lock` contention failure and received a deterministic
   regression. Scoped review ended clean; only manifest/install reports remained.
7. Task 7 exposed CLI/help/copy/projection/TUI/supervisor surfaces. The CLI RED
   ran 34 tests with 14 expected missing-surface reports plus one invalid test
   fixture; the projection/TUI RED ran 36 with missing status/model/render
   behavior. Review found one Important uncertainty-precedence gap and one Minor
   no-write coverage gap. The five-test RED had two failures, two errors, and one
   already-green no-write control; an additional stale-snapshot RED failed 1/1.
   Commit `a0bb9c6c0768d2f1a8352d1ee50e667bdd2d19e1` closed both findings.
   Compensation CLI preview/confirm deliberately remain typed
   `effect_compensation_plan_unavailable` and physically read-only because
   durable truth does not carry a reconstructable complete compensation action.
   Task 7's scoped review ended clean.
8. Task 8 first added the required manifest inventory and Effect schema-family
   tests without changing the manifest. The exact two-test command failed 2/2 in
   0.003s: the manifest lacked 22 Effect, reconciliation, Worker exec/isolation,
   and acceptance artifacts, and the Effect schema family was empty. The frozen
   base test passed 1/1 in 0.011s before the fix. The manifest was then regenerated
   once by `_deployable_paths()` plus SHA-256 over each file; its top-level fields
   were preserved and its sorted `files` list now has 191 entries. The three
   inventory/schema/frozen tests passed 3/3 in 0.018s.

Task 8 also strengthened the real gauntlets. An initial five-test run passed four
and failed the mutated-pipe case because the test invoked a nested host Seatbelt
rather than the existing ruled isolation seam; production was unchanged and the
corrected test passed. The final source-inventory test initially used a wrong
selector (one loader error), then overreached across the legacy `workers.py`
registry import (one assertion failure). Narrowing that test to new effect/worker
modules corrected the test premise without changing production. These test-only
non-green runs are retained here rather than described as product failures.
The first staged diff-check also returned exit 2 for three Markdown hard-break
spaces in this evidence header; those spaces were removed without a production
change before the final staged check.

9. The independent whole-branch review confirmed four Important gaps together:
   confirmed spend maps could omit claimed keys; built-in Git observation copied
   the immutable claim into measured spend; retry/terminal validation compared
   an accepted watermark to the whole lawful later tail; and compensation could
   strand a proposal if acceptance won before its separate intent. The exact
   11-test consolidated RED ran in 1.633s with 9 failures and 6 subtest errors;
   direct and reconciled empty/subset spend, Git-without-measurement, lawful
   later-tail acceptance, hostile post-acceptance intent, and the deterministic
   compensation interleaving all failed while lawful controls remained green.
   Production was untouched through that RED.

   The fix requires exact claimed-budget key coverage for every complete
   confirmed state; preserves exact Git ref/object observation as
   `unknown/reconciliation_inconclusive` without confirmation or measured spend;
   validates accepted evidence at its stored watermark while replaying the full
   tail and separately refusing later same-attempt intents; and holds the
   truth-free acceptance fence across authoritative compensation revalidation,
   proposal append, and distinct compensation-intent append. Exact crash retry
   recovers a durable proposal, concurrent exact retries return one canonical
   pair, and changed retries refuse. The exact 11-test GREEN ran in 1.708s.

   Broader adjudication retained independently supplied confirmed-measurement
   controls and changed only expectations that had mislabeled built-in Git
   identity observation as spend measurement or any lawful proposal tail as
   overtaking. The affected bank first exposed 12 such obsolete expectations,
   then passed 195/195 in 34.264s. The Effect/reconciliation/Run/Worker/Spawn/
   gauntlet bank first exposed one remaining obsolete Git lawful control, then
   passed 522/522 in 91.926s. The first complete run preserved three obsolete
   Sequencer proposal-tail assertions (1262 tests, 176.878s); the corrected
   cache, durable-lookup, and single-request controls also prove that a persisted
   post-acceptance same-attempt intent still refuses. Their exact bank passed
   4/4 in 0.530s before the fresh full run below.

10. The consolidated-fix rereview found one remaining crash window: after a
    durable compensation proposal but before its separate intent, acceptance
    could persist and make exact compensation retry permanently refuse. The
    exception RED reproduced that acceptance win. A proposal without its exact
    later bound compensation intent now blocks new acceptance until exact retry
    appends the missing intent. Already-accepted lawful proposal tails continue
    to validate their immutable stored watermark; every later same-attempt
    intent still refuses. The focused compensation/acceptance bank passed 23/23
    and the complete affected bank passed 523/523.

11. Focused rereview then found proposal recovery compared only a subset of the
    original plan. A changed `requested_by` with a recomputed caller plan digest
    was accepted. The exact RED failed 1/1. `compensation_proposed` now carries
    a required `compensation_plan_digest` over the complete canonical plan, and
    recovery requires exact equality before constructing the missing intent.
    The exact RED passed after the fix; the expanded runtime/schema/Effect/
    Worker/Spawn/gauntlet bank passed 579/579.

## Proven invariants

- Record and lifecycle: the v1 schema/runtime family is closed and byte-bounded;
  physical append order is authority; malformed, duplicate, torn, oversized,
  non-UTF-8, non-I-JSON, causally reordered, cross-operation, and stale prefixes
  fail closed. Exactly one primary terminal outcome can become current.
- Authority: generic JSONL append, a retained capability, a child-built record,
  raw private frames, mutable module/class/callback substitutions, and Worker
  descendants cannot acquire controller-only Effect append authority. A lawful
  parent private-pipe control accompanies denial tests.
- Worker isolation and exec: the built-in effect path uses a closed adapter spec,
  fresh interpreter exec, opened-and-digested prelude sources, exact inherited
  descriptors, environment closure, an OS isolation handshake before adapter
  import/callback, and process-group cleanup before first reap. Unsupported
  isolation is typed and executes zero adapter callbacks.
- Locking and retry: Effect idempotency and outcome selection occur under the
  real Effect transaction lock. `effects/acceptance.lock` carries no truth and
  serializes Effect intent against Run acceptance without nesting the two ledger
  locks. Compensation confirmation holds that same fence across authoritative
  source revalidation and its two distinct physical appends. Exact retries
  return canonical durable testimony; divergent and stale retries refuse, while
  lawful proposal or unrelated tails do not invalidate an immutable accepted
  watermark. Deleting an empty acceptance lock does not change either projection.
- Cross-ledger terminal join: successful v1 Run acceptance repeats the sorted
  Effect operations, physical watermark, and terminal-row digest from a guarded
  current snapshot. Full-tail replay still enforces integrity, but retry and
  terminal checks validate evidence at the stored accepted watermark. No new
  same-attempt intent may appear after result acceptance. Incomplete, failed,
  unknown, and stale effect states cannot manufacture success.
- Reconciliation: only a closed request crosses a digest-bound fresh-exec channel.
  The parent binds request, child PID/channel, exact result, exit, EOF, current
  durable evidence, and append transaction. Process exit, approval, dispatch,
  acknowledgement, missing observation, malformed output, timeout, and cleanup
  failure never become confirmation. Exact built-in Git observation proves only
  ref/object state and remains non-authorizing unknown without independently
  trusted complete resource measurement.
- Compensation: preview is deterministic and read-only; proposal and its
  separate operation are acceptance-fenced distinct rows; exact retry can recover
  a durable proposal; a proposal-only crash prefix blocks new acceptance until
  that recovery completes; the proposal binds the full canonical plan digest so
  every changed-field retry refuses; execution requires separate confirmed
  terminal evidence;
  changed operation/digest/terminal references fail closed. Proposal and provider
  exit are not execution proof.
- Budgets and outcomes: measured spend is bound to declared reservations and
  current effect evidence. Complete confirmed evidence contains exactly every
  claimed budget key; an empty or subset map refuses replay. Unknown or
  incomplete external state ranks ahead of ordinary failure as operator
  attention; confirmed/failed/unknown and compensation state are shared by CLI,
  status, Supervisor, and TUI.
- Operator surfaces: list/show, status, Supervisor, and TUI are projection-only.
  Compensation CLI refusal is no-write for existing and missing roots. Visible
  help/copy is generated from the registered copy ledger.
- Source inventory: production contains one Effect JSONL coordinate,
  `effects/records.jsonl`; new effect/worker modules do not import a registry or
  Sequencer authority; closed Worker/reconciliation field sets exclude bearer,
  cookie, credential, password, request-body, raw-record, secret, and token
  fields. The private descriptor channels are transport only, not durable truth.

## Final executed gates

The following results are from the final runtime/test/manifest candidate bytes
before this evidence-only file was added. Evidence-only post-write checks are
recorded in the Task 8 report.

```text
python3 -m unittest -v \
  tests.test_effects tests.test_effect_controller \
  tests.test_effect_reconciliation tests.test_effect_cli tests.test_workers \
  tests.test_runtruth tests.test_run_limits tests.test_outcomes \
  tests.test_spawn_groups tests.test_schemas tests.test_manifest
```

Exit 0; 397/397 in 63.828s. The displayed transcript was truncated, but the
authoritative final summary and exit status were retained.

```text
python3 -m unittest -v \
  tests.test_gauntlet_concurrency tests.test_gauntlet_crash \
  tests.test_gauntlet_fuzz
```

Exit 0; 74/74 in 19.807s. This includes same-idempotency contention,
conflicting outcomes, a real-process intent/acceptance race, every Effect append
and rollback crash seam, restart retry, hostile persisted prefixes, forged
authority, mutated/reentrant pipe testimony, Git coordinate/ref/output attacks,
compensation proof, and Unicode/schema parity.

```text
python3 -m unittest -q
```

Exit 0; 1255/1255 in 153.844s. No skip, deselection, xfail, or xpass category
was reported. Known nested-isolation `sandbox initialization failed: Operation
not permitted` diagnostics were printed; their assertions passed.

```text
python3 -m slip.selftest
```

Exit 0; 1255/1255 in 166.424s; final artifact:
`{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
The verbose transcript was repeatedly truncated during streaming, but the final
test summary, artifact, and exit status were retained.

```text
python3 -m unittest -v tests.test_manifest tests.test_schemas tests.test_copy_ledger
```

Exit 0; 57/57 in 7.929s.

```text
python3 -c 'from pathlib import Path; from slip.manifest import verify_manifest; print(verify_manifest(Path.cwd()))'
```

Exit 0; printed `[]`.

```text
/usr/bin/git diff --exit-code 62bcef5391e4192291f027e5b4dc6e5bed45858c -- schemas/v0 bundle/c7.1 bundle/c7.2
/usr/bin/git diff --check
```

Both exited 0 and were silent.

```text
python3 -m unittest -v tests.test_source_scrub tests.test_copy_ledger \
  tests.test_gauntlet_fuzz.EffectLedgerPrefixFuzzTests.test_effect_source_inventory_has_one_ledger_and_closed_pipe_fields
```

Exit 0; 11/11 in 0.382s. This combines the repository tracked-source/history
scrub, generated-copy equality, and the bounded Effect ledger/authority/closed-
field inventory check. Raw private-socket refusal and control-character coverage
also ran in the 74-test hostile gauntlet above.

### Whole-review consolidated-fix gates

```text
python3 -m unittest -q \
  tests.test_effects tests.test_effect_controller \
  tests.test_effect_reconciliation_protocol \
  tests.test_effect_reconciliation_observer \
  tests.test_effect_reconciliation_exec tests.test_effect_reconciliation \
  tests.test_effect_cli tests.test_runtruth tests.test_run_limits \
  tests.test_outcomes tests.test_workers tests.test_worker_bootstrap \
  tests.test_worker_bootstrap_protocol tests.test_worker_isolation \
  tests.test_spawn_groups tests.test_gauntlet_concurrency \
  tests.test_gauntlet_crash tests.test_gauntlet_fuzz \
  tests.test_gauntlet_recovery tests.test_gauntlet_snapshot \
  tests.test_gauntlet_time
```

Exit 0; 522/522 in 91.926s.

```text
python3 -m unittest -q
```

Exit 0; 1263/1263 in 173.962s. No skip, deselection, xfail, or xpass category
was reported. Expected nested-sandbox denial diagnostics came from passing
refusal controls.

```text
python3 -m slip.selftest
```

Exit 0; 1263/1263 in 232.482s; final artifact:
`{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.

### Final residual exception gates

The exact RED
`CompensationTests.test_proposal_only_crash_blocks_acceptance_until_exact_recovery`
failed because acceptance did not refuse after the proposal-only crash. After
the scoped correction:

- compensation plus acceptance focus: 23/23 in 2.826s;
- complete affected bank: 523/523 in 93.204s;
- first full run: 1264 tests in 187.222s with three load-sensitive failures in
  unchanged observer/socket and concurrency controls;
- exact failed-selector recovery: 3/3 in 3.153s;
- required fresh full run: 1264/1264 in 168.573s;
- selftest: 1264/1264 in 198.412s, followed by
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.

The first full-run failures remain part of the record and are not counted as a
pass. Direct manifest verification returned `[]`; Python compile, diff-check,
and frozen schemas/v0 plus C7.1/C7.2 comparisons were silent and exited zero.

The first focused rereview of that correction found changed-plan recovery:
changing `requested_by`, recomputing the caller plan digest, and retrying joined
the earlier proposal. Its exact RED failed 1/1. The final proposal schema now
requires `compensation_plan_digest`; runtime stores the digest of the complete
canonical plan and requires equality on recovery. Exact GREEN was 1/1; the
expanded affected bank passed 579/579. Final full passed 1264/1264 in 178.213s;
selftest passed 1264/1264 in 170.084s and emitted `bundle_verified`.

```text
python3 -m unittest -q tests.test_manifest tests.test_schemas \
  tests.test_copy_ledger tests.test_source_scrub \
  tests.test_gauntlet_fuzz.EffectLedgerPrefixFuzzTests.test_effect_source_inventory_has_one_ledger_and_closed_pipe_fields
```

Exit 0; 66/66 in 9.912s. Direct `verify_manifest(Path.cwd())` returned `[]`.
The final exact-byte static rerun after this evidence write is recorded in the
ignored consolidated-fix task report.

## Supported-host callback-contract fix round (2026-08-14)

the architect's closure ruling at
`docs/rulings/2026-08-14-post-v1-closure-herdr-ruling.md` found the first real
supported-macOS execution degraded with `spawn_context_hook_missing`. The
fresh-exec bootstrap reconstructs `CodexAppServerAdapter`,
`ClaudeHeadlessAdapter`, or `PiRpcAdapter`, then the shared post-isolation
runtime requires Spawn and Effect context hooks before invoking the adapter.
The real built-ins did not implement those hooks. Earlier forced-supported
tests substituted adapters that did, while this lane's real nested sandbox
took the honest unsupported branch; together those facts masked the defect.

The exact RED reconstructs all three real built-ins with the closed
`BuiltInAdapterSpec` and forces both contexts through `run_adapter_session`,
patching only external provider execution. It ran one test with three subtest
failures in `0.049 s`; every adapter emitted only `failure`. The shared Codex
base now accepts validated, one-shot copies of the Spawn and Effect contexts
and their emitters; Claude and Pi inherit that contract. No isolation,
bootstrap, provider process-group, workspace, or parent effect-authority code
changed.

The same exact test passed `1/1` in `0.049 s`. The reconstructed-bootstrap plus
Codex/Claude/Pi adapter bank passed `53/53` in `9.297 s`. The complete
`WorkerEffectPipeTests` plus `WorkerEffectAuthorityTests` bank passed `43/43`
in `11.183 s`; nested Seatbelt diagnostics in that run were expected passing
unsupported controls.

The final combined bootstrap protocol/isolation, real-adapter, Effect, and
manifest bank passed `146/146` in `21.691 s`.

The deployable manifest digest for `slip/adapters/codex_live.py` was refreshed
mechanically after the final source bytes. The Wake/Hold whole-review inventory
test is now bound to its exact closed candidate
`097867580d79d6fc7874d0dc55a689b4f4ab1669`, rather than moving `HEAD`, and
that evidence banks the independent task-local audit receipt required by the
ruling.

Final rebuilt-tree verification after the workstation restart:

- fresh full `python3 -m unittest -q`: `1412/1412` in `191.919 s`, exit 0;
- fresh `python3 -m slip.selftest`: `1412/1412` in `237.694 s`, exit 0,
  followed by `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
- direct manifest verification: `[]`;
- source scrub, generated-copy equality, and exact Effect source inventory:
  `12/12`, exit 0, including a post-evidence rerun;
- private-cache compile, `git diff --check`, and the frozen schemas/v0 plus
  C7.1/C7.2 byte comparison: silent, exit 0.

The workstation restart discarded an earlier uncommitted isolated worktree and
its in-flight full-suite handle. The lane was rebuilt from the surviving exact
branch ref, the RED was reproduced, and every receipt above was re-executed;
the discarded run is not counted.

## Supported-host Git finalization and fixture repair (2026-08-14)

the architect's first independent supported-macOS rerun after the callback-contract fix
removed `spawn_context_hook_missing` but found nine failures in the exact
`WorkerEffectPipeTests` plus `WorkerEffectAuthorityTests` bank. This lane
reproduced the same nine `git_finalize_failed` outcomes in 13.106 seconds.
Outside the nested Codex sandbox, bare Apple Git failed because it could not
open `/dev/null` read/write under the Worker profile; changing Git's hooks path
did not change that result.

The exact real-host RED added a test-only `os.open(os.devnull, os.O_RDWR)`
control beside the existing tenant-write denial. It failed 1/1 in 0.830 seconds
with `EPERM`. The production correction adds one literal `/dev/null` exception
to the default-allow, tenant-write-deny macOS profile. The same selector then
passed 1/1 in 3.168 seconds: `/dev/null`, workspace, and scratch were writable,
while the tenant write remained denied.

The first 43-test rerun removed every `git_finalize_failed` outcome and exposed
a second supported-host-only defect in tests: fresh-exec fixtures still used a
bare Python command or expected a parent-only custom adapter object to cross
the closed `BuiltInAdapterSpec` boundary. Seven cases timed out and two prelude
controls correctly reached `isolation_ready` instead of the nested host's typed
unavailable result. That run failed nine tests in 425.699 seconds and is not
counted as a pass.

The fixture-only repair uses the existing reference Codex app-server command,
runs thread/provider/fork/link probes from that real post-isolation provider,
keeps inherited-descriptor closure in its dedicated real-backend control, and
accepts either canonical supported or typed-unavailable first frames in tests
that stop before adapter execution. No additional production authority or
adapter behavior changed. The nine exposed selectors passed 9/9 in 9.729
seconds. the architect's exact two-class bank then passed 43/43 in 12.455 seconds on
`macos-sandbox`, with no `git_finalize_failed` or `process_timeout` outcome.

The first expanded isolation/bootstrap/Effect/adapter/manifest bank passed 143
of 146 tests but three pre-isolation controls received no frame within their
legacy three-second test wait under bank load. That 146-test run failed in
77.782 seconds and remains a failed receipt. Widening only those bounded host
startup waits to ten seconds produced 3/3 exact recovery in 5.544 seconds and a
fresh complete 146/146 pass in 25.214 seconds.

Final exact-byte verification:

- source/schema/copy/Effect inventory: 72/72 in 8.763 seconds;
- direct `verify_manifest(Path.cwd())`: `[]`;
- fresh full suite: 1412/1412 in 228.841 seconds, exit 0;
- fresh selftest: 1412/1412 in 199.964 seconds, exit 0, followed by
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
- private-cache compile, `git diff --check`, and the frozen C7.1/C7.2 plus
  schemas/v0 manifest controls: exit 0.

Only the `slip/worker_isolation.py` deployable digest changed in
`bundle-manifest.v0.json`. The primary `lane/hm0` checkout remained untouched;
all edits and receipts belong to the isolated `codex/herdr-adapter-source`
worktree.

## Limitations and non-claims

- Git reconciliation evidence is non-authorizing local fixture observation.
  `git_local` uses temporary repositories and exact held-directory identity;
  `git_remote_explicit` uses local controlled fixture/process output to exercise
  its exact ref and coordinate rules. Neither measures resource spend or proves a
  live hosted remote, credential, network, publication, or GitHub state.
- `github_explicit` and `deployment_explicit` are deliberately unavailable and
  return closed `unknown/adapter_unavailable` results. `none` is deliberately
  `unknown/reconciliation_inconclusive`. No GitHub, deployment, shell, external
  API, or authenticated live adapter was activated or passed.
- This macOS nested sandbox printed `Operation not permitted` while starting a
  second Seatbelt. Those controls prove the typed unavailable/zero-callback path
  here. They do not prove supported-host kernel enforcement, Landlock ABI
  availability, detached-host behavior, or every provider kernel.
- Private socketpairs and inherited descriptors are exercised as bounded
  transport with exact peer/request/result closure. This is not a claim that an
  arbitrary raw socket is an authority surface; hostile raw frames refuse.
- Compensation API behavior is green, but the operation-only CLI cannot derive a
  complete action plan from current durable truth. Its typed unavailable result
  is a documented limitation, not successful compensation execution.
- Historical manifest/install failures were expected Task 8 deferrals and grew
  from 2 to 22 as runtime scope expanded. They are resolved only by the mechanical
  manifest refresh and final green gates above. Historical intermittent/load
  failures, refused pytest invocations, fixture mistakes, child diagnostics, and
  Task 8 test-premise corrections remain disclosed in the chronology.
- No final test was skipped. Collected or isolated evidence is not relabeled as a
  live external or supported-host pass.
- The prior callback-contract round exercised the real built-in classes but not
  the supported macOS kernel boundary. This follow-up executed that boundary
  locally outside the nested Codex sandbox. the architect's independent rerun remains
  the external closure gate; this lane's supported-host GREEN is not relabeled
  as the architect's receipt.

## Review status

Tasks 1 through 7 and the reconciliation-observer amendment had clean scoped
Critical/Important review verdicts after their recorded fix rounds. Independent
whole-branch review then confirmed the four Important gaps recorded above; this
consolidated wave fixes all four with local GREEN evidence. A second independent
post-fix verdict is not claimed here. No push, bus checkpoint, publication,
activation, deployment, release, or merge is authorized by these local results.

## the architect supported-host gate verdict (2026-08-14, round 2)

**PASS — GATE GREEN at `9736ab6f`.** On this supported host (unsandboxed
macOS, sandbox-exec available) — the environment where round 1 degraded
with `spawn_context_hook_missing` and the first fix degraded with
`git_finalize_failed` — my independent runs now show:
`WorkerEffectAuthorityTests` + `WorkerEffectPipeTests` **OK**, full
`python3 -m unittest -q` **OK**, `python3 -m slip.selftest` **exit 0**.
The supported-host kernel-enforcement path has now executed and passed
for the first time in its history. Consequences per the post-v1 closure
ruling: **item 5 (Effect Ledger) CLOSED** · items 6 (Thread Observer)
and 7 (Wake/Hold) conditional-closures **RESOLVED — CLOSED**. The
post-v1 program is fully closed. Lane proceeds to the name sweep
(FLOATI_NAME_SWEEP_BRIEF.md, license task included), then TD-5301.
— the architect.
