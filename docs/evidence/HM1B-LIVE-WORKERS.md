# HM-1b live-workers evidence

Status date: 2026-07-31.

This evidence records executed local behavior and explicit absences. It
includes one completed real-provider Codex proof turn. It does not claim live
ACP compatibility or hosted CI for the final evidence checkpoint until those
separate gates are observed.

## Identity and boot

- Branch: `lane/hm0`.
- Boot tip: `6e2cd6becac8c87c0e2839e9fdc4130143b2bb05`.
- Boot tip matched `origin/lane/hm0`; `6e2cd6b` was an ancestor.
- Required boot inbox poll returned exit 31 with `intentional_silence`.

A later pre-stand-down poll presented Fable message
`msg-019fba10d6947750894720570f1198ad`, binding worker consumption laws 11–12
from Puddle commit `616be1263c1e901db32e40ca8f8199ad5d4fc6e6`. The named Git document was
read before stand-down, the consumption changes below were implemented, and
the message was acknowledged as `ack-019fba2b8d7574a2bffbebbff8f35f94`.

## RED-first receipts and runner

The initial focused command was:

```sh
python3 -m unittest -v tests.test_workers tests.test_schemas
```

Observed before implementation: exit 1. The worker module and receipt schema
were absent; seven tests failed for that expected reason. After the minimal
implementation, the same command exited 0 with 14 tests and `OK`.

The runner proof uses the real append-only work, authority, and worker ledgers.
It observes the authority-checked claimed state before adapter spawn, records
`claim → spawn → drive → bind_artifact → complete`, and retains the Git
artifact binding on the completed work item. A separate path records
`process_died` as degradation and leaves the item claimed.
Inactive-node, absent-work, absent-adapter, authority, and lost-claim-race
refusals append a separate finite `worker_refusal` record while leaving the
adapter unlaunched. Adversarial tests also reject retroactive sessions on
completed work and mismatched completion artifacts; malformed adapter output
becomes `adapter_malformed_output` instead of stranding a driving session.
The work log is the sole validated consumption coordinate: oldest-owned
selection and claim share one transaction. Corrupt work evidence records and
raises `consumption_state_unavailable`; intact no-work remains
`worker_work_absent`. Authority-ledger corruption stays distinct as
`authority_state_unavailable`, and claim-time authority turnover records
`worker_authority_changed` instead of a work-plane race. Worker execution
creates neither delivery nor acknowledgment receipts.

## Codex app-server worker

The Fable ruling committed at `aedf92b` answered executable ownership,
provider traffic, credential inheritance, workspace, approval, deadline,
shutdown, and artifact questions. The implementation checkpoint is
`baafa20c3881dc5d40c63eead96cc5cfa5abc065`.

Work items opt into one derived mapping at
`/private/tmp/slipway-work/<work-id>`. The worker creates it only after the
authority-checked claim. The local reference harness executes the exact stdio
sequence `initialize → initialized → thread/start → turn/start`, interleaves
unrelated responses and notifications, and completes one real local file
mutation. The runner then records
`claim → spawn → drive → bind_artifact → complete`, retains the workspace,
commits only regular in-workspace files, and binds every path to the exact
local commit SHA. `.slipway/transcript.jsonl` and app-server stderr remain
untracked pointers inside the retained workspace and never enter bus records.

Focused tests distinguish failed turns, malformed envelopes, missing and
overflow artifacts, symlinks, invalid or missing workspace mappings, all
three current permission request methods, TTL-minus-margin exhaustion,
generic grandchildren, and an active app-server under both outer timeout and
abrupt adapter death. Review regressions also prove fresh launch-time TTL
observation, terminal plane-honest degradation for prelaunch and post-drive
authority corruption, owner-private workspace/evidence modes with
symlink/wrong-owner refusal, and template/hook/signing/pathspec-safe Git
finalization with exact-tree and clean-status verification. Re-review found a
remaining metadata trust boundary; new RED cases proved `.git` replacement
and executable local/global filter or fsmonitor configuration. Finalization
now retains and checks the original metadata inode, rejects replacement,
rebuilds clean metadata after the turn, and disables system/global Git config.
A subsequent lifecycle review found that eventual outer cleanup still allowed
a background descendant to race finalization. A completed-turn RED harness
left a delayed same-group mutator; app-server descendants now run in an
explicitly registered child group that is fully quiesced before metadata reset
and remains available to the outer runner for abrupt-death cleanup.

