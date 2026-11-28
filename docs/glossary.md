# Glossary

One plain sentence for every Floati-specific term the README uses, the
nearest standard term (or *no standard equivalent*), and where Floati's
meaning differs from it. The separations DESIGN Law 14 draws hold here: a
lease is never liveness, delivery is not consumption, an acknowledgment is
not completion. No term is renamed.

**DRAFT-ZCODE 2026-10-11 — every sentence in this file is draft copy by
a build seat, filed for the architect's review.** The census that keeps the
term set honest is `tests/test_docs_glossary.py`: it derives the product's
vocabulary from the live CLI parser, the output contract and the record
kinds, and reds on a README term with no entry below.

### ack

A receipt that says a seat has SEEN an envelope — nothing more.

- Nearest standard term: acknowledgment (at-least-once messaging)
- Where it differs: it never means the work is done or agreed; disagreement is a reply, work is a separate work receipt, and an ack is never withheld to signal anything.

### ask

One local command that records an addressed question and returns the linked answer, a decline, or a typed UNKNOWN.

- Nearest standard term: request-reply
- Where it differs: the question and its answer, decline, or timeout are linked ledger records on this machine; answered is reported only together with the answer receipt; a timeout or an unreadable answer is UNKNOWN, and the command opens no listener.

### board

The live fleet picture as a terminal chart — or, as a verb, to attach a session to its seat, take over the wake claim and drain.

- Nearest standard term: dashboard (noun); taking over a shift (verb)
- Where it differs: the boarding order is fixed — attach, take over the wake claim, then drain — and every step is receipted.

### bus

The receipt-ledger fleet members write to: notifications, work, receipts and wake coordinates, all inside one explicit root.

- Nearest standard term: message bus / pub-sub
- Where it differs: it is git- and file-backed with zero network and zero discovery; every write leaves a receipt, and nothing is ever scanned or discovered.

### capability

A capability is one thing a harness can do with Floati — messaging, observation, wake or managed execution — each measured on its own.

- Nearest standard term: capability
- Where it differs: each harness surface is assessed separately, so one capability does not imply another.

### chart

A map of every declared fleet root — a Harbor Chart — rendered as text or a live TUI.

- Nearest standard term: system topology map
- Where it differs: only roots the operator declared appear; there is no discovery by design.

### claim

Taking one work item for yourself, recorded as a claim on the orchestration log.

- Nearest standard term: claiming a ticket
- Where it differs: a claim is an append-only ledger record checked against a grant (work authority), never a file lock or a private promise.

### confluence

The read seam a consuming observer app reads through: explicit grants, materialized receipt bundles, and managed sessions.

- Nearest standard term: read-only API
- Where it differs: access is granted per consumer by an explicit idempotent act; there is no discovery, watcher, network or mutation API.

### consume

Record the recipient's explicit attestation that it acted on one acknowledged envelope.

- Nearest standard term: application-level consumption acknowledgment
- Where it differs: delivery and ACK never imply action; a separate receipt binds the exact envelope, recipient, prior ACK and acting session, and repeating the recipient and envelope replays that receipt.

### context

Evidence about the harness a seat runs in, plus Tide signals that recommend or direct a context turnover.

- Nearest standard term: telemetry
- Where it differs: readings are recorded testimony with idempotency keys; a policy recommends or directs, it never acts by itself.

### context policy

One Tide policy: a metric, a threshold, and an action — recommend or direct.

- Nearest standard term: alert rule
- Where it differs: it lives in the fleet root, is set and cleared by receipted acts, and "direct" is a strong signal to the seat, never a forced kill.

### contract

A ledger record kind whose validator pins the fields a bounded agreement between fleet members carries.

- Nearest standard term: schema
- Where it differs: it is validated at every read, and a record its reader does not know refuses as malformed evidence (exit 33) instead of being ignored.

### degraded

The exit-35 status: the run completed, but at least one check could not speak.

- Nearest standard term: partial success
- Where it differs: each finding names the check that degraded, so you read the artifact instead of guessing what was skipped.

### describe

Print the live command contract — every verb, argument and default — as data.

- Nearest standard term: self-describing API document
- Where it differs: it is projected from the running parser of this installed build, so what it says is what this binary does.

### dispatch

A record of a work assignment that links the recipient, the exact brief revision, and the delivery branch.

- Nearest standard term: work assignment
- Where it differs: you can recover the exact brief and see which revision each returned artifact was filed against. The delivery reference records how the assignment was sent.

### doctor

Diagnose a fleet root and its install bundle, and chase every red to the receipt that names it.

- Nearest standard term: health check
- Where it differs: every finding is typed and cited; incomplete installer observation is `degraded` (exit 35), never a silent pass.

