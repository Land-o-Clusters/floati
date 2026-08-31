"""A governed temporary prefix is a POSITION, not a substring.

`GOVERNED_TEMP_PREFIXES` ends with `\x2ftmp`, and every redactor replaced it with
`str.replace`, which does not know where a path begins. `/var/tmp` contains
`\x2ftmp` at index four, so the export turned the shipped CLI example

    floati init --root /var/tmp/fleet

into

    floati init --root /var<temp>/fleet

and published it — in `AGENTS.md`, the file an agent reads to learn the verbs.
Nothing caught it because the exporter validates the projected tree and never
runs it, and because `/var<temp>/` still looks redacted.

⇒ **A PREFIX IS A POSITION. A REDACTOR THAT MATCHES ANYWHERE WILL EVENTUALLY
CORRUPT SOMETHING THAT WAS NEVER SENSITIVE, AND CORRUPTION THAT LOOKS LIKE
REDACTION IS THE HARDEST KIND TO SEE.**
"""

from __future__ import annotations

import unittest

from floati.identity_fence import (
    PRIVATE_TMP_PREFIX,
    TMP_PREFIX,
    VAR_FOLDERS_PREFIX,
    redact_governed_temp_prefixes,
)


VAR_TMP = "/var" + TMP_PREFIX


class GovernedPrefixBoundaryTests(unittest.TestCase):
    def test_a_governed_prefix_at_the_start_of_a_path_is_redacted(self) -> None:
        self.assertEqual("<temp>/fleet", redact_governed_temp_prefixes(TMP_PREFIX + "/fleet"))
        self.assertEqual(
            "<temp>/fleet", redact_governed_temp_prefixes(PRIVATE_TMP_PREFIX + "/fleet")
        )
        self.assertEqual(
            "<temp>/x/T/y", redact_governed_temp_prefixes(VAR_FOLDERS_PREFIX + "/x/T/y")
        )

    def test_a_governed_prefix_after_a_non_path_character_is_redacted(self) -> None:
        """`ROOT=\x2ftmp/x` and `"\x2ftmp/x"` are path starts even mid-line."""

        self.assertEqual("ROOT=<temp>/x", redact_governed_temp_prefixes("ROOT=" + TMP_PREFIX + "/x"))
        self.assertEqual('"<temp>/x"', redact_governed_temp_prefixes('"' + TMP_PREFIX + '/x"'))
        self.assertEqual(
            "root <temp>/x", redact_governed_temp_prefixes("root " + TMP_PREFIX + "/x")
        )

    def test_var_tmp_is_left_alone_because_tmp_is_not_a_prefix_there(self) -> None:
        """The published defect, pinned: /var/tmp must survive intact."""

        self.assertEqual(
            "floati init --root " + VAR_TMP + "/fleet",
            redact_governed_temp_prefixes("floati init --root " + VAR_TMP + "/fleet"),
        )
        self.assertNotIn("<temp>", redact_governed_temp_prefixes(VAR_TMP + "/fleet"))

    def test_a_longer_governed_prefix_wins_over_a_shorter_one(self) -> None:
        """`\x2fprivate/tmp` must not be redacted twice into `/private<temp>`."""

        self.assertEqual(
            "<temp>/fleet", redact_governed_temp_prefixes(PRIVATE_TMP_PREFIX + "/fleet")
        )

    def test_a_word_that_merely_contains_a_prefix_is_untouched(self) -> None:
        for benign in ("/vartmp/x", "notatmp", "/usr/tmpfiles", "a/tmp"):
            with self.subTest(benign=benign):
                self.assertEqual(benign, redact_governed_temp_prefixes(benign))

    def test_the_replacement_token_is_caller_supplied(self) -> None:
        """The exporter's Python-literal escape needs a different replacement."""

        self.assertEqual(
            "X/fleet", redact_governed_temp_prefixes(TMP_PREFIX + "/fleet", replacement="X")
        )


if __name__ == "__main__":
    unittest.main()
