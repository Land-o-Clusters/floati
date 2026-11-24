# OpenCode worker lease and client restart receipt, 2026-10-25

OpenCode 2.1.4 has a shipped headless adapter. The configured default provider
failed the original intake, so the default adapter's `surface_verified` stays
false. The explicit model `opencode/glm-5.3-flash-free` completed both governed
worker calls in the follow-up measurement. This is a Python `WorkerRunner`
measurement with a model override; the public worker CLI still does not offer
OpenCode as an adapter choice.

The historical direct CLI drill terminated its own client, then resumed the
same native session with `--session` after a scratch Floati node's one-minute
lease expired. The assistant recalled the seeded nonce. This proves client
restart recovery on that service, not a server restart or governed native
session resume. `--continue` was observed in help but was not exercised.

After the shared headless worker protocol repair, the follow-up used a scratch
bus, a temporary one-minute node, and the real OpenCode adapter with this argv:

```text
opencode run --model opencode/glm-5.3-flash-free --format json -- 'Write OK to <owned-workspace>/answer.txt.' 'Do nothing else.'
```

| Observation | Receipt |
| --- | --- |
| Initial governed round trip | `claim → spawn → drive → bind_artifact → complete`; committed `answer.txt` containing `OK`; terminal `worker-receipt-01a17421c44c7fda9bae441c852edfc6` |
| Lease | `lease-01a17421a98c798ba8749f74d35b2ab0`, expired `2026-10-25T17:36:25.580Z` |
| Already admitted worker | Helper began `17:36:23.664Z`; claim recorded `17:36:23.666Z`; completed with a bound artifact at `17:36:32.458Z`, after the same lease expired; terminal `worker-receipt-01a17422aeca7d188f1b65a092363966` |
| Fresh expired-node start | `node_lease_expired` at `17:36:32.468Z`, before spawning; refusal receipt reason `worker_claim_lost`; no worker transition receipts |
| Cleanup | Both runners recorded empty `alive_after_cleanup`; scratch bus and all three provisioned workspaces were removed |

The follow-up made **two additional model-capable `opencode run` calls**,
within its three-call allowance. Both inherited the owner's configured HOME.
An initial timeout-argument refusal occurred before any model launch and was
retained as a preflight attempt. Tests use scratch HOME and make no model calls.
No credentials or provider configuration were read or changed.

The original fixture records the former `adapter_error`; it remains unchanged.
Its replay no longer requires that defect to exist in current code. The new
capture proves that an admitted ordinary Python worker can complete after its
node lease expires, while a fresh expired-node claim is refused before spawn.
It does not prove governed native-session resume or the separate native-worker
launch path.

Public replay inputs:

Digests below identify the original raw captures before the public export's
JSON path adaptation. Exported fixture bytes can therefore have different
hashes while retaining the replayed classification.

- [Original direct CLI capture](../../../tests/fixtures/opencode-issue16-2026-10-25.json), SHA-256 `b5aae6c920cd309218345881baae9ff95c46b06adb7c9480c49c436b5fb43dc1`.
- [Governed worker capture](../../../tests/fixtures/opencode-issue16-worker-2026-10-25.json), SHA-256 `20468093b84453aa7453cbd91cf0cd8a45517915c603aad026a71699769fc0dd`.
- [Replay and sensitivity tests](../../../tests/test_opencode_issue16.py).
- [Owned measurement helper](../../../scripts/measure_opencode_issue16_worker.py).
- [Shared worker protocol tests](../../../tests/test_headless_worker_protocol.py).

Full internal measurement and command ledger:
`docs/evidence/f2-issues-16-opencode-2026-10-25.md`.
This receipt is the public citation for that report. No issue comment was
posted and no issue was closed.

Final verification: 429 tests passed, no skips, exit 0. This includes the
issue-16 replay and sensitivity tests, shared protocol tests, every adapter
test module, public export tests and the required whole-tree census modules.
