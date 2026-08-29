# H1 — onboard a scratch node

**Family:** onboard. Capture: `docs/evidence/gauntlet/captures/H-skeleton-run.json` (23598 bytes, sha256 `8b01eaeff030e4ec3de568c09b25fb28303fccc0e5d0448f1ae9c1d25aada149`).
**Trunk:** `c4dd4a164328f91407e4103562a0e6308d573f73`
**Scratch:** `~/Projects/floati-grok/.gauntlet-scratch/h20260828004028`

`floati node add` is absent. This drill used the verbs that exist.

## init --solo

```text
argv: ["/usr/bin/python3", "-m", "floati", "init", "--root", "<scratch>", "--solo", "grok-h", "--harness", "Codex"]
exit: 0
stdout_bytes: 367
stdout_sha256: cb354ccf68925661e7663245858ef5d6971b7907014bd09c27ffa4d524a93e31
```

`tenant_id=h20260828004028`, `solo.node_id=grok-h`, `solo.harness=Codex`.

## register peer

```text
argv: ["/usr/bin/python3", "-m", "floati", "register", "--root", "<scratch>", "peer-h1", "--harness", "Codex"]
exit: 0
```

`registry-01a045cf99777973879b91357abbe2dc`, `node_id=peer-h1`, `state=active`.

## Defects filed (not fixed)

1. **`floati node add` ABSENT.** WS-B wizard is not on this trunk. Not repaired by this seat.

**Verdict: PASS** (onboard analog via init+register)
