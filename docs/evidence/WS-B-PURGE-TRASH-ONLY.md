# WS-B purge — Trash-only preserved-root sanitation

Status: **DRAFT - seven-finding dark repair complete; activation and Fable
copy restamp remain separate**

Original base: `0701f4d88f5a1dfb638e3550b2b0e489bddcf894`
Repair base: `ad62e46a7f039e6e1ee4c88bc2c890162d663715`
Integrated main: `b6e5c25537466bc615f47da7efb75ae2adf3932f`
Branch: `lane/ws-b-purge`
RED commit: `57d88e5` (`test: pin seven purge repair blockers`)
Repair implementation commit: `2476775` (`fix: close purge activation blockers`)
Restoration-proof fix: `916b8a6` (`fix: prove purge rollback restoration`)
Review RED commit: `7c59dff` (`test: expose purge transaction review gaps`)
Descriptor-bound repair commit: `59cf44d` (`fix: bind purge moves to verified
directories`)
Rollback-inspection fix: `4637e78` (`fix: close purge rollback inspection
evidence`)
Rollback-target identity fix: `e5b5f16` (`fix: preserve replaced rollback
targets`)

## Scope

`floati/purge.py` remains a dark, separately registered `purge` seam. It
accepts only explicit absolute preserved-root directories and moves whole
roots into the fixed, validated macOS account Trash. The account home comes
from the effective account record, not caller-controlled `HOME`; tilde roots
are not expanded. The public writer and helper surfaces expose no alternate
Trash destination, and execution validates every caller-visible plan field
back to the fixed Trash and requested roots. Tests replace only the private
`_trash_dir` resolver. This row does not activate the shared CLI/help seam or
restamp the purge manifest entry.

The writer inventories regular files with SHA-256, size, source path, root,
and planned Trash path. Dry-run receipts mark those digests as `plan-scan`.
Successful move receipts mark them `post-move-verified` only after a complete
descriptor-relative identity/digest observation following the exclusive root
rename. Source-parent and Trash directory descriptors remain held through the
whole forward/rollback transaction. This is an observation boundary, not a
claim that a mutable directory remains frozen after the receipt.

No user-data hard-delete primitive or copy-and-remove fallback exists. Forward
and rollback moves use the host filesystem's exclusive rename primitive, so an
occupied case- or normalization-equivalent destination cannot be overwritten.
Visible copy remains `DRAFT -`.

## Seven accepted blockers and repairs

1. **Symlinked ancestor:** a preserved root whose fully resolved path differs
   from its lexical input refuses with `purge_root_symlink_ancestor`; the
   refusal names both paths. The intentionally conservative `/var` to
   `/private/var` over-refusal is preserved by the governed ruling.
2. **Caller-selected destination:** `trash_dir` was removed from
   `PurgeWriter` and `plan`; execution revalidates the fixed Trash identity,
   every root target name/parent, every file target, and the original writer
   request before mutation. The fixed account home ignores `HOME`.
3. **Late EXDEV detection:** every planned root device is compared with the
   fixed Trash device before the first source rename. Cross-device requests
   refuse as `purge_cross_device`.
4. **Swallowed rollback:** rollback uses descriptor-relative exclusive rename.
   If any prior root cannot be proven restored at the original path with its
   planned identity, `purge_rollback_failed` is a `DurabilityFailure` carrying
   closed `stranded_root_receipts`; restored roots are listed separately.
   `_handle` preserves that evidence as `degraded` with exit 35.
5. **Scan-to-rename drift:** each source root and parent are opened no-follow,
   checked with `fstat`, completely rescanned through held descriptors, and
   rebound immediately before descriptor-relative rename. The moved root and
   complete file inventory are verified again after movement. Source-parent,
   source-name, Trash-path, or file/root drift cannot yield success receipts.
6. **Lexical collision reservation:** the actual root move uses macOS
   `renameatx_np(..., RENAME_EXCL)`. The filesystem, rather than string
   comparison, decides destination equivalence and prevents overwrite.
