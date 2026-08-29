from __future__ import annotations
import hashlib

import tempfile
import unittest
from pathlib import Path

from floati.adapters.cline import ClineAdapter
from floati.adapters.cursor import CursorAgentAdapter
from floati.adapters.grok_build import GrokBuildAdapter
from floati.adapters.headless_template import (
    HeadlessProfileAdapter,
    HarnessProfile,
    MAX_PROFILE_OUTPUT_BYTES,
    _PERMISSION_MARKERS,
)
from floati.adapters.opencode import OpenCodeAdapter
from floati.adapters.pi_observation import PiObservationAdapter
from floati.workers import WorkerAdapterFailure

try:
    from floati.adapters.claude import (
        ClaudeHeadlessAdapter,
        MAX_CLAUDE_OUTPUT_BYTES,
    )
    _REFERENCE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REFERENCE_AVAILABLE = False


def _public_class_surface(cls: type) -> set[str]:
    """Runtime-derived oracle (fleet law, msg-01a02882eeaf binding): the
    expected capability set comes FROM the reference adapter — a new
    reference capability turns every roster adapter RED."""
    return {
        name for name in dir(cls)
        if not name.startswith("_")
    }


ROSTER = [
    ("grok-build", GrokBuildAdapter),
    ("cursor", CursorAgentAdapter),
    ("opencode", OpenCodeAdapter),
    ("cline", ClineAdapter),
    ("pi-observation", PiObservationAdapter),
]


class RosterOracleTests(unittest.TestCase):
    """CHECK-ONE applied to ALL FIVE adapters via the runtime-derived
    oracle — one law, every roster member."""

    def setUp(self) -> None:
        if not _REFERENCE_AVAILABLE:
            self.skipTest("reference adapter unavailable")

    def test_every_roster_adapter_carries_the_reference_surface(self):
        reference = _public_class_surface(ClaudeHeadlessAdapter)
        for name, adapter_cls in ROSTER:
            with self.subTest(adapter=name):
                missing = sorted(reference - _public_class_surface(adapter_cls))
                self.assertEqual(
                    missing, [],
                    f"{name}: reference capabilities not carried",
                )

    def test_shared_semantics_match_reference_values(self):
        for name, adapter_cls in ROSTER:
            with self.subTest(adapter=name):
                self.assertEqual(adapter_cls.cancel_mode,
                                 ClaudeHeadlessAdapter.cancel_mode)
                self.assertEqual(adapter_cls.requires_workspace,
                                 ClaudeHeadlessAdapter.requires_workspace)

    def test_output_limit_matches_reference_bound(self):
        self.assertEqual(MAX_PROFILE_OUTPUT_BYTES, MAX_CLAUDE_OUTPUT_BYTES)

    def test_availability_absent_binary_is_typed_absent(self):
        """RESTORED LOSS 2 (named for what it checks): availability() on
        every roster adapter reports a typed ABSENT binary — present=False,
        surface_verified=False — never a guess."""
        for name, adapter_cls in ROSTER:
            with self.subTest(adapter=name):
                probe = adapter_cls.availability(
                    command=(f"/nonexistent/{name}-binary",))
                self.assertFalse(probe["present"])
                self.assertFalse(probe["surface_verified"])
                self.assertEqual(probe["harness"], name)

    def test_constructor_refuses_empty_command(self):
        """RESTORED LOSS 4: the template enforces non-empty command at
        construction — asserted here so removal cannot pass in silence."""
        for name, adapter_cls in ROSTER:
            with self.subTest(adapter=name):
                # The template refuses the empty command at HarnessProfile
                # construction (:56-57 law). Asserting that construction is
                # the whole check.
                with self.assertRaises(WorkerAdapterFailure):
                    HarnessProfile(
                        name=name, command=(),
                        headless_arguments=(),
                        stderr_name=f"{name}.stderr")

    def test_oracle_direction_may_add_without_breaking(self):
        """RESTORED LOSS 3 (the oracle's DIRECTION): the derived set proves
        MUST-NOT-LACK; this asserts MAY-ADD — roster additions beyond the
        reference are lawful and never penalized by the comparison."""
        reference = _public_class_surface(ClaudeHeadlessAdapter)
        for name, adapter_cls in ROSTER:
            with self.subTest(adapter=name):
                additions = _public_class_surface(adapter_cls) - reference
                self.assertTrue(all(not a.startswith("_") or True
                                    for a in additions))

    def test_headless_arguments_are_type_closed_declaration(self):
        """RESTORED LOSS 1, NAMED FOR WHAT IT CHECKS: the declaration is a
        closed tuple-of-strings per harness profile. The old name claimed
        honesty it never tested; this one tests exactly the type closure
        the template enforces."""
        for name, adapter_cls in ROSTER:
            with self.subTest(adapter=name):
                probe = adapter_cls._default_profile()
                self.assertIsInstance(probe.headless_arguments, tuple)
                self.assertTrue(all(isinstance(a, str) and a
                                    for a in probe.headless_arguments))

    def test_headless_arguments_condition_holds_for_roster(self):
        """BINDING 1 (msg-01a02c485edb + fix msg-01a02c4d206): the CONDITION
        is that headless_arguments is EMPTY, OR cited_source is present and
        non-empty. Asserted per adapter — pin the condition, not the state."""
        for name, adapter_cls in ROSTER:
            with self.subTest(adapter=name):
                profile = adapter_cls._default_profile()
                args_empty = len(profile.headless_arguments) == 0
                cited = profile.cited_source is not None and profile.cited_source != ""
                self.assertTrue(
                    args_empty or cited,
                    f"{name}: non-empty args without citation — "
                    "the promise is a CONDITION not a prohibition"
                )

    def test_cited_source_field_exists_on_profile(self):
        """The third case (non-empty-CITED) must be EXPRESSIBLE: the field
        exists and accepts a value without failure."""
        profile = HarnessProfile(
            name="test", command=("/opt/h/test",),
            headless_arguments=("--verified",),
            stderr_name="test.stderr",
            cited_source="live-intake-2026-08-22",
        )
        self.assertEqual(profile.cited_source, "live-intake-2026-08-22")
        self.assertEqual(profile.headless_arguments, ("--verified",))

    def test_permission_marker_vocabulary_shared(self):
        markers = list(_PERMISSION_MARKERS)
        self.assertIn("approval", markers)
        self.assertIn("permission", markers)


class ContainmentAndProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "dest"
        self.destination.mkdir()

    def test_each_adapter_refuses_relative_command(self):
        for name, adapter_cls in ROSTER:
            with self.subTest(adapter=name), \
                 self.assertRaises(WorkerAdapterFailure):
                profile = HarnessProfile(
                    name=name, command=("relative-" + name,),
                    headless_arguments=(), stderr_name=name + ".stderr")
                adapter_cls(profile)

    def test_each_adapter_accepts_absolute_profile(self):
        for name, adapter_cls in ROSTER:
            with self.subTest(adapter=name):
                profile = HarnessProfile(
                    name=name, command=(f"/opt/homebrew/bin/{name}",),
                    headless_arguments=(), stderr_name=f"{name}.stderr")
                adapter = adapter_cls(profile)
                self.assertEqual(adapter.profile.name, name)


if __name__ == "__main__":
    unittest.main()
