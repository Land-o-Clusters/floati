# FL4.5 Floati internal-rename evidence

Status: local, SHA-bound implementation evidence only. This record does not
claim a Fable verdict, publication, push, installation, restart, activation,
release, or owner-only repository/folder rename.

## Immutable identities and review boundary

- Immutable base: d059b3671c8c98f0c3ce9c9fb091b71b73bcc8bf
- Final tested implementation candidate:
  de72ab8cc9e72f53973a4fec373e6a25272f9e7a
- Branch at the tested candidate: codex/herdr-adapter-source.
- The candidate was clean before final verification. The evidence commit below
  is documentation-only and is intentionally separate from the tested
  implementation candidate.

At this document commit, the Fable gate is **NOT YET requested/received**.
The local reviews and command receipts below are not a substitute for that
gate.

## History and RED/GREEN chronology

The feature range starts after the immutable base. Its planning/ruling history
is:

- 959d4cbd79772de9e9be4fe94d80645bfed0730e — design;
  e1e1ff09c546ee586887e35a3b6e52c8488c418b — namespace ruling;
  2c670fff8cb7a5a325bab6c91091662c2ba18bc8 — exact refusal copy;
  fc58f148de7901f2dda56d279f3fa0b46e8c0b14 — implementation plan; and
  2486f5d40ec2f333ef9c440c3377101f638f2dfe — RED-gate correction.
- Class 1 RED: 0ea1ba090ed4e10f74cb61407e8bc5ed2430b4fc,
  with 3 assertion failures and 0 errors. Class 1 GREEN:
  e6788520e82ee511323296c261cbdaf2cbd54b45, after plan correction
  5b1a6504599a23a41df9887fbda020e655022676; its focused receipt was
  56/56 passing, with 49/49 bounded gauntlets and 19/19 source-scrub plus
  installer-shadow checks.
- Class 2 RED: eaaed79aebfa5f2409ad3673af2ddd79743d4b37; its final
  corrected RED bank was 85 tests, 57 assertion failures, and 0 errors at
  9372bf519c4931f182411a7e228bfaaf19d3fc2e. Class 2 GREEN:
  39348bcd061b54f92e33f45b3141ba9993bba90c, 149/149 passing.
- Class 3 RED: edf68854f06d504893295958847f8d71d75f4ffa, with governing
  checks finalized at 167579c4f3de6a6a4a9c2136d17bb66c9a80b964. Its exact
  expanded RED receipt was 356 tests, 1,260 assertion/schema failures, and
  0 errors. Class 3 GREEN was rebaselined at
  2d1a9907f93c09e683d9ca9c7973a1722869ca12 and completed at
  eca51f527eef89b547b1b254586fdff465ac2b27: 141/141 focused and
  497/497 expanded tests passing.
- Living documentation reconciliation was committed at
  7f46dfac2171e4d09b4e4a1fa0bc19d033365dd4; its focused
  name-sweep/source-scrub receipt was 16/16 passing.
- The first whole-branch Task 8 review at 7f46dfac2171e4d09b4e4a1fa0bc19d033365dd4
  was blocked by two sets of Important runtime-identity findings. The bounded
  fix, de72ab8cc9e72f53973a4fec373e6a25272f9e7a, repaired only that final
  finding set and stale installer-shadow expectations. Re-review of that
  delta returned zero findings, with specification PASS and quality PASS.

## Four-class disposition

### Class 1 — internal code identity: complete locally

- The Python package is floati/; the retired slip/ package and scripts/slip
  launcher are absent. The canonical regular executable is scripts/floati,
  with no compatibility shim.
- Static, dynamic, bootstrap, worker, subprocess, test, and fixture imports
  resolve under floati. The error/root/registration identities are
  FloatiError, FloatiRoot, and FloatiRegistrationIdentity.
- Shipped SLIP_* environment coordinates became FLOATI_*; runtime process and
  adapter labels are Floati identities, including
  floati-conformance-adapter, floati-orchestrator-lane-*, floati-worker-adapter,
  and floati-thread-observer.
- Wake identity is com.landoclusters.floati.oneshot.*.

### Class 2 — storage clean break: complete locally

- New operational names are .floati, .floati-install, .floati-snapshots,
  .floati-effect-worker-, and floati-effect-worker-.
- A reused workspace containing any retired .slipway* artifact is refused by
  the typed legacy_workspace_artifacts result before an adapter, deployment
  writer, evidence directory, transcript, Git action, provider, staging area,
  or install metadata is read or created.
- The exact Fable-owned singular/plural refusal copy is exercised by the
  Class 2 bank. The guard reads direct child names only; it neither follows
  nor opens the old artifact or a legacy symlink, and it does not migrate or
  delete either one.
- Singular receipt: "workspace refused: legacy artifact '.slipway' predates
  the Floati rename; nothing was read, migrated, or deleted; start a fresh
  root, or archive the legacy artifacts yourself and run again".
- Plural receipt: "workspace refused: legacy artifact '.slipway' and 1 more
  predate the Floati rename; nothing was read, migrated, or deleted; start a
  fresh root, or archive the legacy artifacts yourself and run again".
- Codex, Claude, Pi, deployment, snapshot, CLI, and worker entrypoints are
  covered by early-preflight and no-mutation tests.

### Class 3 — frozen protocol and bundle identity: complete locally

- Present schema identifiers use the owned
  https://landoclusters.com/floati/schemas/ origin. Floati titles replace
  retired product titles; x-floati-* replaces x-slipway-*; and positive
  work-root references use /tmp/floati-work.
- Wake schema/runtime labels use the exact
  com.landoclusters.floati.oneshot.* identity, including the JSON-escaped
  regular-expression form. The intentionally missing ten schema $id values
  remain absent; none was invented to complete the rename.
