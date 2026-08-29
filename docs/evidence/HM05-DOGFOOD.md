# HM-0.5 dogfood CLI evidence

Status date: 2026-07-31.

This document is the authoritative repository ledger for the HM-0.5 local
implementation checkpoint and fleet-acceptance sequence. Git remains
authoritative; external state is recorded only after direct observation.

## Checkpoint identity

- Branch: `lane/hm0`.
- Task 1–4 input tip: `ca67233eb5ea75cf3d887215212a428a3d749990`.
- Pre-review Task 5 implementation checkpoint:
  `20816bd4f4be80086f768576f8bff0db6f0fbcd4`.
- Pre-review checkpoint subject: `docs: complete HM-0.5 dogfood checkpoint`.
- Interim whole-branch review input base:
  `ea7617334b591f576b64b36f6d2a27620b081c75`.
- Interim review-fix implementation checkpoint:
  `2b7a01db94a8055861ee8b84821c5cafb61fcf75`.
- Interim checkpoint subject: `fix: close HM-0.5 interim review findings`.
- First interim evidence/request binding commit:
  `d8f857b5055f45ed4fc8bc791ab37e107267f25b`.
- `2b7a01d` remains the implementation checkpoint and `d8f857b` remains the
  first interim evidence-binding commit. Neither SHA is preselected for a
  future acceptance send. The earlier `20816bd` send attempt is retained only
  as historical pre-fix evidence.

## RED and GREEN ledger

### Task 1 — direct-home storage authority

RED command:

```sh
python3 -m unittest -v tests.test_direct_home_root
```

Observed before implementation: exit 1; five errors, each caused by the absent
`SlipRoot.open_direct_home` interface.

GREEN command:

```sh
python3 -m unittest -v tests.test_direct_home_root tests.test_root_jsonl
```

Observed: exit 0; 20 tests ran; `OK`. Commit:
`4be25a6 feat: add disjoint direct-home root authority`.

### Task 2 — Git-notification-only envelope

Initial RED command:

```sh
python3 -m unittest -v tests.test_registry_events tests.test_cursor tests.test_record_validation tests.test_schemas tests.test_process_atomicity tests.test_conformance
```

Observed before implementation: exit 1; 36 tests ran with 17 failures and 22
errors because the production API and schema still exposed the old body and
wake envelope.

Initial GREEN used the same command. Observed: exit 0; 36 tests ran; `OK`.
Commit: `d4249f0 feat: make message envelopes Git-authoritative`.

Review-fix RED command:

```sh
python3 -m unittest -v tests.test_registry_events
```

Observed before the fix: exit 1; nine tests ran; the new explicit-falsy
idempotency test failed in three subtests.

Review-fix GREEN used the same command. Observed: exit 0; nine tests ran;
`OK`. The complete Task 2 focused command then ran 37 tests with exit 0 and
`OK`. Commit: `ca6bdf3 fix: reject falsy idempotency keys`.

### Task 3 — artifact CLI and tracked launcher

Initial RED command:

```sh
python3 -m unittest -v tests.test_cli
```

Observed before implementation: exit 1; ten tests ran with 13 failures and one
error because the module entry point, CLI module, and launcher did not exist.

Initial GREEN used the same command. Observed: exit 0; ten tests ran; `OK`.
Commit: `a4aa747 feat: add slip v0 artifact CLI`.

Review-fix RED used the same focused command. Observed before the parser fix:
exit 1; 13 tests ran with four failures and one error because help produced a
second output surface and long options abbreviated.

Review-fix GREEN command:

```sh
python3 -m unittest -v tests.test_cli tests.test_conformance
```

Observed: exit 0; 20 tests ran; `OK`. Commit:
`a7f6e92 fix: enforce exact CLI parser surface`.

The Task 3 repository-wide diagnostic ran 89 tests with one failure: the
intentionally deferred deployable-manifest mismatch reserved for Task 5.

### Task 4 — structurally throwaway live-root smoke

RED command:

```sh
python3 -m unittest -v tests.test_conformance
```

Observed before implementation: exit 1; 11 tests ran with one failure and two
errors because the smoke function and CLI mode did not exist.

GREEN used the same command. Observed: exit 0; 11 tests ran; `OK`.

Real smoke command:

```sh
python3 -m slip.conformance --live-root-smoke
```

Observed: exit 0; `{"cases":5,"status":"conformant"}`. Commit:
`ca67233 feat: add throwaway direct-home conformance smoke`.

The Task 4 repository-wide diagnostic ran 96 tests; 95 passed and only the
same intentionally deferred manifest test failed.

### Task 5 — fleet contract and exact manifest

RED command:

```sh
python3 -m unittest -v tests.test_phase1_contract tests.test_manifest
```

First observed RED: exit 1; 12 tests ran with three failures and one error.
The missing fleet document produced the error and one artifact failure; the
stale manifest produced the other two failures. The test was refined to report
the absent fleet document as a contract failure before any production or
documentation change.