### Real-provider proof boundary

A fresh fleet was created at
`/private/tmp/slipway-hm1b-live-20260731-1`. The first real item was
`work-019fba52105171f2b9473c25db908d78`, with retained workspace
`/private/tmp/slipway-work/work-019fba52105171f2b9473c25db908d78`.
The exact CLI command durably wrote claim receipt
`worker-receipt-019fba523cfe74729d29b1034a897809`, then degradation receipt
`worker-receipt-019fba523de7790aae834c20cd9b9441` with
`outcome_code=process_died`.

The retained stderr proves the managed sandbox prevented app-server from
initializing its normal SQLite state under `~/.codex`.
The transcript pointer contains only the outbound `initialize` envelope. No
provider turn completed. No process survived the attempt.

A second open item, `work-019fba52a0f77de6a5d41c0a0080b359`, was prepared
for the exact retry. The required platform escalation was rejected because
the reviewer treated normal Codex credentials/provider traffic as an
unapproved export risk and explicitly prohibited workaround. The second item
therefore remains open and its workspace has not been created. Phase A is
`BLOCKED: platform permission`, not passed. The appended platform ruling
request is in `RULING-REQUEST-HM1B-CODEX-LIVE-BOUNDARY.md`.

After the operator replied `youre good`, the fleet received a fresh epoch-3
authority grant and the exact command was resubmitted. The platform rejected
it before launch, ruling that the reply was not specific informed approval of
normal Codex credentials/provider traffic and again forbidding workaround.
The open item remained unclaimed and no new workspace, app-server process,
provider request, or worker receipt was created.

Fable then committed and pushed the approval-path ruling at `b72287b`, with
`PUSH GO` for `a8e2c21`. The owner supplied the exact informed approval in the
lane. Under fresh authority grant
`authority-019fba82502c7c3ca1936e8714c3a72b` at epoch 4, this exact approved
command executed once unsandboxed:

```sh
python3 -m slip worker run \
  --root /private/tmp/slipway-hm1b-live-20260731-1 \
  --as floati-proof --adapter codex
```

It returned exit 0 with `transition=complete` for work item
`work-019fba52a0f77de6a5d41c0a0080b359` and session
`worker-019fba82901e737791575c235b68ba47`. The durable receipt chain is:

1. `claim` — `worker-receipt-019fba82901e7b1a8220af5f865f1b87`
2. `spawn` — `worker-receipt-019fba8298d37ba0afdbff5501c8c8ba`
3. `drive` — `worker-receipt-019fba8298d37c9588ef00c466bc8630`
4. `bind_artifact` — `worker-receipt-019fba82fe057545be34cb051454d3b9`
5. `complete` — `worker-receipt-019fba82fe06725bada7bdb343c0c383`

Both binding receipts and the completed work item name the retained workspace
`/private/tmp/slipway-work/work-019fba52a0f77de6a5d41c0a0080b359`,
repository `local/work-019fba52a0f77de6a5d41c0a0080b359`, document
`PROOF.txt`, and commit `746dc783b96f75b586fa77ab9bd1aa90fe730af8`.
Independent verification observed commit type `commit`, exact tree
`PROOF.txt`, exact content `FLOATI live worker proof`, and empty
`git status --porcelain`. The parent, workspace, and `.slipway` directories
were mode `0700`; transcript and stderr pointers were mode `0600` and remained
untracked. An exact post-run process query found no
`/opt/homebrew/bin/codex app-server --stdio` process. Phase A real-provider
proof is therefore PASS; the earlier sandbox degradation remains preserved as
distinct honest evidence rather than being overwritten.

