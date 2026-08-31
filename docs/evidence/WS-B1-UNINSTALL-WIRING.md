# WS-B1 uninstall wiring

Status: **DRAFT - awaiting integration-train reconciliation and the architect copy gate**

## Dark module delivered

`floati/uninstall.py` owns the complete uninstall behavior. It reads only the
installed `.floati-install/manifest.v0.json`, validates the full owned set and
every SHA-256 before mutation, removes only unchanged bundle files, then removes
the ownership manifest. Files absent from the manifest are reported and left in
place. Bus roots and ledgers are not installable paths and are retained.

`--dry-run` returns the same ordered, digest-bound removal receipt set, including
the ownership manifest, while leaving every byte unchanged. A normal run emits
one SHA-256 receipt for every removed file.

## Integration seam

The integration train owns `floati/cli.py`, static help, and final bundle
manifest regeneration. After the train lands, reconciliation consists of:

1. import `register_cli` from `floati.uninstall` under an unambiguous local name;
2. call that registration function once with the top-level command subparsers;
3. add static `floati uninstall --help` copy with `--destination` and
   `--dry-run` documented;
4. regenerate `bundle-manifest.v0.json` last, after every train and lane module
   is present.

The module's registration function adds exactly one `uninstall` parser and
binds the existing artifact handler contract. This lane does not edit any of
those shared integration files while Phase 0 is running.

## RED-first evidence

- Initial focused run failed at import with `ModuleNotFoundError: No module named
  'floati.uninstall'` before production code existed.
- The leading tests prove a foreign file remains byte-identical and a bus ledger
  remains byte- and inode-identical.
- A second RED replaced an owned file in place with same-length bytes after
  preflight; the first implementation removed it. The removal-boundary check now
  reopens the exact regular file and verifies device, inode, size, and SHA-256
  before unlink.
- Focused gate: `python3 -m unittest -v tests.test_uninstall`.

## Baseline accounting

Before WS-B1, `python3 -m unittest -q` ran 1,519 tests and ended with three
pre-existing documentation-gate failures caused by the newly ratified weekend
documents and publication-checklist wording. WS-B1 does not modify those files
or relabel that baseline as green.
