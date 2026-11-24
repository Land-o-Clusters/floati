# Claude idle inbox dependency receipt

Date: 2026-10-29. Verified client version: **Claude Code 2.1.309**.

Floati uses the session inbox described in Claude Code's
[cross-session messaging reference](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket),
respects the inbound controls described by the
[`crossSessionInbound` settings reference](https://code.claude.com/docs/en/settings-reference#crosssessioninbound),
and presents waiting mail through the documented
[`UserPromptSubmit` hook](https://code.claude.com/docs/en/hooks#userpromptsubmit).

The undocumented behaviour verified on 2.1.309 is that a newline-JSON user
message can be accepted without a reply on the sending connection. Floati
uses this only as a mail-free doorbell. Socket EOF does not prove delivery:
confirmation requires Floati's own presentation receipt, shared with its
hook relay and Stop path. Acknowledgment remains an explicit operation.

Readable settings that say hold or refuse prevent ringing. Otherwise the
receiver's own inbound policy remains authoritative. Floati never weakens it.
On macOS/Linux Floati sends no authentication line, reads no messaging token
and stores no token. Windows uses the Stop path because authentication is
required there.

Typed fallbacks retain the Stop path:

| Reason | Condition |
| --- | --- |
| `claude_inbox_unavailable` | Missing, stale or unreachable inbox, or unsupported platform |
| `claude_inbox_held` | Readable settings say hold |
| `claude_inbox_refused` | Readable settings say refuse |
| `claude_inbox_unverified_version` | Unknown client version or a version outside the verified set |
| `claude_inbox_unconfirmed` | No presentation receipt after the bounded ring and one retry |

This receipt records the dependency and observed protocol behaviour. Live
doorbell-versus-Stop latency and idle CPU qualification remain unmeasured.
