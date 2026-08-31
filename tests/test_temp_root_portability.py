"""No fixture may spell a platform's temporary root as a literal.

Seventy-six fixtures spelled it `\x2fprivate/tmp`, which exists on macOS and does
not exist on Linux, and the ubuntu CI runner raised `FileNotFoundError` for
every one of them. The suite was measuring the host, not the product — and
because the only machine anyone ran it on was a Mac, the suite was green
locally and had been red in CI for days without anyone reading the cause.

⇒ **A TEST THAT PASSES ONLY ON THE AUTHOR'S PLATFORM IS AN INSTRUMENT
REPORTING ON ITS ENVIRONMENT WHILE NAMING A PRODUCT SYMBOL.**

The fence is derived, not enumerated: it scans every test module rather than
holding a list that would go stale the first time somebody adds a file.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.temp_roots import PLATFORM_TEMP_LITERAL, REAL_TEMP_ROOT
from tests.temp_roots import REAL_TEMP_ROOT


TESTS = Path(__file__).resolve().parent


class TemporaryRootPortabilityTests(unittest.TestCase):
    def test_the_resolved_root_is_a_real_directory_and_not_a_symlink(self) -> None:
        """A symlinked root resolves differently once a path exists under it."""

        self.assertTrue(os.path.isdir(REAL_TEMP_ROOT), REAL_TEMP_ROOT)
        self.assertFalse(os.path.islink(REAL_TEMP_ROOT), REAL_TEMP_ROOT)
        self.assertEqual(os.path.realpath(REAL_TEMP_ROOT), REAL_TEMP_ROOT)

    def test_no_test_module_hardcodes_a_platform_temporary_root(self) -> None:
        """Catches a new fixture pinning a platform temporary root as a literal.

        The pattern is imported rather than written here on purpose: A FINDING MAY
        NOT QUOTE ITS OWN FORBIDDEN TOKEN, or the scanner reports itself.
        """

        offenders: list[str] = []
        for path in sorted(TESTS.glob("test_*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = PLATFORM_TEMP_LITERAL.search(line)
                if match is not None:
                    offenders.append(f"{path.name}:{number}: {match.group(1)}")
        self.assertEqual(
            [],
            offenders,
            "use `dir=REAL_TEMP_ROOT` from tests.temp_roots; a literal temporary "
            "root is a host fact, and it is not the same host everywhere",
        )


if __name__ == "__main__":
    unittest.main()
