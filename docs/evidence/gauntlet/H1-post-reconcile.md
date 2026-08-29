# H1 — post-reconcile onboard via `floati node add`

**Family:** onboard (re-run after Wave 2 landing). Capture: `docs/evidence/gauntlet/captures/H1-post-reconcile-run.json` (9181 bytes, sha256 `caf60af3119490d6351d1ea8683479953722ee5bd23eaffa0f75c6180dd42560`).
**Trunk:** `origin/main` `17aaf1162a577889a101569fe65c48fdbdfdabc7` merged into `refs/heads/lane/grok-gauntlet`.
**Scratch:** `~/Projects/floati-grok/.gauntlet-scratch/h1r20260828015848`

Pre-reconcile H1 (`docs/evidence/gauntlet/H1-onboard.md`) filed `floati node add` ABSENT. On this trunk the verb is present.

## node add is public

```text
argv: ["/usr/bin/python3", "-m", "floati", "node", "--help"]
exit: 0
```

Synopsis names `{add|retire|switch|role|boot|teardown|explain|state-flush}`.

## permanent add (Cursor)

```text
argv: node add --root <scratch> --node grok-h1 --harness Cursor --lifetime permanent
exit: 0
```

`registry-01a0461753c2787bb05d05837560fa6c`, `role=Cursor`, `state=active`, `boot_command=null`, `teardown_command=null`. Workspace `nodes/grok-h1` exists.

## temporary add (Codex)

```text
argv: node add --root <scratch> --node temp-h1 --harness Codex --lifetime temporary --lease-minutes 30
exit: 0
```

Registry plus `node_lease` (`lease-01a04617541273e0b01b2c82856d472a`). One-command `boot` / `teardown` projected. Workspace `nodes/temp-h1` exists.

## invalid node refused

```text
argv: node add --node ../escape ...
exit: 20
code: node_invalid
```

No escape workspace was created.

Live fleet root was not used. Product source was not edited.

**Verdict: PASS**
