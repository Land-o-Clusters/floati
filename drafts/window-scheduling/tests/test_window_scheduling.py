"""F5 RED-first tests — the two safety REDs first, then scheduling mechanics."""

import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from window_scheduling.scheduler import (
    _CAUSES,
    SchedulingRefusal,
    Scheduler,
    Window,
)

NOW = datetime(2026, 8, 23, 2, 0, 0, tzinfo=timezone.utc)


def _worker(label: str) -> str:
    return f"worker-{label}"


def stated_window(opens="2026-08-23T00:00:00Z", closes="2026-08-23T08:00:00Z",
                  provider="ollama"):
    return Window(
        provider=provider,
        opens_at=opens,
        closes_at=closes,
        opens_source="stated_by_provider",
        closes_source="observed_in_record",
    )


class TheTwoNamedREDs(unittest.TestCase):
    def test_red_1_an_unknown_window_schedules_nothing(self):
        scheduler = Scheduler(windows=[], paused_nodes=[])
        with self.assertRaises(SchedulingRefusal) as caught:
            scheduler.schedule(_worker("alpha"), "summarize", "unknown-provider", NOW)
        self.assertEqual(caught.exception.cause, "window_unknown")
        # and nothing was scheduled — the refusal is the whole outcome
        self.assertEqual(scheduler._windows, {})

    def test_red_2_boundary_without_stated_source_cannot_enter(self):
        with self.assertRaises(SchedulingRefusal) as caught:
            Window(
                provider="ollama", opens_at="2026-08-23T00:00:00Z",
                closes_at="2026-08-23T08:00:00Z",
                opens_source="stated_by_provider",
                closes_source="extrapolated_from_past",   # invented
            )
        self.assertEqual(caught.exception.cause, "boundary_not_stated")

    def test_extrapolated_opens_boundary_also_refuses_construction(self):
        with self.assertRaises(SchedulingRefusal) as caught:
            Window(
                provider="ollama", opens_at="2026-08-23T00:00:00Z",
                closes_at="2026-08-23T08:00:00Z",
                opens_source="heuristic_guess",
                closes_source="stated_by_provider",
            )
        self.assertEqual(caught.exception.cause, "boundary_not_stated")


class MechanicsTests(unittest.TestCase):
    def test_schedule_inside_window_runs_now_with_basis_stamped(self):
        scheduler = Scheduler(windows=[stated_window()])
        plan = scheduler.schedule(_worker("alpha"), "summarize", "ollama", NOW)
        self.assertEqual(plan.run_at, "2026-08-23T02:00:00Z")
        self.assertIn("stated_by_provider", plan.window_basis)
        self.assertEqual(plan.window_provider, "ollama")

    def test_request_before_open_defers_to_the_stated_open(self):
        scheduler = Scheduler(windows=[stated_window()])
        early = NOW - timedelta(hours=5)
        plan = scheduler.schedule(_worker("alpha"), "summarize", "ollama", early)
        self.assertEqual(plan.run_at, "2026-08-23T00:00:00Z")

    def test_expired_window_refuses_and_invents_nothing(self):
        scheduler = Scheduler(windows=[stated_window()])
        late = NOW + timedelta(hours=12)
        with self.assertRaises(SchedulingRefusal) as caught:
            scheduler.schedule(_worker("alpha"), "summarize", "ollama", late)
        self.assertEqual(caught.exception.cause, "window_expired")
        self.assertIn("no later window is known", caught.exception.detail)

    def test_paused_node_schedules_nothing_night_watch_composition(self):
        scheduler = Scheduler(
            windows=[stated_window()], paused_nodes=["bob"])
        with self.assertRaises(SchedulingRefusal) as caught:
            scheduler.schedule("bob", "summarize", "ollama", NOW)
        self.assertEqual(caught.exception.cause, "node_paused")


class TermsRespectFenceTests(unittest.TestCase):
    BANNED = ("rotat", "evade", "bypass", "next_account",
              "fallback_account", "maximize_throughput")

    @staticmethod
    def banned_identifiers(source):
        """Every identifier the AST can see: definitions, Assign targets,
        AnnAssign fields (the form every dataclass field uses), and
        annotated arguments. A fence blind to one syntactic form is a
        promise, not a fence (gate binding 3)."""
        import ast

        identifiers = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                identifiers.add(node.name.lower())
            elif isinstance(node, ast.Name):
                identifiers.add(node.id.lower())
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg.lower())
        return identifiers

    def test_inverted_window_refuses_construction_window_incoherent(self):
        with self.assertRaises(SchedulingRefusal) as caught:
            Window(
                provider="ollama", opens_at="2026-08-23T23:00:00Z",
                closes_at="2026-08-23T21:00:00Z",
                opens_source="stated_by_provider",
                closes_source="observed_in_record")
        self.assertEqual(caught.exception.cause, "window_incoherent")

    def test_unreadable_timestamp_refuses_construction_typed(self):
        with self.assertRaises(SchedulingRefusal) as caught:
            Window(
                provider="ollama", opens_at="not-a-time",
                closes_at="2026-08-23T21:00:00Z",
                opens_source="stated_by_provider",
                closes_source="observed_in_record")
        self.assertEqual(caught.exception.cause, "timestamp_unreadable")

    def test_scheduler_surface_is_free_of_rotation_evasion(self):
        import inspect

        from window_scheduling import scheduler as module

        hits = [
            (identifier, token)
            for identifier in self.banned_identifiers(inspect.getsource(module))
            for token in self.BANNED
            if token in identifier
        ]
        self.assertEqual(hits, [],
                         "scheduler surface encodes rotation/evasion: %r"
                         % (hits,))

    def test_fence_catches_annassign_fields_perturbation(self):
        """THE FENCE'S OWN FENCE: an AnnAssign field carrying a banned
        identifier must redden. Gate binding 3 - the one syntactic form
        the whole surface is written in cannot be invisible."""
        planted = "class Fake:\n    rotate_to_next_account: str = ''\n"
        matched = [
            identifier for identifier in self.banned_identifiers(planted)
            if "rotate" in identifier
        ]
        self.assertEqual(matched, ["rotate_to_next_account"])


class WiringContractTests(unittest.TestCase):
    def test_closed_refusal_set_is_documented_bijectively(self):
        wiring = Path(__file__).parents[1] / "WIRING.md"
        text = wiring.read_text(encoding="utf-8")
        refusal_section = text.split(
            "## 7. Refusals carried by the build (closed set)", 1
        )[1]
        documented = set(re.findall(r"`([a-z_]+)`", refusal_section))
        self.assertEqual(documented, _CAUSES)


if __name__ == "__main__":
    unittest.main()
