# Floati Protocol Core — HM-0 through HM-1 Phase A

## Scope and posture

Phase 1, HM-0.5, and HM-1 Phase A form a local filesystem protocol core: versioned record
schemas, append-only event and receipt evidence, an explicit registry, three
separated expiry planes, a dependency-free artifact CLI, and a harness-neutral
conformance artifact. It has no daemon, network transport, UI, wake adapter,
external runtime dependency, or publication claim.

Product-visible strings remain `COPY PENDING — ARCHITECT`.

## HM-3I charter mirror — bounded local run graph

The binding authority for this scope is `HARBOR_MASTER.md` from the upstream
product this contract was designed against at
`a111202b228d34c2b371bcc5e2c4798206474439`. The lawful HM-3I scope is a
**bounded local run graph** with durable run-truth records: it is finite and
acyclic, local, and data-only. It permits no arbitrary embedded code, no
general condition-expression language, no distributed scheduling, and no
claim to replace Temporal, LangGraph, or similar workflow engines; no model-authored graph mutation without a durable plan_amendment record is permitted. The general workflow engine remains fenced.

**Product boundary (verbatim, binding):** Floati is the deterministic local
operating kernel for heterogeneous coding-agent fleets: it may admit, schedule,
fence, suspend, cancel, reconcile, verify, and prove. It is never the reasoning framework, a hosted control plane, a general workflow engine, a secret vault,
an account-rotation system, a remote multi-tenant scheduler, or an authority
that converts model confidence into truth.

## HM-1 Phase A orchestration surfaces

Mail remains the Git-authoritative notification ledger at `events.jsonl`.
Orchestration truth is a separate append-only `work/items.jsonl` containing
strict `work_item` and `work_transition` records. The two ledgers share the
single exported compact-I-JSON frame in `floati.framing`; neither owns a second
encoder or decoder. Work state is projected from the validated record order,
so claiming and completing one item cannot consume adjacent open items.

Work claims bind the exact active authority subject, holder, and epoch before
the claim transition is appended. Capability declarations use the honest
`unavailable` / `read_only` / `read_write` modes and expire visibly. Approval
requests bind an exact active authority epoch and a requested scope plus TTL.
One request accepts one terminal decision: approval may preserve the exact
scope and shorten the TTL, while denial is a durable decision receipt with no
grant fields.

`Supervisor.snapshot` is report-only. It reads registry, liveness, authority,
mutual-exclusion, inbox, and acknowledgment evidence without taking or
creating filesystem locks. Its result retains liveness, authority, and mutex
as three separately named states, reports unknown evidence distinctly, and
lists persisted active leases whose expiry has passed. It never wakes,
acknowledges, claims, renews, releases, or repairs a node.

## HM-1 Phase B operator workflows

The dependency-free `floati` surface adds `status`, `watch`, `receipts`,
`work`, and `supervise`. Status, watch, and the later harbor board consume one
`FleetProjection`; supervision supplies the same node rows without importing
an action API. Watch compares projections without their observation timestamp,
so an unchanged poll is silent rather than manufacturing activity. Its 250 ms
default and bounded interval preserve the artifact process result.

`send` accepts an optional reply binding and explicit idempotency key. Legacy
v0 envelopes without `reply_to` remain readable; a new reply must name an
existing message whose sender/recipient are exactly reversed. The returned
envelope echoes the effective idempotency key. Delivery, acknowledgment, and
reply remain different facts.

The work CLI appends the Phase-A item/transition records directly. Claims
require an exact live authority subject, holder, and epoch; completion requires
the exact claim holder. `receipts <node>` returns delivery, acknowledgment,
and denial histories under separate keys. `supervise` exposes no action flag
and retains the physically read-only snapshot proof.

Every command and work subcommand has static offline help with name, synopsis,
description, options/arguments, exit statuses, and examples. Help is the only
non-artifact CLI output. Its provisional text is registered once and generates
`docs/COPY-LEDGER.md` for the architect's voice pass.

## Root and tenant model

The phase-1 namespace API receives a `FloatiRoot` created from an explicit
absolute path and explicit tenant identifier. Missing or relative roots
refuse. No
environment variable, home directory, current directory, or named tenant is a
fallback.

Writable evidence lives below:

```text
<explicit-root>/tenants/<explicit-tenant>/
```

