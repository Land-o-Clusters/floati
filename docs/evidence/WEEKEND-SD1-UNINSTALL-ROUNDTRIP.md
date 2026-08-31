# DRAFT — Weekend SD-1 uninstall round-trip evidence

Date: 2026-08-28  
Lane: `build lane`  
Branch: `repair/sd1-uninstall-20260828`

## Scope and fences

This repair is limited to the manifest-exact Floati uninstall path and its
regression coverage. It does not edit a README, activate or flip any wake
surface, remove a fleet root, or touch any foreign-project artifact. The governed
chaos fleet at `~/floati-chaos-20260828/fleet` is retained.

## Root cause

`scripts/floati-codex-wait` entered the install-owned manifest in commit
`94f19c9aacc9dc19ec6a2d9fbc29b81883671075` on 2026-08-27. The install writer
accepts every safe relative path verified by `bundle-manifest.v0.json`, but the
uninstall reader independently bounds deletion to a narrower tool-path set.
That set still permitted only `scripts/floati`, so the later waiter launcher
was installable but not uninstallable. The live chaos-site manifest therefore
failed before mutation with:

`uninstall_manifest_invalid: manifest path is outside the Floati tool bundle: scripts/floati-codex-wait`

The repair adds that exact launcher to the deletion boundary. It does not
broaden the boundary to arbitrary scripts.

## RED then GREEN

Permanent coverage now exercises install, dry-run uninstall, and actual
uninstall with both owned launchers while proving a foreign fleet ledger is
retained. A second test checks every current install-manifest path against the
uninstall deletion boundary, so any future owned-set addition re-gates the
round trip.

- RED: 2 tests; one error and one failure; both named
  `scripts/floati-codex-wait`; 0.125s.
- GREEN: 2/2 passed in 0.112s.
- Deploy/uninstall bank: 22/22 passed in 1.866s.

## Live acceptance preview

The repaired source ran a non-mutating dry-run against exactly
`~/floati-chaos-20260828/floati`. It returned `status: ok`,
`dry_run: true`, `removed_count: 0`, no foreign files, and digest-bound
receipts for all 267 installed owned files plus the ownership manifest. The
preview included `scripts/floati-codex-wait`. No live byte was removed during
this preview.

## Frozen evidence

- Candidate commit used for live acceptance:
  `981db443c77ad53c070b7d1f1e794f512ac73862`.
- Manifest SHA-256:
  `b53e2859a3528eed5f920904e73a7a2ba0df78da545f4425fa3afe003c8df726`.
- First canonical run: 1,968 tests in 231.974s, RED only because this evidence
  file repeated a scrub-forbidden foreign-project token. The token was removed;
  the source-scrub bank then passed 8/8 in 0.475s.
- Canonical repaired-tree run: 1,968/1,968 passed in 212.213s.
- Live uninstall: `status: ok`, `dry_run: false`, `removed_count: 268`, with
  267 owned-file receipts plus the ownership-manifest receipt.
- Postcondition: the installed waiter launcher is absent. The tool destination
  retains only `.floati-install` journal evidence; the separate chaos fleet
  directory remains present with its ledgers and receipts.

The final remote SHA and fleet envelope receipt are supplied in the governed
row-boundary message because a Git commit cannot truthfully contain its own
object ID.
