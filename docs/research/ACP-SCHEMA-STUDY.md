# ACP v1 to Floati schema study

Status date: 2026-07-31. Scope: study only; no ACP adapter, process, transport,
configuration, or activation exists in this goal.

## Version and source boundary

This study targets ACP **v1 Latest**. The official index currently labels v1
latest and v2 draft, so v2 is excluded from the next implementation contract.
The source set is the official ACP documentation index plus the v1
initialization, session setup, prompt turn, tool call, and filesystem pages:

- [ACP documentation index](https://agentclientprotocol.com/llms.txt)
- [v1 initialization](https://agentclientprotocol.com/protocol/v1/initialization)
- [v1 session setup](https://agentclientprotocol.com/protocol/v1/session-setup)
- [v1 prompt turn](https://agentclientprotocol.com/protocol/v1/prompt-turn)
- [v1 tool calls](https://agentclientprotocol.com/protocol/v1/tool-calls)
- [v1 filesystem](https://agentclientprotocol.com/protocol/v1/file-system)

ACP v1 negotiates a major protocol version and optional capabilities during
`initialize`; omitted capabilities mean unsupported. Baseline session methods
are `session/new`, `session/prompt`, `session/cancel`, and `session/update`.
Optional methods such as load, resume, close, list, delete, extra roots, and
configuration must remain capability-gated.

## Mapping table

| ACP v1 observation | Candidate Floati record | Mapping law or gap |
| --- | --- | --- |
| `initialize` client/agent capabilities | `capability_record` | Emit one bounded observation per understood capability. Omitted/false is `unavailable`; filesystem read without write is `read_only`; write or terminal execution is never inferred from generic ACP support. Preserve expiry; do not turn a handshake into authority. |
| `session/new` and returned session ID | `work_item_record` plus an external-session binding | A session is not mail, liveness, authority, or mutex. Current artifact bindings require Git repo/SHA/doc, so opaque ACP session IDs need a new typed external binding before implementation. |
| `session/prompt` content | no direct v0 record | Mail accepts Git notification metadata only and work titles are bounded summaries, not arbitrary prompts. The faithful proxy must retain content in a governed external artifact and bind only its content-free reference. |
| `session/update` plan | `work_item_record` / `work_transition_record` | Pending plan entries can become work items. ACP statuses exceed Floati's current `claim`/`complete` transition vocabulary; `in_progress`, `failed`, and replacement semantics need a ruled extension rather than lossy coercion. |
| `tool_call` / `tool_call_update` | work item/transition plus artifact binding | Never equate `completed` with branch, test, push, or release completion. Raw input/output may contain secrets and requires a scrubbed artifact contract or omission. |
| `session/request_permission` | `approval_request_record` | Bind requester, capability, scope, TTL, and the exact authority epoch. ACP option labels/kinds remain provider data, not permission. |
| permission response | `approval_decision_record` | `selected` must map through a finite ruled option map. `cancelled` is neither approval nor denial; it needs an explicit cancelled decision state or a separate outcome record. Never auto-approve from ACP's allowance that clients may do so. |
| `session/prompt` response with `stopReason` | no direct v0 record | `end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, and `cancelled` are semantic outcomes. They are not delivery receipts, acknowledgments, protocol denial receipts, or work completion. Add a worker-turn outcome record. |
| JSON-RPC request/response exchange | no receipt inference | A matching response is protocol correlation, not Floati delivery or acknowledgment. Emit delivery/ack only from their own durable bus observations. |
| filesystem read/write and terminal support | `capability_record` plus approval | Read and write must remain distinct modes/scopes. Capability does not create approval; approval does not create authority or a mutex hold. Absolute ACP paths must additionally pass Floati root/path confinement. |
| session cancel/close | worker-turn outcome; possible work transition extension | Cancellation stops activity but does not prove cleanup, completion, release of authority, or release of mutex. Those planes require their own evidence. |
| message/resource/tool content | artifact binding when Git-addressable | `resource_link` or file URI is not automatically a Git artifact. Add a bounded URI/digest record before accepting non-Git content. |
| `usage_update` | no current record | Context and cumulative cost are useful but require currency, aggregation, reset, and provider-truth rulings. Never fold them into wake-cost context bytes. |
| active ACP process/session | no plane inference | Process/session existence is not Floati liveness. A future adapter may emit a TTL-bound observation only after a named probe; it can never infer authority or mutex. |

## Faithful-proxy requirements

The next goal must add a version-pinned ACP v1 schema snapshot and a finite
method allowlist. It must preserve JSON-RPC IDs and ordering, bound line and
total byte counts, quarantine extensions, reject duplicate object keys, and
distinguish notifications from responses. Transport success, request
correlation, user acknowledgment, tool outcome, work completion, and artifact
publication must remain different facts.

The adapter also needs new typed records before it can be honest:

1. external worker session binding;
2. worker turn outcome, including refusal and cancellation;
3. non-Git artifact reference with digest and confinement metadata;
4. additional work statuses or an explicit lossless projection rule;
5. approval cancellation distinct from approved and denied.

Capability observations remain tri-state and TTL-bound. ACP omission is
`unavailable`, not unknown support; an unobserved or expired handshake remains
expired/absent evidence. No ACP capability grants execution authority.

## Next-goal implementation fence

Implementation may begin only after a committed ruling names: ACP v1 schema
digest/version, local transport command and environment, executable ownership,
allowed methods, maximum outstanding requests, byte/time/depth bounds,
workspace-root confinement, artifact retention/scrub policy, permission option
mapping, cancellation outcomes, and shutdown behavior. Draft ACP v2 and remote
HTTP/WebSocket transports remain excluded. This study adds no configuration
because no runtime consumer exists.
