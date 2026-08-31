"""RED-first fixture scenarios (NIGHT_WATCH_SPEC §9, 1-16)."""

import unittest

from night_watch.budget import BudgetTable
from night_watch.events import NightEvent
from night_watch.render import render_morning_report
from night_watch.watch import NightWatch


def _worker(label: str) -> str:
    return f"worker-{label}"


def table(max_wakes=10, max_idle=2, max_depth=4, citation="test-citation"):
    return BudgetTable(
        max_wakes_per_node=max_wakes,
        max_idle_burn_wakes=max_idle,
        max_chain_depth=max_depth,
        coalesce_window_seconds=60,
        source_citation=citation,
    )


class WatchBase(unittest.TestCase):
    def watch(self, **kwargs):
        return NightWatch(
            window_start="2026-08-22T22:00:00Z",
            window_end="2026-08-23T06:00:00Z",
            budget=table(**kwargs),
        )

    @staticmethod
    def feed(watch, *events):
        for kind, node, moment in events:
            watch.fold(NightEvent(kind, node, moment))


class ScenarioTests(WatchBase):
    def test_1_quiet_night_is_zero_findings(self):
        watch = self.watch()
        report = watch.morning_report()
        self.assertEqual(report.violations, [])
        self.assertEqual(report.loops, [])
        self.assertFalse(report.any_paused())

    def test_1b_idle_soak_node_is_stated_not_alarm(self):
        watch = self.watch()
        self.feed(watch, ("work_completed", _worker("alpha"), "22:30"))
        report = watch.morning_report()
        alice = report.per_node[_worker("alpha")]
        self.assertEqual((alice.mails, alice.wakes), (0, 0))
        self.assertFalse(alice.paused)

    def test_2_mail_then_wake_then_work_happy_path(self):
        watch = self.watch()
        self.feed(
            watch,
            ("mail_landed", _worker("alpha"), "22:30"),
            ("wake_requested", _worker("alpha"), "22:31"),
            ("work_completed", _worker("alpha"), "22:40"),
        )
        node = watch.morning_report().per_node[_worker("alpha")]
        self.assertEqual((node.wakes, node.mails, node.work_items), (1, 1, 1))
        self.assertFalse(node.paused)

    def test_3_wake_without_mail_counts_idle_burn(self):
        watch = self.watch()
        self.feed(watch, ("wake_requested", "bob", "22:30"))
        node = watch.morning_report().per_node["bob"]
        self.assertEqual(node.idle_burns, 1)

    def test_4_idle_burn_threshold_emits_violation_and_pause(self):
        watch = self.watch(max_idle=2)
        self.feed(
            watch,
            ("wake_requested", "bob", "22:30"),
            ("wake_requested", "bob", "23:30"),
            ("wake_requested", "bob", "00:30"),
        )
        report = watch.morning_report()
        bob = report.per_node["bob"]
        # Pause fired AT the threshold; the third attempt arrived while
        # paused and is a recorded refusal, not a counted burn.
        self.assertEqual(bob.idle_burns, 2)
        self.assertTrue(any(v.dimension == "idle_burn" for v in report.violations))
        self.assertTrue(bob.paused)
        self.assertTrue(any("node_paused" in r for r in bob.refusals))

    def test_5_wake_while_paused_is_refused_and_recorded(self):
        watch = self.watch(max_wakes=1)
        self.feed(
            watch,
            ("mail_landed", "carol", "22:00"),
            ("wake_requested", "carol", "22:05"),
            ("mail_landed", "carol", "22:10"),
            ("wake_requested", "carol", "22:15"),  # over max_wakes -> pause
            ("wake_requested", "carol", "22:20"),  # while paused
        )
        report = watch.morning_report()
        carol = report.per_node["carol"]
        self.assertTrue(carol.paused)
        self.assertTrue(any("node_paused" in r for r in carol.refusals))

    def test_6_reset_observed_resumes_quota_pause_once(self):
        watch = self.watch(max_wakes=1)
        self.feed(
            watch,
            ("mail_landed", "carol", "22:00"),
            ("wake_requested", "carol", "22:05"),
            ("quota_ceiling_hit", "carol", "22:10"),
            ("reset_observed", "carol", "23:00"),
            ("reset_observed", "carol", "23:30"),
        )
        carol = watch.morning_report().per_node["carol"]
        self.assertFalse(carol.paused)
        self.assertEqual(len(carol.resumes), 1)

    def test_7_two_mails_one_wake_inside_coalesce_window_is_clean(self):
        watch = self.watch()
        self.feed(
            watch,
            ("mail_landed", "dan", "22:00"),
            ("mail_landed", "dan", "22:01"),
            ("wake_delivered", "dan", "22:02"),
        )
        report = watch.morning_report()
        self.assertEqual(
            [f for f in report.violations if f.dimension == "coalesce"], []
        )

    def test_8_two_shareable_wakes_emit_coalesce_missed(self):
        watch = self.watch()
        self.feed(
            watch,
            ("mail_landed", "dan", "22:00"),
            ("wake_requested", "dan", "22:01"),
            ("mail_landed", "dan", "22:01:30"),
            ("wake_requested", "dan", "22:03"),
        )
        report = watch.morning_report()
        missed = [v for v in report.violations if v.dimension == "coalesce_missed"]
        self.assertEqual(len(missed), 1)

    def test_9_cycle_edge_names_the_chain_and_pauses_members(self):
        watch = self.watch()
        watch.fold(NightEvent("loop_edge", "A", "22:00", to_node="B"))
        watch.fold(NightEvent("loop_edge", "B", "22:01", to_node="C"))
        watch.fold(NightEvent("loop_edge", "C", "22:03", to_node="A"))
        report = watch.morning_report()
        self.assertEqual(len(report.loops), 1)
        loop = report.loops[0]
        self.assertEqual(loop.kind, "cycle")
        self.assertEqual(set(loop.chain), {"A", "B", "C"})
        for member in ("A", "B", "C"):
            self.assertTrue(report.per_node[member].paused)

    def test_10_depth_past_bound_names_the_chain(self):
        watch = self.watch(max_depth=1)
        watch.fold(NightEvent("loop_edge", "A", "22:00", to_node="B"))
        watch.fold(NightEvent("loop_edge", "B", "22:01", to_node="C"))
        report = watch.morning_report()
        self.assertEqual(len(report.loops), 1)
        self.assertEqual(report.loops[0].kind, "depth")

    def test_11_wakes_over_ceiling_violate_and_pause_at_quota(self):
        watch = self.watch(max_wakes=2)
        self.feed(
            watch,
            ("mail_landed", "eve", "22:00"),
            ("wake_requested", "eve", "22:01"),
            ("mail_landed", "eve", "22:10"),
            ("wake_requested", "eve", "22:11"),
            ("mail_landed", "eve", "22:20"),
            ("wake_requested", "eve", "22:21"),
        )
        report = watch.morning_report()
        eve = report.per_node["eve"]
        self.assertGreater(eve.wakes, 2)
        self.assertTrue(any(v.dimension == "max_wakes" for v in report.violations))
        self.assertTrue(eve.paused)

    def test_12_replay_is_byte_identical_pure_fold(self):
        events = [
            NightEvent("mail_landed", _worker("alpha"), "22:30"),
            NightEvent("wake_delivered", _worker("alpha"), "22:31"),
            NightEvent("loop_edge", "B", "22:32", to_node="C"),
        ]
        first = self.watch()
        for event in events:
            first.fold(event)
        second = self.watch()
        for event in events:
            second.fold(event)
        self.assertEqual(first.morning_report(), second.morning_report())

    def test_13_unknown_event_kind_refuses(self):
        watch = self.watch()
        with self.assertRaises(Exception) as caught:
            watch.fold(NightEvent("seance", _worker("alpha"), "22:00"))
        self.assertIn("unknown_event_kind", str(caught.exception))

    def test_14_inverted_window_refuses(self):
        with self.assertRaises(Exception) as caught:
            NightWatch(
                window_start="2026-08-23T06:00:00Z",
                window_end="2026-08-22T22:00:00Z",
                budget=table(),
            )
        self.assertIn("window_inverted", str(caught.exception))

    def test_15_budget_table_without_citation_refuses_construction(self):
        with self.assertRaises(Exception) as caught:
            BudgetTable(
                max_wakes_per_node=10, max_idle_burn_wakes=2,
                max_chain_depth=4, coalesce_window_seconds=60,
                source_citation="",
            )
        self.assertIn("budget_citation_required", str(caught.exception))

    def test_16_renderer_emits_placeholder_keys_only(self):
        import re

        watch = self.watch()
        self.feed(
            watch,
            ("mail_landed", _worker("alpha"), "22:30"),
            ("wake_delivered", _worker("alpha"), "22:31"),
        )
        rendered = render_morning_report(watch.morning_report())
        placeholders = re.findall(r"\[\[[a-z0-9._]+\]\]", rendered)
        self.assertGreater(len(placeholders), 0)
        stripped = re.sub(r"\[\[[a-z0-9._]+\]\]", "", rendered)
        self.assertNotIn(_worker("alpha"), stripped)


if __name__ == "__main__":
    unittest.main()
