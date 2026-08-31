# HM-0 phase-1 local evidence

**Evidence captured:** 2026-07-31T13:16:47Z
**Implementation SHA:** `d1b9291d0dd6404a2ed5fde8c63f6a9f74127eb6`
**Branch:** `refs/heads/lane/hm0`
**Base:** `d0ebbe62389c1d915d896900c610ea8da01dbcc0`

## Full artifact gate

Command:

```bash
PYTHONPYCACHEPREFIX=<temp>/slipway-pycache python3 -m slip.selftest
```

Observed result at the implementation SHA:

```text
Ran 69 tests in 0.928s

OK
{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}
exit 0
```

The direct CI command is `python3 -m slip.selftest`; the temporary bytecode
cache prefix is needed only for this sandboxed local verification environment.
The bundle manifest covers 23 deployable Python and schema files.

## Exact-range and compilation gates

Commands:

```bash
git diff --check d0ebbe6..HEAD
PYTHONPYCACHEPREFIX=<temp>/slipway-pycache python3 -m py_compile slip/*.py tests/*.py
```

Observed result: both exited `0` with no diagnostic output.

## Generated-artifact source scrub

Command:

```bash
python3 -c 'from pathlib import Path; from slip.scrub import scan_generated_tree; h=scan_generated_tree(Path.cwd()); print("scrub_hits="+str(len(h))); raise SystemExit(bool(h))'
```

Observed result:

```text
scrub_hits=0
exit 0
```

The same scrub runs inside full unittest discovery. Its positive fixture proves
that the gate detects the prohibited private source name case-insensitively.
The pre-publication owner brief is the only explicit exclusion.

## Review-driven hardening covered by the gate

- root and observation objects cannot be directly constructed; observation
  exposes no tenant path and creates no lock or metadata file;
- every durable write requires a validated writable root and a contained
  tenant-relative path; absolute, traversal, and symlink escapes refuse;
- every append and read validates an exact v0 record-kind contract;
- ledgers have record, byte, and count bounds, short-write rollback, file
  fsync, and parent-directory fsync on creation;
- registry uniqueness, message idempotency, and sparse-ack replay are locked
  process transactions, with four-process contention tests;
- delivery and acknowledgment evidence is cross-checked against message
  evidence so forged receipts and truncated history fail integrity checks;
- liveness, authority, and exclusion reject time regression and Boolean
  epochs; deadline-at-or-below-TTL remains enforced;
- conformance calls run in a persistent isolated worker with a bounded timeout,
  documented parser exits, repeatable run tenants, and full plane lifecycle
  coverage; hang and process-death fixtures both map to exit `30`.

## Scope ruling

`RULING-REQUEST-HM0-PHASE-SPLIT.md` records option A as resolved by the
owner's explicit instruction to build phase-1 deliverables (a)–(g). Broader
HM-0 charter surfaces are deferred and receive no implementation,
conformance, activation, deployment, or completion claim in this evidence.

## Evidence ceilings before push

- the architect push gate: **PENDING** for implementation SHA `d1b9291`.
- Hosted CI: **NOT OBSERVED** because the branch has not been pushed.
- Remote branch equality: **NOT OBSERVED** because push remains gated.
- Installed/deployed bundle: **NOT DEPLOYED / NOT CLAIMED**.
- Publication, public name, and license: **OWNER-TIER / OPEN**.