Cross-tenant observation is an opt-in, root-bound read capability naming the
exact tenant set. Only a validated `FloatiRoot` can mint it. Filesystem
reachability does not imply observation authority, an observation object
exposes no raw tenant path, and every durable write requires a validated
writable root plus a contained relative path. Absolute paths, traversal, and
symlink escapes refuse.

HM-0.5 adds the separately named
`FloatiRoot.open_direct_home(path, *, create=False)` authority used by the CLI.
It derives the tenant identifier from the explicit absolute path's basename
and uses that exact resolved directory as the tenant home. It refuses a
missing path unless `floati init --root <root>` requests creation, and refuses any
existing child named `tenants` so that a namespace root cannot be interpreted
as a direct home. Creation refuses an existing non-directory with
`direct_home_not_directory`; expected directory-creation failures become the
stable `root_unavailable` refusal rather than escaping as filesystem
exceptions. No CLI command derives or defaults the durable home.

## Record model

The JSON Schema 2020-12 documents in `schemas/v0/` are strict objects with
`schema_version: 0`. Protocol record identifiers are UUIDv7 values with their
RFC version and variant bits enforced by both generation tests and schema
patterns.

Message envelopes are Git-authoritative notifications carrying sender,
recipient, repository name, exact lowercase 40- or 64-character commit SHA,
repository-relative document path, bounded note, and idempotency key. They
have no generic body or wake field.

The separate future-adapter wake-cause record still bounds context bytes and
wake counts and distinguishes `self_wake`, `external_injection`, and
`resurrection`. HM-0.5 neither writes that record nor installs wake plumbing.

Delivery receipts record exact presented item identifiers. Acknowledgment
receipts record exact acknowledged item identifiers. Denial receipts record a
refused attempt and stable reason. None is evidence that work was acted on,
and the protocol defines no `done` state.

## Durable event and receipt flow

JSONL records are bounded to 64 KiB; each ledger is bounded to 64 MiB and
100,000 records. Every append and read validates the exact record-kind v0
contract, including its complete field set, schema version, UUIDv7 prefix,
tenant, timestamp, enum, identifier, array, and numeric bounds. Records are
serialized in a stable compact form, appended with one `O_APPEND` write under
`flock`, and fsynced before success. A new ledger also fsyncs its parent
directory. A short write restores and fsyncs the prior complete length before
reporting failure. Ledger locks use nonblocking condition polling with a
one-second deadline and refuse as `ledger_lock_timeout`; the outer authority
and mutual-exclusion compare-and-swap lock uses the same deadline and refuses
as `cas_lock_timeout`. Readers reject incomplete lines, malformed JSON, non-object
records, duplicate identifiers, schema drift, tenant disagreement, and total
ledger-limit violations.

Free-text durable fields reject terminal control characters and Unicode bidi
controls before projection. Physical LF-frame ordinal is authoritative within
each ledger. Multi-ledger replay uses the fixed source order `work`, `worker
receipt`, `worker refusal`, `denial`, then physical source ordinal; timestamp is
testimony and never a sort key. The receipt ticker uses reverse append ordinal
within fixed receipt-source precedence. Status uses the last observed append
under its fixed source traversal instead of the numerically greatest clock.

### HM-3H published soak budgets

These tolerances are copied unchanged from `HM3H_GAUNTLET_BRIEF.md`. The
2026-08-01 run used exact profiles with 10,000 work items and 100,000 relevant
events, one warmup, three measured samples, and the median statistic.

| Reader | Published budget | Measured median | Gate |
| --- | ---: | ---: | --- |
| status | <150ms | 1515.810ms | **FAIL** |
| inbox | <100ms | 2646.223ms | **FAIL** |
| replay render start | <300ms | 948.167ms | **FAIL** |
| board full redraw | <250ms | 2884.598ms | **FAIL** |
| doctor | <2000ms | 108.678ms | PASS |

The gate is failed, not widened. `RULING-REQUEST-HM3H-SCALE-READ-PATH.md`
requests separate feature authority for a governed acceleration coordinate;
none is added by this hardening pass.

