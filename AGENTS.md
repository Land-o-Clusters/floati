# AGENTS.md — operating floati as an agent

Floati is built to be operated by agents as well as people: every verb emits
one JSON artifact, every failure is a typed refusal with a named reason, every
durable action leaves a receipt, and repeated commands are idempotent under an
explicit key. You never have to guess whether your own action worked — read
the artifact and the receipt.

Floati itself makes zero model calls and sends no telemetry. Its four counted
outbound paths are explicit and fenced below; it is operable BY agents, but it
does not contain one.

## How to behave (all models: gpt-5.6-sol, gpt-6-astra, Claude)

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
- Delegation: parallelise independent work with subagents; write briefs a human can read.
- Style: concise paragraphs; lists only for parallel items; commands in fenced blocks. Report the exact commit, file, and count.

## Install

**Test runner (canonical, the only one):** `PYTHONDONTWRITEBYTECODE=1 python3
-m unittest discover` — system python3, plain shell. This repository has no
pytest and no `.venv` by design; a harness's managed pytest gateway demanding
one is the wrong instrument, and its refusal is not a gate on this work.
`pyproject.toml` is the package-metadata authority: Python 3.9 or newer,
with zero dependencies.

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

**Harness wake hooks (measured incidents in `docs/AGENT-OPERATIONS.md`):** a session
already running when a wake hook is installed never runs it, so have the user trust or
enable the hook in the harness UI and relaunch; `wake arm` refuses, fail-closed, without
the waiter workspace binding and Codex-wait consent. A GUI hook save can silently shadow
a working registration with one that cannot exec ("it appears in the UI" is not
installed), so after ANY GUI save prove the hook FIRES with a real turn-end dispatch.
Prefer the registration type the harness actually execs (Shell form for a command line;
the Process form takes an argv array).

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

## Verbs (contracts: `COMMAND --help`; the full reference is in `docs/AGENT-OPERATIONS.md`)

Every durable verb requires an explicit absolute `--root`; there is no default root, no
home scan, and no discovery.

- Bootstrap: `init` (`--solo NODE --harness H` for one seat) · `register` · `retire` (self only).
- Nodes and roles: `node {add|retire|switch|role|boot|teardown|explain|state-flush}`
  (preview-first; temporary nodes take `--lease-minutes`) · `role {list|show}`.
- Mail: `send --root --from --to --repo --sha SHA --doc PATH --note TEXT [--reply-to ID]
  [--idempotency-key KEY]` · `inbox --session SESSION` (acks on drain; `--peek` is explicit)
  · `ack` (repeat `--id` for one exact batch) · `sent`. Delivery and acknowledgment are
  separate receipts; `status: ok` from `send` proves the append, never the delivery.
- Truth surfaces: `describe --json` · `verify` · `journal {checkpoint|verify}` ·
  `signature {sign|verify}` · `status` · `snapshot` · `log` (`--replay`) · `effects`/`effect`
  · `threads`/`thread` · `graph` · `plan` · `receipts` · `board`.
- Evidence, repair, health: `overlap report` · `repair quarantine` · `doctor` (`--probe`) ·
  `watch` · `presence {report|show}` · `epoch roll`.
- Confluence: `confluence {grant|revoke|status|bundle}` (read seam only: no discovery,
  no watcher, no network, no mutation API).
- Work: `grant` / `grant revoke` · `work` · `worker` · `sequencer` · `supervise` · `orchestrate`.
- Intake, transport, context, quota: `intake {scan|adopt|show}` · `mcp serve --root ROOT
  --as NODE --session SESSION` · `context` · `quota {collect|show}`.
- Wake: `wake {pause|resume|status|arm} --root ROOT --as NODE --session SESSION` (one
  session per call; marker-only and receipted; hook registration is never edited) · the
  Codex Stop waiter is `scripts/floati-codex-wait --root ROOT`.
- Cartography and lifecycle: `chart --declared-roots FILE` · `survey` (read-only view of
  foreign buses) · `install` · `update` · `uninstall` · `purge` (Trash only, never deletes).
- Managed wrappers: a seat's `node boot` prints its exact wrapper shapes for `send` and
  `ack`; use those verbatim, never a remembered shape.

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
  foreign buses and it is read-only.
- Never wake, or install wake machinery for, a session without the user's
  recorded consent — and never run a production waiter by hand against a
  live root; fixtures exist.
- Never treat a receipt as more than it says: an append is not a delivery,
  a delivery is not an acknowledgment, a poll is not a wake.
- Never invent a number floati did not measure. Absences are typed and
  cited, not filled in.

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
  `floati wake arm --root ROOT --as NODE --session SESSION --workspace PATH` at every
  session turnover; takeover is predecessor-bound and built for this.
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
