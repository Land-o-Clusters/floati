# Claude/CLI wake re-measurement (grok, 2026-09-04)

MX-3 re-measure at declared current `2.1.251 (Claude Code)`. Supersedes the
*citation* of `H-claude-wake-remeasure-2-2026-08-29.md` (measured 2.1.238);
that file remains a photograph and was not rewritten.

Declared executable: the canonical 2.1.251 file named by
`docs/evidence/conformance/C2-claude-cli-version-2026-09-03.md`. Not PATH.
Homebrew `claude` is still 2.1.231 and was not selected.

## Measured, this machine, today

- **Version:** `2.1.251 (Claude Code)`, exit 0, 22 bytes, sha256
  `1aaadbe01265e82bd28c2e2639a2a6a0604edbb124f997e9d3a4d09b823c6fb8`.
- **Surface enumeration (`--help`)**, exit 0, 19282 bytes, sha256
  `5ff2e7a0bca8535fb9ec097fa0a21e9d6b735ed94104fa0d1f58ac73a841d52d`:
  `-p/--print` · `--bg/--background` · `--continue`/`--resume`/`--from-pr` ·
  `--bare` names hooks (skip hooks) · Commands list has **no** `serve` ·
  **no** `listen` · **no** `watch`. A substring hit on `serve` is MCP
  "servers" prose, not a waiter-subscribe command. `gateway` is enterprise
  auth/telemetry, not an external wake bus.
- **Authenticated headless invoke:**
  `claude -p 'Reply with exactly: WAKE-PROBE-OK' --output-format json`
  exit 0, 6.428 s, stderr 0, stdout 1720 bytes, sha256
  `4178e6bb9fca65a9322705d006e15ffaf678817c0ae2d44391cc75d0f7871818`.
  Parsed: `type=result`, `subtype=success`, `is_error=false`,
  `result` exactly `WAKE-PROBE-OK`, session
  `3a2c27cc-4115-4f1a-80cf-9cccabe5b999`. Billing fields are not copied
  into this receipt.

## Conclusion

**claude / cli · wake = daemon — CONFIRMED at 2.1.251.** A cold seat still
has nothing external to subscribe to; waking it means starting/resuming a
process. `--bg` is a process the operator starts. Grade: **MEASURED**. The
cell value is unchanged — daemon — and now carries live proof at the
declared current version.
