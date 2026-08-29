# Example fleet contract

Status date: 2026-07-31.

The `tenant-a` example uses the durable home `/var/tmp/floati-tenant-a`.
Every command below passes that root explicitly. Floati has no default root,
environment-root fallback, daemon, or wake command.

| Node | Harness | Repository |
| --- | --- | --- |
| reviewer | Claude | no fixed repository; receives repository-bound fleet notifications |
| lane-app | Codex | `~/fleet/app` |
| lane-floati | Codex | `~/fleet/floati` |

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

# Run by lane-app from its Codex harness.
/repo/floati/scripts/floati register --root /var/tmp/floati-tenant-a lane-app --harness Codex

# Run by lane-floati from its Codex harness.
/repo/floati/scripts/floati register --root /var/tmp/floati-tenant-a lane-floati --harness Codex
```

## Polling and receipts

Every node polls at boot and before stand-down. There is no wake adapter in
HM-0.5:

```sh
/repo/floati/scripts/floati inbox --root /var/tmp/floati-tenant-a --as reviewer
/repo/floati/scripts/floati inbox --root /var/tmp/floati-tenant-a --as lane-app
/repo/floati/scripts/floati inbox --root /var/tmp/floati-tenant-a --as lane-floati
```

An inbox presentation writes a delivery receipt. After reviewing the Git
notification, the receiving node may acknowledge that exact message:

```sh
/repo/floati/scripts/floati ack --root /var/tmp/floati-tenant-a --as reviewer --id <message-id>
```

Acknowledgment is not completion. It records the exact presented message ID;
it does not claim that the named commit or document was read, applied, tested,
accepted, or finished. Git and the repository-relative evidence document
remain authoritative. A lost notification costs only polling latency.

## Notification and replay

A sender names a repository, exact lowercase 40- or 64-character Git SHA,
repository-relative evidence document, and bounded note:

```sh
/repo/floati/scripts/floati send --root /var/tmp/floati-tenant-a --from lane-floati --to reviewer --repo floati --sha <checkpoint-sha> --doc docs/evidence/checkpoint.md --note checkpoint
/repo/floati/scripts/floati log --root /var/tmp/floati-tenant-a
```

The message is a notification, not a substitute for the named Git evidence.
