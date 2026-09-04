# Agent operations — the long-form rules behind AGENTS.md

Moved verbatim from `AGENTS.md` on 2026-09-04 (agent-instruction-files overhaul).
`AGENTS.md` keeps the binding one-line form of each rule; this file keeps the full
verb reference, the incident history, measured examples, and remedies unchanged.
Every governance entry below was paid for by a real multi-agent incident.

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


Every durable verb requires an explicit absolute `--root`; there is no default
root, no home scan, and no discovery. `COMMAND --help` prints the full
contract for each.

- **Bootstrap:** `init` (create/validate one direct-home fleet root;
  `--solo NODE --harness H` for one-seat setup) · `register` · `retire`
  (self-retirement only).
- **Nodes and roles:** `node {add|retire|switch|role|boot|teardown|explain|state-flush}`
  (preview-first; temporary nodes take `--lease-minutes`) · `role {list|show}`.
- **Mail:** `send --root --from --to --repo --sha SHA --doc PATH --note TEXT
  [--reply-to ID] [--idempotency-key KEY]` · `inbox --session SESSION`
  (ack-on-drain by default; `--peek` is explicit) · `ack` (repeat `--id` for one
  exact batch) · `sent` (read-only sender receipt projection). Delivery and
  acknowledgment are separate receipts; `status: ok` from `send` proves the
  append, never the delivery.
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
  and acknowledgment planes.
- **Confluence:** `confluence {grant|revoke|status|bundle}` — the read
  seam for a consuming observer app: one explicit per-root, per-consumer
  read grant; the bundle materializes the receipts-read surface under the
  grant it was produced under. No discovery, no watcher, no network, no
  mutation API.
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
  session's cache, the harness trust gate).
- **Codex Stop waiter:** `scripts/floati-codex-wait --root ROOT` is the
  documented installed entrypoint. It derives node, workspace, and acting
  session only from the validated hook payload; those identities are not
  caller-selectable flags.
- **Cartography:** `chart --declared-roots FILE` (only explicitly declared
  roots) · `survey` (user-invoked, read-only report of buses floati did not
  install — it never writes, drains, acks, registers, or locks a foreign
  bus).
- **Lifecycle:** `install` · `update` · `uninstall` (see Install above) · `purge`
  (moves only explicitly named roots to Trash; never deletes).

**Managed wrappers:** a harness seat provisioned with a managed bus profile
sends through its wrapper binary, which pins root/from/repo and takes exactly
`<wrapper> <profile> send --to NODE --sha SHA --doc PATH --idempotency-key KEY
--note TEXT [--reply-to ID]`. Its acknowledgment shape is `<wrapper> <profile>
ack --id MSG_ID [--id MSG_ID ...] --session SESSION_ID`; every id is explicit
and the acting session is required. A seat's own boot projection (`node boot`)
prints its exact wrapper shapes — use those verbatim, never a remembered
shape.

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
- **Boarding order is fixed: attach, take over the wake claim, then drain.** A session
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
  `floati wake arm --root ROOT --as NODE --session YOUR_SESSION --workspace PATH` —
  takeover is predecessor-bound and built for this. Do it at every session turnover.
  Breaking: assuming the hook is broken and disabling it.
