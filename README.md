<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/floati-icon.svg#gh-dark-mode-only">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/floati-icon.svg#gh-light-mode-only">
    <img src="docs/assets/floati-icon.svg" alt="THE BUOY" width="180">
  </picture>
</p>

<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/floati-wordmark-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/floati-wordmark.svg">
    <img src="docs/assets/floati-wordmark.svg" alt="floati" width="320">
  </picture>
</h1>
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
exist. You are the bus, the scheduler, and the one who checks
whether anything died.

Floati takes those jobs. Register your agents as nodes — whatever
harness they run in — and they share one bus: dispatch work, message
each other with full provenance, wake when mail lands, and show up
on one board. Cross-harness fleets are the whole point; a Codex
worker, a Claude reviewer, and an OpenCode scout in one orchestrated
plan is a Tuesday.

<p align="center">
  <img src="docs/demo/hero-three-fault-replay.gif" alt="A three-fault replay" width="1400">
</p>

A worker killed, a sequencer killed, a reboot — and every event
reconstructed from receipts, in order, on demand. Nothing here is
animated by the demo; it is played back from the fleet's own
records.

<sub>Every moving image in this README was recorded from a real ledger. The capture directories under <code>docs/evidence/captures/</code> carry a per-file SHA-256 manifest; <code>docs/demo/</code> gets its manifest with the next recapture, and until then that directory's images are the one place this README asks you to take its word.</sub>

## What you can do with it

### Talk to your fleet

```bash
floati send --root /absolute/fleet --from architect --to builder-a \
  --repo myapp --sha <40-hex> --doc docs/briefs/row-1.md --note "Row 1 is yours."
```

Every message is a typed envelope with provenance — sender,
recipient, tenant, repo, SHA — validated on the way in, refused when
malformed. Delivery, acknowledgment, and consumption are separate
records, so "did they get it?" has an actual answer. `floati receipts
NODE` shows the three histories as distinct evidence, and a refusal
is a fourth record with its own typed code.

### Wake the node you just dispatched

Stop-hook waiters for your harnesses and an optional per-fleet
daemon mean a dispatched node wakes in seconds — not whenever
someone remembers to check a terminal. Wake is opt-in per fleet,
armed by a consent receipt (`floati wake arm`), off by default:
nothing wakes without your recorded say-so, and `floati wake status`
shows exactly what is armed. A floati waiter is a polite neighbor —
instant silent exit for any workspace that isn't its own, other
buses' hooks and mail never touched.

### Orchestrate across harnesses

```bash
floati orchestrate --root /absolute/fleet --plan /absolute/plan.json --adapter codex --deadline 120
```

Plans fan work across registered workers with dependency edges —
work stays `BLOCKED` until prerequisites complete. Workers can live
in different harnesses; the adapter layer speaks each one's dialect
so the plan doesn't have to. Degradation is typed, and drain refuses
to declare victory until work state, terminal receipts, controller
exits, and descendant cleanup audits all agree. Today the
`orchestrate` verb takes one adapter choice, `codex`; the fleet
underneath it is any mix.

Every side effect a worker takes is its own record: `floati effects`
lists them, `effect reconcile` checks them against what the harness
reports, and `effect compensate` records the undo. `floati threads`
and `thread attach` put an observer on a harness's own thread, so the
ledger sees what the harness saw.

### See the whole harbor

<p align="center">
  <img src="docs/evidence/captures/readme-b-20260904/harbor-board-dark.svg" alt="The Harbor Board: three nodes with separate liveness, authority, and lock lamps" width="1400">
</p>

The Harbor Board: liveness, authority, and lock state as three
separate lamps — three different questions, never blended into one
green dot. Keyboard-first, redraws only on state change. `floati
watch` streams the same deltas as text, and `floati supervise` holds
a run open and reports as it goes.

<p align="center">
  <img src="docs/evidence/captures/readme-b-20260904/harbor-chart-multibus-dark.svg" alt="Multi-bus harbor chart: two declared roots, their architects, and the downstream edge" width="1400">
</p>

Running more than one fleet on this machine? Declare your roots
(floati never scans your disk) and `floati chart` draws the harbor:
buses, nodes, architect seats, what's downstream, last activity.
`floati survey` goes further — it reports agent buses on this
filesystem that floati did *not* install, including whether a
foreign waiter is bound to one of your workspaces. Read-only, on
your request.

### Know which node went deaf — and which step died

