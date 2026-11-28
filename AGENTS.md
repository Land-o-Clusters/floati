# AGENTS.md — operating floati as an agent

Floati is built to be operated by agents as well as people: every verb emits
one JSON artifact, every failure is a typed refusal with a named reason, every
durable action leaves a receipt, and repeated commands are idempotent under an
explicit key. You never have to guess whether your own action worked — read
the artifact and the receipt.

Floati itself makes zero model calls and sends no telemetry. Its four counted
outbound paths are explicit and fenced below; it is operable BY agents, but it
does not contain one.

## How to behave

- Precedence: the owner's live instruction > this file > a seat's `SEAT.md` or role
  projection > any skill or plugin prompt. If a skill or file makes you stop, ask, or
  deviate, name the file and quote the line that did it.
- Ask vs proceed: finish everything the request and the repository already authorise
  before asking anything. Ask only when a choice would materially change the result and
  nothing in the repo, a ruling, or the plan settles it. Do not add unsolicited warnings,
  disclaimers, or approval flows for hypothetical risk. Owner-tier always asks: money,
  publishing, credentials, key custody, anything irreversible outside the repo.
- Test scope: a reversible change runs the affected unittest modules, no mirror tests; a change
  to a refusal, receipt, ledger format, or the install manifest runs `python3 -m unittest discover`.
- Use subagents as needed for independent work; write briefs a human can read.
- Review bar for changes to this repository: the test scope above passed; after any change to
  command registration or help metadata, `python3 -m floati.command_codegen --check` is clean;
  every new refusal names its remedy; the report gives the exact commit, file, and count.

## Install

**Test runner:** use Python's `unittest`. In a development worktree,
`scripts/agent-bootstrap.sh` provisions the pinned test environment and
`make doctor` checks its setup. See [the contributor guide](CONTRIBUTING.md)
for the worktree and managed test commands.
`pyproject.toml` is the package-metadata authority: Python 3.9 or newer,
with zero third-party runtime dependencies. Full verification additionally needs
Minisign, Pillow and jsonschema; see `CONTRIBUTING.md` for setup.

**On Linux, run the suite under `umask 022`, on a box where no other user holds
`<temp>/floati-work`.** The worker-isolation check refuses a group-writable package
tree, and Ubuntu's default umask (002) makes a fresh clone one. The work root is the
fixed path `<temp>/floati-work` (`floati/host_paths.py`), created `0700` by whichever
user runs first; if another user — a CI runner included — already holds it, this
user's runs are refused with `PermissionError`. Measured on Ubuntu 24.04, 2026-09-11.

```
git clone <this repository> /absolute/path/floati-src
cd /absolute/path/floati-src
python3 -m floati install --source /absolute/path/floati-src --destination /absolute/path/floati
```

Install writes only the exact files named by the source bundle manifest and
emits SHA-256 receipts. `floati update` refreshes the same destination the
same way; `floati uninstall --destination PATH [--dry-run]` removes only
unchanged manifest-owned tool files and always retains bus roots, ledgers,
and foreign files. Run the binary as `<destination>/scripts/floati` (or
`python3 -m floati` from a source checkout). `doctor`'s installer-shadow
check reads the installed destination from the `FLOATI_INSTALL_DESTINATION`
environment variable when `--destination` is not passed.

**The launcher never resolves its interpreter through `PATH`:** `scripts/floati` tries
`/usr/bin/python3` then `/bin/python3`; `FLOATI_PYTHON` overrides with one absolute canonical
path (symlinks refused); otherwise it refuses, typed, exit 20. Detail: `docs/AGENT-OPERATIONS.md`.

