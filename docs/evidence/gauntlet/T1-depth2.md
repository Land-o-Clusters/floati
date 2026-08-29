# T1 depth-2+ follow-up — remaining C transcript cells

**Dispatch:** `msg-01a046646fc27f1da48ce1e41004dfce` (amendment at `e001a6fed34b875083d119c335fc2798944aa752`).
**Lesson this row tests:** a key scan at depth 1 answers about depth 1.
**Discipline:** key **paths** only; no values. No GUI, no credentials, no live-fleet writes, no product source edits.

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/gauntlet/captures/T1-depth2-keypaths.json` | 19624 | `554a0a0482ba58ec9d2383af182ec2426dba506bc08264e6a1a06196966d526c` |

Control: same machine, Python 3.9.6. Probe UTC from capture `started_utc`. Claude floati-grok transcript used as a **positive control** (architect already measured `message.usage` on 646 rows).

Depth: 8. JSONL: every complete line ≤64KiB, cap 2000 lines.

## Results

| cell (T1 said C or shallow) | files | records | usage-class key paths at depth ≥2 | verdict after full-depth look |
|---|---|---:|---|---|
| **claude / cli** (control) | `~/.claude/projects/-Users-operator-Projects-floati-grok/*.jsonl` | 11 | `message.usage.input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, … | **stands as class A** (matches the amendment; this seat reproduces the nest) |
| **codex / cli** jsonl beyond line one | small file 11 lines: window fields only. Midsize `rollout-2026-07-14T22-06-41-…jsonl` (375494 bytes, 64 lines) | 64 | `payload.info.last_token_usage.{input_tokens,output_tokens,cached_input_tokens,reasoning_output_tokens,total_tokens}`, same under `total_token_usage`, plus `payload.info.model_context_window` | **C falls.** Remaining-context is class-A DERIVED: latest `last_token_usage.total_tokens` (or input+cache) over `model_context_window`. Same stamp as Claude: per-event, from disk, no seat. Small files may omit the `info.*` nest — T2 must scan until those paths appear or the file ends. |
| **cursor / desktop** `agent-transcripts` | this grok session jsonl (716463 bytes) | **517 / 517** | **none** (`usageish_paths=[]`) | **C stands.** Nested keys are tool-call inputs (`message.content[].input.command`, …), not token usage. |
| **grok / cli** `summary.json` siblings | `summary.json`, `updates.jsonl`, `chat_history.jsonl`, `events.jsonl`, `prompt_context.json` | summary 1 object; updates 1; chat_history 2 | **none** | **C stands** for token/remaining-context on disk. Turn-count proxies in `summary.json` (`num_messages`) are unchanged. |

## What T2 may add

- Codex jsonl is **richer than T1 line-one**: `payload.info.last_token_usage` + `model_context_window` are real class-A gauges when the nest is present.
- Cursor agent-transcripts still cannot feed a percent policy from disk.
- Grok remaining-context still needs class-B `/context` (or the `num_messages` / `du` proxies already in T1).

Wake-family daemon drills were **not** started on this follow-up (architect: stand by until the acceptance run is the queued row).