<p align="center">
  <img src="docs/evidence/captures/readme-b-20260904/doctor-delivery-health-dark.svg" alt="Doctor report: one node RED with a 17-minute-old undelivered envelope; loopback probe PASS and DEAF; state DEGRADED" width="1400">
</p>

"The node went quiet" is not a diagnosis. The doctor states per-node
undelivered counts, oldest-message age, and last drain even when
everything is green. `doctor --probe` sends a self-addressed
envelope through each node's own delivery path and reports PASS or
DEAF per node, without touching anyone else's mail. Two things to
know before you read a DEAF: the probe's budget is per node, sixty
seconds by default, so set `--probe-budget` low on a big fleet; and
a node with no waiter armed is DEAF by definition — the probe reports
the fact, it does not know whether you meant it.

Liveness is a separate question from mail. A node reports about
itself, and only itself, with `floati presence report`; `presence
show` prints the last report, its TTL and its expiry — and expiry
means *no report since*, never *down*.

### Onboard and tear down nodes like it's nothing

<p align="center">
  <img src="docs/evidence/captures/readme-b-20260904/onboard-wizard-dark.gif" alt="Node onboarding: the records preview shown before write, then the commit receipt" width="1400">
</p>

`floati node add` walks you through a new node: identity, harness,
permanent or temporary. Temporary nodes boot with one command and
tear down with one. Switching a node between providers or models is
a recorded reassignment, not a re-onboarding. Every wizard step
prints the exact records it will write before writing them — the
wizard fronts the same verbs you could type, never a second path.
Nodes keep their working folders nested under the fleet root, so ten
agents don't mean ten directories strewn across your home.

### Give every node a role

A fleet isn't just processes — it's an architect, reviewers,
builders, scouts, each needing the right boot instructions and a
clean hand-off ritual when a session ends. Floati generates and
explains boot and wind-down commands per node from its role
(`floati node explain`), runs them where the harness allows, and
keeps them current as the fleet changes — so standing up your whole
fleet stops being an act of memory.

### Treat context like the resource it is

Agent sessions degrade as their context fills, and every harness
handles it differently. Floati tracks what each harness actually
exposes (`floati context policy`, `context reading record`), hands a
node a turnover ritual before it drowns — wind down, port the
working state, boot the successor — and never invents a pressure
number it can't measure. Provider usage comes from the harness's own
logs, on request: `floati quota collect`, then `quota show`.

### Take work in from GitHub

`floati intake scan` reads a repository's issues into candidate work,
`intake show` and `intake adopt` turn one into a work item with the
issue as its provenance, and `intake dispatch` hands it to a node.
`intake adopt --source github` reads one issue through the explicit `gh`
executable you name. That is a network call which may receive only ambient
`GH_TOKEN` or `GITHUB_TOKEN` — see the network section below.

### Replay any run

<p align="center">
  <img src="docs/evidence/captures/readme-b-20260904/flight-recorder-replay-dark.gif" alt="Flight recorder replaying a completed two-work orchestration in order" width="1400">
</p>

The flight recorder replays a finished run as an ordered timeline:
claims, worker turns, degradations, denials, completions. Playback
speed changes the waiting, never the order.

### Bring your agent — or be the human

