# HM-1 Phase D evidence

Status date: 2026-07-31.

This ledger records the dark external-worker contract checkpoint. It does not
claim a live Codex or ACP process, network compatibility, credentials,
activation, deployment, hosted CI, or push.

## Identity

- Branch: `lane/hm0`.
- Phase C evidence tip: `daef52b936337e3e1402a62ffe2ec20b705f749a`.
- Phase D implementation and study: `75e6da5`.
- Evidence-binding commit: not predicted by this document.

## RED-first ledger

The Phase D RED command was:

```sh
python3 -m unittest -v tests.test_codex_adapter_contract tests.test_schemas
```

Observed before implementation: exit 1; 15 tests ran with nine failures.
The codec, three schemas, and recorded fixtures were absent.

Final focused command:

```sh
python3 -m unittest -v tests.test_codex_adapter_contract tests.test_schemas tests.test_manifest
```

Observed: exit 0; 23 tests ran; `OK`. The complete selftest then ran 153
tests with `OK` and returned `bundle_verified` for `refs/heads/lane/hm0`.

## Contract boundary

- Three recorded categories round-trip: request, response, notification.
- Unknown root fields are quarantined from contract fields but restored on
  encode, preserving fixture bytes semantically without treating extensions
  as approved contract fields.
- Complete JSON is bounded to 1 MiB and 64 nesting levels; non-finite JSON,
  Boolean IDs, category ambiguity, and malformed error envelopes refuse.
- A syntax-tree test rejects process, socket, HTTP, and URL imports, and scans
  for common launch/connect calls. No live process or network command ran.
- Fixture provenance records the original Puddle three-message ruling and its
  later provider-specific amendments; this phase locks envelope categories,
  not an invented or stale outbound sequence.

## ACP study boundary

`docs/research/ACP-SCHEMA-STUDY.md` was refreshed from official ACP material on
2026-07-31. It targets v1 latest and excludes v2 draft. It maps initialization,
sessions, prompt/update, tool, permission, filesystem, cancellation, artifact,
usage, and process observations to existing Slipway records or explicit schema
gaps. The study creates no runtime configuration or implementation.

## Complete local gate

Fresh gate commands after the README, design, evidence, and ruling request
were present:

```sh
python3 -m slip.selftest
python3 -m slip.conformance --live-root-smoke
python3 -c 'from pathlib import Path; from slip.scrub import scan_generated_tree; hits=scan_generated_tree(Path.cwd()); print("scrub_hits="+str(len(hits))); raise SystemExit(bool(hits))'
python3 -m unittest -v tests.test_copy_ledger tests.test_codex_adapter_contract
make demo-capture
python3 -m slip.demo --capture monochrome > <temp>/floati-hm1-mono.txt
cmp docs/evidence/captures/hm1-tui-monochrome.txt <temp>/floati-hm1-mono.txt
git diff --check
```

Observed: selftest exit 0, 153 tests, `OK`, then `bundle_verified` naming
`refs/heads/lane/hm0`; smoke exit 0 with five conformant cases; scrub exit 0
with zero hits; combined copy-ledger and Codex-contract focused check exit 0
with eight tests; regenerated monochrome capture compared byte-for-byte equal;
diff check exit 0 with no output.

## Boundaries

- Dark Codex schemas/codec and fixture round-trip: locally executed.
- ACP mapping study: documented from current official sources; no runtime
  adapter exists.
- the architect voice/gate verdict, push, hosted CI, deployment, activation, live
  Codex compatibility, and live ACP compatibility: pending or unobserved.