### effect

One projected outcome of a run — an operation — inspectable and compensable by its exact id.

- Nearest standard term: saga/outbox effect
- Where it differs: effects are first-class ledger records that you can show, reconcile and compensate; nothing is fire-and-forget.

### effect compensate

Request compensation planning for one effect — preview first, then confirm.

- Nearest standard term: compensating transaction
- Where it differs: Floati plans the compensation and receipts the request; the confirm step is a separate, explicit act with its own idempotency key.

### effects

List projected effect status across runs and attempts.

- Nearest standard term: outbox inspection
- Where it differs: it is a read-only projection over the ledger — listing never retries anything.

### epoch

One whole generation of the bus ledger; rolling an epoch archives it and replaces it.

- Nearest standard term: log generation
- Where it differs: an epoch roll is a governed, receipted act behind an explicit authority grant, and readers resolve across epochs or refuse typed.

### envelope

One message on the bus: sender, recipient, repository, commit, document pointer and note.

- Nearest standard term: pub-sub message
- Where it differs: delivery and acknowledgment are separate receipts — a successful send proves only the append.

### grant

Exact work authority: one holder, one subject, one epoch, appended to the ledger.

- Nearest standard term: capability grant
- Where it differs: it is revoked by another append, scoped to an epoch, and checked at claim time — never a role, and never a blanket permission.

### graph

Render the Harbor Chart for one root, or every declared root at once.

- Nearest standard term: topology graph
- Where it differs: declared roots only, always read-only.

### hold

A seat's turn being held open — by a waiter or a wake daemon — so incoming mail can wake it inside the same turn.

- Nearest standard term: long poll
- Where it differs: a hold is testimony in a ledger, and liveness is never inferred from hold (or lease) bookkeeping — Law 14 keeps the planes separately named.

### hook

A harness hook — a script a coding tool runs at turn events — that Floati installs under governance.

- Nearest standard term: lifecycle hook (as in git hooks)
- Where it differs: installs are receipted and a second install is byte-identical; uninstall restores the pre-install bytes.

### hook install

Install the exact harness hook for a seat, with the hold derived from a measured ceiling.

- Nearest standard term: hook installer
- Where it differs: the wake claim it arms is taken over explicitly, and what counts as "installed" is proven by a real firing, not by appearing in a UI.

### inbox

The pending mail for one node, drained per session.

- Nearest standard term: message-queue consumer inbox
- Where it differs: it is a projection over the shared ledger; draining produces delivery receipts, and acknowledgment is a separate act.

### init

Create a direct fleet home at an explicit root.

- Nearest standard term: workspace initialization
- Where it differs: there is no default root and no discovery — init writes the ledger, registry and board scaffolding, receipted, at the root you named.

### install

Write the exact governed bundle to a destination — only the files the manifest names.

- Nearest standard term: package install
- Where it differs: every file gets a SHA-256 receipt, foreign files are preserved and reported, and nothing outside the manifest is ever written.

### intake

Adopt bounded work from outside sources — local Markdown, GitHub issues — as immutable snapshots.

- Nearest standard term: issue ingestion
- Where it differs: adoption is explicit and receipted, and dispatching a mutation binds a preview digest to one effect intent.

### intake adopt

Adopt one explicit intake source into an immutable snapshot.

- Nearest standard term: import
- Where it differs: the snapshot is immutable and idempotent by key, and a preview never touches the source.

### journal

Exact-byte testimony over a journal: write a bounded checkpoint, or verify one.

- Nearest standard term: audit checkpoint
- Where it differs: checkpoints are bounded by kind and verified byte-exactly; a mismatch is malformed evidence (exit 33), never repaired silently.

### journal verify

Verify one bounded checkpoint against the live journal — or against history.

- Nearest standard term: checksum audit
- Where it differs: a failure is a typed stop: do not retry, and name the ledger for investigation.

### lease

Bookkeeping that says a seat holds something — a claim, a daemon — for a while.

- Nearest standard term: lease (distributed systems)
- Where it differs: a lease is never liveness; Law 14 keeps the two separately named, and liveness is self-reported or witnessed, never inferred from lease state.

### replay

One command that reads the receipts linked to one question or work item.

- Nearest standard term: audit trail
- Where it differs: Floati links the original receipts by recorded IDs and cites the Git references recorded on them.

### log

Read the mail, or replay the orchestration evidence like a recording.

- Nearest standard term: log viewer
- Where it differs: it is the bus's mail and evidence reader — `--replay` reconstructs a run from receipts, it is not a text dump.

### mcp

Expose launch-bound agent tools over the Model Context Protocol.

