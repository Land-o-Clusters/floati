# Agent operations — the long-form rules behind AGENTS.md

Moved verbatim from `AGENTS.md` on 2026-09-04 (agent-instruction-files overhaul).
`AGENTS.md` keeps the binding one-line form of each rule; this file keeps the full
verb reference, the incident history, measured examples, and remedies unchanged.
Every governance entry below was paid for by a real multi-agent incident.

Completed `status` and `watch` queries return exit 0; their installer observations
remain nested evidence and do not become the command's exit status. `doctor`
reports incomplete installer observation as degraded (35). Rendering failures
still use cannot_speak (22).

## Install — harness wake hooks (from AGENTS.md)

**Relaunch quirk (measured):** a harness session that was already running
when a wake hook was installed will never run it — harnesses snapshot or
trust-gate hooks. After installing any wake component: tell the user to
review/trust/enable the hook in the harness UI, then relaunch the session.
Never claim a pre-install session is reachable.
`wake arm` has two installer-created prerequisites - the waiter workspace
binding and Codex-wait consent - and refuses, fail-closed, without them.

**GUI hook saves shadow working hooks, silently (measured 2026-08-30).** A
harness settings UI will happily save a hook it cannot run, and that saved hook
**wins over a registration that was working**. The measured shape: a settings-UI
save wrote a *Process*-type hook whose command field held the entire argv line -
so the path contained a space and could never exec - with **no validation and no
logged error**, silently shadowing the *Shell*-type registration that had
dispatched six times in the preceding ten minutes. Sixty-three minutes of
silence followed, and nothing anywhere said why.

So, for any harness with a hook UI:

- **Prefer the registration type the harness actually execs.** Where a UI offers
  a *Process* form and a *Shell* form, the Process form takes an argv **array**
  and will not exec a single string holding the whole command line; the Shell
  form runs the line through a shell and survives a path with a space. Putting
  a whole argv line into the Process form is the failure above.
- **Never treat "it appears in the UI" as installed.** The UI shows what was
  saved, not what can run.
- **After ANY save through a GUI - including one the user made, not you - prove
  the hook FIRES.** Not that it is listed, not that its bytes look right: that a
  real turn-end produced a real dispatch. A hook is bytes on disk; an armed hook
  is one you have watched fire.
- **A GUI save is an event you did not observe.** If wake goes quiet, re-verify
  the registration before diagnosing anything downstream - the harness will not
  tell you it was replaced.

**★ A CONFIGURATION SURFACE THAT ACCEPTS WITHOUT VALIDATING MANUFACTURES A
REGISTRATION THAT LOOKS CONFIGURED AND CANNOT RUN** - and because it looks
configured, every downstream diagnosis starts in the wrong place.

## Verbs — full reference (from AGENTS.md)

### Creating work for a persistent ad-hoc worker

After creating a profile and explicitly granting its launch capability with
`worker adhoc profile --grant-launch`, bind work to that profile through the CLI:

```text
floati work add --root /absolute/fleet --title 'native task' --owner builder-a --adhoc-profile adhoc-profile-...
```

Use the returned work ID as `work_item_id` in the coordinator's dispatch JSON.
Bank that JSON in the profile's repository and send its exact SHA and document
through the coordinator's ordinary `send` command. Then run:

```text
floati worker adhoc run --root /absolute/fleet --as builder-a --session parent-session --profile adhoc-profile-... --envelope msg-... --idempotency-key dispatch-1
```

`--adhoc-profile` derives the existing borrowed workspace, lane and lease binding;
it cannot be combined with `--workspace`. It does not create authority or launch
approval. A stale lease, replaced profile binding or foreign owner still refuses.
Admission also continues to accept an exact existing CAR row/repository/branch
binding.

