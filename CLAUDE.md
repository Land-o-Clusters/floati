@AGENTS.md

<!-- Claude Code reads this file; Codex reads AGENTS.md directly. Everything that is not
     Claude-specific belongs in AGENTS.md so both tools obey one text. -->

## Claude Code specifics
- This repository carries no Claude-specific configuration (no hooks, rules, or commands). The
  wake machinery described in `AGENTS.md` is installed per seat, not per checkout.
- When a managed gateway refuses with an exit-64 `*_APPROVAL_REQUIRED` line, surface the
  refusal verbatim and stop; never retry through another runtime.
