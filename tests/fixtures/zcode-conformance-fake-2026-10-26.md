# Zcode conformance receipt

Mode: fixture-fake. Source: `f7585a8f633af5750128d258d82fa1174b3a9f08`.

Replay JSON: `zcode-conformance-fake-2026-10-26.json`; SHA-256: `4000d476411cb94e5dc1d8004cd73dc60480dd24b1e5a3aeb1e6c7517b16d1f2`.

```json
{
  "status": "ok",
  "surface": "native_success",
  "bus": "send_delivery_ack_verified",
  "work": "complete_with_bound_artifact",
  "live_conformance": false
}
```

One native invocation maximum; empty scratch HOME, no credentials copied. Bus proof is a native-invoked scratch send/drain/ack, not a live hook wake. Work proof requires a committed nonce artifact and complete governed receipts. A fake capture does not establish live support. Missing provider access remains cannot-measure.
