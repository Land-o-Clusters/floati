# CUR-2 · public-clone receipt — the Cursor wire measured from a clean clone (2026-09-11)

**Verdict: PASS.** The shipped Cursor wire — `floati hook install --harness
cursor` and `floati wake wait --harness cursor` — behaves at harbor main
`f0532c456da07c2de5cc7185139f7a79c3d33f8c` exactly as
`docs/evidence/cur-2-2026-09-11.md` (as amended by Am.1 and Am.2) says it
does, from a fresh clean clone with no state the fleet session created. This
receipt is read-only with respect to every live surface: the fleet root, the
seat workspace and the clone all live under one scratch directory, and the
host-global Cursor configuration is hash-proved untouched. No README byte is
edited here.

## Shape of the run

```
git clone <harbor> <temp>/z3-cur2-rcpt/floati && git checkout --detach f0532c45
python3 -m floati init      --root <temp>/z3-cur2-rcpt/fleet --solo coordinator --harness cursor
python3 -m floati node add  --root <temp>/z3-cur2-rcpt/fleet --node builder-a --harness cursor --lifetime permanent
python3 -m floati hook install --harness cursor --root <temp>/z3-cur2-rcpt/fleet \
  --as builder-a --workspace <temp>/z3-cur2-rcpt/seat --runtime <temp>/z3-cur2-rcpt/floati
```

`hook install` wrote `<temp>/z3-cur2-rcpt/seat/.cursor/hooks.json`
(`timeout` 1800, `loop_limit` 1000 — the explicit numeric limit Am.1
requires, never `null`) embedding this command, verbatim:

```
<temp>/z3-cur2-rcpt/floati/scripts/floati wake wait --harness cursor --root <temp>/z3-cur2-rcpt/fleet --as builder-a --runtime <temp>/z3-cur2-rcpt/floati --deadline-seconds 1500 --hook-timeout-seconds 1800 --loop-limit 1000
```

Every case below pipes one stop payload into that exact command string run
from the scratch clone. The runtime the hook execs IS the clone, so the
public-shaped tree carries the whole wire.

**Control.** Host-global `~/.cursor/hooks.json` SHA-256
`69b2efe6f36a7ca321af5e4b4e01c2687336fc39964465401f50bf817f47e859` before
the install and after all four cases — identical. The install wrote only the
declared seat project file.

## The four stop payloads

| # | payload | stdout | stderr | rc | journal row (`state/cursor-wait/builder-a/journal.jsonl`) |
|---|---|---|---|---|---|
| A | `{"status":"aborted","loop_count":0}` | `{}` | empty | 0 | `exit_empty_aborted`, `loop_count 0`, `loop_limit 1000` |
| B | completed, one unread envelope at start | `{"followup_message": "[floati] unread mail for builder-a on …\nDrain it now:\ncd … && python3 -m floati inbox --root … --as builder-a --session cursor-stop-builder-a\nThen ack each envelope and act on the newest architect order first.\nWhen the queue is empty, say so in one line and stop."}` | empty | 0 | `followup_mail_at_start`, `unread 1` |
| C | completed, empty inbox, 5 s deadline | `{"followup_message": "[floati] no mail for builder-a in 0 min. Reply exactly: armed. Then stop."}` | empty | 0 | `followup_rearm`, `waited_seconds 5.0` |
| D | completed, root `chmod 000` (unreadable) | `{"followup_message": "[floati] cannot read the inbox for builder-a on <temp>/z3-cur2-rcpt/fleet: PermissionError: Permission denied. The root may be missing, may not be a floati fleet root, the node may not be registered there, or the ledger may be unreadable. Say that in one line, naming this root and node, and stop."}` | exactly one line: `[floati] builder-a: not journaling under …: it is not a readable floati fleet root, and creating one there would change the reason the next wake reports. The row: {"event": "followup_inbox_unreadable", …, "reason": "PermissionError: Permission denied", …}` | 0 | none — the journal is unchanged (3 lines, cases A/B/C), and nothing is created under the unreadable root |

Reading the four against the amended contract:

- **A** — `{}` is printed for exactly the aborted condition, and the abort
  row names the `loop_limit` beside `loop_count` (Am.1's attributability
  property).
- **B** — mail at start never prints `{}`; the followup is the Am.2 (N4)
  five-line shape with the drain command alone on its own line, `--session`
  included, paths as written.
- **C** — the deadline exit re-arms and never prints `{}`; `waited_seconds
  5.0` is the Am.1 (F3) bound holding at the few-seconds scale.
- **D** — the unreadable root names node, root and typed reason
  (`PermissionError: Permission denied`) in the hook body, writes the row to
  stderr in exactly one line, creates nothing, and leaves the reason stable
  for the next wake (Am.2, N1).

**The drain line runs verbatim.** Case B's command line was executed as
printed (`cd <clone> && python3 -m floati inbox --root … --as builder-a
--session cursor-stop-builder-a`): `status: ok`, 1 message presented,
acknowledgment `ack-01a0918ce1877481901c236071cdee5e`. The fallback session
id `cursor-stop-<node>` (payload carried no `conversation_id`) is a legal
session coordinate, as Am.1 (F4) claims.

## The one deviation, named

Case C runs the installed command with `--deadline-seconds 5` substituted
for the installed `--deadline-seconds 1500` — one value, named here. The
installed default would have held the hook for twenty-five minutes; the
seat fence requires every waiter a seat starts to carry a deadline of a few
seconds. Cases A, B and D are the installed command byte-for-byte.

## What this receipt cannot see

- **No real Cursor IDE was driven.** The payloads are fixtures on stdin; the
  channel policy (where Cursor shows stderr, whether it honours
  `loop_limit: 1000`) remains exactly as unmeasured here as Am.1 and Am.2
  recorded.
- **One node, one sender, one machine, no concurrency**, and no long-horizon
  run: the longest wait executed is 5 s.
- **Case D unreads the root by `chmod 000` on a scratch directory.** A
  missing root or an unregistered node exercise the same F1 path by other
  causes and were not separately constructed here.
- **The clone ran `python3 -m floati` from the checkout tree**; no install
  step, no venv. A public consumer installing differently (pip, a different
  interpreter) is unmeasured by this receipt.

## Reproduction

Every byte of the run above came from commands quoted in this receipt plus
`python3 -m floati` invocations of the clone at `f0532c45`; the scratch
directory is disposable, and re-running the sequence reproduces the four
exits (journal `ts` values and receipt ids differ; stdout bodies, stderr
shapes, exit codes and journal event kinds are the contract).
