# Example fleet contract

Status date: 2026-07-31.

The `tenant-a` example uses the durable home `/var/tmp/floati-tenant-a`.
Every command below passes that root explicitly. Floati has no default root,
environment-root fallback, daemon, or wake command.

| Node | Harness | Repository |
| --- | --- | --- |
| reviewer | Claude | no fixed repository; receives repository-bound fleet notifications |
| builder-app | Codex | `~/fleet/app` |
| builder-floati | Codex | `~/fleet/floati` |

These are the only example node identities. Each node registers itself; an
operator must not pre-create another node's row. Initialize the durable home
once from this repository:

```sh
/repo/floati/scripts/floati init --root /var/tmp/floati-tenant-a
```

Then each harness runs only its own first registration command:

```sh
# Run by the reviewer from the Claude harness.
/repo/floati/scripts/floati register --root /var/tmp/floati-tenant-a reviewer --harness Claude

# Run by builder-app from its Codex harness.
/repo/floati/scripts/floati register --root /var/tmp/floati-tenant-a builder-app --harness Codex

# Run by builder-floati from its Codex harness.
/repo/floati/scripts/floati register --root /var/tmp/floati-tenant-a builder-floati --harness Codex
```

## Polling and receipts

Every node polls at boot and before stand-down. There is no wake adapter in
HM-0.5:

```sh
/repo/floati/scripts/floati inbox --root /var/tmp/floati-tenant-a --as reviewer --session <session-id>
/repo/floati/scripts/floati inbox --root /var/tmp/floati-tenant-a --as builder-app --session <session-id>
/repo/floati/scripts/floati inbox --root /var/tmp/floati-tenant-a --as builder-floati --session <session-id>
```

The default inbox drain writes distinct delivery and acknowledgment receipts
for exactly the returned batch. An explicit process-before-ack workflow uses
`--peek`, then acknowledges the reviewed batch with the acting session:

```sh
/repo/floati/scripts/floati ack --root /var/tmp/floati-tenant-a --as reviewer --id <message-id> --session <session-id>
```

Acknowledgment is not completion. It records the exact presented message ID;
it does not claim that the named commit or document was read, applied, tested,
accepted, or finished. Git and the repository-relative evidence document
remain authoritative. A lost notification costs only polling latency.

## Notification and replay

A sender names a repository, exact lowercase 40- or 64-character Git SHA,
repository-relative evidence document, and bounded note:

```sh
/repo/floati/scripts/floati send --root /var/tmp/floati-tenant-a --from builder-floati --to reviewer --repo floati --sha <checkpoint-sha> --doc docs/evidence/checkpoint.md --note checkpoint
/repo/floati/scripts/floati log --root /var/tmp/floati-tenant-a
```

The message is a notification, not a substitute for the named Git evidence.

## Recorded lane workspaces

A fleet operator declares its external lanes root before seats open row
workspaces. The suggested layout is `~/Projects/<repo>-lanes`; expand it to a
canonical absolute path in `state/lanes-root.json` inside the fleet root:

```json
{"schema_version": 0, "path": "/absolute/product-lanes"}
```

Declare repository aliases in `state/lane-repositories.json`:

```json
{"schema_version": 0, "repositories": {"product": {"path": "/absolute/product-checkout", "default_base": "refs/remotes/origin/main"}}}
```

These declarations name existing operator-selected coordinates. The lanes root
must be outside the bus root. No scan discovers repositories, and lane commands
do not fetch: refresh the declared repository's remote refs through its approved
Git workflow before using a newer base. A checkout carrying seat-fence keys must
already enable `extensions.worktreeConfig`; opening a lane writes empty overrides
for those keys only in the new worktree.

After dispatch, a builder opens its row and reads the returned workspace and
exact `base_sha`:

```sh
floati lane open --root /absolute/fleet --as builder --row row-one --repo product
```

The worktree is `/absolute/product-lanes/builder/work/row-one`, with branch
`codex/lane/builder/row-one`. Work and bank from that returned directory. When
finished, close the recorded workspace:

```sh
floati lane close --root /absolute/fleet --as builder --row row-one
```

Closing retains its Git branch and durable lane history. Dirty files, untracked
files, commits absent from all remote refs, and runtime references block removal.
`--force --why "reason"` explicitly permits losing dirty or unpublished work;
it never overrides runtime use or unavailable inspection. Reopening the same row
requires the operator to archive or rename its retained branch first.

## Integration train workspace

The integrator opens each train through the same ownership record:

```sh
floati lane open --root /absolute/fleet --as integrator --row train-one --repo product
```

Compose the approved banked rows and run the train's required checks from the
returned workspace. Land only through the fleet's existing publication authority.
After the row lands, its declared board entry in `state/lane-board.json` may name
that measured state:

```json
{"schema_version": 0, "rows": {"train-one": "landed"}}
```

Board states are `open`, `landed`, or `struck`; missing board evidence never means
landed. Preview cleanup, then apply the recorded eligible set:

```sh
floati sweep --root /absolute/fleet
floati sweep --root /absolute/fleet --apply
```

Sweep preflights all eligible recorded lanes before removing any. It also lists
unmanaged directories with measured age and bytes, leaves them untouched, and
returns degraded when they exist. Doctor reports open lanes and oldest open age
per node, plus unmanaged bytes under the declared lanes root. Unknown measurements
remain unavailable. Existing parking checkouts, live daemon bindings, and hand-made
worktrees are not migrated or adopted by these recipes.
