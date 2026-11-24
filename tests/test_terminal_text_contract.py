"""Terminal diagnostics retain exact first unsafe character evidence."""
import unittest
import unicodedata

from floati.records import _terminal_unsafe_hit


class TerminalTextContractTests(unittest.TestCase):
    def test_every_ascii_character_retains_control_classification_and_offset(self):
        for ordinal in range(128):
            with self.subTest(ordinal=ordinal):
                expected = ("control", 6) if ordinal < 32 or ordinal == 127 else None
                self.assertEqual(expected, _terminal_unsafe_hit("prefix" + chr(ordinal) + "suffix"))
        self.assertIsNone(_terminal_unsafe_hit(""))

    def test_first_unsafe_character_wins_across_controls_and_unicode(self):
        for character, category in (("\x00", "control"), ("\x1f", "control"),
                ("\x7f", "control"), ("\x85", "control"), ("\ud800", "surrogate"),
                ("\u202e", "bidi"), ("\u2067", "bidi"), ("\u00ad", "bidi")):
            with self.subTest(character=repr(character)):
                self.assertEqual((category, 4), _terminal_unsafe_hit("café" + character + "\x00"))
                self.assertEqual(("control", 0), _terminal_unsafe_hit("\x00" + character))
        self.assertIsNone(_terminal_unsafe_hit("café 水 💧"))

    def test_string_subclass_cannot_claim_unicode_is_ascii(self):
        class MisreportedASCII(str):
            def isascii(self):
                return True

        self.assertEqual(("bidi", 4), _terminal_unsafe_hit(MisreportedASCII("safe\u202e")))

    def test_every_unicode_codepoint_matches_the_terminal_contract(self):
        bidi_controls = {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for ordinal in range(0x110000):
            character = chr(ordinal)
            category = unicodedata.category(character)
            expected = (("control", 4) if category == "Cc" else
                ("surrogate", 4) if category == "Cs" else
                ("bidi", 4) if unicodedata.bidirectional(character) in bidi_controls else None)
            actual = _terminal_unsafe_hit("safe" + character + " suffix")
            if actual != expected:
                self.fail(f"U+{ordinal:04X}: expected {expected!r}, observed {actual!r}")


if __name__ == "__main__":
    unittest.main()