`adhoc_dispatch_authentication_failed` identifies work ownership/binding failures
separately from inactive or mismatched authority. For work failures, the remedy
creates replacement bound work. The coordinator must bank and send a new dispatch
using its returned ID; the old work and Git dispatch cannot be edited in place.
For authority failures, the named active architect runs the exact `floati grant`
remedy. A missing grant starts at epoch 1; expired or revoked grants need the next
epoch. If the remedy names a different epoch from the dispatch, the coordinator
must bank and send a replacement dispatch for that epoch before running it.
An already-current grant is reused rather than renewed. No remedy bypasses the
separate human launch grant; a changed authority coordinate needs its own launch
approval. If there is no active architect, resolve the named role refusal first.


Every durable verb requires an explicit absolute `--root`; there is no default
root, no home scan, and no discovery. `COMMAND --help` prints the full
contract for each.

- **Bootstrap:** `init` (create/validate one direct-home fleet root;
  `--solo NODE --harness H` for one-seat setup) · `register` · `retire`
  (caller-selected NODE; self-registration and self-retirement are operating
  conventions, with no binding to the calling seat).
- **Nodes and roles:** `node {add|retire|drain|switch|role|boot|teardown|explain|state-flush}`
  (preview-first; temporary nodes take `--lease-minutes`) · `role {list|show|new|edit|validate|import}`.
- **Mail:** `send --root --from --to --repo --sha SHA --doc PATH --note TEXT
  [--reply-to ID] [--message-class {normal,fyi}] [--idempotency-key KEY]` · `inbox --session SESSION`
  (ack-on-drain by default; `--peek` is explicit) · `ack` (repeat `--id` for one
  exact batch) · `consume ID --root ROOT --as NODE --session SESSION
  --idempotency-key KEY` (explicit ACTED after ACK) · `sent` (read-only sender
  receipt projection). Delivery, acknowledgment, and consumption are separate
  receipts; `status: ok` from `send` proves the
  append, never the delivery.
  `--message-class fyi` appends non-waking mail: inbox or the next Stop
  presents it, but an already-idle Stop waiter and the wake daemon leave it
  pending. The board labels it `PENDING-AT-PAUSE`. An omitted class means
  `normal`; historical envelopes remain valid with their original wake behavior.
  A new send journals `recipient_readiness` **as observed at** its `observed_at`,
  in one event batch with the envelope. It checks the administrative lease,
  native Stop-hold testimony and recent completed board. One proven arrangement
  gives `recipient_wake_arranged`; all three affirmative absences give
  `recipient_not_listening`; unreadable or missing provenance gives `unknown`.
  The observation is historical testimony, not a promise through append or a
  proof of a running process. A later pause or retirement can change the next
  send's observation without rewriting this one. Exact retries return the
  journaled observation; old envelopes without it return historical `unknown`
  with no invented mechanism checks.
- **Truth surfaces:** `describe --json` · `verify` · `journal {checkpoint|verify}` ·
  `signature {sign|verify}` · `status` · `snapshot` (consented maintainer bundle) ·
  `log` (`--replay` reconstructs from the
  ledger) · `effects` / `effect` · `threads` / `thread` · `graph` · `plan` ·
  `receipts` · `board` (TUI).
- **Overlap evidence:** `overlap report --repository REPOSITORY --base-ref REF
  --left-ref REF --right-ref REF` emits one read-only, local schema-v1 overlap fact.
- **Repair:** `repair quarantine` preserves one exact selected event frame beneath
  the tenant, atomically replaces the ledger, and receipts follower invalidation.
- **Health:** `doctor` (per-node delivery scoreboard; `--probe` loopback
  deafness probe) · `watch`.
