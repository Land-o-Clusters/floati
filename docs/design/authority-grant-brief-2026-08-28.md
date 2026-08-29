# BRIEF — SD-7: the authority grant surface (`floati grant`)

**Fable, 2026-08-28. Status: RULED — this brief is the contract; lane-floati builds RED-first.
Source defect: shakedown 2026-08-28, OPERATOR-LOG §§103–105 — a manually initialized non-solo
fleet has NO path to work authority: `work claim` refuses `authority_missing`, `work complete`
refuses `work_not_claimed`, and no documented verb creates a grant. Role records correctly do NOT
confer authority (keep that separation — it is right), but the missing verb makes `work` a dead
surface outside orchestration-seeded fleets.**

## The contract

**Verb:** `floati grant --root ROOT --as GRANTOR --holder NODE --subject SUBJECT --epoch N`
and its reverse `floati grant revoke --root ROOT --as GRANTOR --holder NODE --subject SUBJECT
--epoch N`. Both are append-only receipted acts in the fleet root's ledger, same as every act.

**G-1 — WHO MAY GRANT: a node holding an ACTIVE `architect` role record at grant time.** Not a
flag, not an env var, not the first caller — the role ledger decides. A grant attempt by any other
node refuses typed (`grant_requires_architect`) naming the remedy (`node role --template
architect`). A fleet with no architect role gets the same refusal with the same remedy — the
refusal tells the operator exactly what the shakedown's operator had to guess.

**G-2 — WHAT IS GRANTED: one (holder node, subject, epoch) coordinate per record.** No wildcards,
no "all nodes", no subject globs — same posture as wake's no-global-selector rule. `work claim
--as NODE --authority-subject S --authority-epoch E` succeeds iff an active grant record matches
all three exactly.

**G-3 — REVOCATION COSTS NO MORE THAN THE GRANT (SD-6 law: leaving may never require more
structure than entering).** Revoke takes the same coordinate, the same grantor class, appends a
revoke record; the claim refusal after revocation NAMES the revoking record. Epoch supersession is
also legal (grant at E+1 makes E claims refuse), but explicit revoke must exist — supersession is
not a substitute for an exit.

**G-4 — THE DECIDER FAILS CLOSED, as today.** Absent grant = refuse. This brief adds the grant
path; it does not soften any refusal. Refusals gain subjects: `authority_missing` must name the
(holder, subject, epoch) coordinate it looked for (SD-2's lesson — a refusal that does not name
its subject converts a defect into an investigation).

**G-5 — SOLO STAYS SOLO.** The solo bootstrap's existing seeded authority is untouched; `grant`
is the non-solo path. Orchestration seeding also untouched — `grant` composes with it, never
replaces it.

**G-6 — AGENTS.md documents the flow** in the lifecycle section, exactly where the operator
looked and found nothing: init → node add → node role (architect first) → grant → work add/claim.
The doc example must be executable verbatim (the receipt-command round-trip law from SD-5: any
command a surface prints or documents must parse against the live parser, proven by test).

## RED-first bank (the shakedown is the fixture)

1. Reproduce §104 exactly: manual fleet, `work add`, claim → `authority_missing` naming the
   coordinate (RED until refusal-naming lands).
2. Grant by architect → claim succeeds → complete succeeds (the operator's stranded
   `work-01a048b7…` scenario, drained).
3. Revoke → claim refuses naming the revoke record.
4. Grant by non-architect → `grant_requires_architect` with remedy.
5. Grant in a fleet with no architect role → same refusal, same remedy.
6. Wildcard/glob holder or subject → typed refusal, no record appended.
7. AGENTS.md example round-trips against the parser.

## Sequencing

After SD-1..SD-6/SD-8 (this row is not launch-blocking: `work` orchestration in hand-built fleets
ships as a documented gap if the owner flips before it lands — the org page already keeps the
claim out). The chaos-site fleet's open item `work-01a048b7a03170999a5681656b7daa49` is the
acceptance fixture: the drill ends by granting, claiming, and completing it through documented
surfaces only.
