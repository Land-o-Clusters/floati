"""The controlled parser double preserves production TOML boundary questions."""
import builtins
import json
import unittest
from unittest import mock

from floati import seat_harness_config
from floati.errors import ProtocolRefusal

try:
    from tests import seat_toml_fixture as fixture
except ImportError:
    fixture = None


class SeatTomlFixtureTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(fixture, "controlled seat TOML fixture is not implemented")
        fixture.install_fixture_toml(self)

    def test_actual_wrapper_reads_root_hook_and_project_fixture_values(self):
        document = seat_harness_config._toml(
            b'# retained operator note\nmodel = "fixture"\n'
            b'[hooks.state."/scratch/with.dot/hooks.json:stop:0:0"]\n'
            b'enabled = false\ntrusted_hash = "fixture"\n'
            b'[projects."/scratch/with.dot/repo"]\ntrust_level = "trusted"\n'
            b'extra = "foreign"\n# preserved annotation\n')
        self.assertEqual({"model": "fixture", "hooks": {"state": {
            "/scratch/with.dot/hooks.json:stop:0:0": {"enabled": False, "trusted_hash": "fixture"}}},
            "projects": {"/scratch/with.dot/repo": {"trust_level": "trusted", "extra": "foreign"}}}, document)

    def test_json_quoted_fixture_coordinates_are_not_split_inside_quotes(self):
        workspace = '/scratch/quote"/back\\slash/unicode-é/repo'
        encoded = ('[projects.' + json.dumps(workspace) + ']\ntrust_level = "trusted"\n').encode()
        self.assertEqual({workspace: {"trust_level": "trusted"}}, seat_harness_config._toml(encoded)["projects"])

    def test_malformed_table_is_a_production_typed_refusal(self):
        self.assert_malformed(b'[projects."broken"\n')

    def test_duplicate_table_is_a_production_typed_refusal(self):
        self.assert_malformed(b'[projects."/scratch/repo"]\ntrust_level = "trusted"\n'
                              b'[projects."/scratch/repo"]\n')

    def test_duplicate_key_is_a_production_typed_refusal(self):
        self.assert_malformed(b'model = "fixture"\nmodel = "changed"\n')

    def test_scalar_table_collision_is_a_production_typed_refusal(self):
        self.assert_malformed(b'projects = "foreign"\n[projects."/scratch/repo"]\n')

    def test_unsupported_constructs_are_refused_instead_of_ignored(self):
        for encoded in (b'model = ["fixture"]\n', b'model = {value = "fixture"}\n',
                        b'model = 1\n', b"model = 'fixture'\n", b'[foreign]\n',
                        b'[[projects]]\n', b'model = "fixture" # inline\n',
                        b'[projects."/scratch/repo".extra]\n', b'# comment with \x00\n',
                        b'model = "fixture"\v'):
            with self.subTest(encoded=encoded):
                self.assert_malformed(encoded)

    def test_projects_scalar_still_hits_production_wrapper_validation(self):
        with self.assertRaises(ProtocolRefusal) as caught:
            seat_harness_config._toml(b'projects = "foreign"\n')
        self.assertEqual("seat_harness_config_invalid", caught.exception.code)
        self.assertEqual("Codex projects must be a table.", caught.exception.detail)

    def test_invalid_utf8_still_hits_production_decoder_validation(self):
        self.assert_malformed(b'model = "\xff"\n')

    def test_inner_import_blocker_preserves_actual_unavailable_parser_refusal(self):
        delegate = builtins.__import__
        def block(name, *args, **kwargs):
            if name in ("tomllib", "tomli"):
                raise ImportError("fixture interpreter has neither parser")
            return delegate(name, *args, **kwargs)
        with mock.patch("builtins.__import__", side_effect=block):
            with self.assertRaises(ProtocolRefusal) as caught:
                seat_harness_config._toml(b'model = "fixture"\n')
        self.assertIn("TOML validation is unavailable", caught.exception.detail)

    def test_tomli_fixture_exercises_the_actual_production_fallback_branch(self):
        fixture.install_fixture_toml(self, module="tomli")
        delegate = builtins.__import__
        imports = []
        def observe(name, *args, **kwargs):
            if name in ("tomllib", "tomli"):
                imports.append(name)
            return delegate(name, *args, **kwargs)
        with mock.patch("builtins.__import__", side_effect=observe):
            document = seat_harness_config._toml(b'model = "fallback fixture"\n')
        self.assertEqual({"model": "fallback fixture"}, document)
        self.assertEqual(["tomllib", "tomli"], imports)

    def assert_malformed(self, encoded):
        with self.assertRaises(ProtocolRefusal) as caught:
            seat_harness_config._toml(encoded)
        self.assertEqual("seat_harness_config_invalid", caught.exception.code)
        self.assertIn("malformed or duplicate TOML", caught.exception.detail)
        self.assertTrue(caught.exception.remedy)


class SeatTomlFixtureIsolationTests(unittest.TestCase):
    def test_contained_fixture_setup_failure_restores_importer_and_scratch(self):
        from tests import test_seat_onboard as onboarding
        from tests.test_seat_onboard_transport import SeatOnboardTransportTests
        original = builtins.__import__
        owner = SeatOnboardTransportTests("test_missing_selected_install_refuses_before_ownership_writes")
        try:
            # Real contained setup installs the importer and creates scratch;
            # the first Git step then fails before setup returns to its owner.
            with mock.patch.object(onboarding.SeatOnboardingTests, "git",
                                   side_effect=RuntimeError("injected setup failure")):
                with self.assertRaisesRegex(RuntimeError, "injected setup failure"):
                    owner.setUp()
            owner.doCleanups()
            self.assertIs(original, builtins.__import__)
            self.assertFalse(owner.fixture.base.exists())
        finally:
            owner.doCleanups()
            if hasattr(owner, "fixture"):
                owner.fixture.doCleanups()  # Keep the RED itself isolated too.

    def test_import_patch_is_local_and_always_removed_by_cleanup(self):
        self.assertIsNotNone(fixture, "controlled seat TOML fixture is not implemented")
        original = builtins.__import__
        owner = unittest.TestCase()
        try:
            fixture.install_fixture_toml(owner)
            self.assertEqual({"model": "fixture"}, seat_harness_config._toml(b'model = "fixture"\n'))
            self.assertIs(json, builtins.__import__("json"))
        finally:
            owner.doCleanups()
        self.assertIs(original, builtins.__import__)
