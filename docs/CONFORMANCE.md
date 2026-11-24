# Conformance v0

Status date: 2026-07-31.

`work_duplicate_open` refuses a new work item matching an existing open item;
use the existing item or change its details. Historical duplicate records remain
readable.

`work_already_claimed` identifies a claim collision; choose an open item or
coordinate with its current holder. `authority_superseded` identifies a prior
holder whose subject now has a newer grant to another node; ask the coordinator
for a current grant.

The conformance artifact is invoked directly:

```bash
python3 -m floati.conformance \
  --adapter package.module:factory \
  --root /absolute/protocol/root \
  --tenant tenant-id
```

The factory accepts one `FloatiRoot` and returns an adapter implementing the
methods exercised by `floati.conformance.run`. Each method returns an
`AdapterResult` with one of these statuses:

- `ok`: the evidence object describes an observed result;
- `refused`: the evidence object contains the stable refusal `code`; or
- `intentional_silence`: policy deliberately chose not to act.

Returning `None`, returning another object shape, and raising unexpectedly are
three different outcomes. The v0 artifact exit codes are:

| Code | Meaning |
| ---: | --- |
| 0 | all conformance cases passed |
| 10 | evidence contradicted a required behavior |
| 20 | adapter or explicit-root configuration was refused |
| 30 | the adapter factory or operation died |
| 31 | the adapter chose intentional silence |
| 32 | the adapter returned no result |
| 33 | the adapter returned malformed evidence |
| 34 | the deployed bundle manifest did not match |

## The launcher's interpreter

`scripts/floati` is the shipped entry point, and it **never resolves its
interpreter through `PATH`** - the discovery the executable-provenance policy
forbids (`docs/rulings/2026-09-01-three-policies-for-finding-an-executable.md`;
`shutil.which` is banned under the same rule). It takes the system-binary
shape: a fixed ordered candidate list, `/usr/bin/python3` then
`/bin/python3`, first present and executable wins.

An operator overrides that with one environment variable:

| Variable | Meaning |
| --- | --- |
| `FLOATI_PYTHON` | one absolute canonical interpreter path; a symlink is refused, so name the resolved target |
| `FLOATI_LAUNCHER_INTERPRETER` | set BY the launcher to the path it selected, so `doctor` can report the choice; not an operator input |

With neither a declaration nor a candidate present the launcher refuses,
typed, at **exit 20** - the refusal code above. There is no `PATH` fallback:
a `PATH` an attacker can prepend to would choose the Python that runs the
whole product, and being unable to start is a safe state in a way that
starting the wrong interpreter is not.

`floati doctor` reports the result as the `launcher_interpreter` finding. Its
detail opens with the source it derived - `declared:` (the operator named it),
`candidate:` (the fixed list), `absent:` (this process was not started through
the launcher, so no launcher choice belongs to it), or `unruled:` (a selection
claiming to be the launcher's that is in neither ruled set, which degrades the
artifact). The finding names the selected path **and** the interpreter actually
running, because the two are not always the same file: `/usr/bin/python3` on
macOS is a shim onto a framework build.

HM-0.5 also provides a structurally throwaway reference-core smoke:

```bash
python3 -m floati.conformance --live-root-smoke
```

The smoke function takes no path or root argument. It owns a
`TemporaryDirectory`, creates a direct home only below that directory, and
round-trips one Git notification through delivery and acknowledgment before
verifying typed `unknown_sender` and `unknown_recipient` refusals expose the
exact sorted active roster while leaving the whole tenant root byte-identical,
including an empty unknown-party denial list. The temporary directory is
removed on return. `--live-root-smoke` is mutually exclusive with `--adapter`,
`--root`, and `--tenant`, so this selftest surface cannot touch the fleet's
durable home.

The adapter-mode fixed cases observe registry writes and, before interpreting
any unknown-party refusal, directly verify that every registered send changed
the tenant root and appended one matching durable `events.jsonl` envelope.
They then observe message delivery, separate delivery and acknowledgment
receipts, a non-contiguous acknowledgment, typed unknown sender/recipient
refusals with the exact active roster and whole-root immutability, an empty
unknown-party denial list, liveness silence, authority deadline enforcement,
and separated exclusion evidence.

The artifact emits one machine-readable JSON object. Product-visible copy is
`COPY PENDING — ARCHITECT`; these status identifiers are protocol vocabulary, not
interface copy.
# Work notification journal

`work add --as D` records an inert `work_notification_record` intent before
the unchanged work item. With an artifact it sends ordinary, normal mail to
the nodes eligible at send time. Completion mail uses the completing actor
and completion artifact, addressed to the intent's dispatcher. Sent testimony
lives only in `receipts/work-notifications/<item>.jsonl`; no journal row is
part of the work ledger or the Confluence bundle.

The fixed notes are `Work available: <item id>` and `Work completed: <item id>`
(`ADD_NOTE` and `COMPLETE_NOTE` in `floati/work_notification.py`). Keys are
`work-add-<item id>-<recipient>` and `work-complete-<item id>-<recipient>`.
Neither note nor key is provenance; the journal's sent row is the join.

Only a recorded sender replays mail in a write verb or `work notify --id I
--as X` (`--all` selects X's pending items). The journal is the first guard,
live envelopes the second, and native retained epoch history the third.
`work show` and `doctor` read without sending. Orphan intents stay inert and
are counted as `work_notification_intent_orphan` with severity `ok`.

Successful work with missing inputs reports `work_add_unnotified` or
`work_complete_unnotified`, with `missing` and `missing_flags`. A send refusal
becomes `work_notification_refused` with recipient, `refusal_code` and the
send's own remedy; the CLI exits degraded (35). Another sender's pending
mail is `work_notification_pending`, naming the sender and exact notify
command. Doctor reports one warning per pending item (35),
`work_notification_conflict` for absent or disagreeing sent-envelope evidence,
or `work_notification_unreadable` naming an unreadable journal; other journals
still report. Conflicting content refuses `idempotency_conflict`, never rewrites.
Car A's `work_duplicate_open`, `work_already_claimed` and
`authority_superseded` retain their meanings and refusal order.
