"""Derived host-relative time bounds for the suite's own subprocess waits.

TIMEOUT-PUB-1: a shipped test must not gate success on a fixed wall clock.
A fixed constant is a bet that the host schedules a child within it, and a
full-suite run on a loaded host collects that bet (five measured instances
in five trees on 2026-09-06/07).  Each bound here is DERIVED from a
measurement of what this host charges, right now, to start a comparable
process, and each function's docstring states the assumption the derivation
makes about the host.  The measurement is taken when the bound is used, not
at import time, so a suite that has been loading the host for minutes
measures a loaded host.

THE VARIABLE, measured by train CF's gate (ceb8791a, fifth instance): the
watch child passes alone on a machine at loadavg 11 and fails inside a
4482-test run, so what fails the child is not ambient load but THIS SUITE
PROCESS'S OWN SUBPROCESS CONCURRENCY - the live and lingering children the
suite keeps around it.  Every probe below is therefore spawned by the suite
process itself, from the same contention domain the bounded child competes
in: when suite churn makes children slow to start and exit, the probe reads
slow and the bound widens; when the domain is quiet, the floor binds.
Loadavg is never used to size a bound; it appears only in failure text,
because a bound tuned to ambient load would be tuned to the wrong variable.
"""

from __future__ import annotations

import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# The probe is itself a subprocess spawn, so it needs its own liveness
# backstop; a probe that outlives it declares the host's price as the
# ceiling rather than hanging the suite.
_PROBE_BACKSTOP_SECONDS = 120.0

# Am.1 (ruling 2026-09-07-a-derived-bound-that-lands-below-the-constant-it-
# replaced): the watch backstop's floor is the value it replaced - main
# carried WATCH_LIVENESS_BOUND_SECONDS = 30.0, and the measured evidence is
# that 30 s was too TIGHT, never too generous (six instances, the last on a
# quiet host).  A floor is therefore DERIVED FROM THE REPLACED VALUE, never
# from a fresh smaller number: however cheaply the probe reads, the bound
# cannot drop below what shipped before it.
REPLACED_WATCH_CONSTANT_SECONDS = 30.0

# The stated assumption: the bounded child needs at most this multiple of
# what the probe measures, capped by a ceiling that declares the host
# unhealthy instead of waiting forever.
_WATCH_MULTIPLIER = 40.0
_WATCH_CEILING_SECONDS = 120.0


def measured_command_start_seconds(argv: list[str], *, samples: int = 2) -> float:
    """Measure what this host charges RIGHT NOW to start one ``argv`` process.

    One unmeasured warmup absorbs the interpreter and bytecode-cache cold
    start, then ``samples`` measured runs are taken and the MAXIMUM is
    returned, because a bound provisioned on the better of two samples
    under-provisions exactly when the host is degrading.  The probe is a
    child of the calling (suite) process, so its cost carries the suite's
    own subprocess contention - the variable train CF measured - and not
    some ambient average the child does not live in.
    """

    subprocess.run(
        argv,
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=_PROBE_BACKSTOP_SECONDS,
    )
    measured = 0.0
    for _ in range(samples):
        began = time.monotonic()
        subprocess.run(
            argv,
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_PROBE_BACKSTOP_SECONDS,
        )
        measured = max(measured, time.monotonic() - began)
    return measured


def _watch_bound(baseline: float) -> float:
    return min(
        _WATCH_CEILING_SECONDS,
        max(REPLACED_WATCH_CONSTANT_SECONDS, _WATCH_MULTIPLIER * baseline),
    )


@lru_cache(maxsize=None)
def _measured_cli_start_seconds() -> float:
    return measured_command_start_seconds(
        [sys.executable, "-m", "floati", "describe", "--json"]
    )


@lru_cache(maxsize=None)
def _measured_bare_interpreter_start_seconds() -> float:
    return measured_command_start_seconds(
        [sys.executable, "-c", "import json, pathlib"]
    )


def derived_watch_liveness_bound_seconds() -> float:
    """TIMEOUT-PUB-1: the liveness backstop for ``floati watch`` children.

    A LIVENESS backstop, not a latency budget: it exists so a child that
    never streams or never honours SIGINT fails instead of hanging the
    suite.  WHAT THE CODE DOES, exactly: the first use in a process takes
    the measurement and the process keeps that draw (the probe is cached),
    so the bound measured "when used" means when FIRST used.  That draw is
    not the whole instrument, because a stall the draw could not see still
    arrives - six instances, the last on a quiet host.  So the failure
    path re-measures: when a child misses this bound, the test takes a
    FRESH draw (``fresh_watch_liveness_bound_seconds``, uncached) and
    extends exactly once, saying so in what it prints and fails with.  The
    floor is the replaced constant (30.0), never a fresh smaller number;
    the ceiling (120 s) declares the host unhealthy instead of waiting
    forever.  The latencies inside the bound are the host's business: they
    are measured and printed, and only deafness fails, naming the loadavg
    it failed under.  Loadavg never sizes a bound.
    """

    return _watch_bound(_measured_cli_start_seconds())


def fresh_watch_liveness_bound_seconds() -> float:
    """An uncached draw of the watch derivation - the failure path's re-measure.

    Identical derivation to ``derived_watch_liveness_bound_seconds`` (same
    probe, same stated 40x assumption, same replaced-constant floor and
    ceiling), but it always takes a new measurement instead of the
    process's cached first draw.  Called when a child has already missed
    the bound: a miss is evidence the contention domain just turned hot,
    so the extension is provisioned from the domain as it is NOW.
    """

    return _watch_bound(
        measured_command_start_seconds(
            [sys.executable, "-m", "floati", "describe", "--json"]
        )
    )


def derived_codex_handshake_budget_seconds() -> float:
    """TIMEOUT-PUB-1: the fixture Codex app-server handshake budget.

    Assumes the fixture completes its three-message JSONL handshake within
    20x what this host charges, measured when this budget is used, to start
    one bare interpreter with the fixture's imports.  The 5 s floor covers
    a host that was quiet when measured; the 30 s ceiling is the product's
    own ``quota_collector_invalid`` bound, which refuses anything larger.
    """

    return min(30.0, max(5.0, 20.0 * _measured_bare_interpreter_start_seconds()))
