# DRAFT - Opt-in wake daemon design

Status: **DRAFT - design only; no daemon is implemented or activated by this row**

Date: 2026-08-28

Owner frame: weekend program WS-A, North Star ruling 5, and the exact-session
wake controller at `49bf646add2145620ed972c8dc216954e1086d7e`.

## DRAFT - Outcome and non-goals

The daemon is an optional local durability layer for harnesses whose native
Stop/hook or extension process does not remain healthy for long sessions. It
observes one explicitly declared Floati root, identifies only explicitly bound
sessions, and invokes that harness's already governed wake adapter. It does not
replace the bus, infer task state, discover roots, open a network listener,
edit hook registration, keep a model turn alive, or claim delivery before a
harness wake receipt exists.

The default is **off**. Absence of a daemon is not a failure and renders no
half-state. Activation requires a current root-bound consent receipt and an
explicit local start. Installation, consent, start, pause, resume, and removal
remain separate operations.

## DRAFT - Authority and identity

One daemon instance is bound to exactly:

- one canonical Floati direct-home root;
- one tenant id derived from that root;
- one active registry node;
- one declared harness adapter; and
- one local daemon instance id.

There is no default root, ambient bus fallback, home scan, repository scan,
foreign-bus scan, or node guess. The workspace map and active registry used by
the landed Codex waiter remain the identity source for Codex. Other harness
adapters must publish an equally explicit binding before they can participate.
Unknown, retired, cross-tenant, symlinked, malformed, or stale bindings refuse.

Consent is append-only and root-bound. The consent record names the node,
harness, adapter version/digest, minimum and maximum polling interval, maximum
backoff, and activation epoch. Revocation or supersession closes that epoch.
Consent to the existing Stop waiter does not silently authorize the daemon;
the daemon has its own explicit consent because it changes process lifetime.

## DRAFT - Process and lifecycle

The proposed public lifecycle is intentionally separate from exact-session
pause control:

```text
DRAFT - floati wake daemon consent --root ROOT --as NODE --harness HARNESS
DRAFT - floati wake daemon start --root ROOT --as NODE --harness HARNESS
DRAFT - floati wake daemon status --root ROOT --as NODE --harness HARNESS
DRAFT - floati wake daemon stop --root ROOT --as NODE --harness HARNESS
DRAFT - floati wake daemon revoke --root ROOT --as NODE --harness HARNESS
```

These spellings are design placeholders, not shipped commands. The daemon is
started by an explicit host-local supervisor selected by the installer. It has
no TCP, UDP, HTTP, WebSocket, Unix-domain, or MCP listener. Control remains
bounded CLI-to-ledger state plus the host supervisor's ordinary process
lifecycle. A single-owner lock prevents two instances for the same
root/node/harness coordinate. An abandoned owner becomes `unknown` until a
takeover procedure proves process absence; it never fails open into a second
poller.

Start validates consent and registry identity before creating runtime state.
Stop records intent, requests graceful exit, waits a bounded interval, and
reports whether process absence was proven. Revoke prevents the next start but
does not claim a live process stopped. Removal preserves consent, lifecycle,
pause, wake, delivery, exhaustion, and backpressure receipts.

Sessions that predate adapter installation or trust-gate approval may not be
reachable. Status must say `DRAFT - relaunch required: session predates adapter
installation or trust approval` when that fact is established. It must not
repeat the retracted claim that installing a waiter retrofits an already
running session.

## DRAFT - Event loop and delivery truth

The daemon loop is a pull-only state machine:

1. validate current consent, active registry identity, owner lock, and adapter
   digest;
2. read only the explicitly declared node inbox/wake-hold projection;
3. enumerate only sessions already bound to this root/node/harness by the
   adapter's ruled binding source;
4. exclude sessions with an exact pause marker;
5. coalesce shareable message work into one bounded adapter invocation;
6. ask the adapter to wake one exact session;
7. append a wake attempt only after the harness confirms the prompt/action;
8. leave messages pending until the bus's ordinary delivery/ack flow advances;
9. back off on silence, refusal, quota, exhaustion, or unknown adapter state;
   and
10. periodically revalidate consent, registry identity, adapter digest, and
    owner lock.

A poll is not a delivery. A process being alive is not a wake. A prompt request
is not a successful prompt. A wake receipt is not an acknowledgment. The
existing wake-hold and wake-attempt receipt distinction remains authoritative.
If the adapter outcome is unknown, the daemon records unknown/refused evidence
and does not synthesize `woke`.

## DRAFT - Exact-session pause integration

The landed `floati wake pause|resume|status` controller is the single pause
source. Before every adapter invocation the daemon checks the node-scoped
session marker at `state/wake-control/<node>/<session-sha256>.json`.

- A valid marker yields recorded `paused`, intentional silence, and no adapter
  call.
- A malformed or unreadable marker fails closed as `pause_unknown`; it never
  becomes active by default.
- Resume affects only the exact predecessor-bound marker and does not edit
  consent, daemon ownership, or hook registration.
- Status projects pause independently from daemon liveness, inbox depth, and
  harness trust. `paused`, `deaf`, `inactive`, `unbound`, and `unknown` remain
  distinct lamps.

The daemon never creates a global marker and accepts no wildcard session
selector. Session ids are adapter testimony, bounded before hashing, and never
derived from a window title, process name, repository branch, or cached role
label.

## DRAFT - Adapter contract

Every adapter implements the same closed local interface:

```text
DRAFT - enumerate_bound_sessions(root, node) -> exact session bindings
DRAFT - observe_session(binding) -> reachable | unreachable | unknown
DRAFT - request_wake(binding, reason, deadline) -> woke | refused | unknown
DRAFT - explain_entry(binding) -> DRAFT operator idiom
```

The interface accepts no arbitrary executable, prompt, credential, endpoint,
or environment map from the daemon caller. Adapter executable and argument
shape are installed, digest-bound product data.

- **Codex:** uses the explicit workspace map, active Floati registry, installed
  Stop/waiter trust boundary, and exact Codex session id. A session started
  before install requires relaunch. The entry idiom includes the landed
  `floati wake pause|resume|status` commands.
- **Cursor:** first longevity consumer. Its binding must come from an explicit
  Cursor adapter record; the stale registry role label is never treated as
  measurement. No UI scraping or process-name guess is allowed.
- **Claude, OpenCode, Cline, grok-build, Pi, herdr, and t3 participation:** each
  remains unsupported by the daemon until its own adapter publishes an exact
  binding source, wake action, refusal vocabulary, and three-cycle receipt.
  Unsupported means absent/refused, never generic fallback.

## DRAFT - Backpressure and budgets

The loop has three independent bounds:

- a minimum idle polling interval;
- exponential backoff capped by the consent record; and
- a per-session and per-node wake budget inside a rolling window.

Fresh envelopes may reset idle backoff but cannot bypass a paused session,
adapter refusal, consent closure, or wake budget. Multiple envelopes for one
session coalesce into one wake reason when their delivery semantics permit it.
Repeated unknown/refused outcomes trip a circuit breaker and require a later
healthy observation or explicit operator reset. Exhaustion records visible
backpressure; it does not acknowledge mail or mark the node deaf.

Night-watch may direct a pause through the ordinary exact-session controller,
but it never edits daemon state itself. Window scheduling may propose a next
poll time only when a measured window is present; the daemon retains its own
hard maximum backoff and consent checks.

## DRAFT - Longevity acceptance

Cursor's measured weak flank is approximately 28 minutes. Acceptance therefore
requires a run longer than the defect window and at least three complete
deadline cycles. The minimum evidence run is:

1. bind one real Cursor session (grok is the first live subject per owner
   ruling) and record the exact adapter identity;
2. run at least three configured deadline cycles spanning more than 35 minutes;
3. deliver one unique envelope before each cycle boundary;
4. require one real session wake receipt per envelope;
5. pause the session for one cycle and prove recorded intentional silence with
   no wake receipt;
6. resume exactly that session and prove the next envelope wakes it;
7. restart the daemon once and prove ledger/marker continuity without duplicate
   delivery or acknowledgment; and
8. retain timestamps, daemon instance/epoch, session digest, message ids,
   adapter digest, outcome, and backoff state in the evidence artifact.

Three rapid unit-test iterations do not satisfy longevity. A labeled exercise,
manual callback, or synthetic `outcome: woke` row is not organic evidence.

## DRAFT - Failure and recovery table

| Condition | Required state | Forbidden claim |
| --- | --- | --- |
| consent absent/revoked | inactive | running, armed |
| registry node retired | refused | eligible |
| owner process uncertain | unknown | stopped, safe takeover |
| session pause marker valid | paused | deaf, absent |
| pause marker malformed | pause_unknown | active |
| harness trust missing | relaunch_required or refused | reachable |
| adapter unavailable | adapter_unknown | woke |
| wake deadline expires | backpressure/exhausted | delivered |
| receipt append fails after prompt | wake_evidence_unknown | woke-with-receipt |
| foreign or undeclared root | refused without opening it | surveyed, healthy |

Recovery is additive: correct the binding, renew consent, relaunch the harness
when required, or close the unknown owner epoch after proving absence. No
recovery path deletes bus history, pause receipts, or state workspaces.

## DRAFT - Verification matrix before implementation may ship

1. RED: no consent means no process/runtime files.
2. RED: undeclared and foreign roots are never opened.
3. RED: two instances for one coordinate cannot both own the loop.
4. RED: global/wildcard pause cannot be expressed.
5. RED: a malformed pause marker fails closed before adapter invocation.
6. RED: adapter success without durable evidence is `unknown`, not `woke`.
7. GREEN: idle, fresh work, held work, exhaustion, and breaker transitions are
   deterministic under a fake clock.
8. GREEN: pause for one session leaves sibling sessions and nodes active.
9. GREEN: revocation, graceful stop, crash, restart, and ruled takeover retain
   receipt continuity.
10. GREEN: per-adapter conformance plus the Cursor longer-than-35-minute,
    three-cycle live run.
11. GREEN: full repository suite, exact manifest, copy ledger, source scrub,
    and no-listener static fence.

## DRAFT - Self-review decision

The design is **accepted for a later implementation plan**, not accepted as a
shipped capability. It satisfies the ratified boundaries: opt-in and off by
default; exact root/node/harness/session identity; marker-only pause; receipts
without delivery overclaim; Cursor longevity beyond the measured defect; no
listener, discovery, foreign-root access, hook mutation, global pause, or
generic adapter fallback.

Open implementation inputs remain bounded and explicit: the versioned daemon
consent/lifecycle schemas, the host supervisor choice per installer, and one
binding/wake contract per harness. Those inputs do not block this design row and
must not be guessed during implementation.