A send takes one lock-free snapshot of the active registry before it acquires
the message writer lock or appends a message. An `unknown_sender` or
`unknown_recipient` refusal exposes the complete lexically sorted active roster
in its typed detail, using `(none)` when no node is active, and creates or
changes no entry anywhere beneath the tenant root: no registry lock, message,
or denial receipt. The other stateful send refusals remain durable denial
receipts: `idempotency_conflict`, `reply_to_unknown`, and
`reply_to_parties_mismatch`. The idempotency check and append are one
process-safe locked transaction after the party check. Replaying the same key
and payload returns the original envelope. Reusing the key for different
content refuses. Registry uniqueness and sparse-ack replay use the same
transaction boundary.

Presentation appends a delivery receipt for at most 1,000 exact items.
Acknowledgment appends explicit item identifier sets. Cursor state is the
validated union of those sets, so acknowledging B does not consume A or C.
Unknown, foreign-recipient, and never-presented items refuse without changing
acknowledgment evidence. Reads cross-check every durable acknowledgment and
delivery against current message evidence, so truncation or forged receipts
are integrity failures rather than silent disappearance.

## Artifact CLI and fleet polling

The executable `scripts/floati` resolves its own tracked checkout directory,
changes to the repository root, and delegates to `python3 -m floati`. Absolute
launcher invocations therefore select this checkout independently of caller
cwd. The exact command surface is `init`, `register`, `send`, `inbox`, `ack`,
and `log`. `init` takes one explicit direct-home path; every other command
requires `--root`. There is no default root or wake command. Every invocation
emits one compact JSON artifact and preserves the existing distinguishable
exit classes.

The example fleet is defined in `docs/FLEET.md`: `reviewer` uses Claude,
`builder-app` uses Codex for `~/fleet/app`, and `builder-floati` uses Codex for
`~/fleet/floati`. Each node registers itself. Nodes poll their own inbox at
boot and before stand-down. Delivery and acknowledgment remain receipts only;
acknowledgment is not evidence of completion.

## Three separate planes

The protocol does not expose one generic expiry-record abstraction. Its public
types, schemas, paths, operations, and evidence remain distinct:

| Plane | Public record/API | Evidence states |
| --- | --- | --- |
| Liveness | `LivenessPresenceStore` | `present`, `silent`, `expired` |
| Authority | `AuthorityGrantStore` | exact holder and CAS epoch |
| Mutual exclusion | `MutualExclusionHoldStore` | exact holder and CAS epoch |

Liveness evidence never grants authority. Authority never proves that a
kernel/file exclusion hold exists. An exclusion hold never claims its holder
is alive.

Authority `claim`, `renew`, and `release`, plus mutual-exclusion `acquire`,
`renew`, and `release`, serialize their read/transition/write under a dedicated
CAS lock. First ownership uses epoch 1. Takeover after release or TTL expiry
increments the persisted epoch. Wrong holders, stale epochs, and expired
renewals refuse without mutation.

Every claimed interval enforces:

```text
deadline_seconds <= ttl_seconds
```

Tests cover shorter and equal deadlines as accepted directions, the inverse as
a refusal, concurrent claims with exactly one winner, release, expiry,
takeover, and stale operations.

## Conformance and distinguishability

Any Python adapter can expose its harness behind the `AdapterResult` boundary
and run:

```bash
python3 -m floati.conformance \
  --adapter package.module:factory \
  --root /absolute/root \
  --tenant tenant-id
```

The runner observes durable registry, send, delivery, acknowledgment, denial,
liveness, and the full claim/renew/release/stale/expiry paths for authority and
exclusion. Every adapter call runs in one persistent isolated worker with a
bounded timeout; a hang, uncaught exit, or process death maps to adapter-death
evidence. Each conformance invocation uses a unique run tenant, so repeated
runs do not collide. It returns its own artifact code; no pipe or wrapper
determines success:

| Exit | Outcome |
| ---: | --- |
| 0 | conformant |
| 10 | behavioral contradiction |
| 20 | explicit-root or adapter configuration refusal |
| 30 | adapter factory or call died |
| 31 | intentional silence |
| 32 | absent result |
| 33 | malformed evidence |
| 34 | deployed bundle mismatch |

Intentional silence, death, absence, malformed evidence, refusal, behavioral
failure, and success therefore cannot render identically.

