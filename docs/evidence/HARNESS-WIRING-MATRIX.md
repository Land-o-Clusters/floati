# Harness wiring matrix — luna

Capture date: 2026-08-28<br>
Branch: `lane/luna-wiring`<br>
Governing brief: `docs/design/harness-wiring-brief-2026-08-28.md` (owner-source SHA-256 `36c0ca724e4f59b034c49bf04b2998f701f8d530c94cd229eda4383b586a167e`)<br>
Version inventory: [H wake-posture matrix](https://github.com/Land-o-Clusters/floati/blob/main/docs/evidence/gauntlet/H-wake-posture-matrix.md)

## Scope and secret boundary

The four unwired targets are recorded below: Cline 3.0.60, pi 0.84.3,
grok-build via the installed `grok` 1.0.5 override, and T3 v0.0.35. Codex,
Claude, Cursor, and OpenCode were already wired and were not changed. `herdr`
is not an LLM-turn harness and is out of scope.

The cheap profile is OpenRouter model
`deepseek/deepseek-v4-flash-0731`, checked against the
[OpenRouter model catalog](https://openrouter.ai/api/v1/models) at capture time
(approximately $0.06/M input and $0.12/M output). Where the harness supports
OpenRouter routing, the profile pins `relace/fp4` and sets
`allow_fallbacks=false`.

The OpenRouter key value is intentionally absent from this document, the
config entries changed for this lane, and the receipts. The only credential
references are the `OPENROUTER_API_KEY` environment name and the macOS
keychain service name `com.openai.codex.openrouter.api-key`. Shell commands
below resolve or inherit the value without printing it.

## Matrix

| harness | version and provider-config citation | applied config and key mechanism | one-turn receipt | verdict |
|---|---|---|---|---|
| cline | `3.0.60`, `/opt/homebrew/bin/cline`; [C5 live binary receipt](conformance/C5-cline-conformance-live.md#real-binary-receipts). Provider surface: [Cline OpenRouter configuration](https://docs.cline.bot/provider-config/openrouter). | No config change. Cline's documented OpenRouter setup asks for an API key, and its live CLI surface exposes `--key <api-key>` / `auth --apikey` as literal-key inputs; no env or command-lookup surface was found. | No turn: hard-fence STOP after the literal-only finding. | **gap** — typed GAP; owner must paste a literal personally or rule this harness out. |
| pi | `0.84.3`, `/opt/homebrew/bin/pi`; [C7 live binary receipt](conformance/C7-pi-conformance-live.md#real-binary-receipts). Provider surface: [pi custom models](https://pi.dev/docs/latest/models#provider-configuration), [value resolution](https://pi.dev/docs/latest/models#value-resolution), and [providers](https://pi.dev/docs/latest/providers). | `~/.pi/agent/models.json`: OpenRouter base URL, `openai-completions`, model `deepseek/deepseek-v4-flash-0731`, and `apiKey` set to `!security find-generic-password -w -s com.openai.codex.openrouter.api-key`; routing pins `relace/fp4` with fallback disabled. | [Pi probe](#pi-probe): exit `0`; response echoed `model=deepseek/deepseek-v4-flash-0731`; `408` input / `27` output / `435` total tokens. | **wired** |
| grok-build (override) | Named `/opt/homebrew/bin/grok-build` absent; measured override `/opt/homebrew/bin/grok` `1.0.5 (5115b46bc909)`; [C6 override receipt](conformance/C6-grok-build-conformance.md#real-binary-receipts). Provider surface: [Grok custom models](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/11-custom-models.md#configuring-custom-models) and [credential resolution](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/11-custom-models.md#credential-resolution). | `~/.grok/config.toml`: alias `luna-openrouter-deepseek` maps to the OpenRouter base URL and DeepSeek model, uses `api_backend = "chat_completions"`, and reads `env_key = "OPENROUTER_API_KEY"`. Grok's documented custom-model surface has no OpenRouter request-body provider pin field, so `relace/fp4` cannot be asserted at this layer. | [Grok probe](#grok-build-probe): exit `0`; response echoed `model=deepseek/deepseek-v4-flash-0731`; `23,721` input / `16` output / `23,737` total tokens; one actual provider turn. | **wired** — direct OpenRouter path verified; provider pin is a documented surface limitation. |
| t3 | `v0.0.35`, `/opt/homebrew/bin/t3`; [C9 live CLI receipt](conformance/C9-t3-compatibility-live.md#cli-identity). Provider surface: [T3 provider setup](https://github.com/pingdotgg/t3code/blob/main/docs/user/install.md#providers) and [provider drivers](https://github.com/pingdotgg/t3code/blob/main/docs/internals/providers.md#provider-drivers). | T3 has no native OpenRouter driver; it composes the enabled Grok driver. `~/.t3/userdata/settings.json` enables Grok, uses `/opt/homebrew/bin/grok`, and registers `luna-openrouter-deepseek` as a custom model. T3 inherited `OPENROUTER_API_KEY` from the launch environment; no key was persisted in T3 settings. | [T3 probe](#t3-probe): server reached ready; dispatch exit `0`; completed response echoed `model=deepseek/deepseek-v4-flash-0731`; provider event log recorded roughly `12,253` total tokens (T3 did not expose an input/output split). | **wired** — composed through Grok; no native T3 OpenRouter driver and no independent T3 provider pin. |

## Exact probe commands

All commands used the keychain lookup only inside a child environment or an
inherited process environment. The lookup output was never sent to stdout,
stderr, a file, or this evidence document.

### Pi probe

```sh
OPENROUTER_API_KEY="$(security find-generic-password -w -s com.openai.codex.openrouter.api-key)" /opt/homebrew/bin/pi --provider openrouter --model deepseek/deepseek-v4-flash-0731 --mode json --print --no-session --no-tools --no-extensions --no-skills --no-context-files --thinking off -- "Reply exactly with model=deepseek/deepseek-v4-flash-0731 and nothing else."
```

Exit `0`. The JSON response named provider `openrouter`, model
`deepseek/deepseek-v4-flash-0731`, and usage `input=408`, `output=27`,
`total=435`.

### Grok-build probe

```sh
OPENROUTER_API_KEY="$(security find-generic-password -w -s com.openai.codex.openrouter.api-key)" /opt/homebrew/bin/grok -p "Reply exactly with model=deepseek/deepseek-v4-flash-0731 and nothing else." -m luna-openrouter-deepseek --output-format json --max-turns 1 --no-subagents --disable-web-search --permission-mode plan
```

Exit `0`. The final JSON response named
`deepseek/deepseek-v4-flash-0731`; usage was `input=23,721`, `output=16`,
`total=23,737`, with `modelCalls=1`. A prior invocation stopped before any
provider turn because the sandbox could not open Grok's existing session state;
the command above was then run once with the required local-state permission.

### T3 probe

The T3 server was launched with the key in its inherited environment:

```sh
OPENROUTER_API_KEY="$(security find-generic-password -w -s com.openai.codex.openrouter.api-key)" /opt/homebrew/bin/t3 serve --no-browser --mode web --host 127.0.0.1 --port 3774 ~/Projects/floati-luna
```

The one turn was submitted to the authenticated local server with this exact
dispatch command (the cookie file contained only the local T3 session):

```sh
/usr/bin/curl -fsS -b <temp>/t3-luna-cookie -H 'content-type: application/json' --data-raw '{"type":"thread.turn.start","commandId":"0f3b3dd4-1e9f-42b8-a01c-df098dbb3bb0","threadId":"db0322e0-2d90-4cf1-a721-6a8f72782f6c","message":{"messageId":"352b90ea-81f1-4669-9e7d-4fa7d7c42404","role":"user","text":"Reply exactly with model=deepseek/deepseek-v4-flash-0731 and nothing else.","attachments":[]},"modelSelection":{"instanceId":"grok","model":"luna-openrouter-deepseek"},"runtimeMode":"auto","interactionMode":"default","createdAt":"2026-08-28T02:00:02.000Z"}' http://127.0.0.1:3774/api/orchestration/dispatch
```

The server reached its ready message; the dispatch reported exit `0` and the
thread snapshot reached `completed`. The server was then stopped intentionally
with Ctrl-C. The assistant response was exactly
`model=deepseek/deepseek-v4-flash-0731`. T3's provider event receipt is
`~/.t3/userdata/logs/provider/events.db0322e0-2d90-4cf1-a721-6a8f72782f6c.log`;
its `totalTokens` field was `12253`.

## Bounds check

- No hooks were installed.
- No Floati fleet root or fleet-bus path was touched.
- No already-wired harness was modified; Cursor was not used as a fallback.
- No credential value appears in the committed branch or in the config entries
  changed for this lane; no provider log was copied into the branch.

## ADDENDUM (the architect, 2026-08-28): cline GAP → WIRED via native ChatGPT auth

The owner chose cline's own ChatGPT sign-in over a pasted literal — the
better custody model (cline-managed auth tokens, no raw key in
`providers.json`). Verification probe run by the architect, no credential contact:
`cline --json --auto-approve false -- "Reply exactly with the word OK…"` →
exit 0, response `OK`, provider `openai-codex`, model `gpt-5.6-luna`,
usage 3,810 in / 5 out, cost $0.000768. Cost accounting note: cline drill
turns bill the owner's ChatGPT plan, not the OpenRouter meter. Tide note:
the `--json` run_result carries `model.info.contextWindow` (1,050,000) and
full per-run usage — a derivability upgrade for cline's tide row, filed
for the T1 table's next amendment. The original OpenRouter literal-key GAP
row stays verbatim below/above as the photograph it was.
