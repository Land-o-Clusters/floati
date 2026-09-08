"""TIMEOUT-PUB-1: shipped tests must not gate success on a fixed wall clock.

Two shipped tests held wall-clock constants that pass alone and fail inside
a full-suite run — measured four times in four trees on 2026-09-06/07, once
per tree, always the same mechanism:

- ``tests/test_watch.py`` bound its child liveness waits to a module-level
  ``WATCH_LIVENESS_BOUND_SECONDS = 30.0``;
- ``tests/test_quota_adapters.py`` handed its Codex stdio-collector
  handshake a bare ``timeout_seconds=2.0``.

Both ship, so a stranger on a slow laptop receives the same bet.  These
tests pin the replacement property: each bound is DERIVED from a
measurement of this host taken when it is used, the derivation states what
it assumes about the host, and a failure names the loadavg it failed
under.  The ast pins exist so the constants cannot quietly return; the
scaling tests are the teeth — a bound that does not move when the
measurement moves is still a constant wearing a derivation's clothes.
"""

from __future__ import annotations

import ast
import os
import stat
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal

from tests import time_bounds


TESTS_ROOT = Path(__file__).resolve().parent
# The fixture's resetsAt (epoch 1788022800) must not predate observed_at: the
# product refuses a fact whose reset precedes its observation.
OBSERVED_AT = "2026-08-29T12:00:00Z"


def _bound_constant_lines(path: Path, name: str) -> list[int]:
    """Return the lines where ``name`` is bound at module level in ``path``."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                lines.append(node.lineno)
    return lines


def _bare_timeout_keyword_lines(path: Path, function_name: str) -> list[int]:
    """Return the lines where ``function_name`` passes a literal timeout_seconds."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for owner in ast.walk(tree):
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if owner.name != function_name:
            continue
        for node in ast.walk(owner):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "timeout_seconds"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, (int, float))
                and not isinstance(node.value.value, bool)
            ):
                lines.append(node.lineno)
    return lines


class WatchLivenessBoundTests(unittest.TestCase):
    def test_watch_backstop_never_resolves_below_the_constant_it_replaced(self) -> None:
        """Am.1 RED (ruling 2026-09-07-a-derived-bound-that-lands-below...).

        main carries ``WATCH_LIVENESS_BOUND_SECONDS = 30.0``, and the
        measured evidence that refused the first cut is that 30 s was too
        TIGHT, not too generous.  A derived bound whose floor sits below
        the replaced constant is therefore a tighter bet with a
        justification attached: on a host whose probe reads cheap — which
        is exactly what a quiet host, or this suite process's own
        contention domain at a lucky instant, produces — the bound lands
        at 15 s and moves toward the failure the row exists to remove.
        """

        with mock.patch.object(
            time_bounds, "_measured_cli_start_seconds", return_value=0.001
        ):
            cheapest = time_bounds.derived_watch_liveness_bound_seconds()
        self.assertGreaterEqual(
            cheapest,
            30.0,
            f"the derived watch backstop resolved to {cheapest:g}s on the "
            "cheapest host draw available; the constant it replaced on main "
            "is 30.0s, so the derivation's floor must be that replaced "
            "value and never a fresh, smaller number",
        )

    def test_watch_module_binds_no_fixed_liveness_constant(self) -> None:
        lines = _bound_constant_lines(
            TESTS_ROOT / "test_watch.py", "WATCH_LIVENESS_BOUND_SECONDS"
        )
        first = str(lines[0]) if lines else "?"
        self.assertEqual(
            [],
            lines,
            "tests/test_watch.py binds WATCH_LIVENESS_BOUND_SECONDS at module "
            f"level (first at line {first}); a fixed wall clock is a bet on the "
            "host's scheduler that a full-suite run collects. Derive the bound "
            "from this host via tests.time_bounds."
            "derived_watch_liveness_bound_seconds() instead.",
        )

    def test_watch_backstop_grows_with_a_measured_slow_host(self) -> None:
        with mock.patch.object(
            time_bounds, "_measured_cli_start_seconds", return_value=2.0
        ):
            slow = time_bounds.derived_watch_liveness_bound_seconds()
        self.assertGreaterEqual(
            slow,
            80.0,
            "a host measured at 2.0s per CLI start must widen the backstop to at "
            "least the documented 40x; a bound that does not move with the "
            "measurement is a constant, not a derivation",
        )

    def test_watch_backstop_keeps_its_stated_floor_and_ceiling(self) -> None:
        with mock.patch.object(
            time_bounds, "_measured_cli_start_seconds", return_value=0.001
        ):
            quiet = time_bounds.derived_watch_liveness_bound_seconds()
        with mock.patch.object(
            time_bounds, "_measured_cli_start_seconds", return_value=60.0
        ):
            wedged = time_bounds.derived_watch_liveness_bound_seconds()
        self.assertEqual(
            30.0, quiet, "the floor is the replaced constant, never a fresh number"
        )
        self.assertEqual(120.0, wedged, "the ceiling must match the stated assumption")

    def test_fresh_watch_backstop_is_the_same_derivation_uncached(self) -> None:
        """Am.1: the failure path re-measures instead of trusting the first draw."""

        with mock.patch.object(
            time_bounds,
            "_measured_cli_start_seconds",
            side_effect=AssertionError("the fresh draw consulted the cached probe"),
        ), mock.patch.object(
            time_bounds, "measured_command_start_seconds", return_value=0.5
        ) as probe:
            fresh = time_bounds.fresh_watch_liveness_bound_seconds()
        self.assertEqual(30.0, fresh)  # 40 x 0.5 = 20 -> floor 30 binds
        self.assertEqual(1, probe.call_count)

        with mock.patch.object(
            time_bounds, "measured_command_start_seconds", return_value=2.5
        ):
            hot = time_bounds.fresh_watch_liveness_bound_seconds()
        self.assertEqual(100.0, hot, "a hot re-measure must widen the extension (40x)")

        with mock.patch.object(
            time_bounds, "measured_command_start_seconds", return_value=90.0
        ):
            wedged = time_bounds.fresh_watch_liveness_bound_seconds()
        self.assertEqual(120.0, wedged, "the ceiling caps the extension too")

    def test_await_with_one_extension_extends_exactly_once_and_says_so(self) -> None:
        """Am.1: a miss takes a fresh draw and one extension - never a retune."""

        from tests.test_watch import await_with_one_extension

        with mock.patch.object(
            time_bounds, "derived_watch_liveness_bound_seconds", return_value=30.0
        ), mock.patch.object(
            time_bounds, "fresh_watch_liveness_bound_seconds", return_value=45.0
        ) as fresh:
            # met on the bound itself: the fresh draw is never taken.
            calls = []
            bound, extension, met = await_with_one_extension(
                lambda seconds: calls.append(seconds) or True
            )
            self.assertEqual((30.0, 0.0, True), (bound, extension, met))
            self.assertEqual([30.0], calls)
            self.assertEqual(0, fresh.call_count)

            # met inside the extension: exactly one fresh draw, one extension.
            calls = []

            def attempt(seconds: float) -> bool:
                calls.append(seconds)
                return sum(calls) > 30.0

            bound, extension, met = await_with_one_extension(attempt)
            self.assertEqual((30.0, 45.0, True), (bound, extension, met))
            self.assertEqual([30.0, 45.0], calls)
            self.assertEqual(1, fresh.call_count)

            # missed both: one bound, one extension, no more.
            fresh.reset_mock()
            bound, extension, met = await_with_one_extension(lambda seconds: False)
            self.assertEqual((30.0, 45.0, False), (bound, extension, met))
            self.assertEqual(1, fresh.call_count)