- Nearest standard term: MCP server
- Where it differs: the standard term is exact here; Floati binds one server to one root, node and session, and it serves nothing else.

### mcp serve

Serve one launch-bound MCP session.

- Nearest standard term: MCP server process
- Where it differs: the session's coordinates are fixed at startup and never discovered.

### needs

The needs-you inbox: what agents have raised for a person (reviews, approvals, questions), each bound to a ref, commit or files and read with its freshness.

- Nearest standard term: review queue
- Where it differs: an item that names a coordinate goes stale by itself when that coordinate moves and says what moved; marking it read is a person's act at a controlling terminal.

### node

One registered seat in the fleet.

- Nearest standard term: cluster node
- Where it differs: a node is a registry row in a ledger, not a process — being registered says nothing about being alive.

### node add

Register one node, with an explicit harness and lifetime.

- Nearest standard term: host registration
- Where it differs: changes are preview-first, the role assignment and lifecycle prompts are explicit acts, and an undeclared foreign bus in scope is surveyed read-only and adopted only after asking.

### node explain

Project one live node's boot context: its roots, wrapper shapes and standing instructions.

- Nearest standard term: introspection
- Where it differs: it prints the exact managed wrapper shapes to use verbatim — a remembered command shape is never the source.

### orchestrate

Seed a worker fleet from a plan file and run it under one deadline.

- Nearest standard term: job orchestrator
- Where it differs: workers are authority-checked adapters, and an exceeded deadline is the typed `orchestration_deadline` (exit 34), never a silent overrun.

### plan

Explain whether a plan would be admitted — read-only, before anything runs.

- Nearest standard term: planner dry run
- Where it differs: admission is explained with typed reasons; the verb never executes anything it explains.

### presence

Node liveness, as the nodes themselves report it.

- Nearest standard term: heartbeat
- Where it differs: presence is self-reported testimony with a TTL — the fleet never infers liveness from leases, holds or registration.

### presence report

Record this node's own liveness for a stated time-to-live.

- Nearest standard term: heartbeat ping
- Where it differs: it is testimony, receipted; the absence of a report is absence, typed and cited, not a guessed state.

### quiet

One fleet-wide quiet window: Floati waits for the host load to settle, then pauses its own enrolled seats that are building or testing until a touch, a cap or a signal releases them, and writes each seat's CPU time inside the window.

- Nearest standard term: maintenance window
- Where it differs: it holds only Floati's enrolled seats and pauses only build and test work; everything else is observed, listed and told through `floati quiet status`, never held.

### receipt

The durable record that one exact act happened.

- Nearest standard term: receipt
- Where it differs: the standard term is the point — Floati just refuses to let a receipt mean more than its kind says.

### receipts

One node's receipt history: what it sent, what was delivered, what was acknowledged.

- Nearest standard term: audit trail
- Where it differs: a receipt proves exactly what it says and no more — an append is not a delivery, a delivery is not an acknowledgment.

### refused

The exit-20 status: the request was refused before any mutation.

- Nearest standard term: request refusal (4xx)
- Where it differs: the artifact's `evidence.code` is a stable machine reason naming what to fix — the request is wrong, not the system; never retry unchanged, and never retry a policy refusal at all.

### register

Add a named seat and its harness to the fleet registry.

- Nearest standard term: enrollment
- Where it differs: registration adds a durable roster entry. Workspace and hook setup use their own commands and consent.

### repository

The named repository an envelope's commit lives in.

- Nearest standard term: git remote
- Where it differs: envelopes carry repository plus commit so a recipient can read the exact document; the bus checks the envelope's shape, and an unresolvable commit is its own typed row.

### retire

Remove a node's registry row — always self-only.

- Nearest standard term: deregister
- Where it differs: you retire yourself, never someone else; user records outlive the tool, and leaving is a first-class feature with its own receipts.

### role

A root-local template of standing instructions a seat can be assigned.

- Nearest standard term: role profile
- Where it differs: roles are validated files in the fleet root, assigned by a receipted act — never implied by a harness, a model or a memory.

### seat

One node's place in one declared workspace — where identity comes from.

- Nearest standard term: account/session pair
- Where it differs: identity comes from the workspace seat declaration, never from memory; identity precedes resolution.

### send

Append one notification to the bus: from, to, repository, commit, document, note.

- Nearest standard term: publish (pub-sub)
- Where it differs: success proves the append, never delivery — delivery and acknowledgment are separate receipts, each named.

### sequencer

The optional local managed writer that serializes ledger writes for a root.

- Nearest standard term: single-writer service
- Where it differs: it is a mode, not a requirement — daemonless "direct" mode is one command away, and which mode is live is observable.

