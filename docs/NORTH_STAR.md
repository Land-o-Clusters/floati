# FLOATI NORTH STAR — the owner's vision, made canonical (Fable, 2026-08-27)

**Owner mandate (2026-08-27, verbatim in intent):** Floati is not a truth ledger with a CLI.
It is the **fleet operating system for local coding agents** — the ledger is its foundation and
its honesty brand, not its ceiling. Every future sprint is measured against this document.

**Naming ruling (Fable, 2026-08-27, owner may overrule):** the product and binary are **`floati`**.
`slip` was never the CLI — Slipway was a charter name and a node id. Nautical vocabulary
(Harbor Board, harbor chart, buoy) stays as *feature* names. All remaining Slipway residue is
swept per WS-G of the weekend program (live surfaces rename; frozen evidence keeps its verbatim
history — a gate doc quoting old output is a fixture and is never edited).

## The vision, as capabilities (V1–V14)

Status is measured against `origin/main` @ `99a15c0` **plus the unmerged dark branches**
enumerated in the weekend program. "DARK" = built and gated on a branch, not on main.

| # | Capability | Status | Receipt |
|---|---|---|---|
| V1 | Serial JSONL bus, hardened, typed provenance, trivially easy node onboard/teardown | **BUILT** (core) | ledger + envelopes + acks on main; `init --solo`, register/retire; refusal-first exit contract |
| V2 | Adapted to the popular harnesses (codex, claude, cursor, opencode, cline, grok-build, pi, herdr, t3 code) | **DARK** | five-harness roster + parity battery gated 8/8 on `u2/manifest-contract` (4,031 ins); codex adapter live on main; herdr/opencode observation branches |
| V3 | Wake: optional daemon set; harness stop-hooks; lanes stream dispatches live | **PARTIAL/DARK** | OpenCode plugin vendored + wake-identity canonicalized on `codex/wake-identity-canonicalization` + `lane/fleet-ops-window`; **no Codex Stop waiter exists — P0 of the program**; no daemon |
| V4 | Delivery truth: catch non-acking lanes, deaf nodes, stalled mail | **DARK** | `relief/doctor-delivery-health`: per-node undelivered/oldest-age/last-drain scoreboard + `doctor --probe` loopback deafness probe, RULED 15m stall threshold |
| V5 | Context management: turnover at context pressure, efficient windows, porting between models | **ABSENT** | no code, no charter. WS-E spec first; v0 is honest about what harnesses do not expose |
| V6 | Admin TUI in the grok-build class: onboard wizard, provider switch, permanent + temporary nodes | **PARTIAL** | Harbor Board (status) on main; `codex/tui-excellence` (3,207 ins, 08-08, needs L4 re-gate); **admin console absent** |
| V7 | Role management: generate + explain + (where allowed) run boot and prep-clear commands per node | **ABSENT** | nothing built anywhere; this productizes the fleet's own hand practice. WS-D |
| V8 | Puddle as optional visual interface | **SEAM READY** | CONFLUENCE-v0 schemas + `status --json` + `graph --json` on main; consumer side deliberately out of this repo |
| V9 | Robust install / update / **uninstall** / teardown | **PARTIAL** | install + update on main (manifest-exact, never-prune-foreign); **uninstall command does not exist** — off-brand for a consent-and-blast-radius product |
| V10 | Eye candy — fun and impressive to use | **PARTIAL** | wall, flight-recorder replay, demo GIFs are real; `codex/tui-excellence` dark; more in WS-B/WS-F |
| V11 | Monitoring: down nodes, node problems, injection, cross-bus poisoning | **PARTIAL** | doctor + watch + three-lamps liveness on main; heartbeat liveness needs the daemon (V3); **security posture ruled below** |
| V12 | Multi-bus management on one filesystem: what is downstream, switch architects, ASCII harbor charts | **ABSENT** | `graph` is per-root; multi-root is a dark contract. WS-B builds the declared-roots registry + chart |
| V13 | Filesystem mechanics: a project folder per node, nested, never sprawling the user's home | **ABSENT** | today's fleet sprawls `~/Projects` + the system temporary directory (which has lost work). WS-B charters the layout |
| V14 | **All-knowing survey**: detect buses on this filesystem that floati did not install; alert the user | **ABSENT** | WS-B. This machine is the corpus: three non-floati agent-bus roots exist beside ours under `~` today |
| V15 | **Agent-first-class operation**: point an agent at the repo — install, onboard, operate, uninstall, zero human relays | **PARTIAL by accident** | typed exits/refusals/idempotency/receipts already agent-gold; the gaps are chartered in `docs/design/agent-surface-spec-2026-08-28.md` (WS-I) |

## Standing rulings that bind every capability above

1. **The wake posture.** "Nothing wakes without you" is a load-bearing promise. Daemons and
   stop-hooks are **opt-in per root, armed by a consent receipt in the ledger, off by default**.
   The promise becomes: *nothing wakes without your recorded say-so.*
2. **The word "telemetry" is banned** for V11. Floati phones home never. What V11 is: **local
   diagnostics** — the watch, the doctor, the harbor master's duty. Any copy or code comment that
   says telemetry for a local feature is a defect.
3. **Security claims are provenance claims.** Floati does not claim to "detect prompt injection"
   — a classifier claim we cannot receipt. It **proves provenance and flags boundary violations**:
   unregistered sender, foreign `tenant_id`, SHA on no ref, cross-root writes, envelopes arriving
   outside the registry's grammar. Mechanical, testable, honest. Copy must never say more.
4. **No home scan — survey on request.** V14's discovery runs only when the user invokes it
   (wizard offer or explicit command), reports foreign buses **read-only**, and never writes,
   drains, acks, or registers against a bus floati did not create. Detection of *our own* lanes
   being waited on by a foreign waiter is in scope (the 2026-08-27 stop-hook conflict is the
   type specimen).
5. **Cross-bus coexistence is a first-class contract** (learned from the incumbent-bus conflict of 2026-08-27):
   any wake component floati installs into a shared harness config (Codex hooks, OpenCode
   plugins) must (a) resolve identity through floati's own registry, (b) **exit silently and
   instantly for unbound workspaces**, (c) never read, edit, or shadow another bus's registration,
   markers, or state, and (d) leave an auditable install receipt. The same courtesy is *expected*
   of foreign waiters and *verified* by the survey (V14).
6. **THE EXIT DOOR (owner ruling 2026-08-28, product-wide flavor): leaving is a first-class
   feature, and it is part of the honesty brand.** Every capability ships WITH ITS REVERSE, at
   equal polish: wake → pause/resume · register → retire · install → uninstall · orchestrate →
   drain · boot → teardown · consent → revocation. Stopping is always one obvious command,
   receipted like everything else, and never destroys the user's record (data outlives the
   tool). A product confident in its worth does not make leaving hard — and a copy pass that
   finds an entrance without its exit has found a defect.
7. **The existing product laws carry forward unchanged:** refusal-first exits · receipts beat
   boards · no network without consent · explicit absolute roots · L2 WIRING / L3 RED-first
   frozen-tree committed-and-banked / L4 re-gate on drift · Cold Read Rule on all copy ·
   conformance matrix cells only with passing receipts.

## What v1 ships vs what the program builds

The publication candidate (`80bf9e86`, S-F1 clear) remains the flip artifact **in content class**:
ledger core + board + replay + orchestration. The weekend program (see
`docs/status/WEEKEND_PROGRAM_2026-08-28.md`) integrates the dark estate and builds V3/V4/V6/V7/
V9/V12/V13/V14 toward a substantially stronger flip. **Flip timing, name confirmation, and license
confirmation stay owner-tier.**
