<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/floati-icon.svg#gh-dark-mode-only">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/floati-icon.svg#gh-light-mode-only">
    <img src="docs/assets/floati-icon.svg" alt="THE BUOY" width="180">
  </picture>
</p>

<h1 align="center">Floati</h1>
<p align="center"><strong>The fleet operating system for local coding agents.</strong></p>
<p align="center">Any harness, any mix — one bus, one board, one set of receipts.</p>

<p align="center">
  <img alt="license AGPL-3.0" src="https://img.shields.io/badge/license-AGPL--3.0-E8622C">
  <img alt="platform macOS today" src="https://img.shields.io/badge/platform-macOS%20today-3E4A56">
  <img alt="telemetry zero" src="https://img.shields.io/badge/telemetry-zero-3E4A56">
</p>

You're already running a fleet. An agent in Codex, two in Claude,
one in OpenCode, something experimental in Cursor — each in its own
terminal, each with its own dialect, none of them aware the others
exist. You are the bus, the scheduler, and the guy who checks
whether anything died.

Floati takes those jobs. Register your agents as nodes — whatever
harness they run in — and they share one bus: dispatch work, message
each other with full provenance, wake when mail lands, and show up
on one board. Cross-harness fleets are the whole point; a
Codex worker, a Claude reviewer, and an OpenCode scout in one
orchestrated plan is a Tuesday.

<p align="center">
  <img src="docs/demo/hero-three-fault-replay.gif" alt="A three-fault replay" width="1400">
</p>

A worker killed, a sequencer killed, a reboot — and every event
reconstructed from receipts, in order, on demand. Nothing here is
animated by the demo; it is played back from the fleet's own
records.

<sub>Every moving image in this README is a real capture of a real fleet.</sub>

## What it runs with

Floati is worth installing for a single harness: a durable work log,
receipts, and a board for your own sessions. Cross-harness fleets
are the superpower, not the entry fee.

<!-- capability-matrix:begin — GENERATED from docs/capability-matrix.v0.json by
     scripts/capability-matrix-render.py; edit the dataset, rerun the script.
     Every cell links the receipt that earned it; no cell says more than its receipt. -->
Every harness on this bus shares an append-only ledger, replay, doctor, receipts, and typed refusals — those do not vary by surface.

