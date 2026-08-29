# SD-3 installed role-template boundary

Status: **DRAFT - integration evidence candidate**

Branch: `repair/sd3-installed-roles-20260828`

## Finding

The shakedown refusal was a packaging omission, not a parser or template-data
defect. At the exact pre-E2 trunk `0f3e185ed383e4e393744058a1522729c8148c55`,
the six typed files existed under `roles/shipped/`, but neither
`floati.manifest._deployable_paths` nor `bundle-manifest.v0.json` selected them.
The installed launcher therefore had no role bytes at the path resolved from
its installed `floati/admin_cli.py` module.

E2 later repaired the production boundary by deriving the shipped inventory
from `SHIPPED_ROLE_NAMES`. The activated install now contains exactly the six
files, and its real `scripts/floati role list` query returns `architect`,
`builder`, `github-manager`, `researcher`, `reviewer`, and `sre`.

## RED and perturbation proof

A detached worktree at the pre-E2 trunk ran the new installed-runtime test
against a bundle assembled only from that commit's manifest. It failed exactly
at the product boundary:

- one test executed;
- installed `role list` exited 20;
- artifact code: `role_template_path_invalid`;
- detail: `role template path could not be opened safely`.

On the repaired tree the same test passes. It then removes only
`roles/shipped/reviewer.json` from the scratch installed tree and proves the
real installed launcher returns the same typed exit-20 refusal. This
perturbation demonstrates that the guard observes installed packaging, not the
source checkout.

## Permanent guard

`tests/test_context_activation.py` now creates a runtime containing exactly the
manifest-listed bytes, invokes its installed `scripts/floati` launcher from a
separate working directory, and requires the exact six-role roster. No
production byte or user-facing copy changes in this row.

## Verification

- New installed-runtime guard: **1 test, OK**.
- Install, uninstall, activation, and role-template focused bank: **38 tests,
  OK**.
- Canonical full suite after linearizing onto `origin/main` at
  `32177fcdfbbe3a45cea2ac89e72dd14a22d25e18`: **2,024 tests, OK**, 195.610
  seconds. The final rebase added NC-1 production and regression bytes, so L4
  was rerun instead of relying on the earlier 2,019-test result.
- Negative missing-template perturbation: exit 20,
  `role_template_path_invalid`.
- Manifest-last verification: `[]`; manifest SHA-256
  `74705e372900b377c3398a597d0c2b03c6e6bf0674674f0e05c2f2985da9cd1b`.
- Foreign-project/name-sweep bank: **13 tests, OK**.
- `git diff --check`: no output, exit 0.

## Boundary

This row proves source-to-install closure for the shipped role library. It does
not install or activate a new runtime, alter a hook, grant authority, flip a
release surface, or edit `README.md`.