**Harness wake hooks** (incidents in `docs/AGENT-OPERATIONS.md`): a session already running
when a wake hook is installed never runs it, so have the user trust or enable the hook and
relaunch. `wake arm` refuses, fail-closed, without the waiter workspace binding and
Codex-wait consent. A GUI hook save can silently shadow a working registration ("it appears
in the UI" is not installed), so after ANY GUI save prove the hook FIRES with a real turn-end
dispatch. Prefer the registration type the harness actually execs (Shell form for a command
line; the Process form takes an argv array).

## The output contract

Every command prints exactly one JSON artifact on stdout:

```
{"artifact_version": 0, "command": "<verb>", "status": "<status>", "evidence": {...}}
```

`status` is `ok`, `refused`, `cannot_speak`, `intentional_silence`,
`no_result`, `malformed_evidence`, `orchestration_deadline`, or `degraded`. On refusal, `evidence.code`
is a stable machine-readable reason and `evidence.detail` says what to fix.
`status --json` and `graph --json` are the stable version-zero machine
contract (`docs/CONFLUENCE-v0.md`).

## Exit codes, with remedies

| exit | meaning | remedy |
|---:|---|---|
| 0 | done (`ok`); artifact holds the result | proceed |
| 20 | refused before any mutation (`status: refused`) | the request is wrong, not the system — fix the argument, identity, or missing consent named in `evidence.code`/`detail`; do not retry unchanged |
| 22 | `cannot_speak`: result exists but cannot be rendered safely for this terminal | re-run with `--json` |
| 31 | `intentional_silence` (nothing to say; e.g. a waiter with no participant) | clean no-op; do not treat as failure |
| 32 | `no_result`: query ran, nothing matched | treat as an empty set |
| 33 | `malformed_evidence`: durable evidence is malformed or inconsistent | stop; do not retry; report the named ledger for investigation |
| 34 | `orchestration_deadline`: the orchestrated run exceeded its deadline | re-run with a larger `--deadline` |
| 35 | `degraded`: the run completed but at least one check could not speak | read the artifact's findings; each names the check that degraded |

`status` and `watch` return exit 0 for a completed query even when nested
installer observations are incomplete; inspect that evidence for warnings.
`doctor` maps incomplete installer observation to `degraded`, exit 35.

## Verbs (contracts: `COMMAND --help`; the full reference is in `docs/AGENT-OPERATIONS.md`)

Every durable verb requires an explicit absolute `--root`; there is no default root, no
home scan, and no discovery. Node changes are preview-first. A seat's `node boot` prints its
exact managed wrapper shapes for send and ack; use those verbatim, never a remembered shape.

The command table below is generated from the live parser. Run
`python3 -m floati.command_codegen --write` after editing command registration or
reviewed help metadata; `--check` detects drift in this table and the static help.
Full syntax for every subcommand: `floati describe --json` or `COMMAND --help`.

<!-- BEGIN GENERATED COMMAND TABLE -->
| Command | Purpose |
| --- | --- |
| `describe` | project the live command contract |
| `setup` | set up one seat in a new local fleet |
| `cross` | manage local cross-machine opt-in |
| `overlap` | derive local overlap evidence |
| `lane` | open and close recorded lane workspaces |
| `sweep` | inspect recorded and unmanaged lane workspaces |
| `reclaim` | delete idle declared build output |
| `checkout-liveness` | check whether a checkout is in use |
| `init` | create a direct fleet home |
| `confluence` | the read seam for a consuming observer app |
| `register` | register one named node |
| `retire` | retire one named registry row |
| `journal` | exact-byte journal testimony |
| `repair` | govern explicit ledger repair |
| `signature` | Minisign artifact testimony |
| `ask` | ask a peer and wait for its reply, or answer a question |
| `replay` | read an ask or work receipt chain |
| `send` | append a Git notification |
| `verify` | reproduce a typed delivery claim |
| `inbox` | drain pending mail |
| `ack` | acknowledge presented messages |
| `sent` | project sender receipt state |
| `log` | read mail or replay orchestration evidence |
| `status` | summarize the fleet |
| `who` | show who is on the fleet |
| `snapshot` | build one consented maintainer support bundle |
| `effects` | list projected effect status |
| `effect` | inspect or operate one effect |
| `threads` | list registered thread observations |
| `thread` | operate one registered thread attachment |
| `graph` | render the Harbor Chart |
| `plan` | explain read-only plan admission |
| `doctor` | diagnose root and bundle integrity |
| `watch` | poll for fleet deltas |
| `wait` | hold a turn until a named condition |
| `waiter` | arm exact waiter consent for a declared workspace |
| `receipts` | inspect node receipt history |
| `consume` | record action on an acknowledged message |
| `supervise` | report fleet health |
| `presence` | declare fleet presence or report node liveness |
| `board` | open the TUI harbor board |
| `orchestrate` | seed and run a worker fleet |
| `sequencer` | manage the optional local writer |
| `work` | operate the orchestration log |
| `dispatch` | record and recover local dispatch evidence |
| `intake` | adopt bounded work-queue sources |
| `worker` | run one authority-checked worker |
| `mcp` | expose launch-bound agent tools |
| `install` | install the exact governed bundle |
| `update` | update the exact governed bundle |
| `epoch` | govern one whole bus epoch |
| `grant` | append exact work authority |
| `node` | node administration |
| `role` | inspect and author root-local role templates |
| `quota` | inspect or collect cited local quota testimony |
| `chart` | multi-bus Harbor Chart |
| `survey` | read-only foreign-bus survey |
| `seat` | create, remove, and board explicit seats |
| `hook` | install explicit native harness hooks |
| `wake` | control exact wake coordinates |
| `uninstall` | remove exact owned tool bytes |
| `net` | manage local fleet topology declarations |
| `context` | inspect context evidence and manage Tide signals |
| `purge` | move the exact roots you list into the account Trash; never deletes |
| `hold` | manage project holds and inbound mail holds |
| `needs` | raise and read what needs a person |
| `territory` | declare leased working paths and preview overlap |
| `quiet` | hold enrolled seats' builds and tests for one quiet window |
| `backup` | export one retained epoch archive |
<!-- END GENERATED COMMAND TABLE -->

## Standard workflows

- **Solo:** `init --root R --solo me --harness Codex` → `work`/`log`/`board`.
- **Fleet:** `init` → `node add` per seat → `send`/`inbox`/`ack` between seats → `board`.
- **Manual non-solo work authority:** add the architect first,
  assign its shipped role, add the holder, grant one exact coordinate, then
  add and claim work. The reverse uses the same coordinate and architect gate.

```text
floati init --root /var/tmp/fleet
floati node add --root /var/tmp/fleet --node architect-a --harness Codex --lifetime permanent
floati node role --root /var/tmp/fleet --node architect-a --template architect --answer repo=floati --answer never_touch=foreign-project --answer owner_stops=owner-tier
floati node add --root /var/tmp/fleet --node builder-a --harness Codex --lifetime permanent
floati grant --root /var/tmp/fleet --as architect-a --holder builder-a --subject work-claims --epoch 1
floati work add --root /var/tmp/fleet --title bounded-work --owner builder-a
floati work claim --root /var/tmp/fleet --id work-00000000000070008000000000000000 --as builder-a --authority-subject work-claims --authority-epoch 1
floati grant revoke --root /var/tmp/fleet --as architect-a --holder builder-a --subject work-claims --epoch 1
```

- **Health check:** `doctor --root R --source S` → chase every red with the receipt it
  names → `doctor --probe` for suspected deafness.
- **Map the estate:** declare roots in a file → `chart --declared-roots F` →
  `survey` when you suspect a bus you did not create.
- **Leave:** `wake pause` per session → `node retire`/`retire` →
  `uninstall --dry-run` → `uninstall`. Leaving is a first-class feature:
  every capability has its reverse, and user records outlive the tool.

## Fences an agent must respect

- Never scan the filesystem for roots; operate only on roots the user named.
- Never read, edit, or shadow another bus's registrations, markers, hooks,
  or state — coexistence, never modification. `survey` is the only lens on
  foreign buses and it is read-only; `purge` moves declared roots to Trash and never deletes.
- Never wake, or install wake machinery for, a session without the user's
  recorded consent — and never run a production waiter by hand against a
  live root; fixtures exist. Wake control is marker-only and never edits hook registration.
  A wake-daemon breaker coordinate in `wake status` can be underivable with reason
  `runtime_missing`, `runtime_symlink`, or `runtime_malformed`.
- Never treat a receipt as more than it says: an append is not a delivery,
  a delivery is not an acknowledgment, a poll is not a wake.
- Never invent a number floati did not measure. Absences are typed and
  cited, not filled in.
- Low-level `register` and `retire` do not bind NODE to the calling seat; use your own seat.

## Fleet governance — binding rules (detail and incidents: `docs/AGENT-OPERATIONS.md`)

- An ack means SEEN, nothing more. Disagreement is a reply; work is a work receipt. Never
  withhold an ack to signal anything; use `--peek` only for an explicit process-before-ack flow.
- A question typed into your own session is invisible to every other node. Put the wait on
  the bus as an envelope, then keep working on what does not depend on the answer.
- Answer with coordinates (exact commit, file, count), never summaries; copy SHAs by command
  substitution; say what you did NOT touch. Silence is evidence only when you name the root
  and node you drained.
- Your identity comes from your workspace seat declaration, never from memory. A remembered
  command that names no root belongs to another fleet: stop and ask.
- A reassignment is a dispatch: old and new owner both get an envelope. Do not take a claimed
  task; do not assume an unclaimed one is blocked; check the mail and the log.
- A refusal that names its contract is an instruction (apply the printed shape once); a
  refusal that names a policy is a stop. Never retry a refusal unchanged, never retry a
  policy refusal at all. A wrapper's contract can be stricter than the bare CLI; the wrapper
  is the contract for that seat.
- Boarding order is fixed: attach, take over the wake claim, then drain. Re-run
  `floati wake arm --root ROOT --as NODE --session SESSION --workspace PATH --idempotency-key KEY` at every
  session turnover; a live predecessor requires `--take-over`, a paused claim does not.
- Most seats run with no human watching. Never wait for operator approval that will never
  come: route decisions to the fleet's DECLARED coordinator as an envelope and keep working;
  owner-tier questions park with the coordinator. Topology and coordinator authority are
  declared at fleet setup, in writing; authority is per-fleet, never inherited.
- On Codex, HEALTHY idle is a turn that does not visibly end (the Stop waiter holds it). A
  turn that ends promptly is a diagnostic flag (stale wake claim, tripped breaker, pause
  marker, exhaustion): check `doctor` before assuming quiet.
- Safe fixes are reads and governed verbs; breaking fixes are raw file edits, wrapper
  bypasses, and identity guesses. When a refusal names no remedy, envelope your coordinator.
  `malformed_evidence` on an unknown record kind is usually version skew: update the reader,
  never edit a ledger.

## Where the rest lives

- `docs/AGENT-OPERATIONS.md`: the full verb reference and the incident-backed long form of
  every rule above (hook shadowing, message/ack hygiene, topology, troubleshooting), verbatim.
- `docs/CONFLUENCE-v0.md` (the `status --json` / `graph --json` machine contract),
  `docs/FLEET.md`, `docs/FLEET-AUTONOMY.md`, `docs/TRUTH-GUARANTEES.md`.