| harness / surface | bus | work | wake | auto-turnover | native compact verb | boot | managed-send |
|---|---|---|---|---|---|---|---|
| codex / cli | [live](docs/evidence/conformance/C1-codex-conformance-live.md) | [adapter](docs/evidence/conformance/C1-codex-conformance-live.md) | [daemon](docs/evidence/gauntlet/H-wake-posture-matrix.md) | [—](docs/evidence/gauntlet/T1-depth2.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [yes](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [yes](docs/evidence/gate-wsb-b5-2026-08-27.md) |
| codex / desktop | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [daemon](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| claude / cli | [live](docs/evidence/conformance/C2-claude-conformance-live.md) | [adapter](docs/evidence/conformance/C2-claude-conformance-live.md) | [daemon](docs/evidence/gauntlet/H-claude-wake-remeasure.md) | [—](docs/evidence/gauntlet/T1-depth2.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| claude / desktop-chat | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [n/a](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| claude / ide-extension | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [daemon](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| opencode / cli | [live](docs/evidence/conformance/C3-opencode-conformance-live.md) | [adapter](docs/evidence/conformance/C3-opencode-conformance-live.md) | [event-driven](docs/evidence/gauntlet/H-wake-posture-matrix.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| opencode / desktop | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [event-driven](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| cursor / cli | [live](docs/evidence/conformance/C4-cursor-conformance-live.md) | [adapter](docs/evidence/conformance/C4-cursor-conformance-live.md) | [daemon](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| cursor / desktop | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [daemon](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-depth2.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| cline / cli | [live](docs/evidence/conformance/C5-cline-conformance-live.md) | [adapter](docs/evidence/conformance/C5-cline-conformance-live.md) | [daemon](docs/evidence/gauntlet/H-wake-posture-matrix.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| grok / cli | [live](docs/evidence/conformance/C6-grok-build-conformance.md) | [adapter](docs/evidence/conformance/C6-grok-build-conformance.md) | [daemon](docs/evidence/gauntlet/H-wake-posture-matrix.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| grok / desktop | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [n/a](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| pi / cli | [live](docs/evidence/conformance/C7-pi-conformance-live.md) | [adapter](docs/evidence/conformance/C7-pi-conformance-live.md) | [event-driven](docs/evidence/gauntlet/H-wake-posture-matrix.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| herdr / cli | [live](docs/evidence/conformance/C8-herdr-conformance-live.md) | [—](docs/evidence/wave2-r3-herdr-loopback-client-2026-08-27.md) | [n/a](docs/evidence/gauntlet/H-wake-posture-matrix.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| t3 / cli | [CLI](docs/evidence/conformance/C9-t3-compatibility-live.md) | [—](docs/evidence/conformance/C9-t3-compatibility-live.md) | [event-driven](docs/evidence/gauntlet/H-wake-posture-matrix.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| t3 / desktop | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [event-driven](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| devin / cli | [CLI](docs/evidence/conformance/C11-devin-conformance-live.md) | [—](docs/evidence/conformance/C11-devin-conformance-live.md) | [event-driven](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| antigravity / cli | [CLI](docs/evidence/conformance/C12-antigravity-conformance-live.md) | [—](docs/evidence/conformance/C12-antigravity-conformance-live.md) | [event-driven](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |
| antigravity / desktop | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [—](docs/evidence/conformance/C0-DELTA-surface-axis.md) | [daemon](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/gauntlet/T1-tide-survey.md) | [—](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) | [—](docs/evidence/conformance/C0-managed-send-surface.md) |

Provider notes (wiring matrix; not grid columns):

- codex / cli — [already-wired-not-reprobed](docs/evidence/HARNESS-WIRING-MATRIX.md): Wiring matrix: already wired and out of scope; not re-probed this row.
- claude / cli — [already-wired-not-reprobed](docs/evidence/HARNESS-WIRING-MATRIX.md): Wiring matrix: already wired and out of scope; not re-probed this row.
- opencode / cli — [already-wired-not-reprobed](docs/evidence/HARNESS-WIRING-MATRIX.md): Wiring matrix: already wired and out of scope; not re-probed this row.
- cursor / cli — [already-wired-not-reprobed](docs/evidence/HARNESS-WIRING-MATRIX.md): Wiring matrix: already wired and out of scope; not re-probed this row.
- cline / cli — [wired](docs/evidence/HARNESS-WIRING-MATRIX.md): Addendum: ChatGPT sign-in (openai-codex / gpt-5.6-luna). Original OpenRouter literal-key GAP row stays as the photograph.
- pi / cli — [wired](docs/evidence/HARNESS-WIRING-MATRIX.md): OpenRouter via models.json command-lookup; probe exit 0.
- grok / cli — [wired](docs/evidence/HARNESS-WIRING-MATRIX.md): OpenRouter via env_key OPENROUTER_API_KEY; provider pin is a documented surface limitation. C6 receipt is grok-build via this grok override.
- t3 / cli — [wired](docs/evidence/HARNESS-WIRING-MATRIX.md): Composed through Grok; no native OpenRouter driver.
- herdr / cli — [—](docs/evidence/HARNESS-WIRING-MATRIX.md): Not an LLM-turn harness; wiring out of scope.
- devin / cli — [—](docs/evidence/conformance/C11-devin-conformance-live.md): Wiring N/A (vendor account, not OpenRouter).
- antigravity / cli — [—](docs/evidence/conformance/C12-antigravity-conformance-live.md): Wiring N/A (Google account, not OpenRouter).
<!-- capability-matrix:end -->

Platforms: macOS today. Anything POSIX is intended; a platform joins
this list the same way a harness does — with receipts.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/floati-architecture-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/floati-architecture-light.svg">
    <img src="docs/assets/floati-architecture-light.svg" alt="Floati architecture: harnesses feed one append-only root, and every operator surface is a projection of its ledgers" width="1400">
  </picture>
</p>

One picture: harnesses at the edge, one append-only ledger in the
middle, projections derived from it — never a second source of
truth.

## What you can do with it

### Talk to your fleet

```bash
floati send --root /absolute/fleet --from architect --to lane-codex \
  --repo myapp --sha <40-hex> --doc docs/briefs/row-1.md --note "Row 1 is yours."
```

Every message is a typed envelope with provenance — sender,
recipient, tenant, repo, SHA — validated on the way in, refused when
malformed. Delivery, acknowledgment, and consumption are separate
records, so "did they get it?" has an actual answer.

### Wake the lane you just dispatched

Stop-hook waiters for your harnesses and an optional per-fleet
daemon mean a dispatched lane wakes in seconds — not whenever
someone remembers to check a terminal. Wake is opt-in per fleet,
armed by a consent receipt, off by default: nothing wakes without
your recorded say-so. And a floati waiter is a polite neighbor —
instant silent exit for any workspace that isn't its own, other
buses' hooks and mail never touched.

### Orchestrate across harnesses

```bash
floati orchestrate --root /absolute/fleet --plan /absolute/plan.json --adapter codex
```

Plans fan work across registered workers with dependency edges —
work stays `BLOCKED` until prerequisites complete. Workers can live
in different harnesses; the adapter layer speaks each one's dialect
so the plan doesn't have to. Degradation is typed, and drain refuses
to declare victory until work state, terminal receipts, controller
exits, and descendant cleanup audits all agree.

### See the whole harbor

<p align="center">
  <img src="docs/evidence/captures/post-campaign-20260828/harbor-board-dark.svg" alt="The Harbor Board: three nodes with separate liveness, authority, and lock lamps" width="1400">
</p>

The Harbor Board: liveness, authority, and lock state as three
separate lamps — three different questions, never blended into one
green dot. Keyboard-first, redraws only on state change.

<p align="center">
  <img src="docs/evidence/captures/post-campaign-20260828/harbor-chart-multibus-dark.svg" alt="Multi-bus harbor chart: two declared roots, their architects, and the downstream edge" width="1400">
</p>

Running more than one fleet on this machine? Declare your roots
(floati never scans your disk) and `floati chart` draws the harbor:
buses, nodes, architect seats, what's downstream, last activity.
`floati survey` goes further — it reports agent buses on this
filesystem that floati did *not* install, including whether a
foreign waiter is bound to one of your workspaces. Read-only, on
your request.

### Know which lane went deaf — and which step died

<p align="center">
  <img src="docs/evidence/captures/post-campaign-20260828/doctor-delivery-health-dark.svg" alt="Doctor report: one node RED with a 16-minute-old undelivered envelope; loopback probe PASS and DEAF; state DEGRADED" width="1400">
</p>

"The lane went quiet" is not a diagnosis. The doctor states per-node
undelivered counts, oldest-message age, and last drain even when
everything is green, and `doctor --probe` sends a self-addressed
envelope through a node's own delivery path to prove it can still
hear — without touching anyone else's mail.

### Onboard and tear down nodes like it's nothing

<p align="center">
  <img src="docs/evidence/captures/post-campaign-20260828/onboard-wizard-dark.gif" alt="Node onboarding: the records preview shown before write, then the commit receipt" width="1400">
</p>

`floati node add` walks you through a new node: identity, harness,
permanent or temporary. Temporary nodes boot with one command and
tear down with one. Switching a node between providers or models is
a recorded reassignment, not a re-onboarding. Every
wizard step prints the exact records it will write before writing
them — the wizard fronts the same verbs you could type, never a
second path. Nodes keep their working folders nested under the
fleet root, so ten agents don't mean ten directories strewn across
your home.

### Give every node a role

A fleet isn't just processes — it's an architect, reviewers,
builders, scouts, each needing the right boot instructions and a
clean hand-off ritual when a session ends. Floati generates and
explains boot and wind-down commands per node from its role, runs
them where the harness allows, and keeps them current as the fleet
changes — so standing up your whole fleet stops being an act of
memory.

### Treat context like the resource it is

Agent sessions degrade as their context fills, and every harness
handles it differently. Floati tracks what each harness actually
exposes, hands a lane a turnover ritual before it drowns — wind
down, port the working state, boot the successor — and never
invents a pressure number it can't measure.

### Replay any run

<p align="center">
  <img src="docs/evidence/captures/post-campaign-20260828/flight-recorder-replay-dark.gif" alt="Flight recorder replaying a completed two-work orchestration in order" width="1400">
</p>

The flight recorder replays a finished run as an ordered timeline:
claims, worker turns, degradations, denials, completions. Playback
speed changes the waiting, never the order.

### Bring your agent — or be the human

Humans and agents are both first-class operators here. Point your
agent at this repo: `AGENTS.md` is its manual, every verb
self-describes in JSON, every refusal names its remedy, and every
action leaves a receipt your agent can verify — it never has to
guess whether its own message arrived. The keyboard-first flows and
the declarative `--json` flows are the same engine wearing two
idioms, so your fleet reads identically whether you run it or your
agent does.

## Why it doesn't fall over

Every harness already writes a session log. Those are per-harness,
mutable, uncorrelated, and they can't answer a fleet question. Under
floati, everything above runs on one append-only, typed ledger —
the board, the chart, the doctor, the replay are all projections of
it, and if a projection ever disagrees with the receipts, the
receipts win. Kill a worker, kill the sequencer, reboot the machine: nothing is
lost and nothing lies — the ledger survives every fault, the whole
run replays on demand, and floati refuses to continue past what it
cannot prove, telling you exactly why in a typed exit code.
Ambiguous identity, expired authority, malformed envelopes — same
answer: refusal with a reason, never a guess.

No telemetry. Nothing leaves this machine — measured, not promised:
the only network-class socket in the product dials loopback on your
own machine, behind explicit consent, and cannot listen.

The full promise — and precisely what floati refuses to guess — is
written down: **[Truth Guarantees](docs/TRUTH-GUARANTEES.md)**. If a
promise on that page can't be demonstrated by a test or a receipt,
it doesn't belong on it.

## Start alone

Every durable command names an explicit absolute root. There is no
default root, no home scan, and nothing that wakes without you.
Point it somewhere; that directory is the entire blast radius.

```bash
floati init --root /absolute/my-sessions --solo me --harness Codex
floati work add --root /absolute/my-sessions --title "Record this session"
floati board --root /absolute/my-sessions
```

## Grow the fleet

```bash
floati node add --root /absolute/fleet          # the wizard
floati orchestrate --root /absolute/fleet --plan /absolute/plan.json --adapter codex
floati log --root /absolute/fleet --replay --speed 4
```

The installed child harness owns its own provider traffic and
credentials; floati opens no network connection and reads no
credential.

## Leave cleanly

Every door in floati has an exit beside it, at equal polish: pause
the wake, retire the node, drain the run, uninstall the tool —
each one obvious, each one receipted, none of them touching your
records. A product that's sure of its worth doesn't make leaving
hard.

```bash
floati uninstall --destination /absolute/install --dry-run
```

Manifest-exact removal with receipts. Files floati did not install
are never touched, and your ledgers are never part of an uninstall —
the record outlives the tool, which is rather the point of a record.

## Compose with it

`floati status --root /absolute/fleet --json` is the stable
version-zero machine contract; `floati graph --json` is its topology
twin. `docs/CONFLUENCE-v0.md` and its JSON Schemas define the
read-only seam for downstream consumers — a GUI, a dashboard, or
anything that wants to draw your harbor in glass instead of ASCII.

## Verify

```bash
python3 -m unittest discover
python3 -m floati.selftest
python3 -m floati.conformance --live-root-smoke
```

Visible CLI language is generated into `docs/COPY-LEDGER.md`. Hosted
CI, deployment, and release are separate gates; a local green suite
doesn't manufacture them.

Product code is AGPL-3.0; the interchange schemas and bundle
specifications are Apache-2.0.