HM-0.5 also exposes `python3 -m floati.conformance --live-root-smoke`. The
zero-argument smoke function owns a `TemporaryDirectory`, creates one direct
home inside it, and exercises one send, delivery, acknowledgment, and typed
unknown-sender/recipient refusals. Each refusal exposes the complete sorted
active roster and leaves the whole tenant root byte-identical, including an
empty unknown-party denial list. It cannot accept a live root or the
adapter-only `--call-timeout`, and cleans up the throwaway home. Adapter mode
retains its 2-second default and accepts explicit timeouts from 0.01 through
60 seconds.

## Deploy integrity and activation

`bundle-manifest.v0.json` names protocol version `0`, canonical ref
`refs/heads/lane/hm0`, and the SHA-256 digest of every deployable Python and
schema file plus the tracked executable launcher, including `scripts/floati`,
`floati/cli.py`, and `floati/__main__.py`.
`python3 -m floati.selftest` runs all tests, verifies the exact
tracked deployable set and every digest, and prints the canonical ref each run.
This makes "merged" and "deployed bundle identity" different protocol facts.

Phase 1 adds no dormant flag or future-consumed configuration. A later change
that introduces configuration must land its consumer and conformance behavior
in the same change or fail the gate.

## Binding-law disposition

1. Three planes have separate names and evidence.
2. Delivery and acknowledgment are separate; neither claims action or
   completion.
3. Wake frequency and context bytes remain bounded future-adapter fields;
   HM-0.5 has no wake surface.
4. Every degradation class has a distinct artifact result.
5. The versioned manifest names the canonical ref and exact deployed set.
6. Every durable home is explicit; the namespace API also requires the tenant,
   while the disjoint direct-home API derives it only from the explicit path.
7. Status observations below are dated and paired with re-derive commands.
8. Cross-tenant observation is an unforgeable root-bound read capability.
9. No inert configuration exists.
10. CI invokes `python3 -m floati.selftest` directly.

`floati watch` is a streaming projection surface, not a buffered report. It
emits the initial observation as one compact artifact before an unbounded loop
waits, then emits only changed observations. A normal operator interrupt exits
zero without a traceback. The bounded `--iterations` form uses the same
iterator and exists for deterministic automation and tests.

## Harbor Board

The HM-1 board projects durable records; it does not synthesize a fourth
combined health state. Each node retains separate `LIVE`, `AUTH`, and `MUTEX`
lamps. Refusals and stale holds precede normal rows, work state is visibly
separate from receipts, and delivery and acknowledgment counts remain
distinct.

The interaction contract is keyboard-complete: arrows or `j`/`k` select,
Enter expands detail, `a` writes a durable acknowledgment through the normal
acknowledgment core, and `q` exits. The input wait is at most 250 ms, but the
renderer writes only when the projected state or interaction state changes.
Narrow viewports are bounded, color uses buoy orange on harbor slate, and
monochrome preserves the same words and ordering. A non-terminal consumer
receives one `PLAIN DUMP` frame and exits.

`make demo` owns a temporary directory and seeds a deterministic synthetic
fleet spanning present/silent/expired liveness, active/expired/absent
authority and exclusion, open/claimed/completed work, delivery,
acknowledgment, denial, and pending mail. It refuses a nonempty caller-supplied
root. Each redraw reprojects that durable temporary root, including after an
interactive acknowledgment; the demo is synthetic, but its refresh behavior
uses the same storage truth as a caller-supplied root. The plain `floati`
shorthand does not silently choose a durable root or
synthetic data: until a separately governed root-selection contract exists,
the board remains `floati board --root /absolute/root` and the polish fixture is
explicitly `floati board --demo` or `make demo`.

## Dark external-worker contracts

HM-1 records three Codex app-server JSON envelope categories: request,
response, and notification. The codec bounds a complete encoded frame to 1
MiB and nesting to 64 levels, validates category fields, quarantines unknown
root fields, and losslessly restores those fields on re-encode. It imports no
process, socket, HTTP, or URL module. These contracts do not authorize a
method sequence, worker launch, credential use, approval, or network access.

The recorded fixture provenance distinguishes the original three-message
provider ruling of the upstream product this contract was designed against from
its later provider-specific sequence amendments. Floati locks only the
envelope categories needed by this phase, so it neither copies an obsolete
method sequence nor silently expands the current one.

ACP remains study-only. The dated v1 mapping keeps capability, approval,
transport receipt, semantic outcome, work completion, artifact publication,
liveness, authority, and mutual exclusion as separate facts. A runtime ACP
adapter requires new typed external-session, worker-outcome, and non-Git
artifact records plus a finite transport/method/lifecycle ruling.