### sequencer serve

Run the local managed writer for this root, as this node.

- Nearest standard term: writer daemon
- Where it differs: takeover is explicit (`--takeover`), and serving is observable and reversible.

### setup

<!-- DRAFT-OPUS 2026-11-26 -->

Prepare one seat in a new local fleet and keep a receipt for teardown.

- Nearest standard term: provisioning
- Where it differs: Floati previews the files it will write and waits for confirmation. Teardown checks the receipt and the current files before removing anything.

### signature

Minisign testimony over an explicit artifact: sign it, or verify it against a public key.

- Nearest standard term: code signing
- Where it differs: signing is bound to a journal id and sequence — the signature testifies about ledger state, not about a release blob.

### status

Summarize the fleet: nodes, mail, wake coordinates, health.

- Nearest standard term: status query
- Where it differs: a completed query exits 0 even when nested installer observations are incomplete — the warnings live in the evidence, where you read them.

### thread

One registered provider thread — a conversation a harness owns — that observations attach to.

- Nearest standard term: external process handle
- Where it differs: attaching, observing and detaching are separate receipted acts; an observation pulls provider status, it never invents it.

### uninstall

Remove the exact tool bytes Floati owns — nothing else.

- Nearest standard term: package uninstall
- Where it differs: only unchanged manifest-owned files go; bus roots, ledgers and foreign files are always retained, and `--dry-run` shows the set first.

### verify

Reproduce a typed delivery claim — prove the append a send receipt named.

- Nearest standard term: verification
- Where it differs: it re-derives the claim from the ledger; a poll or a memory is never a substitute for evidence.

### wait

Hold this turn until a named condition is true — for example, fresh mail.

- Nearest standard term: blocking wait
- Where it differs: it holds a harness turn and is fixture-gated — a production waiter is never run by hand against a live root.

### waiter

The armed process that holds a seat's turn open, listening for mail until its deadline.

- Nearest standard term: long-poll consumer
- Where it differs: on a healthy idle seat the turn does not visibly end — a turn that ends promptly is a diagnostic flag (stale claim, tripped breaker, pause marker, exhaustion), not quiet.

### wake

Rousing a seat when mail arrives, and the verbs that govern it: pause, resume, arm, status, daemon.

- Nearest standard term: wake-on-mail
- Where it differs: wake control is marker-only and receipted — it never edits hook registration, and consent is recorded before any wake machinery exists.

### wake arm

Arm one exact acting session: bind the workspace, the waiter and the consent.

- Nearest standard term: arming a watch
- Where it differs: it is re-run at every session turnover; a live predecessor needs `--take-over`, a paused claim does not.

### wake daemon

Manage one local wake daemon — the background poller that wakes a seat between turns.

- Nearest standard term: background agent
- Where it differs: every coordinate — consent, bind, install, start, status, stop, remove, revoke — is a separate receipted step in the same vocabulary.

### wake status

Inspect one exact session's wake coordinates, including the daemon breaker.

- Nearest standard term: status probe
- Where it differs: a coordinate can be underivable with a typed reason (`runtime_missing`, `runtime_symlink`, `runtime_malformed`) — absence is named, never filled in.

### watch

Poll for fleet deltas on an interval.

- Nearest standard term: watch mode
- Where it differs: it reads the ledger and prints deltas as artifacts; like `status`, a completed query exits 0 even with incomplete nested observations.

### who

List active fleet nodes and the observations recorded for each one.

- Nearest standard term: fleet roster
- Where it differs: each measured value carries its observation time and age. Missing or conflicting evidence appears as `UNKNOWN`.

### work

The orchestration log: work items added, claimed and completed on the bus.

- Nearest standard term: ticket queue
- Where it differs: append-only records with authority-checked claims; a reassignment is a dispatch where old and new owners both get an envelope.

### work add

Append one work item.

- Nearest standard term: create ticket
- Where it differs: the item is an immutable ledger record, and claiming it later checks a grant, not a feeling.

### work complete

Complete one work item claimed by the exact actor, optionally recording action on named dispatch envelopes first.

- Nearest standard term: complete ticket
- Where it differs: bare completion never implies message consumption; explicit `--consume` envelopes require prior ACK and an acting session, and consumption is durable even if completion needs a retry.

### work show

Project work state — one item, or the whole log.

- Nearest standard term: ticket board read
- Where it differs: the projection is derived from receipts and never mutates anything.

### worker

Run one authority-checked worker turn through a provider adapter.

- Nearest standard term: job worker
- Where it differs: the worker's authority comes from grants in the ledger, its refusals are typed, and Floati itself makes zero model calls.