class CodexHandshakeBudgetTests(unittest.TestCase):
    def test_codex_handshake_budget_is_computed_not_a_bare_constant(self) -> None:
        lines = _bare_timeout_keyword_lines(
            TESTS_ROOT / "test_quota_adapters.py",
            "test_codex_stdio_collector_uses_real_jsonl_handshake_and_no_listener",
        )
        first = str(lines[0]) if lines else "?"
        self.assertEqual(
            [],
            lines,
            "the Codex stdio-collector handshake test passes a literal "
            f"timeout_seconds (first at line {first}); a fixed budget is a bet "
            "on the host's scheduler that a full-suite run collects. Derive it "
            "from this host via tests.time_bounds."
            "derived_codex_handshake_budget_seconds() instead.",
        )

    def test_codex_budget_grows_with_a_measured_slow_host(self) -> None:
        with mock.patch.object(
            time_bounds,
            "_measured_bare_interpreter_start_seconds",
            return_value=1.0,
        ):
            slow = time_bounds.derived_codex_handshake_budget_seconds()
        self.assertGreaterEqual(
            slow,
            20.0,
            "a host measured at 1.0s per bare interpreter start must widen the "
            "budget to at least the documented 20x",
        )

    def test_codex_budget_stays_inside_the_product_ceiling(self) -> None:
        with mock.patch.object(
            time_bounds,
            "_measured_bare_interpreter_start_seconds",
            return_value=90.0,
        ):
            wedged = time_bounds.derived_codex_handshake_budget_seconds()
        self.assertEqual(
            30.0,
            wedged,
            "the product refuses timeout_seconds above 30 with "
            "quota_collector_invalid, so the derived budget must never exceed it",
        )

    def test_codex_handshake_completes_when_the_host_starts_the_fixture_slowly(self) -> None:
        """The regression leg: a fixture the host starts slowly still completes.

        The shipped handshake test held ``timeout_seconds=2.0`` while its
        fixture had to be spawned and schedule three JSONL round trips
        inside that budget — four full-suite runs proved a loaded host
        cannot. This leg consumes the same real host time a loaded host
        charges (a 3 s start) and must still complete.
        """

        from floati.quota_adapters import collect_codex_app_server

        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "codex-slow-host"
            executable.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys
                    import time

                    time.sleep(3.0)
                    for line in sys.stdin:
                        message = json.loads(line)
                        if message['method'] == 'initialize':
                            print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
                        elif message['method'] == 'account/rateLimits/read':
                            print(json.dumps({
                                'id': message['id'],
                                'result': {
                                    'rateLimits': {
                                        'limitId': 'codex',
                                        'primary': {
                                            'usedPercent': 25,
                                            'windowDurationMins': 15,
                                            'resetsAt': 1788022800,
                                        },
                                        'secondary': None,
                                        'rateLimitReachedType': None,
                                    }
                                },
                            }), flush=True)
                            break
                    """
                ),
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            budget = time_bounds.derived_codex_handshake_budget_seconds()
            began = time.monotonic()
            try:
                receipt = collect_codex_app_server(
                    executable,
                    observed_at=OBSERVED_AT,
                    idempotency_key="codex-slow-host-1",
                    timeout_seconds=budget,
                )
            except ProtocolRefusal as raised:
                self.fail(
                    f"the fixture needed {time.monotonic() - began:.3f}s of host "
                    f"time inside a derived {budget:g}s budget and the collector "
                    f"gave up ({raised.code}: {raised.detail}); host loadavg "
                    f"{[round(value, 2) for value in os.getloadavg()]}"
                )
            self.assertEqual("0.250000", receipt.facts[0].state.value)


if __name__ == "__main__":
    unittest.main()