## HM-1b worker evidence and live boundary

HM-1b adds `worker_receipt` as a separate append-only evidence kind. Each
receipt binds one worker session to one work item, node, adapter, exact
authority subject and epoch, transition, outcome code, and Git artifact
bindings. The finite transition vocabulary is `claim`, `spawn`, `drive`,
`bind_artifact`, `complete`, and `degrade`. Receipt projection maps these to
the operator states `claim`, `driving`, `degraded`, and `complete`; it never
inspects a process, PID, socket, credential, or network route.

`WorkerRunner` selects the oldest owned open work item and requires exactly
one active authority grant held by the node before it appends the work claim
or calls an adapter. Authority-ledger integrity failure remains the separate
`authority_state_unavailable` refusal. The validated append-only work log is
the only consumption coordinate: oldest-owned selection and claim share one
transaction, and unavailable integrity fails closed as
`consumption_state_unavailable`, while an intact log with no owned open work
is the distinct steady state `worker_work_absent`. Authority turnover during
the atomic claim records `worker_authority_changed`, never a work-claim-race
label. Worker consumption never
creates a delivery or acknowledgment receipt; preview and transport remain
separate evidence. A successful adapter path records every transition,
binds Git artifacts through the existing work log, and emits completion only
after the work log is completed. A completion receipt must match both the
session's bound artifacts and the work-log completion; already-completed work
cannot acquire a retroactive worker session. Each adapter stage runs in a
bounded child process. Process start failure, timeout, death, arbitrary adapter
failure, malformed output, and the ruled Codex boundary map to a finite typed
degradation vocabulary and leave claimed work visible rather than fabricating
completion. Pre-action refusals such as an inactive node, absent work, absent
adapter, missing or ambiguous authority, or a lost claim race are written
separately as `worker_refusal` receipts; a refusal is evidence of a rejected
attempt, not a worker action or work claim.

## FLOATI dependency-aware fleet orchestration

`work_item.needs` is an optional bounded list of earlier work IDs in the same
append-only consumption ledger. Projection refuses an unknown or future edge,
and a claim is eligible only after every dependency projects `completed`.
Operator readiness is therefore derived as `blocked`, `ready`, `claimed`, or
`done`; it is never a scheduler-only fact.

`FleetOrchestrator` accepts a bounded topological plan with at least three
unique workers and more items than workers. It preflights the registry and one
exact active authority grant per worker, seeds the canonical work ledger, then
forks one controller per worker. Controllers reuse `WorkerRunner`, wait on
blocked work, and can overlap independent items. The parent streams only
receipt-derived board state at a maximum 250 ms interval.

The three final states have non-overlapping contracts. `deadline` (34) means
the fleet clock elapsed and owns cancellation. `degraded` (35) means a typed
worker or evidence failure prevented a complete chain. `drained` (0) requires
all seeded work to be done, exactly one complete terminal worker session per
item, every controller to exit zero, and no audited adapter or registered
descendant process group to survive cleanup. Completed work alone is never
sufficient to report drain.

The local degradation API acts only after an exact durable `drive` receipt.
It can terminate one controller (`process_cancelled`), append an explicit
expired state for one exact authority epoch (`authority_expired_mid_claim`),
or signal a supported local adapter to create a real hanging descendant
(`process_timeout`). Final artifacts retain controller exit codes, final work,
terminal sessions, receipt chains, drill outcomes, and process cleanup audits.

The architect ruling at `aedf92b` clarifies that Floati code makes no network
call, while the installed child harness owns its normal provider traffic and
credential state. Work items can now record only the derived absolute
`/tmp/floati-work/<work-id>` mapping; legacy rows without the optional
field remain readable. A missing mapping records `worker_workspace_missing`
and terminal `workspace_mapping_missing` evidence before launch.

`CodexAppServerAdapter` invokes the installed app-server over stdio, drives
`initialize`, `thread/start`, and `turn/start`, and streams notifications to
the retained untracked workspace transcript. It routes no credentials through
the bus. Command, file-change, and additional-permission requests are denied
and terminate as `approval_required_unattended`; no auto-approval path exists.
The effective deadline is the minimum of the grant deadline, runner ceiling,
and freshly observed remaining authority TTL minus a fixed one-second margin.
The app-server and its descendants run in a separately registered local
process group. Normal shutdown quiesces that entire group before any artifact
or Git finalization begins; the parent runner also retains the registration so
outer timeout, cancellation, or abrupt adapter death still terminates and
reaps the child group.

