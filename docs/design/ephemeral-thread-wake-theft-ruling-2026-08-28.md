# RULING — Ephemeral spawned threads participate as the seat, and one stole a live dispatch

**the architect, 2026-08-28 ~14:25Z. Status: RULED.** Repair seat: build lane (wake-daemon owner). Priority:
**before the binding campaign** — the campaign proves the product as of its run, and this defect sits
inside the wake path the campaign exercises.

## The finding (owner-reported, then measured — every claim below has a receipt)

Codex (and possibly other harnesses) can spawn a peer thread that communicates with its parent inside
the harness. Such a thread fires the harness **Stop** hook itself. The Floati waiter is registered on
Stop, and `codex_wait.py` resolves participation from the hook payload's **cwd**
(`resolve_participant(bus_home, cwd)`), so **any thread whose cwd is inside a bound workspace IS the
seat** — session identity is checked only for shape (non-empty, ≤512 chars) and against an opt-out
marker that nothing writes for spawned threads.

Witnessed live, `receipts/wakes/build lane.jsonl`:

- 04:41:46Z — architect dispatches E2 CONTEXT ACTIVATION to build lane
  (`msg-01a046ac844f7db998fe1c8e705f3f56`) — the row that unblocks grok's context family, the last
  gauntlet family on the feature-complete ladder.
- 04:54:34.801Z — `wake_hold_receipt` `wake-hold-01a046b83e31781597e36025725fc9f5`: an evaluate
  consumed the message's freshness (fresh → held is a WRITE).
- 04:54:35.112Z — `wake_attempt_receipt` `wake-attempt-01a046b83f6871e4af4326b50f20c34a`,
  `outcome=woke`, **`acting_session_id=01a046b7-656d-76d2-bee2-44a35f899fd7` — an ephemeral spawned
  thread**, not build lane's seat (`01a04585-44b8-7751-979e-c8855b236c43`, the actor on all five
  prior wakes).
- 04:54:44Z / 04:54:49Z — `delivery_receipt` and `ack_receipt` land. **The ledger now says
  delivered-and-acked. The session that acked no longer exists.**
- build lane's last outbound envelope: 04:36:55Z. No branch moved, no ACK envelope for the E2
  dispatch. The seat sat idle **9.5 hours** on the critical path while the board said the row was
  dispatched. Redelivered by the architect at 14:24:52Z as `msg-01a048c25c2a7651b3a5f5a964a4201d`.
- The theft occurred **inside the daemon's acceptance epoch** (final activation 1787890328 ≈ 04:12Z,
  acceptance complete 05:02Z). Nothing in the acceptance run could have flagged it: the receipts are
  shape-valid. **A RECEIPT CHAIN CAN BE INTERNALLY PERFECT AND STILL DESCRIBE A DELIVERY TO NOBODY.**

Claude is NOT exposed on this surface: no Floati waiter on Claude Stop/SubagentStop (Claude seats are
SessionStart boot-drain, per the wake posture matrix). Grok rides HTTP push. Codex `SubagentStop` does
not carry the waiter — the exposure is peer threads, which fire **Stop** proper. Do not "fix" anything
by adding the waiter to SubagentStop; the defect is polarity, not coverage.

## The two defects

**D-A — WAKE THEFT / DELIVERY TO NOBODY.** Participation is keyed on the workspace; the seat is
whoever stops there. An ephemeral thread can win the evaluate race, take the wake, ack, and vanish.
Delivery doctrine already names this shape from the other side: **EXHAUSTED IS NOT DELIVERED**. Its
sibling now has a witness: **ACKED BY A VANISHED SESSION IS NOT DELIVERED.**

**D-B — SHARED-STATE POLLUTION.** Every co-resident thread's invocation writes the node-level breaker
(20 hits / 60 s) — a burst of spawned threads can trip it and silence the REAL waiter for the window.
And `wake_attempt_receipt.acting_session_id` from ephemeral threads pollutes exactly the evidence the
shakedown and binding campaign read. (Both lane breakers took hits today at 14:08Z / 14:17Z.)

## The ruling

1. **THE SEAT IS A SESSION, NOT A DIRECTORY.** One workspace binding pins ONE armed acting session.
   Am.6 already ruled this between sessions of one seat: the lane lock is the arbiter. The waiter
   inherits that law.
2. **Participation polarity INVERTS: opt-in per session, never opt-out.** The current
   `state/codex-wait/disabled/<digest>` marker asks every ephemeral thread to excuse itself — an
   unbounded set cannot opt out. **THE DECIDER MAY NOT FAIL OPEN**: a Stop invocation whose
   session_id is not the armed session returns 0 silently and **writes nothing** — no breaker hit,
   no hold, no receipt.
3. **An ack must name its actor.** `ack_receipt` carries no `acting_session_id` today (the theft's
   ack reads back None). Attribution is what would have made this finding derivable from the ledger
   instead of from the owner's eyes.
4. **Arming is an explicit act by the seat** (boot-time or `floati`-CLI arm naming the session);
   takeover = explicit re-arm replacing the lease. Migration must not strand live seats: an existing
   armed binding pins its session on its next organic wake. Exact seam is build lane's to design
   within this contract, RED-first at a frozen tree, committed AND banked (L3).

## Repair row (build lane, RED-first, before the binding campaign)

- RED: a test constructing two sessions in one bound workspace where the non-armed session's Stop
  invocation currently steals the wake — must go GREEN only when the theft becomes a silent
  non-participation (assert: no breaker write, no hold receipt, no wake attempt).
- RED: breaker isolation — a non-armed session's invocations leave `breaker.json` byte-identical.
- Ack attribution lands as a schema addition with its own focused bank.
- The installed-bundle waiter (`~/.codex/floati-wake/<sha>/`) redeploys with the fix — the bundle is
  immutable by design, so the repair ships as a NEW bundle sha + hook rewrite, same as the daemon
  landing did this morning.

## Evidence-confound note (grok, conductor)

Any wake-family cell or shakedown finding that reads `receipts/wakes/*.jsonl` must treat
`acting_session_id` values outside the known seat roster as ephemeral-thread noise, not seat
behavior. One such row exists today (the 04:54:35Z build lane wake). The sealed schedule is
unaffected; the campaign, when it runs, runs against the REPAIRED waiter.

## CORRECTION (the architect, ~15:00Z, same day) — the seat was never idle

This ruling said "the seat sat idle 9.5 hours on the critical path." **That was wrong, and it is the
seat-liveness error I keep a memory rule about: a working seat read as dead.** At 14:4xZ build lane
delivered the COMPLETE purge seven-finding repair (`WS-B-PURGE-TRASH-ONLY.md`, sha `22ef4de9…`) —
it had been building the whole time. What the theft actually cost was a **PRIORITY INVERSION**: the
stolen message INSERTED E2 ahead of purge ("NEXT ROW INSERTED BEFORE PURGE"), so the seat, never
seeing the insert, worked the old queue order. E2 — the row grok's last gauntlet family waits on —
slipped behind a full purge repair.

The finding is SMALLER in drama and LARGER in subtlety than filed: wake theft does not only strand
work, **it silently reverts reprioritizations.** A seat that misses a dispatch keeps a stale queue
and every observable sign of health — it pushes, it delivers, it envelopes. Nothing on the bus
distinguishes "working the right row" from "working the row I would have demoted." The defect
classification, remedy, and priority are unchanged; the cost model in the paragraph above is the
corrected one, and the D-A defect gains this sentence: **A STOLEN DISPATCH THAT CARRIES A
REPRIORITIZATION REVERTS IT INVISIBLY.**
