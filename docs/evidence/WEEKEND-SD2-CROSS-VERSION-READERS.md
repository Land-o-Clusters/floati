# DRAFT — Weekend SD-2 cross-version reader evidence

Date: 2026-08-28  
Lane: `build lane`  
Branch: `repair/sd2-reader-law-20260828`

## Ruled contract

A shared bus is read by its oldest participant. Whole-fleet readers therefore
keep processing when a newer writer appends a well-formed kind that this
reader does not implement. They skip the unknown row and emit a deterministic
`unrecognized_kinds` entry containing its kind, count, and first record id.

Known kinds remain exact-contract evidence: malformed known rows still fail
closed. Read failures now name the ledger path, record id, and kind. Writers
remain strict and cannot append an unknown kind through the governed API.

The event-ledger vocabulary is owned once by `EVENT_KINDS`; status, doctor,
graph, supervise, and the status snapshot source import that vocabulary instead
of maintaining smaller reader-local copies.

## RED then GREEN

- RED: 2 tests in 0.181s. Status, supervise, and graph each failed
  `record_kind_invalid` on a well-formed future kind; malformed-known errors
  omitted ledger attribution.
- First GREEN: 2/2 in 0.111s.
- Integrated reader/snapshot/CLI/schema/manifest bank: 178/178 in 18.955s.
- The compatibility fixture appends two rows of the same future kind, proving
  skip, count (`2`), first-id retention, and the same note on all four named
  whole-fleet surfaces.
- The negative fixture appends a malformed known `message_envelope`; status,
  graph, and supervise raise `record_fields_invalid`, while doctor reports exit
  33, and every detail includes `events.jsonl`, the exact id, and the kind.

## Protocol and packaging

- Frozen JSON inventory remains 143 assets with unchanged path digest
  `e6eff4279c7b34f3058615f80300da3148adfab492120ec341b5f4317bebc856`.
- Intentional schema content digest rebaseline:
  `10c3f30bcf876d50b3e8939f03726179e336832b45a5158c295ec5a5f838ec32`.
- `bundle-manifest.v0.json` was regenerated after all deployable source and
  schema bytes; direct verification returned `[]`.

## Frozen evidence

The exact final suite count, duration, manifest digest, remote SHA, and fleet
envelope receipt are supplied after the branch tip is frozen and pushed.
