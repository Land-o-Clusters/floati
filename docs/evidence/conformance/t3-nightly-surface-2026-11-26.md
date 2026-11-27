# T3 Code Nightly surface receipt

Measured on 2026-11-26. The app was **T3 Code Nightly 0.1.13-nightly.20261126.5305**. Its bundled launcher reported `t3 v0.1.13-nightly.20261126.5305`. A separate Homebrew launcher was still on `t3 v0.0.35`. The August CLI cells describe that older version. The August desktop cells describe Nightly 0.0.35-nightly.20260826.1195.

## Installed CLI and matching source

- **MEASURED (help)**. The bundled `t3 --help` lists `thread`. Running `t3 thread --help` lists `list`, `start`, `send`, and `show`. Each subcommand's help lists `--json`. The corresponding CLI implementation is in the tagged source. [Thread command](https://github.com/pingdotgg/t3code/blob/bc0f04da94f64b5e487c598761a43b4cf06b2ac9/apps/server/src/cli/thread.ts#L1049-L1121)
- **SOURCE VERIFIED**. The release tag `v0.1.13-nightly.20261126.5305` resolves to `bc0f04da94f64b5e487c598761a43b4cf06b2ac9`. It documents host-side `t3 thread` without OAuth and an OAuth MCP entry point for outside agents. [Release](https://github.com/pingdotgg/t3code/releases/tag/v0.1.13-nightly.20261126.5305) · [Outside agents](https://github.com/pingdotgg/t3code/blob/bc0f04da94f64b5e487c598761a43b4cf06b2ac9/docs/user/outside-agents.md)
- **SOURCE VERIFIED**. External MCP access levels are `read-only`, `approval-required` (Supervised), `auto-accept-edits`, `auto`, and `full-access`. The read-only level has orchestration read scope. The other levels add orchestration operate scope, and full access adds settings and source-control write scopes. The caller's grant caps its requested agent mode. [Access schema](https://github.com/pingdotgg/t3code/blob/bc0f04da94f64b5e487c598761a43b4cf06b2ac9/packages/contracts/src/auth.ts#L270-L302) · [Invocation context](https://github.com/pingdotgg/t3code/blob/bc0f04da94f64b5e487c598761a43b4cf06b2ac9/apps/server/src/mcp/McpInvocationContext.ts#L87-L118)
- **SOURCE VERIFIED**. MCP Events (`io.modelcontextprotocol/events`) exposes milestones by polling, streaming, or HTTPS webhook. The documented buffer holds the latest 1,000 events in memory and webhook leases run up to one hour. The schema names `thread.run_settled`, `thread.needs_input`, `thread.input_resolved`, `thread.guest_request`, `thread.guest_request_resolved`, `pr.watch_wake`, `workflow.run_started`, `workflow.needs_input`, `workflow.input_resolved`, `workflow.run_paused`, `workflow.run_resumed`, and `workflow.run_ended`. [Event delivery](https://github.com/pingdotgg/t3code/blob/bc0f04da94f64b5e487c598761a43b4cf06b2ac9/docs/user/outside-agents.md#L35-L41) · [Milestone schema](https://github.com/pingdotgg/t3code/blob/bc0f04da94f64b5e487c598761a43b4cf06b2ac9/packages/contracts/src/milestones.ts#L59-L160)

## Runtime evidence

The prior **T3-HOOK-PROBE-1** receipt (`t3-hook-probe-report.md`, 2026-11-26 01:56–01:59 UTC) is **MEASURED** on the preceding `v0.1.13-nightly.20261125.5258`. In a Claude Code parent, the project `PreToolUse` hook on `mcp__t3-code__delegate_task` fired. A deny prevented the child from starting and its reason reached the model. The allow control started a child. Codex-parent hook behavior is **UNMEASURED** and would need its own allow and deny probes inside a T3 Codex thread. Claude Code on the newer build and `PostToolUse` were also unmeasured.

| Current Nightly observation | Status and evidence needed |
|---|---|
| CLI thread operations | **UNMEASURED** live. Would need a scratch project and thread to check `list` and `show` JSON, launch, message delivery and terminal outcomes. |
| MCP permission enforcement and milestones | **UNMEASURED** live. Would need approved read-only and write-capable clients, a scratch thread, a refused write, and observed delivery for deliberately triggered milestones. |
| Floati bus and work on CLI or desktop | **UNMEASURED**. Would need a scratch fleet with send, receive and ack receipts, followed by a work claim and completion and a check of the registered T3 observation path. |
| Floati wake on CLI or desktop | **UNMEASURED**. Would need an idle seat on each surface, an authorized event and a wake-to-delivery receipt. |
| Auto-turnover, native compaction, boot and managed send | **UNMEASURED** on both surfaces. Would need a session handoff, an exercised native compaction path, a seat board or boot receipt, and an authorized managed-send outcome. |
| Provider wiring | **UNMEASURED**. Would need a measured provider route and a completed turn on this build. |
