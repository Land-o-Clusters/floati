# WS-E v1 SPEC — THE TIDE TABLES: threshold-triggered turnover (Fable, 2026-08-28; owner-ordered)

**Owner order:** optional per-node thresholds for compaction or lane
turnover — "context hits 70% → prep-clear + fresh boot" — plus easy wizard
pinning of models/harnesses. Pinning EXISTS (`node add`, `node switch`,
`node role` — landed at reconcile; the Regatta dresses them). This spec is
the threshold half. Complements Puddle through the CONFLUENCE seam:
policy state and tide readings ship as JSON for the glass.

## The honesty spine (E1 still binds)

No harness EXPOSES remaining context (E1, receipted) — so no gauge and no
threshold pretends otherwise. But a harness's own written artifacts can
make pressure DERIVABLE: transcript token counts, session-file growth,
turn counts, plus a CITED model window size. **A number computed from the
harness's own records and a cited constant carries a DERIVED stamp with
its formula and sources; where nothing derivable exists, the metric is a
typed absence and the user picks a proxy metric instead. ESTIMATE never
appears — no estimator exists.**

## T1 — THE TIDE SURVEY (grok, NOW; E1's sibling, does not wait on anything)

Per harness (all current surfaces): what turnover-relevant signals does it
WRITE — transcript/session files (paths, formats, token fields), turn
markers, size growth? What is the model's context window (vendor-cited)?
Does a compaction verb exist (e.g. a /compact-class command), and is it
invocable non-interactively? Output: the DERIVABLE-METRICS TABLE, one row
per harness × metric, each cell cited or typed-absent — the posture-matrix
discipline. This table is the WHOLE authority for what thresholds may be
offered; no metric ships that is not in it.

## T2 — POLICY RECORDS (per-node, optional, off by default)

`floati context policy set --root R --node N --metric M --threshold V
--action recommend|direct` (+ `show`/`clear`). Metric M must exist in the
tide table FOR THAT NODE'S HARNESS — offering a 70%-of-window policy on a
harness with no derivable fraction is refused with the citation. Policies
are ledger records with receipts; `--json` twins throughout. The wizard
(`node add`) gains one optional step offering the metrics the tide table
supports for the chosen harness — never a generic slider.

## T3 — EVALUATION rides the wake daemon

The daemon's loop (being built) gains one bounded read per cycle: compute
the node's tide metrics from the tide table's recipes, compare against its
policy. No new process, no polling outside the daemon's consented cadence,
zero evaluation for nodes with no policy. Every evaluation that crosses a
threshold emits a TIDE receipt (metric, value, formula, threshold).

## T4 — THE ACTIONS (floati orchestrates; it never pretends to manage the
harness's memory)

- **recommend:** a receipted TIDE NOTICE envelope to the node's architect
  (and the board shows the flag) — a human or architect-seat decides.
- **direct:** floati sends the node a TURNOVER DIRECTIVE envelope carrying
  its own E2 turnover recipe, holds NEW dispatches to that node
  (wake-hold, existing mechanism), and the successor boots from the D3
  projection. The SEAT executes its own prep-clear — exactly the fleet's
  live practice, productized. Where the tide table says a native
  compaction verb exists and is non-interactive, `--action compact` may be
  offered for that harness only.
- Every action is receipted; a directive is a DELIVERY, not a completion —
  the turnover is complete when the state-flush receipt (D5) lands, and
  the board says which side of that line a node is on.

## Sequencing

T1 now (grok). T2–T4 spec-complete here but build AFTER: E2 lands (the
recipe is the payload) and the daemon lands (the evaluator rides it).
Build seat: lane-floati post-daemon (T3 is daemon-internal) with T2's CLI
on lane-puddle if parallel is wanted. Gauntlet gains a tide family when
T2–T4 ship; the campaign operator sets one policy and survives one
directed turnover.

## CORRECTION (owner, 2026-08-28, minutes after the T1 dispatch): E1 RESCOPED

The owner: "every harness exposes remaining context, they just do it with
different calls — /usage etc." CONFIRMED as a scope error in the conclusion,
not the measurement: E1 probed the EXTERNAL CLI surface (flags, headless
invocations) and correctly found nothing there — but in-session commands
(/usage, /context, /cost, /status and kin) are a surface an external probe
cannot see by construction, and several harnesses expose usage exactly
there. A SCOPED ANSWER CLEARED THE WHOLE QUESTION; the exonerating
direction, again.

**T1 WIDENS to three access classes per harness × metric:**

- **A — external/programmatic:** files on disk, server APIs (opencode
  `serve` endpoints, codex app-server protocol), anything floati can read
  without a session's help;
- **B — in-session:** slash/command surfaces the SEATED AGENT can invoke
  and self-report over the bus. A class-B reading is the node's TESTIMONY —
  stamped SELF-REPORTED with the command that produced it, valid for
  triggering that node's OWN turnover, never rendered as a fleet-measured
  fact;
- **C — none found** (typed absence, cited).

**The NO GAUGES ruling NARROWS accordingly:** no gauge from fabricated or
unavailable data — but a gauge fed by class-A derivation or class-B
testimony, stamped as such, is honest. E2's absence dataset gains the
access-class column; its current cells are class-A answers and say so.

## T1 GATED PASS (Fable, 2026-08-28) — the table is now THE AUTHORITY

`docs/evidence/gauntlet/T1-tide-survey.md` @ `138f178d`, both capture
digests re-hashed and matched, merged to main. What T2 may offer, verbatim
from the table: Codex class-A DERIVED (jsonl `context_window` + growth) ·
OpenCode class-A DERIVED (`opencode.db` `session.tokens_*` — the
best-derivable harness) · Grok class-A proxies (`num_messages`, `du`
bytes) + class-B `/context` family with a shipped 85% auto-compact
threshold · Pi's catalog `contextWindow` as a cited constant only ·
class-B SELF-REPORTED testimony wherever a seat can invoke its own
`/context`-family command — with the measured limitation that a CURSOR
AGENT LOOP CANNOT (composer-only; a Cursor seat's tide policy therefore
rides class-A/proxy metrics or human-typed testimony). REFUSED forever
until their cells change: percent-of-window policies on herdr, t3, agy,
devin, cursor-agent, and every C-cell surface. E1 stands as a correct
class-A CLI-flag photograph; this table is its widening, not its
retraction.

## T1 AMENDMENT (Fable, 2026-08-28, owner prompt + direct measurement): CLAUDE IS CLASS-A, RICHLY

The owner expected Claude to show usage easily and was right. The T1 cell
said class-A-poor because the key scan read TOP-LEVEL transcript keys — but
Claude Code nests `message.usage` at depth 2, and a live measurement (646
rows in a real transcript, key names only) shows `input_tokens`,
`output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`,
plus per-row model. **Claude/cli remaining-context is therefore a true
class-A DERIVED fraction:** latest assistant row's
`input_tokens + cache_read_input_tokens` (the current context load) over
the vendor-cited model window — per-turn, from disk, no seat cooperation.
This likely makes Claude the best-derivable harness beside OpenCode.

Lesson pinned beside the E1 rescope: **A KEY SCAN AT DEPTH 1 ANSWERS ABOUT
DEPTH 1.** T2 implementers: every class-C transcript cell in the table
(cursor `agent-transcripts`, codex jsonl beyond line one, grok
`summary.json` siblings) deserves one depth-2+ probe before its C stands.
Grok's row was honest as scoped; no re-round — this amendment is the
depth-2 receipt.
