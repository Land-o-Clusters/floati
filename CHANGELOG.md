# Changelog

All notable changes to Floati are recorded here, by hand. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow the rules in
[RELEASING.md](RELEASING.md). Every release below names its receipt.

## [Unreleased]

## [0.2.1] — 2026-11-27

Ledger contract: `c7.28`.

<!-- DRAFT-OPUS 2026-11-27 -->

**The quickstart works again.**

### What changed

- **The 0.2.0 quickstart failed on macOS and Linux.** It left the shell outside the clone, so Python could not find Floati. The corrected steps enter the clone first and install with `-B`, so no bytecode is written into it. [Published 0.2.0 quickstart](https://github.com/Land-o-Clusters/floati/blob/v0.2.0/README.md#start-here) · [Corrected quickstart](README.md#start-here) · [Install test](tests/test_deploy.py)
- **Uninstall can clean up after itself.** A plain uninstall leaves its receipts behind. The uninstall steps now show how to remove them once uninstall succeeds. [Uninstall steps](README.md#how-it-holds-up) · [Cleanup test](tests/test_deploy.py)
- **An opt-in T3 dispatch gate.** For the provider instances you select, it checks grants on calls from Claude Code parents through the installed hooks. It can record the child IDs T3 returns, and hold new load dispatches during a quiet window or for a held node. [T3 dispatch gate](docs/T3-DISPATCH-GATE.md) · [Dispatch tests](tests/test_t3_dispatch_gate.py)
- **Cost and quota receipts.** Ask and replay read version 2 cost receipts. Opted-in role routing can use verified quota for an exact T3 provider instance. [Cost tests](tests/test_net_ask_1.py) · [Quota tests](tests/test_net_address_1.py)
- **Cross-machine code toward Floati 2.0 is included, and off.** Nothing reaches another machine unless you opt in. [Opt-in tests](tests/test_cross_optin.py)

### What we did not measure

- **Cross-machine operation.** It is not qualified. Do not rely on it.
- **Cost and quota routing with live accounts.** Those paths are tested with fixtures only.

### Release receipt

<!-- Filled from the qualified run: tag and peeled commit, public bundle manifest SHA-256, harbor source, suite count and duration on each platform, skips. -->

## [0.2.0] — 2026-11-26

Ledger contract: `c7.24`.

<!-- DRAFT-OPUS 2026-11-26 -->

**One seat can ask another, and the answer has a receipt.**

The first release since 0.1.3 (2026-10-09). The ledger contract is `c7.24`.

### What changed

- **Ask another seat on the same machine.** `floati who` shows the roster. `floati ask` sends one question and waits for an answer, a decline or a timeout, each linked to its own record. Ask from the CLI, from a selected node on the live board with `?`, or over MCP. Replay the thread afterwards. [Ask and replay](tests/test_net_ask_1.py) · [Roster](tests/test_net_who_1.py) · [MCP](tests/test_mcp_tools.py)
- **Set up a seat with a plan you can read first.** `floati setup` prints what it will write, waits for you to confirm, and keeps a receipt for teardown. [Tests](tests/test_net_setup.py)
- **Hold inbound work and call a quiet window.** [Holds](tests/test_net_hold_inbound_1.py) · [quiet windows](tests/test_quiet_window.py)
- **Long-lived ledgers rotate.** Epoch rotation carries live records forward and keeps verifiable archives under your policy. [Tests](tests/test_epoch_rotation.py)
- **More harnesses, with their limits stated.** OpenCode completed governed work across a lease expiry with a free model, and a fresh claim on an expired lease was refused. Claude has an idle doorbell that checks its version and falls back to Stop. [OpenCode](docs/evidence/conformance/C3-opencode-issue16-2026-10-25.md) · [Claude](docs/evidence/claude-inbox-idle-public-2026-10-29.md)
- **Codex workspace skills for seat setup.** The seat command installer writes the boot and teardown skills into the workspace. [Lifecycle prompt tests](tests/test_lifecycle_prompts.py)
- **An update remedy for older installed readers.** A reader gives an update remedy for a newer record version. Corruption at a supported version still fails the integrity check. [Reader tests](tests/test_installed_reader.py)
- **Banked results for ad hoc work.** A completed result is available to replay after its exact commit has been banked. [Ad hoc worker tests](tests/test_adhoc_threads.py)
- **Roster and fleet state in Confluence.** The bundle can show the active roster and available fleet-state receipts. [Confluence tests](tests/test_confluence_roster.py)

### What we did not measure

- **Idle cost with several waiters at once.** [0.1.3 measured one quiet waiter](#013--2026-10-09). Several together are not qualified, and we are not claiming a number.
- **The cost of the first send on a large ledger** against the build before the new send index.
- **Ask latency.** [The instrument exists](tests/test_net_latency_1.py). No figures yet.
- **Across machines.** `ask` is one machine only.
- **Gemini CLI and Copilot CLI.** Their [headless profiles pass fixtures](tests/test_headless_worker_protocol.py). Neither has been run live, and neither is selected or woken in production paths.
- **Worker teardown.** The [README's worker teardown column](README.md#worker-teardown) still says NOT_YET_MEASURED for every Stop-hook harness.
- **Linux.** The suite runs in CI. The platform is not supported.

### Release receipt

<!-- Filled from the qualified run: tag and peeled commit, public bundle manifest SHA-256, harbor source, suite count and duration on each platform, skips. If the whole suite is not green, say so in the first line, as 0.1.3 did. -->

## [0.1.3] — 2026-10-09

### Fixed

- **An idle Codex waiter no longer re-reads the whole ledger on every poll.**
  `floati wake wait --harness codex` now replays the ledger only when the
  ledger or one of the seat's own records changes. On one
  8 MB ledger an idle waiter fell from 37.9 % of a core to 0.55 %, and found
  the ledger lock busy 0 times in 601 probes instead of 21. A ledger that has
  rolled into archives keeps 0.1.2's polling. Measured on macOS, one waiter.

## [0.1.2] — 2026-09-12

### Added

- **A Cursor wake path.** `floati wake wait --harness cursor` is the Codex
  waiter's twin for Cursor's stop hook: it blocks on the seat's inbox and
  answers with a drain follow-up when mail arrives, or a re-arm at its own
  deadline. `floati hook install --harness cursor` writes the seat project's
  `.cursor/hooks.json` with an explicit root and node; the hook is scoped to
  that workspace, and re-rooting the chat leaves it behind. Receipt from a
  clean public clone:
  `docs/evidence/conformance/C4b-cursor-stop-hook-public-clone-2026-09-11.md`.
- **`floati doctor` names a dormant seat that is holding undelivered mail**,
  read from the wake daemon's own holder testimony. A live seat, or an empty
  inbox, adds no line.
- **Three recorded demonstrations under `docs/demo/`**, each with the recipe
  that regenerates it: a handoff with its delivery record, a killed worker as
  the board and the doctor see it, and a session ending mid-slice replayed to
  `REPLAY COMPLETE`. Every frame is a capture, listed with its SHA-256.
- **The README, rewritten around the reader's decisions**: install, first
  record, the board, growing a fleet. Every terminal image on it is a capture
  with a manifest; the drawings illustrate, the captures measure.

### Changed

- **Wake daemon lifecycle receipts live in their own file.**
  `receipts/wake-daemon/<node>.jsonl` keeps the lifecycle rows and rolls under
  the operator's ledger policy, archived whole. A heal after a crash carries
  consent forward, and a migration that carries nothing still records that it
  happened.
- **A paused wake daemon backs off like an idle one** instead of polling at
  the floor. Each cycle's CPU is measured; an over-budget cycle backs off to
  the poll ceiling, and the doctor says so.
- **A participation claim whose holder is dead is released within one
  evaluation.** Liveness is measured by pid and process start time, never by
  name. This is the mechanism behind public issue #15.
- **`send` refuses an empty `--note` where the note is authored.** The
  durable record validator is unchanged, so every record ever written still
  reads.

### Fixed

- **A Cursor seat no longer stays dormant after a tool-call death.** Cursor
  delivers that death to the stop hook as an abort. The waiter re-arms once,
  with a line a person can answer; only a second consecutive abort in the
  same conversation ends the loop.
- **The wake attempt slot is scoped by the acting session**, so a seat that
  rebinds no longer trips the replay check on legitimate work.
- **`uninstall` removes exactly what it installed and never walks the
  operator's home** from a bare library call. The daemon's terminal refusals
  exit 0 and final, and a stop proves itself before it reports.
- **The test suite pins `HOME` to a scratch directory** before any test runs,
  so `python3 -m floati.selftest` on your machine cannot reach your own
  LaunchAgents or user units.
- Test fixtures that spawned a git child and raced their own temporary
  directory under load now wait for the child, and the suite's pinned scratch
  `HOME` carries a gitconfig that keeps every fixture's git maintenance
  synchronous, so no test can leave a detached git child behind its cleanup.
- `scripts/lease-check.sh` no longer drops every process whose pid is
  narrower than the widest on the box.
- Terminal captures are black in every browser theme.

## [0.1.1] — 2026-09-09

### Fixed

- Keep worker-session mail out of ordinary inbox delivery receipts, including
  when the snapshot reader appends a cached tail.
- Preserve the caller's working directory in the installed launcher so banked
  SHA and workspace identity checks inspect the invoking repository. Isolate
  Python startup and remove PATH-selected launcher helper execution.
- Create uninstall probes and receipts exclusively through the validated
  directory handle, preserving occupied paths and reporting post-removal
  durability failures as degraded.
- Return typed root refusals for symlink loops. Successful status and watch
  queries retain installer observations without contradictory failure exits;
  doctor reports incomplete or shadowed observations as degraded.
- Correct command claims, validation prerequisites, wake examples, and generated
  capability-matrix links.
- **watch, selftest, and supervise honour SIGINT when a parent ignored it.**
  POSIX copies an ignored SIGINT across exec, and CPython's
  `subprocess.restore_signals` restores SIGPIPE/SIGXFZ/SIGXFSZ only, so a
  child launched from a SIGINT-ignoring parent swallowed Ctrl-C. Those three
  verbs now restore the operator interrupt handler at process entry.
  The fix is `floati/operator_interrupt.py`; public issue #38 carries the
  reproduction. (This entry first cited a receipt that the public repository
  does not carry; corrected in 0.1.2.)

## [0.1.0] — 2026-08-31

The first public release. Everything here exists because a fleet of coding
agents has been running on it daily — the receipts in `docs/evidence/` are
that fleet's own records.

### Added

- **The bus.** A local, append-only message ledger with separate envelope,
  delivery, and acknowledgment receipts — an append is not a delivery, and a
  delivery is not an acknowledgment. Whole-fleet readers survive records they
  do not recognize: a newer writer cannot blind an older reader.
- **Nodes, roles, and authority.** Registration, six shipped role templates,
  temporary nodes with leases, and architect-gated work authority: exact
  (holder, subject, epoch) grants with revocation that costs no more than the
  grant. Refusals name the coordinate they looked for.
- **Wake.** A consented Stop-hook waiter for Codex sessions with one armed
  acting session per workspace, receipted wakes, and honest trust reporting:
  when your harness un-trusts a modified hook — as it should — the installer
  says `untrusted_pending_user` and tells you the one step to fix it. It
  never trusts itself.
- **Wake daemon.** `wake daemon` supplies consent, binding, installation, start, status, stop, removal, and revocation for one harness coordinate.
- **Context and Tide.** Read-only context status and turnover receipts, and
  tide tables: thresholds over measurable facts only. A policy that names a
  metric without a shipped evaluator is refused with the citation in the
  refusal.
- **Purge.** Explicit preserved-root sanitation through the macOS Trash,
  with digest-bound receipts for every file moved. There is no delete
  primitive in the module — proving otherwise is a security report.
- **Lifecycle round-trip.** Manifest-exact install, update, and uninstall:
  only owned files, foreign files preserved, and a permanent guard that every
  owned path stays uninstallable. Releases are verifiable on disk with
  `install --ref` and `doctor`.
- **The TUI.** Harbor board, live harbor map, and a flight-recorder replay —
  plain-first, keyboard-primary, honest in every color tier, `NO_COLOR`
  respected as policy. Every screenshot and recording in the README is a real
  capture of a real fleet. Each capture directory ships a `manifest.json`
  with a per-file SHA-256. The three diagrams are drawings, and they are the
  only images here that are not captures.
- **Truth guarantees.** `docs/TRUTH-GUARANTEES.md` wires every promise to the
  test or receipt that enforces it, including the promises about what Floati
  refuses to do. `tests/test_no_listener_fence.py` structurally enforces that
  nothing listens and nothing phones home.

### Release receipt

Recorded in the tagged release notes at publication: bundle manifest SHA-256,
full-suite result and platform, and the conformance receipts behind the
README's harness table.
