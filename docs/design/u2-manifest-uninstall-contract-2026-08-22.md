# U2-MINIMAL — install manifest + uninstall contract (v1)

Filed by alice-necro 2026-08-22 per architect assignment (msg-01a027d6f4d7,
LAUNCH CUT owner-ratified @09283dc7). Normative input:
`docs/design/floati-hardening-intake-2026-08-22.md` §U2. Status: CONTRACT
FOR APPROVAL — build begins only on architect approval of this doc.

Design law honored: **append-only, replay-not-guess, Trash-only user-data
sanitation, fail-closed at the first unreadable byte.**

## 1. The manifest — `install-manifest.v1.jsonl`

One JSONL file in the install dir (beside the `floati` binary). Append-only:
every wiring action appends exactly one entry; entries are never edited or
removed (uninstall is a REPLAY, and even uninstall appends).

```json
{
  "v": 1,
  "ts": "2026-08-22T12:00:00Z",
  "actor": { "command": "install", "floatiVersion": "1.0.0" },
  "action": "install",
  "kind": "file",
  "path": "/usr/local/bin/floati",
  "op": "create",
  "sha256": "<hex of content at write time>",
  "backupPath": null,
  "selector": null,
  "preserved": false
}
```

Field law:

- `kind` ∈ `file | hook_entry | plugin | flag | marker | state_dir |
  bus_root | dir` — closed set; unknown kinds on read are PRESERVED +
  reported (forward-compat: never guessed).
- `op` ∈ `create | modify | replace | delete`.
- `hook_entry` (shared-file surgery, e.g. hooks.json) additionally carries
  `selector` (the surgical key we added) and REQUIRES a pre-edit
  `backupPath` (timestamped copy of the whole file before our edit).
  Removal = restore-from-backup minus selector; never rewrite blind.
- `bus_root` / `state_dir` entries carry `"preserved": true` implicitly on
  uninstall (user records); `--purge` flips them to Trash-moves.
- `prev_hash`: each entry appends `"prevHash": <sha256 of previous entry's
  canonical bytes>` — cheap tamper-evidence in the house CAS idiom; v1
  verifies the chain during dry-run and reports the break offset.
- Manifest itself is never deleted by uninstall; it is RENAMED to
  `<manifest>.uninstalled.<ts>` and referenced from the tombstone receipt.

## 2. Uninstall semantics

### 2.1 `floati uninstall --dry-run` (first-class)

Prints, without touching anything:

1. **REVERSE-REPLAY PLAN** — manifest entries newest-first that WILL be
   removed (shipped classes: `file`, `plugin`, `marker`, `flag`,
   `hook_entry`), each with its recorded path + op.
2. **SURGICAL STEPS** — shared-file edits called out explicitly:
   backup-first restore of `<container>` minus `selector`.
3. **PRESERVED** — every `bus_root`, `state_dir`, and unknown-kind entry,
   with absolute paths ("your ledgers and receipts live here").
4. **Chain check** — prev-hash verification result + first-bad-offset if
   broken.
5. Missing manifest ⇒ plan prints `no manifest found; nothing attributable`
   and exit 0. **Never a glob** — no manifest, nothing to attribute.

### 2.2 Default uninstall (no flags)

Replays the plan for real: shipped classes removed (own files deleted;
shared files surgically restored after fresh timestamped backups); bus
roots and state dirs PRESERVED with their paths printed; tombstone receipt
written to a user-visible path (`~/floati-uninstalled-<ts>.json`) recording
every removal, preservation, backup path, and the renamed manifest — then
the path is PRINTED, not hidden.

### 2.3 `--purge`

Same as default, plus: preserved bus roots/state dirs are moved to the
macOS Trash under collision-safe names (`floati-<rootname>-<ts>`), never
hard-deleted, and an exported summary receipt lands beside the tombstone.
Trash-only sanitation law — hard-delete of user data is out of scope forever.

### 2.4 Fail-closed

First unreadable/corrupt manifest line STOPS the replay: everything after
that offset is reported as UNRESOLVED, partial work is rolled back where
possible, and the error names the byte offset. A half-uninstall must be
visible, not silent.

## 3. Wiring actions that append (v1 set)

`install`, `update`, `register` append entries for what they actually
write: installed binary/launcher files, hook entries added to shared
config, plugins placed, enable flags/markers created, state dirs created,
bus roots initialized. Actions that write NOTHING append nothing. Update
appends `replace` entries with new checksums (old checksum retained in
`note` for rollback chains).

## 4. Acceptance (v1)

On a machine wired as deeply as the intake machine: `uninstall --dry-run`
prints a plan whose PRESERVED section contains every bus root;
`uninstall` executes it; `doctor` from a fresh download reports a clean
host (zero orphaned hooks/plugins/markers/flags); the tombstone + renamed
manifest + Trash contents account for every entry ever written.

## 5. Out of scope (v1)

LaunchAgent management · multi-install discovery · remote/parallel fleet
uninstall · non-macOS Trash semantics · manifest compaction.

— alice-necro, U2-MINIMAL contract. Build begins on architect approval.
