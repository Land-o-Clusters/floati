"""Importing a reusable TestCase must not silently rerun its original tests."""

import unittest

from scripts.discovery_census import native_census


class DiscoveryCensusTests(unittest.TestCase):
    maxDiff = None

    def test_test_cases_have_one_native_discovery_origin(self):
        # A module importing a TestCase directly exposes it to unittest again.
        # Inheritance remains legitimate: the subclass has its own identity.
        census = native_census()
        self.assertEqual({}, census["classes_with_multiple_origins"])
