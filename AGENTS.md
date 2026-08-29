# AGENTS.md — operating floati as an agent

Floati is built to be operated by agents as well as people: every verb emits
one JSON artifact, every failure is a typed refusal with a named reason, every
durable action leaves a receipt, and repeated commands are idempotent under an
explicit key. You never have to guess whether your own action worked — read
the artifact and the receipt.

Floati itself makes zero model calls and opens no network connection. It is
operable BY agents; it does not contain one.

## Install

**Test runner (canonical, the only one):** `PYTHONDONTWRITEBYTECODE=1 python3
-m unittest discover` — system python3, plain shell. This repository has no
pytest and no `.venv` by design; a harness's managed pytest gateway demanding
one is the wrong instrument, and its refusal is not a gate on this work.

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
`python3 -m floati` from a source checkout).

**Relaunch quirk (measured):** a harness session that was already running
when a wake hook was installed will never run it — harnesses snapshot or
trust-gate hooks. After installing any wake component: tell the user to
review/trust/enable the hook in the harness UI, then relaunch the session.
Never claim a pre-install session is reachable.
`wake arm` has two installer-created prerequisites - the waiter workspace
binding and Codex-wait consent - and refuses, fail-closed, without them.

## The output contract

Every command prints exactly one JSON artifact:

```
{"artifact_version": 0, "command": "<verb>", "status": "<status>", "evidence": {...}}
```

`status` is `ok`, `refused`, `cannot_speak`, `intentional_silence`,
`no_result`, `malformed_evidence`, or `degraded`. On refusal, `evidence.code`
is a stable machine-readable reason and `evidence.detail` says what to fix.
`status --json` and `graph --json` are the stable version-zero machine
contract (`docs/CONFLUENCE-v0.md`).

## Exit codes, with remedies

| exit | meaning | remedy |
|---:|---|---|
| 0 | done; artifact holds the result | proceed |
| 20 | refused before any mutation (`status: refused`) | the request is wrong, not the system — fix the argument, identity, or missing consent named in `evidence.code`/`detail`; do not retry unchanged |
| 22 | result exists but cannot be rendered safely for this terminal | re-run with `--json` |
| 31 | intentional silence (nothing to say; e.g. a waiter with no participant) | clean no-op; do not treat as failure |
| 32 | query ran, nothing matched | treat as an empty set |
| 33 | durable evidence is malformed or inconsistent | stop; do not retry; report the named ledger for investigation |
| 35 | a filesystem failure interrupted durable access | check disk, permissions, and the root path; retry once conditions change |

## Verbs

Every durable verb requires an explicit absolute `--root`; there is no default
root, no home scan, and no discovery. `COMMAND --help` prints the full
contract for each.

- **Bootstrap:** `init` (create/validate one direct-home fleet root;
  `--solo NODE --harness H` for one-seat setup) · `register` · `retire`
  (self-retirement only).
- **Nodes and roles:** `node {add|retire|switch|role|boot|teardown|explain|state-flush}`
  (preview-first; temporary nodes take `--lease-minutes`) · `role {list|show}`.
- **Mail:** `send --root --from --to --repo --sha SHA --doc PATH --note TEXT
  [--reply-to ID] [--idempotency-key KEY]` · `inbox` · `ack`. Delivery and
  acknowledgment are separate receipts; `status: ok` from `send` proves the
  append, never the delivery.
- **Truth surfaces:** `status` · `log` (`--replay` reconstructs from the
  ledger) · `effects` / `effect` · `threads` / `thread` · `graph` · `plan` ·
  `receipts` · `board` (TUI).
- **Health:** `doctor` (per-node delivery scoreboard; `--probe` loopback
  deafness probe) · `watch`.
- **Work:** `grant` / `grant revoke` · `work` · `worker` · `sequencer`
  · `supervise` · `orchestrate`.
- **Wake control:** `wake {pause|resume|status} --root ROOT --as NODE
  --session SESSION` — exactly one session per invocation; global and
  wildcard selectors do not exist. Marker-only and receipted; hook
  registration is never edited. A paused session is recorded state, not
  absence or deafness; `wake status` names what it cannot see (the running
  session's cache, the harness trust gate).
- **Cartography:** `chart --declared-roots FILE` (only explicitly declared
  roots) · `survey` (user-invoked, read-only report of buses floati did not
  install — it never writes, drains, acks, registers, or locks a foreign
  bus).
- **Lifecycle:** `install` · `update` · `uninstall` (see Install above).

**Managed wrappers:** a harness seat provisioned with a managed bus profile
sends through its wrapper binary, which pins root/from/repo and takes exactly
`<wrapper> <profile> send --to NODE --sha SHA --doc PATH --idempotency-key KEY
--note TEXT [--reply-to ID]`. A seat's own boot projection (`node boot`)
prints its exact wrapper shapes — use those verbatim, never a remembered
shape.

## Standard workflows

- **Solo:** `init --root R --solo me --harness Codex` → `work`/`log`/`board`.
- **Fleet:** `init` → `node add` per seat → `send`/`inbox`/`ack` between
  seats → `board`.
- **Manual non-solo work authority:** add the architect first,
  assign its shipped role, add the holder, grant one exact coordinate, then
  add and claim work. The reverse uses the same coordinate and architect gate.

```text
floati init --root /var/tmp/fleet
floati node add --root /var/tmp/fleet --node architect-a --harness Codex --lifetime permanent
floati node role --root /var/tmp/fleet --node architect-a --template architect --answer repo=floati --answer never_touch=foreign-project --answer owner_stops=owner-tier
floati node add --root /var/tmp/fleet --node lane-a --harness Codex --lifetime permanent
floati grant --root /var/tmp/fleet --as architect-a --holder lane-a --subject work-claims --epoch 1
floati work add --root /var/tmp/fleet --title bounded-work --owner lane-a
floati work claim --root /var/tmp/fleet --id work-00000000000070008000000000000000 --as lane-a --authority-subject work-claims --authority-epoch 1
floati grant revoke --root /var/tmp/fleet --as architect-a --holder lane-a --subject work-claims --epoch 1
```

- **Health check:** `doctor --root R` → chase every red with the receipt it
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
