# DOCTRINE — **DELIVERY, IDENTITY, AND WAKE.** Binding on all Floati feature work.

**the architect, 2026-08-23.** Owner directive: *"make sure we internalize it for general floati feature
development."* Owner overrules explicitly; silence = consent.

**This is not a bug report promoted to a doctrine.** Floati's product IS delivery between agents. Every defect
below was found in our own bus in a single day, and **each one is a shape this product will keep growing**, so
they are written as laws for the next feature rather than as a fix for the last one.

---

## THE FOUR DEFECTS, AND WHAT EACH ONE IS AN INSTANCE OF

| found in our bus | the general shape |
|---|---|
| Attempt-cap exhaustion called `markDelivered` and **deleted** the pending envelope | **A control surface reported an outcome** |
| `markDelivered` ran **before** `await prompt`, and the catch released claims only | **A commit ran before the thing it commits to** |
| A worktree resolved `node = null`, and the null **flowed into routing** | **A refusal was used as a value** |
| **147 envelopes** addressed to a node that was never once armed | **An address nobody validated** |

---

## D — DELIVERY

**D1. BACKPRESSURE IS NOT AN OUTCOME.** Stopping is a decision about *us*; delivery is a fact about *them*.
A retry cap, a rate limit, a circuit breaker, a queue depth, a timeout — **none of them may write a terminal
state.** Marking something delivered because we stopped trying is a success shape for a thing that did not
succeed, which is `the decider may not fail open` wearing an operations costume.

**D2. COMMIT AFTER, NEVER BEFORE.** No state that means *"this arrived"* is written until the operation that
makes it arrive has **resolved**. On failure, unwind **everything** — tombstone and claim, not just the lock.
**A partial unwind is worse than none**, because it looks like a retry path and is not one. *E1 was found by
build lane MEASURING the ordering rather than reading it, and without it the D1 fix is dead code for the common
case.*

**D3. THE RECORD IS NOT THE RETRY POLICY.** Retrying may stop; **the record may not be deleted.** Deletion is
the specific act that turns a recoverable stall into an unrecoverable loss.

**D4. STOPPED MUST BE VISIBLE.** Anything that stops being retried carries **`attempts` and a stop timestamp**
and appears in a backlog view. **An envelope that stopped and is invisible is a defect; one that says so is a
queue.** D1–D3 without D4 trade a silent loss for a silent backlog — the same defect in a new costume.

**D5. WHAT RE-ARMS IT MUST BE NAMED.** A stopped item that nothing can restart is a graveyard. Name the event
that revives it — identity resolving, a new arrival, an operator act — **or say in the doc that nothing does.**

---

## I — IDENTITY

**I1. AN UNRESOLVED IDENTITY IS A REFUSAL WITH A NAMED CAUSE, NEVER A VALUE.** `null`, `""`, `"unknown"` and a
default id are all the same bug. **A null that reaches a routing decision is the decider failing open one layer
down.**

**I2. MATCH ON IDENTITY, NOT ON A STRING THAT USUALLY CORRELATES WITH IT.** Our fallback matched **exact
directory paths** while **most of this fleet's seats live in git worktrees** — an enumeration that excluded the
majority case. Key on the durable identity (`git-common-dir`, an inode, a registered id), never on a rendering
of it.

**I3. AN ADDRESS NOBODY CAN RECEIVE ON IS A DEFECT AT SEND TIME.** 147 envelopes were accepted for a node that
had never been armed. **The bus took every one and reported `status: ok`.** Sending to an unreachable address
should refuse, or at minimum warn — **acceptance is not delivery, and a send API that cannot tell them apart
teaches its callers to believe the wrong thing.**

---

## W — WAKE

**W1. PULL-ONLY IS A LAWFUL DESIGN. UNDECLARED PULL-ONLY IS NOT.** If nothing wakes a seat, **say so where the
seat's operator will read it**, and name the drain discipline as the mechanism.

**W2. THE DRAIN IS THE WAKE.** On a pull-only bus the seat's own drain is the delivery mechanism, and
infrastructure is redundancy for humans, not a substitute. **Drain at every turn start and before every send.**

**W3. WAKE COVERAGE IS PER-FAMILY AND MUST BE STATED.** Ours splits cleanly: every OpenCode seat is armed,
every Codex-family seat is deaf, because the watcher is an OpenCode plugin. **That was true for weeks and
appeared in no document.** Any feature that reaches agents states which families it reaches **and which it does
not.**

---

## THE VERIFICATION LAW THAT COVERS ALL OF IT

**V1. A SELF-ADDRESSED PROBE IS THE ONLY PROOF OF A DELIVERY PATH.** necro proved its repair by **sending itself
an envelope and watching it arrive** — `identity_resolved late_marker`, then the prompt, then the drain. Config
that looks right proves nothing; a `status: ok` proves nothing. **Send yourself mail and watch it land.**

**V2. UNTIL A PATH IS PROBED, ITS SUCCESS RESPONSE IS NOT EVIDENCE.** This binds the architect first: **I confirm
every dispatch by seat ACK or by branch activity, and I say which.**

---

## HOW THIS APPLIES TO NEW FEATURES

Any Floati feature that **hands work to something else** — task dispatch, worker pools, callbacks, the decision
register, HITL approvals, the sequencer's admission path — **inherits D1–D5, I1–I3, W1–W3 and V1–V2.** In review,
three questions:

1. **What writes a terminal state, and has the thing it describes actually happened yet?**
2. **What happens to this record when we give up — and can anyone see that we did?**
3. **What identity is this routed on, and what does the code do when that identity does not resolve?**

**A feature that cannot answer all three is not ready**, however green its tests are.

— Ruled by the architect. Owner overrules explicitly; silence = consent.
