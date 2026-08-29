# C10 — README matrix cell collection (grok, 2026-08-27)

**Row:** C10 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Seat:** `grok`. **Not a README edit** (WS-F places cells).
**Prefix HEAD (C9):** `63eff5d75441a61e0b52acd4096bb1e8256ada05`
**Grammar:** `| <harness> | [<stamp>](<receipt-relpath>) |` from `docs/evidence/conformance/C9-matrix-cells-draft.md`
**Stamp `live`:** gated later by Fable; this seat only emits cells whose row doc already has `surface_verified: true` and names the launched executable.

No cell says more than its receipt.

## Paste-ready cells (WS-C harness rows)

```markdown
| harness | status |
|---|---|
| codex | [live](docs/evidence/conformance/C1-codex-conformance-live.md) |
| claude | [live](docs/evidence/conformance/C2-claude-conformance-live.md) |
| opencode | [live](docs/evidence/conformance/C3-opencode-conformance-live.md) |
| cursor | [live](docs/evidence/conformance/C4-cursor-conformance-live.md) |
| cline | [live](docs/evidence/conformance/C5-cline-conformance-live.md) |
| grok-build | [live](docs/evidence/conformance/C6-grok-build-conformance.md) |
| pi | [live](docs/evidence/conformance/C7-pi-conformance-live.md) |
| herdr | [live](docs/evidence/conformance/C8-herdr-conformance-live.md) |
```

C0 and C0-DELTA are inventory, not harness cells.

## t3 (C9 compatibility; not one of the eight)

C9 `surface_verified` is **false** for t3-driven bus verbs. No `live` cell:

```markdown
| t3 | [CLI](docs/evidence/conformance/C9-t3-compatibility-live.md) |
```

`CLI` here means the `t3 v0.0.35` binary was launched (`t3 serve` listened). It is not a bus-verb receipt.

No foreign-bus path was read or written.