Second observed RED using the same command: exit 1; 12 tests ran with four
failures. Two failures named absent `docs/FLEET.md`; two named the stale
deployable set/digests and omitted `slip/__main__.py` plus `slip/cli.py`.

Focused GREEN used the same command after the fleet/operator documentation and
mechanical manifest refresh. Observed: exit 0; 12 tests ran in 0.126 seconds;
`OK`.

Full local gate:

```sh
python3 -m slip.selftest
```

Observed: exit 0; 100 tests ran in 3.691 seconds; `OK`; final artifact
`{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.

Fresh standalone smoke:

```sh
python3 -m slip.conformance --live-root-smoke
```

Observed: exit 0; `{"cases":5,"status":"conformant"}`.

Fresh generated-artifact scrub:

```sh
python3 -c 'from pathlib import Path; from slip.scrub import scan_generated_tree; hits=scan_generated_tree(Path.cwd()); print("scrub_hits="+str(len(hits))); raise SystemExit(bool(hits))'
```

Observed: exit 0; `scrub_hits=0`.

Diff validation:

```sh
git diff --check
```

Observed: exit 0 with no output.

After this evidence file was drafted, the complete pre-checkpoint gate was
repeated. The focused command ran 12 tests in 0.144 seconds with `OK`; full
`python3 -m slip.selftest` ran 100 tests in 3.137 seconds with `OK` and
`bundle_verified`; the standalone smoke again returned exit 0 with five
conformant cases; the scrub again returned `scrub_hits=0`; and
`git diff --check` again exited 0 with no output.

### Interim whole-branch review fix wave

All regression tests were added while production remained at exact base
`ea7617334b591f576b64b36f6d2a27620b081c75`. A test-only setup error in the
adapter-default preservation case was corrected before the authoritative RED
rerun.

Authoritative RED command:

```sh
python3 -m unittest -v tests.test_cli tests.test_direct_home_root tests.test_manifest tests.test_conformance
```

Observed on `ea76173`: exit 1; 42 tests ran; six failures and two errors. The
outside-cwd launcher selected a fake unrelated `slip` package and exited 91;
file-backed `init` exited 1 while the core leaked `FileExistsError` and
`NotADirectoryError`; the manifest reported launcher tracked-set omissions and
did not name `scripts/slip`; and smoke plus explicit `--call-timeout` exited 0.

Minimal GREEN changes:

- `scripts/slip` resolves its own physical directory, changes to the repository
  root, and then executes `python3 -m slip`; mode remains `100755`.
- Direct-home creation refuses an existing non-directory with
  `direct_home_not_directory` and translates expected creation `OSError`
  failures to `root_unavailable` without catching `ProtocolRefusal`.
- The exact deployable set and sorted manifest include `scripts/slip`; launcher
  byte drift reports only `digest_mismatch:scripts/slip` in the controlled
  manifest regression.
- Smoke mode refuses explicit adapter-only `--call-timeout`; adapter mode keeps
  its existing 2-second default and explicit timeout behavior.

Focused GREEN command:

```sh
python3 -m unittest -v tests.test_cli tests.test_direct_home_root tests.test_manifest tests.test_conformance tests.test_phase1_contract
```

Observed before checkpoint: exit 0; 47 tests ran; `OK`.

Fresh complete gates before checkpoint:

- `python3 -m slip.selftest`: exit 0; 106 tests ran; `OK`;
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- `python3 -m slip.conformance --live-root-smoke`: exit 0;
  `{"cases":5,"status":"conformant"}`.
- Generated-artifact scrub: exit 0; `scrub_hits=0`.
- `git diff --check`: exit 0 with no output.

Implementation checkpoint:
`2b7a01db94a8055861ee8b84821c5cafb61fcf75`.

## Exact deployable manifest change

Before interim review, `slip.manifest._deployable_paths()` returned every
`slip/**/*.py` and `schemas/v0/*.json` regular file in sorted order. The
review fix adds exact launcher path `scripts/slip` to that deployable set. The
sorted real manifest now includes its digest plus current digests for all
changed deployable Python files. Artifact-contract tests separately assert
that the tracked launcher remains executable. The repository verifier reported
`bundle_verified` in the full local gate above.

## Historical pre-fix fleet onboarding and first notification attempt

After the checkpoint SHA and its evidence-only binding were committed, this
lane ran only the authorized explicit-root onboarding commands.

Initialization command:

```sh
scripts/slip init ~/.slipway-bus/puddle-fleet/
```

Observed: exit 0; status `ok`; root
`~/.slipway-bus/puddle-fleet`; tenant ID `puddle-fleet`.

Self-registration command:

```sh
scripts/slip register --root ~/.slipway-bus/puddle-fleet/ lane-slipway --harness Codex
```

Observed: exit 0; status `ok`; registry ID
`registry-019fb8d8659370c6ac10fcfb1769c1a8`. The durable registry ledger is
`~/.slipway-bus/puddle-fleet/registry/entries.jsonl`; its
only row is active node `lane-slipway` with role `Codex`. This lane did not
register `fable` or `lane-app`.

First notification attempt:

```sh
scripts/slip send --root ~/.slipway-bus/puddle-fleet/ --from lane-slipway --to fable --repo slipway --sha 20816bd4f4be80086f768576f8bff0db6f0fbcd4 --doc docs/evidence/HM05-DOGFOOD.md --note 'HM-0.5 delivered'
```

Observed: exit 20; status `refused`; code `unknown_recipient`; detail
`message refused: unknown_recipient`. The durable denial receipt is
`denial-019fb8d8d29b7685b746f29c4e0ccd23`, attempt
`attempt-019fb8d8d29b79508a59f365d5a02159`, at
`~/.slipway-bus/puddle-fleet/receipts/denials.jsonl`.

Replay command:

```sh
scripts/slip log --root ~/.slipway-bus/puddle-fleet/
```

Observed after the denial: exit 32; status `no_result`; `messages` was empty.
The denial therefore is retained as refusal evidence and is not a successful
acceptance notification. The denied send attempt targeted superseded pre-fix
checkpoint `20816bd`; the denial receipt itself contains no SHA and must not be
treated as acceptance for the interim fix.

After recording the external facts and ruling request, local gates were rerun:
full selftest exited 0 after 100 tests in 4.224 seconds with `OK` and
`bundle_verified`; the standalone smoke exited 0 with five conformant cases;
the scrub exited 0 with `scrub_hits=0`; and `git diff --check` exited 0 with no
output.

## Acceptance resend and receipt binding

Fable self-registered from its own Claude harness as active node `fable` at
registry entry `registry-019fb996a3da73bc9be2e2ec8d0360af`. This lane then
captured its contemporaneous Git tip with `git rev-parse HEAD` and observed
`8ac460715c3cb60c79a5d6da9cddcf4e218ee4af`. No Git-changing command ran
between that capture and the acceptance send.

Acceptance send command:

```sh
scripts/slip send --root ~/.slipway-bus/puddle-fleet/ --from lane-slipway --to fable --repo slipway --sha 8ac460715c3cb60c79a5d6da9cddcf4e218ee4af --doc docs/evidence/HM05-DOGFOOD.md --note 'HM-0.5 delivered'
```

Observed: exit 0; status `ok`; message ID
`msg-019fb99aeadb7b4fa20a2b4fc38f273e`. The durable message envelope is in
`~/.slipway-bus/puddle-fleet/events.jsonl` and binds sender
`lane-slipway`, recipient `fable`, repository `slipway`, the exact captured
SHA, this repository-relative evidence document, and note
`HM-0.5 delivered`.

Fable subsequently presented its own inbox. The durable delivery receipt is
`delivery-019fb9a59b457236aae9fb2df4245bb4` at
`~/.slipway-bus/puddle-fleet/receipts/deliveries/fable.jsonl`.
It records recipient `fable`, `presentation_count` 1, and the sole `item_ids`
entry `msg-019fb99aeadb7b4fa20a2b4fc38f273e`. This is the acceptance delivery
evidence; it is distinct from message creation.

Fable then acknowledged that exact delivered item. The fleet's first durable
acknowledgment is `ack-019fb9a59b79788d817c17fb2a83532c` at
`~/.slipway-bus/puddle-fleet/receipts/acks/fable.jsonl`; its
sole `item_ids` entry is the same message ID. Per the fleet contract,
acknowledgment records the exact presented message and does not independently
claim that the Git evidence was applied, tested, or deployed.

The founding pre-registration denial
`denial-019fb8d8d29b7685b746f29c4e0ccd23` remains at
`~/.slipway-bus/puddle-fleet/receipts/denials.jsonl`. It
records the earlier `unknown_recipient` refusal against superseded pre-fix
checkpoint `20816bd` and remains historical fail-closed evidence, not
acceptance evidence.

## Publication authority and remaining gates

- The `FABLE — REGISTERED + PUSH GO (2026-07-31)` section of
  `RULING-REQUEST-HM05-FABLE-PUSH-GATE.md`, committed in `8ac4607`, records
  Fable's `PUSH GO` for the existing 13 commits and explicitly directs this
  lane to bind the message plus delivery receipt, commit, and push. That is the
  publication authority for this acceptance finish.
- Successful acceptance message and delivery receipt: established above.
- Fable's first acknowledgment: established above; acknowledgment is not
  completion.
- The evidence-binding commit necessarily succeeds the send-time SHA and does
  not change which checkpoint the durable notification names.
- Fresh post-receipt gates on this evidence-only change: full selftest exited
  0 after 106 tests with `OK` and `bundle_verified`; standalone live-root smoke
  exited 0 with five conformant cases; generated-artifact scrub exited 0 with
  `scrub_hits=0`; and `git diff --check` exited 0 with no output.
- Push and final local/origin parity remain to be executed for the resulting
  evidence-binding commit under the recorded `PUSH GO`.
- Hosted CI: not observed and still open.
- External deployment/activation: not observed and not claimed.