- The Class 3 freeze binds 120 JSON assets, sorted-path SHA-256
  836adfba58ec486a2e8a09b701a7188582670a47a9e107b15d868774e147e8da,
  and path-and-byte SHA-256
  2d618c7412338401d660c16ca179cf295bfa7f6cb277ba7912976a9206879e42.
  It rejects untracked additions/removals, path and byte drift, and symlinked
  roots/assets.
- C7.1 refreshed 37 source digests plus its projection digest (38 fields;
  31 unique source files). C7.2 refreshed 38 source digests plus its
  projection digest (39 fields; 32 unique source files). Catalog $id/source
  bytes, SHA-256 rows, non-digest values, and key order were audited.
- The frozen manifest stays at 206 rows. Its retired slip/** and scripts/slip
  rows fell from 79 to 0; its floati/** and scripts/floati rows are 80; and
  schema_version 0, protocol_version "0", and canonical_ref
  refs/heads/lane/hm0 are preserved.
- Current living documentation was reconciled only in docs/DESIGN.md and
  docs/COPY-LEDGER.md. Those changes replace current .slipway-install,
  .slipway, and /tmp/slipway-work instructions/transcript coordinates
  with their ruled Floati equivalents; no historical material was rewritten.

### Class 4 — owner-only external coordinates: pending

GitHub/repository and local-folder rename actions are owner-only. They have
not been performed or inferred by this lane, and remain pending explicit owner
execution and verification.

## Exact final candidate gates

All following results are bound to
de72ab8cc9e72f53973a4fec373e6a25272f9e7a, before this documentation-only
commit:

| Command | Result |
| --- | --- |
| python3 -m unittest -q | Exit 0; 1,460 tests in 180.458s; OK. |
| python3 -m floati.selftest | Exit 0; 1,460 tests in 184.335s; OK; final artifact {"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}. |
| python3 -c 'from pathlib import Path; from floati.manifest import verify_manifest; print(verify_manifest(Path.cwd()))' | Exit 0; exact output []. |
| git diff --check | Exit 0; no output. |
| git status --short --branch | Clean at the tested candidate. |

The bundle_verified artifact proves the selftest's local test-and-manifest
contract only. It is not publication or Fable acceptance.

## Non-vacuous mutation controls

All four controls began from the same clean candidate, copied the target to
/tmp/floati-task8-mutation.Vjz6lq before mutation, restored the exact
backup afterward, and confirmed the stated pre/post SHA-256 equality before
the next control. Every focused gate exited 1 for its intended reason; status
was clean after each restoration.

| Control | Temporary mutation and focused gate | Required failure receipt | Pre/post SHA-256 |
| --- | --- | --- | --- |
| Retired import coordinate | In floati/worker_bootstrap.py, from floati.worker_errors import WorkerAdapterFailure was changed to from slip.worker_errors import WorkerAdapterFailure. Gate: python3 -m unittest -v tests.test_internal_rename.InternalRenameCodeIdentityTests.test_dynamic_runtime_modules_resolve_under_floati | Exit 1; Ran 1 test; failures=1; legacy module namespace remains in floati/worker_bootstrap.py. | f087dd27885eb53cf1a52e21fc63ca713ce8f5754de91ae29d357db9f3339ab4 |
| Retired workspace evidence directory | In floati/storage_identity.py, EVIDENCE_DIRECTORY = ".floati" was changed to EVIDENCE_DIRECTORY = ".slipway". Gate: python3 -m unittest -v tests.test_codex_live_adapter.CodexAppServerSessionTests.test_session_drives_exact_stdio_sequence_and_correlates_responses | Exit 1; Ran 1 test; failures=1; self.workspace / ".floati" / "transcript.jsonl" is not a file. | d17a9ea7520cd674c74b831fe9872493540d4427855b7aa268224626556697a1 |
| Unowned schema origin | In schemas/v0/work-item-record.schema.json, the $id origin was changed from https://landoclusters.com/floati/schemas/ to https://slipway.dev/schemas/. Gate: python3 -m unittest -v tests.test_internal_rename.InternalRenameCodeIdentityTests.test_schema_ids_use_owned_floati_origin_without_assigning_missing_ids | Exit 1; Ran 1 test; failures=1; reported https://slipway.dev/schemas/v0/work-item-record.schema.json. | be26a743b9706811e9859247a45157f7159fb5d32d6221f1a9be32b70f8ef96d |
| Stale manifest digest | In bundle-manifest.v0.json, the floati/conformance.py SHA-256 row was changed to 64 zeroes. Gate: python3 -c 'from pathlib import Path; from floati.manifest import verify_manifest; errors = verify_manifest(Path.cwd()); print(errors); raise SystemExit(1 if errors else 0)' | Exit 1; exact output ['digest_mismatch:floati/conformance.py']. | d41e82214ea919f69f10dd343ea1a3745eecb33bc04b43e1d81b560e45ec898d |

After the fourth restoration, direct manifest verification printed exactly [];
git diff --check exited 0; and the worktree was clean at the implementation
candidate. No full-suite, selftest, evidence packet, fleet-bus action, or push
was performed during the mutation-only step itself.

## Explicit exclusions and publication boundary

- Historical documents, prior evidence, rulings, HM material, and captures
  were classified and retained; they were not rewritten by this rename.
- The live ~/.slipway-bus state and the governed
  ~/.slipway-bus/puddle-fleet bus root were not migrated.
- No install, restart, activation, release, push, or publication has occurred
  as part of this evidence packet.
- Class 4 remains owner-pending, as above.

The next external action, if separately authorized by the governing flow, is a
SHA-bound Fable gate request for this evidence commit. Until an explicit PASS
clears publication, the branch remains local-only.
