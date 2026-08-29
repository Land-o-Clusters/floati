"""Behavior tests for the finite, data-only repository policy contract."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal

try:
    from floati.policy import (
        Policy,
        PolicyDeploymentChecker,
        PolicyDeploymentStatus,
        RepositoryPolicy,
    )
except ModuleNotFoundError:
    Policy = None
    PolicyDeploymentChecker = None
    PolicyDeploymentStatus = None
    RepositoryPolicy = None


VALID_POLICY = '''# Repository policy; presentation must not be authority.
schema_version = 0
capability_registry = ["review", "workspace_write"]

[limits]
max_items = 64
max_depth = 16
max_fan_out = 8
max_active_attempts = 8

[budgets.build]
unit = "attempts"
limit = 32

[worker_profiles.codex]
capabilities = ["review", "workspace_write"]
cancel_mode = "native"
callback_support = true
max_concurrency = 1

[capability_selectors.review_write]
all_of = ["review", "workspace_write"]

[routing.review_write_codex]
worker_profile = "codex"
capability_selector = "review_write"
rank = 0

[retry_classes.transient]
automatic = true
[retry_classes.permanent]
automatic = false
[retry_classes.operator_required]
automatic = false
[retry_classes.policy_refusal]
automatic = false
[retry_classes.cancelled]
automatic = false
[retry_classes.unknown_effect]
automatic = false

[approval_requirements.low]
required = false
[approval_requirements.medium]
required = false
[approval_requirements.high]
required = true
[approval_requirements.critical]
required = true

[verification.unit]
argv = ["python3", "-m", "unittest", "tests.test_policy"]

[merge_gates.local]
verification_ids = ["unit"]
'''


REORDERED_POLICY = '''schema_version=0 # semantic equivalent presentation
capability_registry=["review", "workspace_write"]

[merge_gates.local]
verification_ids=["unit"]

[verification.unit]
argv = [ "python3" , "-m" , "unittest" , "tests.test_policy" ]

[approval_requirements.critical]
required=true
[approval_requirements.high]
required=true
[approval_requirements.medium]
required=false
[approval_requirements.low]
required=false

[retry_classes.unknown_effect]
automatic=false
[retry_classes.cancelled]
automatic=false
[retry_classes.policy_refusal]
automatic=false
[retry_classes.operator_required]
automatic=false
[retry_classes.permanent]
automatic=false
[retry_classes.transient]
automatic=true

[routing.review_write_codex]
rank=0
capability_selector="review_write"
worker_profile="codex"

[capability_selectors.review_write]
all_of=["review", "workspace_write"]

[worker_profiles.codex]
max_concurrency=1
callback_support=true
cancel_mode="native"
capabilities=["review", "workspace_write"]

[budgets.build]
limit=32
unit="attempts"

[limits]
max_active_attempts=8
max_fan_out=8
max_depth=16
max_items=64
'''


class RepositoryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(Policy, "floati.policy must expose the finite Policy loader")
        self.assertIsNotNone(RepositoryPolicy, "floati.policy must expose immutable RepositoryPolicy")
        self.assertIsNotNone(PolicyDeploymentChecker, "floati.policy must expose the four-state checker")
        self.assertIsNotNone(PolicyDeploymentStatus, "floati.policy must expose checker vocabulary")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS commonly exposes its temporary directory through `/var`, a
        # symlink.  Policy loading intentionally refuses lexical symlink
        # components, so fixtures use the resolved real temporary path.
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "FLOATI.toml"

    def write_policy(self, content: str = VALID_POLICY) -> Path:
        self.path.write_text(content, encoding="utf-8")
        return self.path

    def load(self, content: str = VALID_POLICY):
        return Policy.load(self.write_policy(content))

    def assert_refuses(self, content: str) -> None:
        self.write_policy(content)
        with self.assertRaises(ProtocolRefusal):
            Policy.load(self.path)

    def test_format_comments_and_named_table_presentation_do_not_change_digest(self) -> None:
        """A loader that hashes raw TOML would miss canonical policy equivalence."""
        first = self.load(VALID_POLICY)
        second = self.load(REORDERED_POLICY)

        self.assertEqual(first.canonical_bytes, second.canonical_bytes)
        self.assertEqual(first.digest, second.digest)

    def test_finite_surface_is_immutable_and_routes_follow_explicit_rank(self) -> None:
        """A mutable policy or input-order routing could alter governed decisions after load."""
        policy = self.load()

        self.assertIsInstance(policy, RepositoryPolicy)
        self.assertEqual(64, policy.limits["max_items"])
        self.assertEqual(("review", "workspace_write"), policy.capability_registry)
        self.assertEqual(("review_write_codex",), tuple(route.route_id for route in policy.routes))
        self.assertEqual((0,), tuple(route.rank for route in policy.routes))
        with self.assertRaises(TypeError):
            policy.limits["max_items"] = 1
        with self.assertRaises((AttributeError, TypeError)):
            policy.digest = "0" * 64

    def test_registry_is_sorted_finite_and_covers_profiles_and_selectors(self) -> None:
        """Catches capability authority entering through unregistered declarations or selectors."""
        baseline = self.load()
        widened = self.load(VALID_POLICY.replace(
            '["review", "workspace_write"]\n\n[limits]',
            '["review", "shell_exec", "workspace_write"]\n\n[limits]',
            1,
        ))
        self.assertNotEqual(baseline.digest, widened.digest)
        candidates = {
            "unsorted_registry": VALID_POLICY.replace(
                '["review", "workspace_write"]\n\n[limits]',
                '["workspace_write", "review"]\n\n[limits]',
                1,
            ),
            "duplicate_registry": VALID_POLICY.replace(
                '["review", "workspace_write"]\n\n[limits]',
                '["review", "review"]\n\n[limits]',
                1,
            ),
            "unregistered_profile": VALID_POLICY.replace(
                'capabilities = ["review", "workspace_write"]',
                'capabilities = ["review", "shell_exec"]',
            ),
            "unregistered_selector": VALID_POLICY.replace(
                'all_of = ["review", "workspace_write"]',
                'all_of = ["review", "shell_exec"]',
            ),
        }
        for name, candidate in candidates.items():
            with self.subTest(name=name):
                self.assert_refuses(candidate)

    def test_loading_verification_argv_never_launches_a_process(self) -> None:
        """Treating verification argv as executable at load time would make policy parsing an effect."""
        marker = self.root / "argv-was-executed"
        policy = VALID_POLICY.replace(
            '["python3", "-m", "unittest", "tests.test_policy"]',
            '["sh", "-c", "touch ' + str(marker) + '"]',
        )

        loaded = self.load(policy)

        self.assertEqual(("sh", "-c", "touch " + str(marker)), loaded.verification["unit"].argv)
        self.assertFalse(marker.exists())

    def test_verification_argv_refuses_dynamic_text_as_data(self) -> None:
        """Dynamic markers in argv would turn a declarative verification command into an interpolation surface."""
        for marker in ("${UNTRUSTED}", "$(touch x)", "{{ template }}", "<% code %>"):
            with self.subTest(marker=marker):
                self.assert_refuses(
                    VALID_POLICY.replace(
                        '["python3", "-m", "unittest", "tests.test_policy"]',
                        '["python3", "' + marker + '"]',
                    )
                )

    def test_closed_semantics_refuse_unknown_dangling_unsorted_duplicate_and_relaxed_values(self) -> None:
        """Permissive or dangling policy data would turn a finite v0 policy into hidden behavior."""
        candidates = {
            "unknown_root": VALID_POLICY.replace("schema_version = 0", "schema_version = 0\nunknown = true"),
            "unknown_nested": VALID_POLICY.replace("max_items = 64", "max_items = 64\nunknown = true"),
            "dangling_profile": VALID_POLICY.replace('worker_profile = "codex"', 'worker_profile = "missing"'),
            "unsorted_capabilities": VALID_POLICY.replace(
                '["review", "workspace_write"]', '["workspace_write", "review"]', 1),
            "duplicate_capabilities": VALID_POLICY.replace(
                '["review", "workspace_write"]', '["review", "review"]', 1),
            "duplicate_rank": VALID_POLICY.replace(
                '[routing.review_write_codex]\nworker_profile = "codex"\ncapability_selector = "review_write"\nrank = 0',
                '[routing.review_write_codex]\nworker_profile = "codex"\ncapability_selector = "review_write"\nrank = 0\n\n'
                '[routing.other]\nworker_profile = "codex"\ncapability_selector = "review_write"\nrank = 0',
            ),
            "retry_relaxation": VALID_POLICY.replace("[retry_classes.permanent]\nautomatic = false", "[retry_classes.permanent]\nautomatic = true"),
            "oversized_limit": VALID_POLICY.replace("max_items = 64", "max_items = 65"),
            "noninteger_limit": VALID_POLICY.replace("max_items = 64", "max_items = 1.5"),
            "missing_retry": VALID_POLICY.replace("[retry_classes.unknown_effect]\nautomatic = false\n", ""),
            "extra_risk": VALID_POLICY.replace("[approval_requirements.low]", "[approval_requirements.extra]\nrequired = false\n\n[approval_requirements.low]"),
            "unknown_gate_ref": VALID_POLICY.replace('verification_ids = ["unit"]', 'verification_ids = ["missing"]'),
        }
        for name, candidate in candidates.items():
            with self.subTest(name=name):
                self.assert_refuses(candidate)

    def test_closed_toml_grammar_refuses_dynamic_and_ambiguous_constructs(self) -> None:
        """General TOML syntax could smuggle aliases, code-like syntax, or unbounded data into policy."""
        candidates = {
            "dotted_key": VALID_POLICY.replace("max_items = 64", "limits.max_items = 64"),
            "inline_table": VALID_POLICY.replace("schema_version = 0", "schema_version = 0\ninline = { value = true }"),
            "array_of_tables": VALID_POLICY.replace("[budgets.build]", "[[budgets.build]]"),
            "float": VALID_POLICY.replace("max_items = 64", "max_items = 64.0"),
            "datetime": VALID_POLICY.replace("max_items = 64", "max_items = 2026-08-08T00:00:00Z"),
            "multiline": VALID_POLICY.replace('unit = "attempts"', 'unit = """attempts"""'),
            "unsupported_escape": VALID_POLICY.replace('unit = "attempts"', 'unit = "attempts\\x20"'),
            "include": VALID_POLICY.replace("schema_version = 0", 'schema_version = 0\ninclude = "other.toml"'),
            "interpolation": VALID_POLICY.replace('unit = "attempts"', 'unit = "${UNIT}"'),
            "callback": VALID_POLICY.replace("schema_version = 0", 'schema_version = 0\ncallback = "sh -c true"'),
            "duplicate_decoded_key": VALID_POLICY.replace("max_items = 64", 'max_items = 64\n"max_items" = 63'),
            "duplicate_table": VALID_POLICY + "\n[limits]\nmax_items = 64\n",
        }
        for name, candidate in candidates.items():
            with self.subTest(name=name):
                self.assert_refuses(candidate)

    def test_policy_path_requires_one_regular_absolute_lexical_floati_file(self) -> None:
        """Resolving arbitrary or symlinked policy paths would erase the governed repository boundary."""
        self.write_policy()
        with self.assertRaises(ProtocolRefusal):
            Policy.load(Path("FLOATI.toml"))
        wrong_name = self.root / "other.toml"
        wrong_name.write_text(VALID_POLICY, encoding="utf-8")
        with self.assertRaises(ProtocolRefusal):
            Policy.load(wrong_name)
        lexical_parent = self.root / ".." / self.root.name / "FLOATI.toml"
        with self.assertRaises(ProtocolRefusal):
            Policy.load(lexical_parent)
        raw_dot_path = str(self.root) + "/./FLOATI.toml"
        with self.assertRaises(ProtocolRefusal):
            Policy.load(raw_dot_path)
        # pathlib has already discarded `.` by this point, so a Path object
        # honestly exposes only its normalized lexical spelling to the API.
        self.assertEqual(self.path, Path(raw_dot_path))
        self.assertIsInstance(Policy.load(Path(raw_dot_path)), RepositoryPolicy)
        with self.assertRaises(ProtocolRefusal):
            Policy.load(self.root / "missing" / "FLOATI.toml")
        directory = self.root / "directory" / "FLOATI.toml"
        directory.mkdir(parents=True)
        with self.assertRaises(ProtocolRefusal):
            Policy.load(directory)
        if hasattr(os, "symlink"):
            link = self.root / "linked" / "FLOATI.toml"
            link.parent.mkdir()
            os.symlink(self.path, link)
            with self.assertRaises(ProtocolRefusal):
                Policy.load(link)
            parent_link = self.root / "linked-parent"
            os.symlink(self.root, parent_link)
            with self.assertRaises(ProtocolRefusal):
                Policy.load(parent_link / "FLOATI.toml")

    def test_path_boundary_exceptions_become_typed_refusal_and_cannot_speak(self) -> None:
        """Raw filesystem or path-like exceptions would let malformed policy paths escape the four-state checker."""
        baseline = self.load().digest

        class ExplodingPath:
            def __fspath__(self):
                raise RuntimeError("pathlike boom")

        candidates = (
            str(self.root / "\x00" / "FLOATI.toml"),
            str(self.root) + "/\udcff/FLOATI.toml",
            ExplodingPath(),
        )
        for candidate in candidates:
            with self.subTest(candidate=type(candidate).__name__):
                with self.assertRaises(ProtocolRefusal):
                    Policy.load(candidate)
                result = PolicyDeploymentChecker.check(candidate, baseline)
                self.assertEqual(PolicyDeploymentStatus.CANNOT_SPEAK, result.status)

    def test_document_source_refuses_noncanonical_whitespace_and_controls_before_parse(self) -> None:
        """Unicode separators or controls in syntax/comments would create policy spellings outside the ruled grammar."""
        candidates = {
            "nonbreaking_space": VALID_POLICY.replace("schema_version = 0", "schema_version\u00a0=\u00a00"),
            "vertical_tab_separator": VALID_POLICY.replace("\n[limits]", "\x0b[limits]"),
            "form_feed_separator": VALID_POLICY.replace("\n[limits]", "\x0c[limits]"),
            "unicode_line_separator": VALID_POLICY.replace("\n[limits]", "\u2028[limits]"),
            "tab_whitespace": VALID_POLICY.replace("schema_version = 0", "schema_version\t=\t0"),
            "nul_comment": VALID_POLICY + "# comment\x00hidden\n",
        }
        for name, candidate in candidates.items():
            with self.subTest(name=name):
                self.assert_refuses(candidate)

    def test_non_utf8_and_oversized_policy_input_refuse_without_raw_parser_errors(self) -> None:
        """A bounded policy reader must fail closed before malformed bytes reach semantic evaluation."""
        self.path.write_bytes(b"\xff")
        with self.assertRaises(ProtocolRefusal):
            Policy.load(self.path)
        self.path.write_bytes(("# filler\n" * 10000).encode("utf-8"))
        with self.assertRaises(ProtocolRefusal):
            Policy.load(self.path)

    def test_checker_uses_only_the_exact_four_states_and_never_claims_review(self) -> None:
        """A checker that manufactures a baseline would silently turn a digest match into review approval."""
        policy = self.load()
        deployed = PolicyDeploymentChecker.check(self.path, policy.digest)
        self.assertEqual(PolicyDeploymentStatus.DEPLOYED, deployed.status)
        self.assertEqual(policy.digest, deployed.observed_digest)
        self.assertEqual(policy.digest, deployed.reviewed_digest)

        drifted = PolicyDeploymentChecker.check(self.path, "0" * 64)
        self.assertEqual(PolicyDeploymentStatus.DRIFTED, drifted.status)
        self.assertEqual(policy.digest, drifted.observed_digest)

        absent = PolicyDeploymentChecker.check(self.path, None)
        self.assertEqual(PolicyDeploymentStatus.ABSENT, absent.status)
        self.assertEqual("reviewed_digest", absent.subject)

        malformed = PolicyDeploymentChecker.check(self.path, "not-a-digest")
        self.assertEqual(PolicyDeploymentStatus.CANNOT_SPEAK, malformed.status)
        self.assertIsNone(malformed.observed_digest)

        self.path.write_text("schema_version = nope", encoding="utf-8")
        malformed_policy = PolicyDeploymentChecker.check(self.path, policy.digest)
        self.assertEqual(PolicyDeploymentStatus.CANNOT_SPEAK, malformed_policy.status)


if __name__ == "__main__":
    unittest.main()