7. **Unreadable traversal omission:** `Path.rglob` was replaced with explicit
   `os.scandir` traversal. Failure to enumerate or inspect any subtree refuses
   as `purge_root_unreadable`; it cannot silently reduce the inventory.

## RED-first evidence

Before any production repair, the new `tests.test_purge_repair` bank ran seven
tests with **seven intentional failures** and no collection errors:

- ancestor alias was accepted;
- public Trash authority remained injectable;
- cross-device preflight seam was absent and mutation was attempted;
- rollback failure was swallowed without stranded evidence;
- a same-window file swap escaped the bulk verifier;
- the exclusive rename seam was absent; and
- an omitted unreadable subtree produced a successful incomplete plan.

The rollback perturbation was refined while production was still untouched so
the same exclusive primitive is exercised in both directions; it remained RED.
The implementation self-review then found a second proof gap: when an already
moved root disappeared from both its original and planned Trash paths before
rollback, path-absence logic could claim restoration. A focused eighth test was
RED (`purge_move_failed` instead of a stranded receipt), then the rollback audit
was changed to require the original root's planned device/inode identity and
the Trash path's absence.

The first independent review returned `Spec: FAIL` / `Quality: CHANGES` with a
forged-plan critical finding plus fixed-home, descriptor binding, post-move
digest, rollback-classification, late-root EXDEV perturbation, and real APFS
equivalence findings. The review RED commit added those cases before the next
production change: **14 tests, 8 intentional failures**. A subsequent
self-review added a source-parent replacement perturbation; it was independently
RED before the post-move parent proof was added. Post-move file mutation and
degraded-but-restored evidence are also pinned directly. A final rollback
inspection perturbation proved that `EIO` could escape before a stranded
receipt; it was RED before rollback inspection became fail-closed.
The final line-by-line audit then replaced a moved Trash target with an
unrelated directory before rollback. The unguarded rollback moved that
replacement to the original path (RED); rollback now checks the target's
planned root identity before any reverse rename.

## Verification through rollback-target fix `e5b5f16`

- `tests.test_purge tests.test_purge_repair`: **27 tests, OK** (original 9 +
  repair/review 18).
- `tests.test_uninstall tests.test_cli tests.test_admin_cli`: **49 tests, OK**.
- `tests.test_source_scrub tests.test_name_sweep`: **21 tests, OK**.
- `tests.test_schemas tests.test_manifest`: schemas all green; **2 expected
  manifest failures**, both the unchanged manifest digest for
  `floati/purge.py`.
- Full canonical discovery: **1,948 tests** in **222.507s**; **2 failures + 3
  errors**. The two failures are the same `floati/purge.py` manifest-digest
  refusal. The three demo-capture errors are downstream of committed-tree
  install refusing that digest. No purge, CLI, schema, or unrelated product
  test failed. Host sandbox-init refusals, a background orchestrator fixture's
  typed `worker_claim_missing` trace, and pre-existing roster resource warnings
  were diagnostic output, not additional test failures.
- `git diff --check`: **OK** before the evidence amend.

The manifest mismatch is deliberate in this held repair row: activation,
static help/copy restamp, and manifest regeneration remain the next governed
row. This evidence makes no activation, release, or publication claim.

## Review

The initial independent review returned `Spec: FAIL` / `Quality: CHANGES`; all
of its findings are represented by the review RED bank and subsequent fixes.
The higher-reasoning final audit additionally found and closed source-parent
replacement, rollback-inspection, and replaced-target gaps with focused REDs.

Two fresh independent review turns were requested after the fixes. Both
completed without returning a visible artifact, so this evidence does **not**
invent a PASS receipt: independent final verdict is `CANNOT_DETERMINE` pending
the architect's checkpoint review. Exact-head focused, full, scrub, no-delete/
no-copy scan, and diff-check receipts remain available above.