## ACP v0

The ACP focused RED command was:

```sh
python3 -m unittest -v tests.test_acp_adapter
```

Observed before implementation: exit 1; four tests failed because the codec
was absent. The same command then exited 0 with four tests and `OK`.

The finite non-launching live probe returned:

```json
{"command": null, "executable": null, "status": "reference_harness_absent"}
```

`claude-code-acp`, `codex-acp`, and `acp-agent` were absent. A plain Claude
binary was present but was not treated as an ACP reference harness and was not
launched. ACP evidence is therefore fixture round-trip plus honest absence.
The conformance runner's independent fixture mode also executed:

```sh
python3 -m slip.conformance --acp-fixture
```

It returned exit 0 with four conformant codec cases and
`reference_harness_absent`; this remains fixture evidence, not live ACP proof.

## Harbor Board and supervision

The combined focused command first failed in five expected places because the
worker CLI and receipt projections were absent. After implementation:

```sh
python3 -m unittest -v tests.test_projection tests.test_supervisor \
  tests.test_tui_render tests.test_demo tests.test_cli_workflows
```

Observed: exit 0; 21 tests; `OK`. The board renders `CLAIM`, `DRIVING`,
`DEGRADED`, and `COMPLETE` from worker receipts only. The demo seeds all four
states. Supervisor tests hash the fleet tree before and after worker-aware
snapshots and observe no mutation; mode remains `report_only`.

## Final local gate

Fresh commands after implementation, docs, generated copy, manifest, and demo
captures were present:

```sh
python3 -m slip.selftest
python3 -m slip.conformance --live-root-smoke
python3 -m slip.conformance --acp-fixture
python3 -c 'from pathlib import Path; from slip.scrub import scan_generated_tree; hits=scan_generated_tree(Path.cwd()); print("scrub_hits="+str(len(hits))); raise SystemExit(bool(hits))'
python3 -m unittest -v tests.test_copy_ledger tests.test_workers \
  tests.test_acp_adapter tests.test_tui_render tests.test_supervisor
make demo-capture
python3 -m slip.demo --capture monochrome
git diff --check
```

The replacement checkpoint gate reran after the live adapter, workspace,
deadline, process-group, CLI, schema, copy-ledger, and manifest changes:

```sh
python3 -m unittest discover -s tests
python3 -m slip.selftest
python3 -c 'from pathlib import Path; from slip.manifest import verify_manifest; errors=verify_manifest(Path.cwd()); print(errors); raise SystemExit(bool(errors))'
git diff --check
```

Observed after the review fixes: the suite exited 0 with 210 tests and `OK`;
selftest repeated all 210 tests and emitted
`{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
direct manifest verification printed `[]`; live-root smoke reported five
conformant cases; ACP fixture conformance reported four conformant cases and
`reference_harness_absent`; source scrub reported zero hits; the focused
worker/copy/ACP/board/supervisor command passed 42 tests; deterministic demo
capture completed; and diff check exited 0 with no output.

The earlier checkpoint observed selftest exit 0 with 188 tests, followed by
`bundle_verified` for `refs/heads/lane/hm0`; live-root smoke exit 0 with five
conformant cases; ACP fixture conformance exit 0 with four cases and
`reference_harness_absent`; source scrub exit 0 with zero hits; the combined
focused command exit 0 with 33 tests and `OK`; deterministic demo generation
exit 0; and diff check exit 0 with no output.

The first independent code review reported no critical findings and five
important findings. Iterative re-review exposed the metadata and descendant
quiescence extensions described above. Every Important finding now has a
RED-first regression and the final independent review reports no Critical or
Important findings with push readiness `Yes`. Its one Minor follow-up is to
add finite JSON-RPC envelope, transcript/stderr, and aggregate artifact byte
ceilings before broader production use. The reviewed implementation and
approval-path ruling are pushed through `b72287b`; final evidence checkpoint
SHA, its Fable push gate, push, hosted CI, and the final stand-down inbox poll
remain pending.
