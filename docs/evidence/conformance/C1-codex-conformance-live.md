# C1 — codex conformance, live (grok, 2026-08-27)

**Row:** C1 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Seat:** `grok` (clean-room)
**Branch:** `refs/heads/lane/grok-conformance` (tip at C0: `bfbbb040eff378c88345662b231052ed318e1983`)
**Executable named for Python:** `/usr/bin/python3` (realpath `/Library/Developer/CommandLineTools/usr/bin/python3`, control `--version` = `Python 3.9.6`, exit 0)
**Codex executable:** `/opt/homebrew/bin/codex` (version below)
**`surface_verified`:** **true** — real-binary receipts in this doc (version string + app-server initialize handshake)

Product source was not edited. Three selftest/discover failures are filed below, not repaired.

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
```

A measured non-zero suite exists beside every FAIL count: live-root-smoke exit 0 with `cases: 5`.

## Check 1 — `python3 -m unittest discover`

Exact command, cwd repo root:

```text
/usr/bin/python3 -m unittest discover
```

Captured via `~/Projects/floati-grok/.conformance-scratch/c1/run_unittest_discover.py` which invoked `sys.executable -m unittest discover` (same binary as `/usr/bin/python3`).

Untruncated stderr artifact: `.conformance-scratch/c1/unittest_discover.stderr` (28506 bytes). Meta:

```text
argv=/Library/Developer/CommandLineTools/usr/bin/python3 -m unittest discover
cwd=~/Projects/floati-grok
exit=1
```

Summary lines from that untruncated stderr:

```text
Ran 1519 tests in 256.622s
FAILED (failures=3)
```

FAIL names (exactly three; zero ERROR):

1. `test_item_ten_and_item_eleven_docs_state_local_coordinates_without_publication_claims` (`tests.test_hm3i_contract.HM3IContractTests`)
2. `test_living_public_docs_are_tenant_neutral` (`tests.test_name_sweep.NameSweepLivingDocumentationTests`) `(relative='docs/PUBLICATION-CHECKLIST.md', pattern='puddle-fleet')`
3. `test_generated_repository_artifacts_are_scrubbed` (`tests.test_source_scrub.SourceScrubTests`)

**Verdict: FAIL** (docs/scrub; not adapter-contract). Adapter modules had no FAIL/ERROR in the paired unsandboxed selftest run (check 2).

## Check 2 — `python3 -m floati.selftest`

Exact command, cwd repo root:

```text
/usr/bin/python3 -m floati.selftest
```

This loader is `unittest.defaultTestLoader.discover("tests", pattern="test_*.py")` then `verify_manifest`. Untruncated log: `.conformance-scratch/c1/selftest.full.txt` (264828 bytes, sha256 `c19258f3f483bc667de557a9e74451369587b703e3df4387109bf9a4dfdcd8b4`). Process exit **10** (`TEST_FAILURE` in `floati/selftest.py`). Manifest verify was **not reached** (no `bundle_verified` / `bundle_mismatch` JSON).

Summary lines from that untruncated log:

```text
Ran 1519 tests in 260.981s
FAILED (failures=3)
```

Same three FAIL names as check 1. Headers-only copy: `docs/evidence/conformance/C1-selftest-failures.txt`.

Codex adapter-contract lines in that log (module `test_codex_adapter_contract`): 7 tests with `... ok`, 0 FAIL, 0 ERROR. Codex live-adapter (`test_codex_live_adapter`): 0 FAIL, 0 ERROR in the failure list. ACP adapter: 4 `... ok`.

**Verdict: FAIL** (same three docs/scrub tests). Codex adapter contract battery: PASS inside this red suite.

## Check 3 — `python3 -m floati.conformance --live-root-smoke`

Exact command:

```text
/usr/bin/python3 -m floati.conformance --live-root-smoke
```

Untruncated stdout (34 bytes, sha256 `c5fd2646ececd4f3ce87149df9a20daea8cdd240f54800b78deafd3bb9de2187`), copied to `docs/evidence/conformance/C1-live-root-smoke.json`:

```text
{"cases":5,"status":"conformant"}
```

stderr: 0 bytes. exit: 0. Started `2026-08-27T21:52:13Z`, ended `2026-08-27T21:52:14Z`.

**Verdict: PASS**

## Check 4 — live-root exercise (scratch root created by this seat)

Root (created here, not the puddle-fleet root, not `/private/tmp`):

`~/Projects/floati-grok/.conformance-scratch/c1-live-root`

| step | executable | exit | result |
|---|---|---:|---|
| init | `/usr/bin/python3 -m floati init --root <root> --solo grok-c1 --harness Codex` | 0 | `status=ok`, `tenant_id=c1-live-root`, `solo.node_id=grok-c1`, `solo.harness=Codex`, `authority_epoch=1` |
| send | `/usr/bin/python3 -m floati send --root <root> --from grok-c1 --to grok-c1 --repo floati --sha bfbbb040eff378c88345662b231052ed318e1983 --doc docs/evidence/conformance/C0-machine-harness-inventory.md --note "C1 live-root control envelope"` | 0 | `msg-01a0453a3e53773b9796455a3fb29f45` |
| inbox | `/usr/bin/python3 -m floati inbox --root <root> --as grok-c1` | 0 | 1 message, `delivery-01a0453a6712763781d05b7aa67bee39`, `presentation_count=1` |
| ack | `/usr/bin/python3 -m floati ack --root <root> --as grok-c1 --id msg-01a0453a3e53773b9796455a3fb29f45` | 0 | `ack-01a0453a777e700e9de63b90ea543cff` |
| inbox after ack | same inbox command | 31 | `intentional_silence`, `messages=[]` |

Ledger counts from the untruncated files (byte and newline counts; not `head`):

| file | exists | bytes | lines |
|---|---|---:|---:|
| `events.jsonl` | true | 467 | 1 |
| `receipts/deliveries/grok-c1.jsonl` | true | 259 | 1 |
| `receipts/acks/grok-c1.jsonl` | true | 226 | 1 |

**Verdict: PASS**

## Real-binary receipts (required for `surface_verified: true`)

### Version

```text
argv: ["/opt/homebrew/bin/codex", "--version"]
exit: 0
stdout_bytes: 18
stdout: codex-cli 0.150.0
stderr_bytes: 0
```

### Live app-server initialize (stdio, scratch cwd)

Scratch cwd created here: `~/Projects/floati-grok/.conformance-scratch/c1-codex-workspace`

```text
argv: ["/opt/homebrew/bin/codex", "app-server", "--stdio"]
cwd: ~/Projects/floati-grok/.conformance-scratch/c1-codex-workspace
timeout_s: 12.0
timed_out: false
exit: 0
handshake_ok: true
stdout_bytes: 192
stderr_bytes: 0
```

Request sent:

```json
{"id":1,"method":"initialize","params":{"clientInfo":{"name":"floati-grok-c1","version":"0"}}}
```

Parsed first line (untruncated in `docs/evidence/conformance/C1-codex-appserver-probe.json`, 1042 bytes, sha256 `fe0feb65225847aeef1e47d6c1a8d7298aa92a9c404dde36e7bdcce52084a6d8`):

```json
{"id":1,"result":{"userAgent":"floati-grok-c1/0.150.0 (Mac OS 26.6.2; arm64) dumb (floati-grok-c1; 0)","codexHome":"~/.codex","platformFamily":"unix","platformOs":"macos"}}
```

How invoked by the live adapter on this tip: `("/opt/homebrew/bin/codex", "app-server", "--stdio")`. This probe used that exact argv against the real binary. It did not drive a full turn (no `thread/start` / `turn/start`); initialize was sufficient to prove the process speaks the app-server envelope.

## Defects filed (not fixed)

1. **Suite red at this tip.** `/usr/bin/python3 -m unittest discover` and `/usr/bin/python3 -m floati.selftest` both: `Ran 1519 tests`, `FAILED (failures=3)`, discover exit 1, selftest exit 10. Codex adapter contract tests are not among the three.
2. **HM-3I checklist phrase drift.** `tests/test_hm3i_contract.py` expects the literal `tenant-default neutralization + migration alias` in `docs/PUBLICATION-CHECKLIST.md`. The living checklist now says `tenant-default neutralization RULED` and **There is NO migration write**. Test fixture lagged the ruling.
3. **Name-sweep vs preservation ruling.** `test_living_public_docs_are_tenant_neutral` forbids `puddle-fleet` in `docs/PUBLICATION-CHECKLIST.md`. The same checklist now records the 2026-08-27 ruling that the live `puddle-fleet` coordinate is preserved verbatim. Test and living copy disagree.
4. **Generated-tree scrub has 6 hits.** Recounted with `floati.scrub.scan_generated_tree(Path.cwd())` (count=6, not from `head`): the four weekend-program/north-star/brief files already on `932e377`, plus `docs/evidence/conformance/C0-machine-harness-inventory.md` from C0. C0 named a private source token while recording a negative finding; that token is what the scrub hunts. C1 copy avoids it. Not repaired.

## Row verdict

| check | verdict |
|---|---|
| unittest discover (1519) | FAIL (3 docs/scrub) |
| floati.selftest (1519) | FAIL (same 3; no manifest stamp) |
| conformance --live-root-smoke | PASS (5 cases) |
| scratch-root init/send/inbox/ack | PASS |
| real `/opt/homebrew/bin/codex --version` | PASS (`codex-cli 0.150.0`) |
| real `codex app-server --stdio` initialize | PASS (`handshake_ok: true`) |
| **surface_verified** | **true** |

C2..C8 remain gated on Fable's Car 4-landed announcement, then rebase onto that tip. Inbox at this row boundary is checked in the same turn as the envelope.