On a completed turn, only regular non-symlink files inside the explicit
workspace are eligible. The parent, workspace, and evidence directories are
owner-private and symlink/wrong-owner parents fail closed. `.floati` and Git
metadata are excluded. Git initializes without ambient templates, disables
system/global configuration plus hooks and signing, and retains the original
metadata-directory identity across the turn. Before finalization it rejects a
replaced `.git`, discards child-modified metadata, initializes a clean local
repository, stages literal paths, and verifies the exact final tree plus clean
status before binding every sorted relative path to one commit SHA. The
workspace and transcript pointer are retained, but transcript content never
enters bus records. The local reference harness proves the five-transition
completion path and all typed failure classes. After the owner supplied
specific informed approval for one exact command, the real provider proof
completed the same five-transition path and bound `PROOF.txt` to a clean local
commit. That permission was consumed by the single turn; the exact runtime
evidence and the preceding sandbox degradation remain distinct in the ruling
request and evidence ledger.

The generic ACP v0 seam is a bounded JSON-RPC fixture codec with duplicate-key
rejection, a finite method allowlist, extension quarantine, and a non-launching
local executable probe. No reference ACP harness was found at the HM-1b
checkpoint, so fixture conformance is distinct from live harness proof.

`Supervisor.snapshot`, `FleetProjection`, and the Harbor Board consume worker
receipts only. Supervision remains report-only and the demo's scripted worker
episode is durable synthetic evidence, not process introspection.

## FLOATI Delta Intake 2 — laws 11–15

This section binds the 2026-07-31 addendum from the upstream product this
contract was designed against to the FLOATI lane. The implementation is local
and filesystem-backed; absence of an installed external provider remains an
explicit evidence state.

### Laws 11–12: consumption is one coordinate

`work/items.jsonl` is the sole authoritative consumption coordinate. The
`ConsumptionLedger` owns its framing, validation, projection, and summary;
`WorkerRunner`, `FleetProjection`, `Supervisor`, `floati watch`, and the Harbor
Board read that same coordinate. A corrupted or unavailable ledger is
`consumption_state_unavailable`, never an empty queue. An intact queue with no
owned open work is `worker_work_absent`; the projection exposes that as
`unsatisfied_wake` so the absence is visible. Delivery, acknowledgment,
preview, and worker consumption remain distinct evidence; worker execution
does not synthesize delivery or acknowledgment receipts.

### Law 13: currency lives in the writer

`floati install` and `floati update` perform the source Git cleanliness and named
ref check inside `DeploymentWriter` before destination mutation. Normal mode
requires `HEAD` to equal the named ref, defaulting to `origin/main`.
`--committed-tree` is an explicit `committed-tree-ci` mode and is printed in
the result; it still requires a clean committed source and a resolvable ref.
The writer verifies `bundle-manifest.v0.json`, copies exactly its managed set,
records ownership under `.floati-install/manifest.v0.json`, and removes only
unchanged files previously owned by Floati. Foreign files, modified stale
files, and foreign symlinks are preserved and reported; no recursive prune or
foreign overwrite is permitted.

### Law 14: plane-honest vocabulary

Consumption, delivery, acknowledgment, liveness, authority, mutex, and worker
outcomes retain separate names and receipts. Liveness is never inferred from
lease bookkeeping. The Pi adapter's fixture proof and the absence of a local
`pi` executable are reported separately; neither is presented as a real
provider turn. Instrumentation prints bounded typed outcomes rather than
silently collapsing absence, timeout, malformed output, or process death.

### Law 15: identity precedes resolution

Namespace roots, direct homes, the tracked `scripts/floati` entry point, and
deployment source/destination entry points refuse symlinked identities before
resolution. The tenancy proof creates two throwaway tenants on one machine,
confirms each can read/write only its own home, and confirms traversal and
symlink escape cannot touch the other home. The deployment writer uses the
governed exact-set manifest and never prunes foreign paths.

### Pi adapter v0