- **Presence:** `presence report --root ROOT --as NODE --ttl-seconds N`
  records only the acting node's own bounded report · `presence show --root
  ROOT` lists what each active node last reported and when; expiry is never
  translated into "down".
- **Epoch lifecycle:** `epoch roll --root ROOT --as NODE --idempotency-key KEY`
  performs one authority-gated coherent roll of the selected event, delivery,
  and acknowledgment planes. Ordinary native send, inbox, ACK and Stop activity
  also maintains due epochs as the active registered node, without a separate
  manual roll grant. The epoch defaults are 8 MiB of active events or seven
  days from the current epoch's first frame; `state/ledger-policy.json` may
  declare the existing positive `max_bytes` and `max_age_days` values. Daemon
  lifecycle receipts retain their separate thirty-day default. Automatic rolls
  carry pending messages and their native proof; an unchanged nonreclaimable
  carried set does not produce repeated archives. Recovery precedes another
  automatic roll and refuses differing live history. Existing exact uncompressed
  archives remain retained; this feature performs no compression or expiry.
  The epoch lock is fair to exclusive callers: while one waits, new shared
  callers queue behind it for up to half their own wait budget, so an
  exclusive wait ends when the shared work already in front of it does.
- **Confluence:** `confluence {grant|revoke|status|bundle}` — the read
  seam for a consuming observer app: one explicit per-root, per-consumer
  read grant; the bundle materializes the receipts-read surface under the
  grant it was produced under. No discovery, no watcher, no network, no
  mutation API. Existing receipts-read bundle versions 1 and 2 retain their
  closed allowlists and omit `receipts/consumed/` and `consumption_receipt`.
  Consumption is available through the native receipt history; exporting it
  would require a separately versioned bundle contract. This omission does
  not expand their supported historical record generations: current
  `delivered_unpresented` records remain outside both closed bundle schemas,
  and actor-bound version-one ACKs remain outside the version-one bundle.
- **Work:** `grant` / `grant revoke` · `work` · `worker` · `sequencer`
  · `supervise` · `orchestrate`.
- **Intake:** `intake {scan|adopt|show}` reads or adopts explicitly supplied
  local Markdown; adoption and outbound issue operations remain unavailable to
  agent sessions.
- **Agent transport:** `mcp serve --root ROOT --as NODE --session SESSION`
  binds one local stdio server to one exact active node and session.
- **Context:** `context` projects or records bounded Tide context evidence.
- **Quota:** `quota {collect|show}` records or inspects citation-bound local
  quota testimony without discovering provider surfaces.
- **Wake control:** `wake {pause|resume|status} --root ROOT --as NODE
  --session SESSION` — exactly one session per invocation; global and
  wildcard selectors do not exist. Marker-only and receipted; hook
  registration is never edited. A paused session is recorded state, not
  absence or deafness; `wake status` names what it cannot see (the running
  session's cache, the harness trust gate) and reports the wake-daemon breaker
  from the runtime (open or closed, threshold, last trip reason). A coordinate
  is underivable with reason runtime_missing, runtime_symlink, or
  runtime_malformed. Nothing on that verb resets the breaker; only a consent
  re-grant does.
- **Codex Stop waiter:** `scripts/floati-codex-wait --root ROOT` is the
  documented installed entrypoint. It derives node, workspace, and acting
  session only from the validated hook payload; those identities are not
  caller-selectable flags.
- **Seat boarding:** `seat board --root ROOT --as NODE --workspace PATH
  --idempotency-key KEY --session SESSION [--take-over]` composes claim,
  resume, and drain in that order. See the boarding contract below.
- **Cartography:** `chart --declared-roots FILE` (only explicitly declared
  roots) · `chart add-root` / `chart remove-root` (rewrite the declared-roots
  file) · `survey` (user-invoked, read-only report of buses floati did not
  install — it never writes, drains, acks, registers, or locks a foreign
  bus). The node-add wizard offers that survey inline when it detects an
  undeclared bus in the parent of the live root, and asks before adopting.
- **Lifecycle:** `install` · `update` · `update rollback --to SHA` · `uninstall` (see Install above) · `purge`
  (moves only explicitly named roots to Trash; never deletes).

After updating an installation, prepare each existing fleet root before its
first send. Run `floati update prepare --root /absolute/fleet` until its
`state` is `done`. Each call validates up to 4,096 frames or 2 MiB of
history, finishing the current frame. The result reports `validated_bytes`,
`source_bytes` and `frames`. To check progress, add `--status`. After an
interruption, repeat the same command to resume.

A send can proceed while preparation is `pending` or `in_progress`. On a
long history it reports the native validation on stderr before reading the
ledger. Historical matches can require another validation until preparation
finishes.

**Managed wrappers:** a harness seat provisioned with a managed bus profile
sends through its wrapper binary, which pins root/from/repo and takes exactly
`<wrapper> <profile> send --to NODE --sha SHA --doc PATH --idempotency-key KEY
--note TEXT [--reply-to ID]`. Its acknowledgment shape is `<wrapper> <profile>
ack --id MSG_ID [--id MSG_ID ...] --session SESSION_ID`; every id is explicit
and the acting session is required. A seat's own boot projection (`node boot`)
prints its exact wrapper shapes — use those verbatim, never a remembered
shape.

## Preparing and removing one Codex seat

`seat add` takes the existing fleet root, committed source repository, an absent
home, exact gateway registry and Codex config files, installed transport name,
coordinator and repository label. It creates `home/repo`, `home/wt` and
`home/scratch`; adds a gateway profile and project trust entry; registers its
suggested unused node; arms the exact workspace waiter; and publishes the seat
declaration. Existing fleet governance and the reviewed global Codex Stop hook
are prerequisites. This verb does not install or retrust a global hook.
Before ownership writes and on completed-add replay, it checks the selected
transport's canonical ordinary interpreter and install, exact manifest digest
and source pin, bounded file hashes and required client entries. Missing or
changed installed bytes refuse; removal remains available to reverse the owned
seat when its transport is broken. These static checks do not prove a live
waiter or the first real Stop-hook fire.
Seat add/remove also require Python 3.11 or newer for standard-library TOML
validation, or optional `tomli` on Python 3.9/3.10; otherwise they refuse before
configuration writes. The package's runtime dependency list remains empty.

```text
floati seat add --root /absolute/fleet --home /absolute/seats/new-seat --source /absolute/source --gateway-config /absolute/codex/fleet-bus-profiles.json --codex-config /absolute/codex/config.toml --transport installed --coordinator architect --repo project --mode desktop
floati seat remove --root /absolute/fleet --home /absolute/seats/new-seat
```

The desktop artifact says **open Codex on `home/repo` and paste this**, followed
by its boot prompt. Opening the parent home instead does not bind the gateway
to a Git repository. `--mode cli --codex-executable /absolute/codex` uses the
explicit CLI's stdio app-server to create a saved thread without starting a
model turn. The creator reads its thread to materialize native storage before
it closes and is collected. A named unsupported history read is retained as
absence in the transcript; it is not persistence proof. An independent provider
must resume the exact stored ID with history hydration excluded, then close
and be collected too. That resume proves stored availability; no-turn evidence
comes from the new creation and the closed startup protocol, not a history scan.
The artifact
records the native Floati pause receipt and prints the exact CLI resume command
and boot prompt. Both provider connections and the resume command bind
`CODEX_HOME` to the declared `config.toml` directory. No starter child remains
running. On the first seat turn,
boarding claims and resumes that exact session through the normal wake path.
An app-server version that cannot resume the saved empty thread refuses rather
than reporting a usable seat. Thread creation,
workspace mapping and waiter consent are distinct from proving that a fresh
Stop hook actually fires; the result reports hearing and live waiter as
unproven until that later observation. A compiled source package also requires
its committed executable `--warm-build-script scripts/example.sh`; output goes
to the owned `scratch/warm-build.log`, with build files confined to `repo/.build`.
CLI startup owns its two logs under `scratch`; the boot prompt does not authorize
workspace edits. Those output paths are recorded before launch and their exact
contents recorded after the child stops, including a failed launch or build.
Removal can therefore reverse completed child writes while preserving unexpected
files. An interrupted child whose output capture was not durably finalized
requires inspection and refuses destructive removal.

Each configuration replacement has a private backup and byte/mode validation.
The durable ownership record under the named fleet root records transaction
intent before mutation and the home inventory before publication. Repeating
an unchanged completed add replays its artifact after verifying its current
capabilities. `seat remove` checks the home inventory and incarnation, removes
the exact profile/trust/map capabilities and its owned CLI pause marker,
disarms consent, retires registration
and deletes the owned home. Its before/after inventory proves that reversal.
Unrelated settings are preserved. Edited homes, uncertain staging directories
and replaced capabilities produce typed refusals that preserve foreign data.
Native receipts, private backups and retired registry/consent history remain
auditable. Interrupted removal resumes from its durable progress record.

`--rehome --node EXISTING --profile EXISTING` explicitly reuses one prior
Codex identity. It replaces that profile, removes its old trust entry and
waiter mappings, and archives a missing-executable adapter only when there is
no runtime evidence. It never starts or repairs a daemon. The full prior-mail
cut is saved as historical evidence and receives exact delivery and SEEN
receipts in bounded batches. Messages arriving after that cut remain pending.
The previous unowned home and its contents are retained.

## Seat boarding — an explicit turnover operation

A seat that has never boarded — registered before the governed path existed, so the
workspace carries no `SEAT.json` — migrates ONCE, by verb, before its first board
(`doctor` prints this exact command as its `seat_declaration_absent` remedy):

```sh
floati seat migrate --root /absolute/fleet --as NODE --workspace /absolute/workspace --idempotency-key KEY
```

The migration publishes fleet governance (when the root has none, with the ruled
values; the coordinator derives from the registry's active architect role, else pass
`--coordinator`) and the workspace declaration, from the registry's own records, with
a receipt. It runs once per registered workspace; every later turnover is the board
below, in one step.

```sh
floati seat board --root /absolute/fleet --as NODE --workspace /absolute/workspace --idempotency-key KEY --session SESSION
```

The workspace must carry a matching seat declaration and an installed,
consented Codex waiter binding. The product CLI requires an explicit
`--session`; it never reads the runtime environment for identity. Health
notices require the caller to supply that declaration alongside their exact
root/node/workspace command. No session discovery or hook installation occurs.

Codex daemon bindings retain the fixed `/opt/homebrew/bin/codex` declaration
and the SHA-256 of its ordinary resolved target. Every read and wake verifies
the target digest; the wake executes that verified canonical target. Moving
identical bytes to a new package version keeps the binding usable. Changed
bytes require the registered trusted Codex Stop waiter to refresh the binding
before retrying boarding. A missing legacy version path prints the exact
`floati wake daemon bind` recovery command. Run that command after reviewing
the replacement, then retry boarding with the same key. The Codex bind route
only refreshes an existing binding for its declared workspace and epoch,
with armed consent and the current waiter
claim. It does not create a binding or take over a session. Generic executable
symlinks remain refused; the fixed declaration alone is not executable trust.

No claim or a claim already belonging to that session can proceed. Any other
recorded claimant requires explicit `--take-over`; the refusal names it.
Receipt age never proves that a process is gone. Takeover displaces exactly
one predecessor and is checked within the claim transaction.

Boarding receipts each stage: claim, resume (including an already-active
no-op), and exact inbox delivery/acknowledgment. A completed key replays only
its recorded batch, including an empty batch. It does not consume new mail.
If a crash leaves a started drain without completion, the same key refuses
with uncertainty instead of draining again. Inspect the named delivery and
acknowledgment ledgers before choosing another key. A receipt proves these
steps; it does not prove a future Stop hook fired.

The vendored Codex gateway supports `<wrapper> <profile> board` only when
the registered profile explicitly permits it. It derives root, node,
workspace, and a stable per-session key, and never adds `--take-over`.
Repeated no-argument calls in the same session replay that boarding key.
Shipping this source does not install a gateway or alter an existing profile.

## Install — launcher interpreter resolution (from AGENTS.md)

**The launcher never resolves its interpreter through `PATH`.**
`scripts/floati` walks one fixed candidate list - `/usr/bin/python3`, then
`/bin/python3` - and takes the first that is present and executable. An
operator overrides that with `FLOATI_PYTHON`, which must name **one absolute
canonical interpreter path**; a symlink is refused, so name the resolved
target. With neither a declaration nor a candidate the launcher refuses,
typed, **exit 20** - it never falls back to `PATH`, because a `PATH` an
attacker can prepend to would choose the Python that runs the whole product.
`floati doctor` reports the outcome as the `launcher_interpreter` finding,
naming the interpreter and whether it was `declared`, a `candidate`, or
`absent` (the process was not started through the launcher).

## Fleet governance — message and ack hygiene

Every rule here was paid for by a real multi-agent incident. They are how a fleet stays
coherent when nobody is watching every window.

- **An ack means SEEN. Nothing more.** Not agreement, not action, not promise.
  Disagreement is a reply; work is a work receipt. Withholding an ack to signal
  displeasure is a defect because it manufactures ghost attention and poisons
  the doctor's numbers. The default inbox drain acknowledges exactly what it
  returns; use `--peek` only for an explicit process-before-ack workflow.

- **An ask that needs a reply is an envelope, not a chat line.** A question typed into
  your own session — "approve this design?", "should I proceed?" — is invisible to every
  other node. If you are waiting on someone, they must be able to see the wait: send it on
  the bus, then keep working on what does not depend on the answer.
- **`status: ok` from `send` proves the append, never the delivery.** Delivery is the
  recipient's receipt; acknowledgment is a third thing. Never report "I told X" on the
  strength of your own send result.
- **Ack on seeing; record action separately.** The default drain records SEEN in
  the same guarded operation. If a workflow explicitly peeks first, acknowledge
  the exact reviewed batch before continuing; replies and work receipts carry
  disagreement, action, and completion.
- **Consumption is an explicit ACTED receipt.** `consume ID --root ROOT --as
  NODE --session SESSION --idempotency-key KEY` requires the recipient's prior
  ACK for that exact envelope; it never authors an ACK automatically. A valid
  predecessor chat's ACK may support the act, while the consumption receipt
  records the current acting session. Repeating the recipient and envelope
  replays the original receipt. `work complete --consume ID --session SESSION`
  accepts 1–1000 unique envelopes and validates the entire batch before writing.
  It writes consumption before completion. Consumption without completion is
  a legal durable state; retry finishes without duplicate consumption. Omitted
  `--consume` retains the existing completion behavior. Delivery, ACK, boarding,
  and bare completion never synthesize consumption.
- **Historical progress can be unknown.** A cross-ledger gap in one node's
  historical evidence reports `message_progress.state: unknown`, its stable
  `reason_code`, and null counts for that node. Status, board observation and
  receipt history remain usable; raw receipt history is retained. Unknown is
  never displayed as measured zero. Consumption authoring still validates its
  exact ACK/delivery evidence and refuses an invalid reference.
- **Answer with coordinates, not summaries.** A report that names an exact commit, file,
  and count can be independently verified; "done" cannot. Copy SHAs by command
  substitution — never retype one by hand.
- **Say what you did NOT touch.** A receipt that names its non-touches ("manifest, schemas,
  public remote untouched") is worth more than one that only names its work, because the
  reader's next question is always "what else moved?"
- **Silence is not evidence.** An empty inbox answer is only meaningful if it names the
  root and node it drained. If a tool's answer does not say where it looked, you do not
  know what its silence means — say which root you drained when you report it.
- **Your identity comes from your workspace, never from memory.** If your working
  directory declares a seat (a `SEAT.md` or marker), it outranks anything you remember
  from other sessions — including procedures. A remembered command that does not name a
  root belongs to another fleet: stop and ask.
- **A reassignment is a dispatch.** If ownership of a task changes, the new owner and the
  old one both get an envelope. Work reassigned in silence gets done twice or not at all.
- **Do not take a claimed task; do not assume an unclaimed one is blocked.** Ask both
  "is someone on it?" and "is it already done?" — of the mail as well as the log.
- **A refusal that names its contract is an instruction; a refusal that names a policy is
  a stop.** When the body says the invocation is malformed and prints the required shape,
  apply that exact shape once — that is following the refusal, not guessing. When it names
  a missing approval, consent, or identity, stop and surface it verbatim. Never retry a
  refusal unchanged, and never retry a policy refusal at all.
- **A wrapper's contract can be stricter than the tool it wraps.** Where a governed
  gateway requires an argument the bare CLI calls optional, the gateway is the contract
  for that seat. Read the shape where you were told to invoke it; the wrapper's doc names
  every difference on purpose.
- **Turnover is one verb per thread. `/boardbus` is the only command a new thread
needs. Every projected operator boarding command selects `--take-over` explicitly;
`/takeover` remains the standalone spelling. Successful boarding presents the
verified artifact's `takeover_report`: `TOOK OVER <node> from <session or none>`.
A replay retains historical displacement but reports no new displacement and
does not drain or acknowledge again. The unflagged native verb keeps its false
default, and a contested call prints:
**Another thread still holds this seat** — `<claimant session>`, bound `<time>`.
If that thread is closed or abandoned, run `/takeover`.

| situation | what the user runs | what happens |
| --- | --- | --- |
| first time on a seat (registered, never boarded) | `/boardbus` | migrates once (`seat migrate`, the coordinator derived or the ruled `--coordinator`), then boards |
| a new thread on the same seat, the old thread CLOSED | `/boardbus` | explicitly takes over the prior claim and reports its session |
| a new thread while the old thread is STILL OPEN | `/boardbus` | explicitly takes over the live predecessor, receipted and reported |
| the Stop hook untrusted (after install or update) | `/boardbus` stops with BL-11's banner | trust once in Codex, relaunch, `/boardbus` |

Boarding order is fixed: attach, take over the wake claim, then drain.** A session
  that drains before it arms answers its mail once and goes deaf to everything after.
  Turnover is the designed case — takeover replaces exactly one predecessor's authority
  and never multiplies waiters. If attach reports hook trust pending, the one-time
  operator trust act comes before anything else relies on the wake path.

## Topology and authority — decided at fleet setup, never assumed

Most fleet seats run with NO human watching. Design every behavior for that fact:

- **Do not wait for operator approval that will never come.** A question typed to "the
  user" from an unmonitored seat is an infinite wait. Route decisions to the fleet's
  DECLARED authority (below) as an envelope, and keep working on what does not depend on
  the answer. If nothing depends on it, proceed within your granted authority and record
  what you decided.
- **Declare the topology at setup.** A fleet picks its shape deliberately — commonly a
  STAR: one coordinator/architect node that dispatches work, gates results, and owns
  cross-seat decisions, with worker seats that never re-task each other directly. Meshes
  are possible; unowned decisions are not. Record the choice where every seat reads it
  (the workspace seat declaration and this file's local equivalent).
- **Decide how much authority the coordinator gets, in writing:** what it may dispatch,
  what it gates before merge, what it may decide alone, and what stays OWNER-TIER — the
  human's list (money, publishing, credentials, key custody, anything irreversible).
  A seat that hits an owner-tier question parks it as an envelope to the coordinator and
  moves on; the coordinator holds it for the human.
- **Authority is per-fleet, never inherited.** Being coordinator on one bus grants nothing
  on another. Cross-fleet actions from a seat are refused by default (see the identity
  rules above).

## Troubleshooting — safe fixes and breaking fixes

Every entry below comes from a real fleet incident. The pattern to internalize: **safe
fixes are reads and governed verbs; breaking fixes are raw file edits, wrapper bypasses,
and identity guesses.** When a refusal names no remedy, stop and envelope your fleet's
coordinator — do not improvise on shared state.

- **`malformed_evidence` on a record kind you don't recognize** — usually VERSION SKEW,
  not corruption: your installed floati is older than the ledger's vocabulary.
  Safe: update the reading installation via the governed `update` verb.
  Breaking: editing the ledger, deleting records, retry loops.
- **`APPROVAL_REQUIRED: manifest differs from the pinned digest`** — your transport's
  bytes changed. That may be a legitimate update or tampering; the fence cannot know.
  Safe: check the install's wiring journal for a governed update receipt, then perform
  the approval act your fleet documents. Breaking: bypassing the wrapper or pointing at
  a different floati binary.
- **Empty inbox when you expected mail** — read which ROOT the answer names before
  concluding anything; a true "empty" about the wrong root is the classic cross-fleet
  trap. Safe: check your workspace seat declaration. Breaking: draining a root you
  merely remember.
- **`send` returned ok but nothing happened** — ok proves the append, never the
  delivery. Safe: `doctor` for the recipient's lease/registration and unread-mail age.
  Breaking: resend loops.
- **Waiters/wakes went quiet after a repair, restore, or rotation** — a replaced ledger
  file has a new identity; watchers must re-arm. Safe: restart your watcher; check for
  a repair notice. Breaking: assuming the fleet went idle.

**Know your harness's healthy idle shape.** On Codex, HEALTHY is that your turn does
NOT visibly end — the Stop waiter holds it open, watching the bus (the status line says
so). **A turn that ends promptly is itself a diagnostic flag**: stale wake claim, tripped
breaker, pause marker, or a real exhaustion — check `doctor` before assuming quiet.

- **Your turn ends instantly instead of waiting for mail** — your seat's wake claim is
  probably still armed to a PREVIOUS session (turnover without re-arm). Safe: run
  `floati wake arm --root ROOT --as NODE --session YOUR_SESSION --workspace PATH --idempotency-key KEY` —
  a live predecessor requires `--take-over`; a paused claim is adoptable without it.
  Do it at every session turnover. Breaking: assuming the hook is broken and disabling it.


## Root-local role authoring

`role list` and `role show ROLE` read the selected root's custom roles together
with the immutable shipped library. All role consumers use the same validator.
Custom files live at `roles/custom/NAME.json`; shipped names are reserved and
cannot be shadowed, imported, or edited.

```text
floati role new --root ROOT --name specialist --from builder --idempotency-key create-specialist
floati role edit --root ROOT --name specialist --set cadence=on-demand --idempotency-key edit-specialist
floati role validate --root ROOT --from local-template.json
floati role import --root ROOT --from local-template.json --idempotency-key import-specialist
```

`new --from` names an existing role to clone. `import --from` and `validate --from`
read one explicit local JSON file; validation is read-only. `edit` accepts either
repeatable `--set FIELD=VALUE` or one `--from PATH`, never both. Values may be JSON
or a bare string; duplicate fields, malformed structured JSON, and schema errors
are refused with a field name. No editor is launched and no template version is
incremented automatically. An edit cannot rename the role.

Each write records digest-bound v1 prepared/applied receipts in
`receipts/role-templates.jsonl`. Repeating the same key and inputs returns the
original completed receipt, even after a later edit. Reusing a key with different
inputs refuses. An unfinished write blocks other writes to that role; repeat its
original request to recover. Recovery verifies the prepared digest and syncs the
file and directory ancestry before recording applied. Foreign replacement bytes
are preserved and refused. A durability failure means publication is uncertain,
not that nothing changed; retain its receipt and repeat the exact request.
Receipt paths are root-relative; source paths and template bodies are not copied
into receipts. Existing v0 role-assignment records retain their original shape.
