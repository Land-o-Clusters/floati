"""L1 fence: this draft stays out of the product package graph.

The floati product graph is FLOATI.toml + bundle-manifest.v0.json (the
governed deployable set). If either ever names this draft, the release
dial has grown a tooth and this test reddens — dark means out of the
release binary by construction, not by promise.
"""

import json
import os
import unittest


class CompilationFenceTests(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..")
        )

    def test_product_manifest_never_names_this_draft(self):
        manifest = os.path.join(self.repo, "bundle-manifest.v0.json")
        with open(manifest, encoding="utf-8") as handle:
            text = handle.read()
        for spelling in ("night-watch", "night_watch", "NightWatch"):
            self.assertNotIn(spelling, text)

    def test_product_config_never_names_this_draft(self):
        config = os.path.join(self.repo, "FLOATI.toml")
        if not os.path.isfile(config):
            self.skipTest("FLOATI.toml not present in this tree")
        with open(config, encoding="utf-8") as handle:
            text = handle.read()
        for spelling in ("night-watch", "night_watch", "NightWatch"):
            self.assertNotIn(spelling, text)


if __name__ == "__main__":
    unittest.main()
