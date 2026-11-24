"""A private copy of the live ledger-contract inputs, for checks that take the contract lock.

The contract check holds `bundle/.ledger-contract.lock` while it runs and
refuses when the file exists. A test that checks the running checkout itself
leaves that file in the tree where a parallel worker can copy or check it. Copy
today's candidate bytes, uncommitted edits included, and check the copy.
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def copy_live_contract_inputs(tool, candidate, *, root=ROOT):
    """Copy what `tool.check` reads from `root` into the empty directory `candidate`."""
    for relative in ("floati", "schemas", "bundle", "tests", "tests/fixtures"):
        if (root / relative).is_symlink():
            raise AssertionError("live contract input parent is a symlink: " + relative)
    for relative in ("floati", "schemas", "bundle"):
        shutil.copytree(root / relative, candidate / relative, symlinks=True,
                        ignore=shutil.ignore_patterns(".ledger-contract.lock"))  # nobody owns a copied lock
    preparation = Path(tool.PREPARATION)
    (candidate / preparation).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / preparation, candidate / preparation, follow_symlinks=False)
    return candidate
