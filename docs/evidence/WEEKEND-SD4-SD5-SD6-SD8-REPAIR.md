# DRAFT — Weekend SD-4, SD-5, SD-6, and SD-8 repair evidence

Date: 2026-08-28  
Lane: `lane-floati`  
Branch: `repair/sd4-5-6-8-20260828`  
Current program parent used for the corrected frozen run: `b22c2b1e40a8ee09c65807ed7d7449e74a003dc7`

## DRAFT — Ruled scope

This packet implements the small-wave order in
`docs/evidence/gate-shakedown-triage-2026-08-28.md` after the SD-3 landing:

- SD-4: lifecycle install, update, and uninstall artifacts do not require a
  fleet root, and each lifecycle parser accepts the documented `--json`
  remedy.
- SD-5: temporary-node boot and teardown commands emitted by `node add`
  round-trip through the live parser. Teardown is the actual `node retire`
  act, not the projection-only `node teardown` surface.
- SD-6: an active wake-daemon consent can be revoked without first creating
  an adapter binding. The bindingless exit never invokes `launchctl`; any
  unbound plist is preserved and named rather than treated as owned.
- SD-8: exact idempotent send replay remains replay, while every new send,
  claim, or fresh delivery involving an expired temporary node refuses with
  its lease identifier and expiry. A late acknowledgment remains honest
  testimony and records the lease state at acknowledgment time.

Historical schema-version-1 acknowledgment receipts without the three new
lease fields remain valid and readable. New receipts carry all three fields
as one optional compatibility group: `node_lease_id`,
`node_lease_state_at_ack`, and `node_lease_expires_at`.

## DRAFT — RED-first record

The new tests were first run against the pre-repair production paths. They
reproduced the ruled defects:

- lifecycle commands inherited root resolution and `--json` was rejected;
- the generated temporary-node command used parser-invalid `--lease` flags;
- bindingless revoke refused `wake_daemon_binding_absent`;
- an expired node reached authority evaluation during claim, and send lacked
  a deterministic act-time boundary.

The permanent RED fixtures are in `tests/test_cli.py`,
`tests/test_node_wizard.py`, `tests/test_wake_daemon_cli.py`, and
`tests/test_node_lease_protocol.py`.

## DRAFT — Verification

### DRAFT — Correction and recut

The first pushed tip,
`8796c0a2ee59cd04656b89b97a5bef66e6c655f4`, did not reproduce the pass
claimed by the first version of this packet. A pure checkout failed three
`tests.test_demo_capture_assets` cases because
`scripts/capture-demo-assets.py` still supplied the removed lifecycle
`install --root` argument. The earlier 2,030-test result was therefore not
valid evidence for that pushed artifact: the tested tree and the eventual
pushed tree were not held to one frozen identity. This recut supersedes that
claim.

The branch is now rebased on program parent
`b22c2b1e40a8ee09c65807ed7d7449e74a003dc7`. The capture caller now follows
the repaired rootless lifecycle parser, and the current-parent license test
checks the live DCO wording and `git commit -s` instruction instead of stale
copy. Neither correction changes deployable manifest inventory.

- Lease, acknowledgment, and schema slice: **16 tests, OK**.
- Affected lifecycle, node, wake, reader, schema, and consumption bank:
  **233 tests, OK in 29.443s**.
- Generated copy ledger equality: **3 tests, OK**. All new help and refusal
  prose is `DRAFT - ` stamped.
- Manifest, scrub, frozen-protocol, copy, and core regression bank after the
  current-main refresh: **96 tests, OK in 17.971s**.
- The pre-recut canonical discovery run found exactly two stale gauntlet
  assertions that still classified install and update as root-taking verbs:
  **2,030 tests, 2 failures in 231.479s**. The gauntlet inventory now proves
  the shared-root contract for bus verbs and the no-root contract for all
  three lifecycle verbs; its focused regression is **2 tests, OK**.
- Corrected caller, license-contract, lifecycle, manifest, copy-ledger, and
  lease focused bank: **51 tests, OK in 3.526s**.
- Final canonical command, on the corrected frozen tree:
  `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover` — **2,034 tests,
  OK in 227.060s, exit 0**.
- `git diff --check`: silent, exit 0.
- Bundle manifest verification: `[]`.
- `bundle-manifest.v0.json` SHA-256:
  `75d67bbb82f975a4b5d8be8111bc7a215eeaad10fa99c21fc23c21a3501f30fc`.
- Frozen JSON inventory remains 143 paths; path digest remains
  `e6eff4279c7b34f3058615f80300da3148adfab492120ec341b5f4317bebc856`;
  the ruled SD-8 content digest is
  `c03b86fb5219cbe4609eae876fa34c6f3668de865565f8c1daf4f9e46d3e7107`.

## DRAFT — Fences

No README file, hook registration, trust setting, release state, or public
activation was changed. No foreign-project artifact or literal was added.
The manifest was updated only after the deployable source and schema bytes
were frozen.
