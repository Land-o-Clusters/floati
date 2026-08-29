# HM-2S Phase A — ACP adapter live probe

Status date: 2026-08-01.

## Contract

`HM2S_BRIEF.md` requires a local responder probe in this order: Claude Code
ACP, Codex ACP, then the already-ruled fixture fallback. A real worker turn is
eligible only when an ACP responder exists. Ordinary Claude Code or Codex
commands are not silently substituted for an ACP transport.

## Executable probe

The non-launching executable probe observed:

```text
claude-code-acp=ABSENT
codex-acp=ABSENT
acp-agent=ABSENT
claude=/opt/homebrew/bin/claude
codex=/opt/homebrew/bin/codex
```

The existing adapter probe returned the compact artifact:

```json
{"command":null,"executable":null,"status":"reference_harness_absent"}
```

The installed ordinary `claude` and `codex` help surfaces advertise MCP and,
for Codex, app-server commands; neither advertises ACP. No ACP responder was
therefore available. No child process, credential access, provider request,
workspace mutation, or approval request occurred. The real-turn branch is an
honest skip, not a live pass.

## Fixture proof

Command:

```sh
python3 -m unittest tests.test_acp_adapter -v
```

Result: exit 0; 4 tests passed. The two governed fixtures decoded and
re-encoded to semantically equal JSON:

```text
tests/fixtures/acp/initialize-request.json: semantic_round_trip=True
tests/fixtures/acp/initialize-response.json: semantic_round_trip=True
```

This proves the bounded fixture codec and honest absence path only. It does
not claim a real ACP session, provider traffic, deployment, activation, or
platform approval.
