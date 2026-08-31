# Conformance v0

Status date: 2026-07-31.

The conformance artifact is invoked directly:

```bash
python3 -m floati.conformance \
  --adapter package.module:factory \
  --root /absolute/protocol/root \
  --tenant tenant-id
```

The factory accepts one `FloatiRoot` and returns an adapter implementing the
methods exercised by `floati.conformance.run`. Each method returns an
`AdapterResult` with one of these statuses:

- `ok`: the evidence object describes an observed result;
- `refused`: the evidence object contains the stable refusal `code`; or
- `intentional_silence`: policy deliberately chose not to act.

Returning `None`, returning another object shape, and raising unexpectedly are
three different outcomes. The v0 artifact exit codes are:

| Code | Meaning |
| ---: | --- |
| 0 | all conformance cases passed |
| 10 | evidence contradicted a required behavior |
| 20 | adapter or explicit-root configuration was refused |
| 30 | the adapter factory or operation died |
| 31 | the adapter chose intentional silence |
| 32 | the adapter returned no result |
| 33 | the adapter returned malformed evidence |
| 34 | the deployed bundle manifest did not match |

HM-0.5 also provides a structurally throwaway reference-core smoke:

```bash
python3 -m floati.conformance --live-root-smoke
```

The smoke function takes no path or root argument. It owns a
`TemporaryDirectory`, creates a direct home only below that directory, and
round-trips one Git notification through delivery and acknowledgment before
verifying typed `unknown_sender` and `unknown_recipient` refusals expose the
exact sorted active roster while leaving the whole tenant root byte-identical,
including an empty unknown-party denial list. The temporary directory is
removed on return. `--live-root-smoke` is mutually exclusive with `--adapter`,
`--root`, and `--tenant`, so this selftest surface cannot touch the fleet's
durable home.

The adapter-mode fixed cases observe registry writes and, before interpreting
any unknown-party refusal, directly verify that every registered send changed
the tenant root and appended one matching durable `events.jsonl` envelope.
They then observe message delivery, separate delivery and acknowledgment
receipts, a non-contiguous acknowledgment, typed unknown sender/recipient
refusals with the exact active roster and whole-root immutability, an empty
unknown-party denial list, liveness silence, authority deadline enforcement,
and separated exclusion evidence.

The artifact emits one machine-readable JSON object. Product-visible copy is
`COPY PENDING — ARCHITECT`; these status identifiers are protocol vocabulary, not
interface copy.
