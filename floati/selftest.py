"""Run the complete Floati selftest without a result-masking wrapper."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .manifest import EXPECTED_CANONICAL_REF, verify_manifest
from .operator_interrupt import restore_operator_sigint


TEST_FAILURE = 10
MANIFEST_MISMATCH = 34
WORKERS_ENV = "FLOATI_SELFTEST_WORKERS"
_CLAUDE_MESSAGING_VARIABLES = (
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
)
_OUTCOME_LINE_CAP = 200
_BLOCK_SEP = "=" * 70
_BLOCK_RULE = "-" * 70
_BLOCK_HEAD = re.compile(r"^(?:FAIL|ERROR): (.*)$")
# Quiet kinds still leave the run successful. fixture_skip is a skip.
# Any other fixture_* or blocked kind is a non-success the runner defined.
_REPORTED_KINDS = frozenset({
    "failure",
    "error",
    "subtest_failure",
    "subtest_error",
    "unexpected_success",
    "fixture_error",
})


def outcome_for(tests_successful: bool, manifest_errors: list) -> int:
    if not tests_successful:
        return TEST_FAILURE
    if manifest_errors:
        return MANIFEST_MISMATCH
    return 0


def emit_verified(stream) -> None:
    payload = {"canonical_ref": EXPECTED_CANONICAL_REF, "status": "bundle_verified"}
    if getattr(stream, "isatty", lambda: False)():
        from .brand import render_buoy_mark

        print(render_buoy_mark(color=True), file=stream)
    print(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=stream,
    )


def parse_invocation(argv, environ) -> tuple:
    """Return (workers or None, output or None).

    No worker count keeps the historical serial suite. An explicit count comes
    from ``--workers`` or, if that is omitted, ``FLOATI_SELFTEST_WORKERS``.
    """

    parser = argparse.ArgumentParser(prog="floati.selftest")
    parser.add_argument(
        "--workers",
        type=int,
        help="parallel runner worker count; omit to stay serial",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="fresh parallel-runner output directory",
    )
    args = parser.parse_args(list(argv))
    if args.workers is not None:
        workers = args.workers
    else:
        raw = environ.get(WORKERS_ENV)
        workers = None if raw is None or raw == "" else int(raw)
    return workers, args.output


def requested_workers(argv, environ) -> int | None:
    workers, _output = parse_invocation(argv, environ)
    return workers


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_parallel_output() -> Path:
    """A not-yet-created directory under a realpath'd temporary parent.

    The parallel runner refuses an output path whose ancestors are not
    ordinary directories, and it refuses a directory that already exists.
    """

    if sys.platform == "darwin":
        base = "\x2fprivate/tmp"
    else:
        base = os.path.realpath(tempfile.gettempdir())
    parent = Path(tempfile.mkdtemp(prefix="floati-selftest-", dir=base))
    return parent / "run"


def finish(tests_successful: bool, manifest_root: Path) -> int:
    if not tests_successful:
        return outcome_for(False, [])
    errors = verify_manifest(manifest_root)
    if errors:
        print(
            json.dumps(
                {
                    "canonical_ref": EXPECTED_CANONICAL_REF,
                    "errors": errors,
                    "status": "bundle_mismatch",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return outcome_for(True, errors)
    emit_verified(sys.stdout)
    return outcome_for(True, [])


def _reported_kind(kind: object) -> bool:
    if not isinstance(kind, str):
        return False
    if kind in _REPORTED_KINDS:
        return True
    if "blocked" in kind:
        return True
    return kind.startswith("fixture_") and kind != "fixture_skip"


def _identity(event: dict) -> str:
    test_id = event.get("test_id")
    if not isinstance(test_id, str) or not test_id:
        test_id = "?"
    subtest = event.get("subtest")
    if isinstance(subtest, str) and subtest and subtest != test_id:
        if subtest.startswith(test_id + " "):
            return subtest
        return test_id + " " + subtest
    return test_id


def _method_form(test_id: str) -> str:
    head, dot, method = test_id.rpartition(".")
    if not dot or not method:
        return test_id
    return f"{method} ({head})"


def _log_label(event: dict) -> str | None:
    """Unittest ``FAIL``/``ERROR`` description for one aggregate event."""

    test_id = event.get("test_id")
    if not isinstance(test_id, str) or not test_id:
        return None
    subtest = event.get("subtest")
    if isinstance(subtest, str) and subtest.startswith(test_id + " "):
        return _method_form(test_id) + subtest[len(test_id):]
    if " " in test_id:
        return test_id
    return _method_form(test_id)


def _traceback_tails(text: str) -> list[tuple[str, str]]:
    """Last non-empty line of each retained ``FAIL``/``ERROR`` block."""

    lines = text.splitlines()
    found = []
    index = 0
    while index < len(lines):
        match = _BLOCK_HEAD.match(lines[index])
        if match is None:
            index += 1
            continue
        label = match.group(1)
        index += 1
        if index < len(lines) and lines[index] == _BLOCK_RULE:
            index += 1
        body = []
        while index < len(lines):
            line = lines[index]
            if line == _BLOCK_SEP:
                break
            if (
                line == _BLOCK_RULE
                and index + 1 < len(lines)
                and lines[index + 1].startswith("Ran ")
            ):
                break
            body.append(line)
            index += 1
        last = ""
        for line in reversed(body):
            if line.strip():
                last = line.strip()
                break
        found.append((label, last))
    return found


def _retained_tails(output: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    if not output.is_dir():
        return found
    for path in sorted(output.glob("*.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, last in _traceback_tails(text):
            found.setdefault(label, []).append(last)
    return found


def _parallel_nonsuccess_lines(output: Path) -> list[str]:
    path = Path(output) / "final.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(report, dict):
        return []
    aggregate = report.get("aggregate")
    events = aggregate.get("events") if isinstance(aggregate, dict) else None
    if not isinstance(events, list):
        return []
    chosen = [
        event for event in events
        if isinstance(event, dict) and _reported_kind(event.get("kind"))
    ]
    if not chosen:
        return []
    retained = _retained_tails(Path(output))
    rendered = []
    for event in chosen:
        identity = _identity(event)
        label = _log_label(event)
        last = ""
        if label is not None:
            queue = retained.get(label)
            if queue:
                last = queue.pop(0)
        line = f"{event.get('kind')} {identity}"
        if last:
            line = f"{line} {last}"
        rendered.append(line)
    shown = rendered[:_OUTCOME_LINE_CAP]
    hidden = len(rendered) - len(shown)
    lines = list(shown)
    if hidden:
        lines.append(f"+{hidden} more")
    lines.append(f"{len(rendered)} non-success")
    return lines


def emit_parallel_nonsuccess(output: Path, stream=None) -> None:
    """Print non-success aggregate outcomes. One line each, then a count."""

    if stream is None:
        stream = sys.stdout
    for line in _parallel_nonsuccess_lines(Path(output)):
        print(line, file=stream)


def parallel_mode_exit(
    workers: int, *, root: Path, manifest_root: Path, output: Path
) -> int:
    """Run ``scripts/test-parallel.py run`` and then the manifest contract.

    Profile and reference are omitted. The runner then schedules by test counts.
    Non-success outcomes are printed before the runner's one-line summary.
    The runner's exit code still decides the manifest step.
    """

    from scripts.fixture_finalizer import run_child

    script = Path(root) / "scripts" / "test-parallel.py"
    child_environment = {name: value for name, value in os.environ.items()
                         if name not in _CLAUDE_MESSAGING_VARIABLES}
    completed = run_child(
        [
            sys.executable,
            str(script),
            "run",
            "--workers",
            str(workers),
            "--output",
            str(output),
        ],
        cwd=os.fspath(root),
        env=child_environment,
        check=False,
        stdout=subprocess.PIPE,
    )
    emit_parallel_nonsuccess(output)
    if completed.stdout:
        text = completed.stdout.decode("utf-8", "replace")
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return finish(completed.returncode == 0, Path(manifest_root))


def main(argv=None) -> int:
    # Selftest can start inside a native Claude session. Test modules must
    # inherit neither its address nor its messaging credential.
    for name in _CLAUDE_MESSAGING_VARIABLES:
        os.environ.pop(name, None)
    restore_operator_sigint()
    if argv is None:
        argv = sys.argv[1:]
    workers, output = parse_invocation(argv, os.environ)
    if workers is None:
        suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py")
        # Discovery is executable code. Refuse if an import reintroduced a
        # native coordinate, before starting any test from that population.
        if any(name in os.environ for name in _CLAUDE_MESSAGING_VARIABLES):
            raise ValueError("test_environment_contains_claude_messaging")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return finish(result.wasSuccessful(), Path.cwd())
    if output is None:
        output = default_parallel_output()
    return parallel_mode_exit(
        workers,
        root=repository_root(),
        manifest_root=Path.cwd(),
        output=output,
    )


if __name__ == "__main__":
    sys.exit(main())