`PiRpcAdapter` speaks Pi's local LF-delimited JSON RPC process boundary through
the existing `WorkerRunner` contract. It correlates request identifiers,
accepts terminal `agent_end`/`turn_end` events, bounds lines, preserves raw
transcript evidence privately, and maps malformed, timeout, and process-death
conditions to typed worker degradation. Pi's own child process owns its model
traffic; Floati does not route provider credentials or network traffic. The
repository fixture proves a complete worker turn. A real `pi` install was not
present at this checkpoint, so live-provider proof is honestly absent.

## FLOATI C6 — visible magic and alone-value

The flight recorder is a pure projection over the existing work, worker,
worker-refusal, and denial ledgers. It observes no process and reads no private
transcript. Timeline order is the complete tuple `(timestamp, record ID,
source path, source ordinal)`; elapsed time is relative to the first durable
event. `--speed` changes only interactive waits. Non-terminal, `TERM=dumb`, or
explicit `--plain` playback writes one append-only timeline without sleeping.
Work and worker sources remain visibly distinct so their paired claims and
completions are not collapsed or presented as duplicates without provenance.
Interactive playback reads the active stream's terminal dimensions for every
run and bounds each synchronized frame to that viewport; a 120×40 fallback is
used only when the stream exposes no terminal size.

Solo bootstrap records one immutable version-zero `solo.json` identity,
registers that exact node, and uses the existing maximum 86,400-second
authority interval for `solo-work`. Argument-light work commands resolve only
that exact node and active grant before calling the unchanged `WorkLog` checks.
No core claim or completion invariant is weakened, and every durable command
still requires an explicit root.

The TUI wall is generated from fixed pure models. Its SVG theme changes only
palette; paired text testimony is byte-equal across light/dark. Idle, live,
degraded, and replay each cover standard and plain modes. Synthetic wall
captures are review instruments, not live proof. The HM-3 light-palette punch
raises cream-background header emphasis to a measured 7:1-or-better contrast
floor without changing testimony; architect voice and push review remain separate.

## FLOATI C7 — Confluence contracts only

`floati status --root ROOT --json` declares reliance on the version-zero
`fleet_status` artifact. The payload names the selected root and tenant,
retains three separate plane states, and projects work, receipt, consumption,
worker-session, and refusal evidence without process inspection.
Without `--json`, status, watch, and board retain the pre-C7 projection shape;
the versioned fields are added only by the explicit machine-contract path.

The receipts-read bundle allowlists the existing work and receipt ledgers and
reuses their durable schemas. Its snapshot is physically read-only, bounded,
and all-or-nothing on malformed evidence. The consumer receives an explicitly
consented path; scanning is outside the contract. No implementation for a
consuming observer app, network, installer, watcher, discovery, credential, or
write surface exists in C7 v0. Floati owns protocol truth; a future build of a
consuming observer app may only render it or request separately ruled actions.

All new positioning and visible strings remain
`PROVISIONAL — ARCHITECT VOICE PASS PENDING`.

## HM-2S — Confluence data plane and operator hardening

The ACP probe checks Claude Code ACP before Codex ACP and then the generic
compatibility name without launching an ordinary harness as a substitute. No
ACP responder was installed at this checkpoint. Both bounded fixtures
round-trip semantically; a provider-backed turn is therefore an honest skip,
not a live pass or a permission workaround.

`ManagedSessions` adds the dark future Managed Mode seam. Adoption requires an
active registered manager and its exact authority lease subject, epoch, state,
and expiry. A release can close only its exact active adoption. Projection uses
only validated adoption and release receipts and contains no dependency on a
consuming observer app, process lookup, or session scan.

`floati graph --root ROOT --json` is the Harbor Chart feed. Its version-zero
topology contains sorted registry nodes, receipt-projected worker sessions,
work dependency edges, and bilateral bridge stubs. It omits observation time,
mtimes, PIDs, process state, and inferred paths so identical ledgers produce
identical topology.

`floati doctor` is physically read-only. It checks invoked root identity,
registry/liveness agreement, the exact bundle manifest, Law 13 named-ref
currency, governed symlink entries, installer shadowing on the supplied PATH,
and only `work/items.jsonl` as the Law 11 consumption coordinate. Artifact RCs separate configuration refusal,
malformed evidence, and degradation. Remediation is suppressed when its
currency prerequisite is not proven.