Humans and agents are both first-class operators here. Point your
agent at this repo: `AGENTS.md` is its manual, every verb
self-describes in JSON (`floati describe --json`), every refusal
carries a typed code and a detail, and every action leaves a receipt
your agent can verify — it never has to guess whether its own message
arrived. `floati mcp serve` exposes the same verbs to an MCP client
as tools, with the node identity pinned at launch so the client
cannot speak as anyone else. The keyboard-first flows and the
declarative `--json` flows are the same engine wearing two idioms, so
your fleet reads identically whether you run it or your agent does.

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
floati node add --root /absolute/fleet --node builder-a --harness Codex --lifetime permanent
floati orchestrate --root /absolute/fleet --plan /absolute/plan.json --adapter codex --deadline 120
floati log --root /absolute/fleet --replay --speed 4
```

The installed child harness owns its own provider traffic and
credentials. A fleet is not the ceiling: one machine can run
several — different harness mixes, an architect seat in each, peer
architects exchanging artifacts but never authority, and no path
between fleets unless you build one:

<picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/floati-multifleet-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/floati-multifleet-light.svg">
    <img src="docs/assets/floati-multifleet-light.svg" alt="Two floati fleets and a solo seat on one machine: each fleet is a star of harnesses around its own append-only root with an architect seat; the architects exchange artifacts as peers; the fleets share nothing by default" width="1440">
</picture>

Every element in that picture is a shipping mechanism — roots,
ledgers, leases, star governance.

## Get it

Floati is one Python package with no dependencies. Clone it and let
it install itself: the installer deploys exactly the files the
manifest names and nothing else, into a directory you choose.

```bash
git clone https://github.com/Land-o-Clusters/floati.git /absolute/floati
python3 -m floati install --source /absolute/floati --destination /absolute/install
```

That installs whatever `main` is today. To install the tagged
release, check the tag out first and name it:

```bash
git -C /absolute/floati checkout v0.1.0
python3 -m floati install --source /absolute/floati --destination /absolute/install --ref v0.1.0
```

`/absolute/install/scripts/floati` is the command; add that `scripts`
directory to your `PATH` or call it by path.
`pyproject.toml` is the package-metadata authority: Python 3.9 or newer, zero dependencies.
CI exercises 3.9; macOS today.

Two things the installer does today that you should know before you
hit them. It refuses to install a checkout whose `HEAD` is not the
ref you named, and the refusal does not yet tell you to check the tag
out ([#6](https://github.com/Land-o-Clusters/floati/issues/6)). And
it refuses when any directory on your `PATH` cannot be read, without
naming which one ([#7](https://github.com/Land-o-Clusters/floati/issues/7)).
Both are open and both are ours.

Afterwards the doctor tells you whether what is on disk still matches
the manifest, file by file:

```bash
floati doctor --root /absolute/my-sessions --source /absolute/floati --destination /absolute/install
```

Expect it to be strict on a fresh install: it names every wake bridge
you have not armed yet and every path it could not read, and reports
the whole as degraded until you have. Some of those findings do not
yet carry a remedy ([#8](https://github.com/Land-o-Clusters/floati/issues/8)).

## What it runs with

Floati is worth installing for a single harness: a durable work log,
receipts, and a board for your own sessions. Cross-harness fleets are
the point, not the entry fee.

<!-- capability-matrix:begin — GENERATED from docs/capability-matrix.v0.json by
     scripts/capability-matrix-render.py; edit the dataset, rerun the script.
     Every cell links the receipt that earned it; no cell says more than its receipt. -->
Every harness on this bus shares an append-only ledger, replay, doctor, receipts, and typed refusals — those do not vary by surface.

Orchestrators (t3, herdr) run other harnesses inside themselves. Floati reads the orchestrator's own surface - its sessions and panes are the truth when agents run there. The harnesses underneath keep their own rows and their own receipts; supporting them is a different promise than supporting the orchestrator.

Surface rows are the reference machine's MEASURED installs (C0-DELTA photograph), not the product catalog. Two absences are deliberate, not oversights: Claude.app is desktop chat, not a Claude Code seat - classified out, the same cut that separates ChatGPT Classic from Codex; and no Codex IDE extension was installed at photograph time - that row lands when the MX-1 campaign photographs one, not before.

Version honesty: claude/cli declared current [2.1.251 (Claude Code) at 2026-09-03](docs/evidence/conformance/C2-claude-cli-version-2026-09-03.md); cells marked `version_stale: true` were measured at 2.1.231 (Claude Code) at 2026-08-27 and 2026-08-28 and keep those receipt-bound stamps.

**CLI surfaces**

| harness | bus | work | wake | notes |
|---|---|---|---|---|
| codex | [live](docs/evidence/conformance/C1-codex-conformance-live.md) | [adapter](docs/evidence/conformance/C1-codex-conformance-live.md) | [daemon](docs/evidence/gauntlet/MX1-codex-cli-wake.md) ● | deep integrations below |
| claude | [live](docs/evidence/conformance/C2-claude-conformance-live-2026-09-04.md) | [adapter](docs/evidence/conformance/C2-claude-conformance-live-2026-09-04.md) | [daemon](docs/evidence/conformance/H-claude-wake-remeasure-2026-09-04.md) ● | confirmed at 2.1.251 (re-measured 09-04) |
| opencode | [live](docs/evidence/conformance/C3-opencode-conformance-live.md) | [adapter](docs/evidence/conformance/C3-opencode-conformance-live.md) | [event-driven](docs/evidence/gauntlet/H-wake-posture-matrix.md) ● | 3-cycle live hold |
| cursor | [live](docs/evidence/conformance/C4-cursor-conformance-live.md) | [adapter](docs/evidence/conformance/C4-cursor-conformance-live.md) | [daemon](docs/evidence/gauntlet/MX1-cursor-cli-wake.md) ● |  |
| cline | [live](docs/evidence/conformance/C5-cline-conformance-live.md) | [adapter](docs/evidence/conformance/C5-cline-conformance-live.md) | [event-driven](docs/evidence/gauntlet/MX1-cline-cli-wake.md) ● |  |
| grok | [live](docs/evidence/conformance/C6-grok-build-conformance.md) | [adapter](docs/evidence/conformance/C6-grok-build-conformance.md) | [daemon](docs/evidence/gauntlet/MX1-grok-cli-wake.md) ● | via installed grok binary |
| pi | [live](docs/evidence/conformance/C7-pi-conformance-live.md) | [adapter](docs/evidence/conformance/C7-pi-conformance-live.md) | [event-driven](docs/evidence/gauntlet/MX1-pi-cli-wake.md) ● |  |
| zcode | [—](docs/evidence/gauntlet/ZC1-zcode-scoping-photograph.md) | [—](docs/evidence/gauntlet/ZC1-zcode-scoping-photograph.md) | [daemon](docs/evidence/gauntlet/MX1-zcode-cli-wake.md) ● | wake measured on arrival; the Stop-hook path is superseded by the daemon |
| herdr | [live](docs/evidence/conformance/C8-herdr-conformance-live.md) | [—](docs/evidence/wave2-r3-herdr-loopback-client-2026-08-27.md) | [n/a](docs/evidence/gauntlet/H-wake-posture-matrix.md) | orchestrator; observation adapter live |
| t3 | [CLI](docs/evidence/conformance/C9-t3-compatibility-live.md) | [—](docs/evidence/conformance/C9-t3-compatibility-live.md) | [event-driven](docs/evidence/gauntlet/MX1-t3-cli-wake.md) ● | orchestrator; observed via its own surface |
| devin | [CLI](docs/evidence/conformance/C11-devin-conformance-live.md) | [—](docs/evidence/conformance/C11-devin-conformance-live.md) | [event-driven](docs/evidence/gauntlet/MX1-devin-cli-wake.md) ● | CLI-compat tier |
| antigravity | [CLI](docs/evidence/conformance/C12-antigravity-conformance-live.md) | [—](docs/evidence/conformance/C12-antigravity-conformance-live.md) | [event-driven](docs/evidence/gauntlet/MX1-antigravity-cli-wake.md) ● | CLI-compat tier |

**Desktop / GUI surfaces**

| harness / surface | wake | notes |
|---|---|---|
| codex / desktop | [daemon](docs/evidence/gauntlet/H-wake-posture-surfaces.md) ○ | ChatGPT.app |
| claude / desktop-chat | [n/a](docs/evidence/gauntlet/H-wake-posture-surfaces.md) | chat app, not a seat - Claude seats run in the CLI or IDE extension |
| claude / ide-extension | [daemon](docs/evidence/gauntlet/H-wake-posture-surfaces.md) ○ | extension ≠ CLI; own row by design |
| opencode / desktop | [event-driven](docs/evidence/gauntlet/H-wake-posture-surfaces.md) ○ |  |
| cursor / desktop | [daemon](docs/evidence/gauntlet/H-wake-posture-surfaces.md) ● | the measuring seat itself |
| grok / desktop | [n/a](docs/evidence/gauntlet/H-wake-posture-surfaces.md) |  |
| t3 / desktop | [event-driven](docs/evidence/gauntlet/H-wake-posture-surfaces.md) ○ |  |
| antigravity / desktop | [daemon](docs/evidence/gauntlet/H-wake-posture-surfaces.md) ○ | not inherited from its CLI |

● measured live · ○ classified from surfaces (the unexercised probe is named in the receipt) · — no receipt yet: we do not claim what we have not measured.

**Deep integrations (codex):** [session boot](docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md) · [managed send](docs/evidence/gate-wsb-b5-2026-08-27.md) — receipt-linked notes rather than grid columns, so one harness's head start does not read as everyone else's gap. The full 20-surface grid, every cell receipt-linked, lives in [docs/capability-matrix.md](docs/capability-matrix.md).
<!-- capability-matrix:end -->

Platforms: macOS today. Anything POSIX is intended; a platform joins
this list the same way a harness does — with receipts. The public
test suite does not yet pass on GitHub's own Linux and macOS runners
at this tip; every failure is classified and the fixes are open
issues, and this sentence leaves when the run is green.

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

## Why it doesn't fall over

Every harness already writes a session log. Those are per-harness,
mutable, uncorrelated, and they can't answer a fleet question.

Under floati, everything above runs on one append-only, typed ledger.
The board, the chart, the doctor, the replay are all projections of
it, and if a projection ever disagrees with the receipts, the
receipts win. `journal verify` checks the ledger's own chain;
`floati verify` reproduces a delivery claim in a fresh worktree at the
claimed commit; `journal checkpoint`, `snapshot` and `epoch roll` are
the recorded ways to move the ledger; `repair quarantine` and `purge`
are the recorded ways to remove from it, and nothing is deleted in
place.

Kill a worker, kill the sequencer, reboot the machine: nothing is
lost and nothing lies. The ledger survives every fault, the whole
run replays on demand, and floati refuses to continue past what it
cannot prove, telling you exactly why in a typed exit code.
Ambiguous identity, expired authority, malformed envelopes — same
answer: refusal with a reason, never a guess. Today a refusal names
its code and its detail; the `remedy` field exists and is mostly
empty, and filling it is open work ([#8](https://github.com/Land-o-Clusters/floati/issues/8)).
One convention to know: a refused or degraded run prints its artifact
on stderr, not stdout ([#22](https://github.com/Land-o-Clusters/floati/issues/22)).

No telemetry, ever. Floati's own sockets are local pipes between its own
processes, and nothing in the product can listen — a test refuses any `bind`
or `listen` outside a local pipe. The outbound paths are counted, and there
are exactly four: two client-only loopback dials, for the herdr and t3
adapters; one HTTPS fetch for updates; and `intake adopt --source github`,
which runs the explicit `gh` executable to read one issue. That subprocess
may receive only a non-empty ambient `GH_TOKEN` or `GITHUB_TOKEN`; Floati
hides `gh`'s stored login configuration. The first three run only behind an
explicit consent receipt. The fourth requires the explicit command but does
not yet have a consent receipt of its own, and that is open
([#25](https://github.com/Land-o-Clusters/floati/issues/25)).

The full promise — and precisely what floati refuses to guess — is
written down: **[Truth Guarantees](docs/TRUTH-GUARANTEES.md)**. If a
promise on that page can't be demonstrated by a test or a receipt,
it doesn't belong on it.

## What it costs to run

Almost nothing is resident. A send, a drain, a doctor run, a status
read are processes that live for one command. Three things stay up
while you use them, each on your say-so: the wake daemon you consent
to per seat, with poll bounds you set at consent time — revoking it
removes the process, provably; `sequencer serve`, which holds a run's
local pipe open while a run is in flight; and `mcp serve`, which
lives exactly as long as the agent client attached to it. The board
and `watch` are interactive and exit when you do.

The readers stay fast at hostile scale — measured, not promised.
At 10,000 work items and 100,000 ledger events:

| Reader | Median | Budget |
| --- | ---: | ---: |
| inbox | 34 ms | <100 ms |
| status | 76 ms | <150 ms |
| replay render start | 47 ms | <300 ms |
| board full redraw | 92 ms | <250 ms |
| doctor | 116 ms | <2,000 ms |
| doctor --probe | budget-shaped: per node, default 60 s | per-node budget × node count |

Those are the fix-round medians of three samples after one warm-up,
from [the gauntlet record](docs/evidence/HM3H-GAUNTLET.md), dated
2026-08-01; the first soak in that same record failed four of the
five budgets, which is why the fix round exists. `doctor` here is the
plain report; `doctor --probe` waits its per-node budget on top.

One number we have not measured yet, so we will not print one: the
wake daemon's resident footprint over a long window. It is a small
polling process with ruled bounds, but "small" is not a
measurement — that receipt is queued, and this section gets the
number when the number exists.

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
`floati confluence adopt` and `confluence release` are the recorded
way a consumer takes and gives back that seam.

## Verify

```bash
python3 -m unittest discover
python3 -m floati.selftest
python3 -m floati.conformance --live-root-smoke
```

Visible CLI language is generated into `docs/COPY-LEDGER.md`. Hosted
CI, deployment, and release are separate gates; a local green suite
doesn't manufacture them.

## What we know is wrong

Every open defect we know about is an issue on this repository, filed
by us, with the measurement that found it. The ones a new user meets
first are linked above. A README that says a product is finished is
lying about a product this young; this one says what it does, what
it costs, and what is still open.

Product code is AGPL-3.0; the interchange schemas and bundle
specifications are Apache-2.0.
