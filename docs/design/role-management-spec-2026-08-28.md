# WS-D SPEC — ROLE MANAGEMENT: boot and teardown as projections (Fable, 2026-08-28)

**Origin:** owner question 2026-08-27 ("how will floati know how to give them identities, rules,
roles beyond a name — make it magical for the user and smart for us"). This spec is the build
contract for North Star V7. It productizes the fleet's own hand practice.

## The three-part answer

**1. Role templates + the wizard interview.** Floati ships a curated library of role archetypes
(sre, github-manager, architect, reviewer, builder, researcher, scout — extensible; a template is
a file). A template is a TYPED record, never a prose blob:

```
role: sre                              # identifier, grammar-checked
duties: [...]                          # what this seat does, one line each
decision_rights: [...]                 # what it may decide alone
stops: [...]                           # what stops for the owner/architect
fences: [...]                          # what it must never touch
cadence: envelope-per-row              # reporting rhythm
questions:                             # what the template cannot infer — the interview
  - key: repo        ask: "Which repository does this seat own?"
  - key: never_touch ask: "Anything this seat must never modify?"
  - key: reports_to  ask: "Which node does it report to?"    default: <architect>
```

`floati node add` (B3 wizard) gains a role step: pick template → answer ONLY the template's
declared questions → answers persist as the node's **role record** in the registry (typed,
versioned, receipted like every registry write). Editing a role later is a recorded reassignment
(B6 pattern), never a re-onboarding.

**2. Boot and teardown are PROJECTIONS, never stored strings.** `floati node boot <id>` composes
the boot command at invocation time from live truth:
- identity, harness, and the seat's EXACT managed-bus verb shapes (the 2026-08-27 lane-puddle
  send-shape stall is the binding precedent — a seat must never have to guess its transport);
- workspace path from the B2 layout convention; state-file location (below);
- the current fleet map: architect node, sibling nodes and roles, declared roots;
- the role record: duties, rights, stops, fences, cadence, interview answers;
- wake status for this seat (armed/eligible/none) so the prompt says whether to poll at
  row boundaries.
A projection cannot go stale: rename the architect or retire a sibling and the next boot says so.
The boot prompt is a board; the ledger stays the truth. `--json` twin for tooling; `floati node
explain <id>` renders the same record as prose for the user ("what is this node and why").

**3. The state-file ritual.** Every node gets `STATE.md` in its workspace slot. Every generated
boot command OPENS with "read your state file first." Every generated teardown/wind-down command
is the mirror: flush state to `STATE.md` → push and envelope anything unbanked (**a
committed-is-not-banked check is part of the generated ritual**) → report DRAINED → mechanical
retire (lease close, folder reported never deleted). Floati guarantees the vessel and the ritual;
the seat's accumulated judgment lives in the file and deepens itself. Floati never parses or
interprets `STATE.md` content — it only proves the ritual ran (file mtime/receipt).

## Template management UX (owner order 2026-08-27: repeatable, personalized, easy to edit)

Templates are **plain files**, in two provenance tiers: the shipped library (read-only, part of
the install) and the fleet's own at `<root>/roles/` (explicit-root doctrine — your library
travels with the fleet). The workflow is COPY-THEN-PERSONALIZE, never edit-the-shipped:

- `floati role list` — shipped + yours, provenance-labeled; `role show <name>` renders one.
- `floati role new <name> --from <archetype>` — copy a shipped or existing template and open it.
- `floati role edit <name>` — opens `$EDITOR`; on save floati VALIDATES and refuses to store an
  invalid template, with typed line-anchored errors (refusal-first, never a silent half-save).
- `floati role validate <file>` / `role import <path>` — sharing is just a file; import validates.
- **The repeatability beat:** at the end of a wizard interview, floati offers to save the
  answered template as a new named role — "set up another one like this" becomes one pick.
- Template writes are receipted like registry writes; a node's role record names its template,
  and `node explain` shows which template version its last boot projected.

## Hard bounds

- **Zero model calls, ever** (README promise). Generation is deterministic composition of typed
  records. A user wanting a richer custom persona authors a new template file (their own
  architect seat may draft it); floati stores and projects it, and never calls a model to do so.
- "Run them where allowed" (V7) means: floati may EMIT the boot command, copy it to clipboard, or
  hand it to a harness adapter that accepts a launch+prompt — behind the same consent posture as
  wake. Floati never impersonates the user in a harness it has no adapter contract with.
- Cold Read Rule on every generated sentence; template library copy is Fable-gated.
- A template's `stops` and `fences` are rendered VERBATIM into every boot projection — a
  projection may add context but never omit a fence.

## Build order (post-WS-B, seat per §6 of the program)

D1 template record + schema + three shipped archetypes (architect, builder, sre) · D2 registry
role record + wizard role step · D3 `node boot` / `node teardown` projections + managed-verb
shapes per harness · D4 `node explain` · D5 state-file ritual receipts · D6 template authoring
guide + the github-manager/reviewer/researcher archetypes. RED-first throughout; the first RED:
a boot projection rendered from a stale fleet map must be impossible to construct (projection
reads ledgers at call time, no cached fleet state).