Bridge v0 joins exactly two local direct homes. Each tenant independently
appends consent; both roots mirror the active record and exact consent IDs. A
forward creates paired receipts stamped `advisory_not_consumption`, never a
message or work record. Either direction revalidates both consents and bridge
records. Every refusal writes bilateral denial evidence; remote transport and
same-root bridges are explicit refusals.

The wall repair changes visual hierarchy without changing protocol truth.
Idle empty instruments collapse into one calm row; worker rows pair title
before a shortened work ID; degraded alerts order denial, unsatisfied wake,
then worker outcome without duplicate receipt echo; replay rails distinguish
work causality from worker receipts; and plain board dumps no longer repeat
the standard header. The later HM-3 light-palette punch closes the measured
contrast item only; architect voice and exact-tip push judgment remain separate.

## HM-3 — Claude headless worker, local gateway, and publication mechanics

`ClaudeHeadlessAdapter` extends the governed `WorkerRunner` rather than
creating a second authority or receipt state machine. It launches one explicit
absolute `claude` executable in print mode with text input, single-result JSON
output, `dontAsk` permissions, no session persistence, and only the built-in
`Read,Write,Edit` file tools. Bash, network-capable tools, `--allowedTools`
preapproval, and bypass modes are absent. A `--` separator terminates Claude's
variadic tool list before the positional work prompt.
The child cwd is the ruled `/tmp/floati-work/<work-id>` mapping. The
adapter never reads, copies, or injects credentials, never uses a shell, and
never enables bypass permissions. Permission-shaped headless failure becomes
`approval_required_unattended`; malformed, oversized, provider-failed, timeout,
and process-death paths remain distinct typed degradations. The existing runner
still owns TTL clipping, process-group termination, Git finalization, and
durable `claim -> spawn -> drive -> bind_artifact -> complete` receipts.

Gateway v0 is a local, dark contract surface. An explicit non-symlink config
permits only `stdio`, `network: disabled`, one absolute workspace root, and
`forward_fail_closed`. `LocalGatewayV0` appends session ingress, sorted
capability declarations, and unresolved approval-forward records to
`gateway/events.jsonl`. Capability declaration requires durable ingress;
approval forwarding requires both the target capability and
`approval.forward`. Forwarding never grants approval, creates authority, writes
the work consumption coordinate, launches a process, or opens a transport.

Doctor accepts gateway configuration only through `--gateway-config PATH`.
It performs no cwd, home, or environment discovery and creates no gateway
workspace. Valid local configuration is an `ok` finding; missing, symlinked,
malformed, remote, or network-enabled configuration is a typed configuration
refusal. As with every doctor family, remediation is emitted only when source
currency prerequisites are established.

The installer-shadow family scans the caller's PATH, exactly as supplied, for
entries that would shadow the installed launcher. The verdict is only as
complete as that PATH: when it omits the install scripts directory, or an
entry cannot be read, the finding is `unknown` with `blocked_entry` naming
what was not scanned. A partial scan is never promoted to `affirmative_none`;
no-shadowing is claimed only when every entry was actually read.

Publication preparation is documentation and read-only proof only.
`docs/PUBLICATION-CHECKLIST.md` names private brief deletion, ruling-request
archive disposition, the license decision hook, final README, contribution
stub, and specification set. `docs/SPEC-DRAFT.md` describes the implemented
serial-bus layer 1 but remains visibly DRAFT. The scrub surface checks both the
current generated tree and Git commit messages/Git notes for the private source
pattern. Nothing in HM-3 publishes the repository or weakens the HM-4 remote
fence.

## Stable reasoning versus dated status

The preceding sections are design reasoning and protocol contracts. The
following claims are dated observations; re-run their commands instead of
copying them forward.

### Dated status claims — 2026-07-31

- Branch base is `d0ebbe62389c1d915d896900c610ea8da01dbcc0`.
  Re-derive: `git merge-base lane/hm0 main && git rev-parse main`.
- Canonical implementation ref is `refs/heads/lane/hm0`.
  Re-derive: `git symbolic-ref HEAD`.
- Full local selftest status is not claimed by this design text; the evidence
  document records the fresh result and SHA.
  Re-derive: `python3 -m floati.selftest`.
- Hosted CI and deployed-bundle state are not established by a local run.
  Re-derive hosted CI from the repository check result and deployment from the
  installed bundle's own manifest verification.
