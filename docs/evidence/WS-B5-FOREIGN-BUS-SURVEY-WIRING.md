# WS-B5 foreign-bus survey wiring

Status: **DRAFT - awaiting integration-train reconciliation and the architect copy gate**

## Read-only survey delivered

`floati/foreign_bus_survey.py` runs only after an explicit `run()` call. Its
search corpus is the union of caller-supplied absolute directories and the
parents of B4 declared roots. It examines each search directory and its
immediate child directories only; it never falls back to the home directory or
recurses through an unbounded tree.

Candidate schema is explicitly apparent, not asserted: classification uses
directory-entry names and file types without reading foreign ledger bytes.
Declared Floati roots are excluded from foreign results.

Optional Codex hook and target files are bounded, absolute, non-symlink JSON
inputs. Correlation canonicalizes path tokens from hook commands and reports a
binding only when that foreign waiter path and one of our declared roots or B2
workspaces appear in the supplied registration files.

The survey imports no send, drain, acknowledgement, registration, or foreign
ledger client. A fixture snapshot proves that every file and directory under
the full survey corpus is byte-for-byte and type-for-type unchanged.

## Integration seam

After the train lands, shared CLI reconciliation adds `floati survey` with:

- a required absolute B4 declared-roots registry;
- repeatable explicit search paths;
- fixed read-only Codex hook and target inputs selected by the ruled installer;
- a DRAFT wizard offer that runs only after the operator accepts it;
- JSON artifact output and Fable-gated human copy;
- final static help and bundle-manifest regeneration.

No live foreign bus was deeply inspected on this lane. The live machine names
in the brief remain existence-check-only integration specimens for the final
gate.

## RED-first evidence

- Initial focused run failed at import with `ModuleNotFoundError: No module named
  'floati.foreign_bus_survey'`.
- The first implementation missed a `/var` versus `/private/var` alias between
  hook testimony and the surveyed root. The focused binding test failed; hook
  command path tokens are now canonicalized without opening the waiter.
- Focused gate: `python3 -m unittest -v tests.test_foreign_bus_survey` (5 tests).
