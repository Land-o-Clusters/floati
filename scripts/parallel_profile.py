"""Read advisory native-test timing measurements for partition balancing."""

import csv
import hashlib
import io
import math
import statistics


def profile_csv(durations: dict) -> bytes:
    """The canonical, deterministic producer shared by references and refresh."""
    if not isinstance(durations, dict):
        raise ValueError('test_durations must be a mapping')
    for test_id, seconds in durations.items():
        if not isinstance(test_id, str) or not test_id.strip():
            raise ValueError('test_durations has an empty test ID')
        if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                or seconds < 0):
            raise ValueError('test_durations must contain finite nonnegative numbers')
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(('test_id', 'seconds'))
    writer.writerows(sorted(durations.items()))
    return stream.getvalue().encode('utf-8')


def completed_durations(report: dict) -> dict:
    """Collect measured methods only, after the native population proof."""
    try:
        from parallel_plan import reconcile
    except ModuleNotFoundError:
        from scripts.parallel_plan import reconcile
    if (report.get('mode') not in ('serial', 'run') or report.get('status') != 'complete'
            or report.get('cleanup', {}).get('successful') is not True):
        raise ValueError('profile requires a completed run with successful cleanup')
    source, interpreter = report.get('source'), report.get('interpreter')
    census, plan, results = report['census'], report['plan'], report['results']
    if (not source or not interpreter or census.get('source') != source
            or census.get('interpreter') != interpreter):
        raise ValueError('profile census identity mismatch')
    aggregate = reconcile(census, plan, results)
    if aggregate != report['aggregate'] or aggregate['successful'] is not report['successful']:
        raise ValueError('profile aggregate mismatch')
    durations = {}
    for result in results:
        if result.get('source') != source or result.get('interpreter') != interpreter:
            raise ValueError('profile worker identity mismatch')
        measured = result.get('test_durations')
        profile_csv(measured)  # Validate IDs and finite nonnegative values once for every worker.
        if set(measured) - set(result['started_ids']):
            raise ValueError('test_durations must name only started IDs')
        if set(measured).intersection(durations):
            raise ValueError('duplicate measured test ID')
        durations.update(measured)
    return durations


def profile_weights(data: bytes, census: dict):
    """Produce exact unit weights; measurements never supply test membership.

    Old native discovery can contain repeated imported aliases. Taking the
    maximum observed duration per ID is a conservative scheduling hint, not
    a deduplication of the actual census. Unmeasured current tests receive the
    median of matched per-ID measurements. All assumptions are receipted.
    """
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), strict=True)
        if reader.fieldnames is None or not {"test_id", "seconds"} <= set(reader.fieldnames):
            raise ValueError("profile requires test_id and seconds columns")
        if len(set(reader.fieldnames)) != len(reader.fieldnames) or any(
            not name.strip() for name in reader.fieldnames
        ):
            raise ValueError("profile has duplicate or empty column names")
        durations = {}
        rows = 0
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("profile row does not match its columns")
            test_id = row.get("test_id")
            if not isinstance(test_id, str) or not test_id.strip():
                raise ValueError("profile has empty test ID")
            try:
                seconds = float(row.get("seconds", ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("profile has invalid duration for " + test_id) from exc
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("profile has invalid duration for " + test_id)
            durations[test_id] = max(durations.get(test_id, 0.0), seconds)
            rows += 1
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("invalid profile CSV") from exc
    population = set(census["ids"])
    matched = population.intersection(durations)
    if not matched:
        raise ValueError("profile has no matching population IDs")
    fallback = statistics.median(durations[test_id] for test_id in matched)
    weights = {
        unit["key"]: sum(durations.get(test_id, fallback) for test_id in unit["ids"])
        for unit in census["units"]
    }
    evidence = {
        "provenance": "ESTIMATE: historical durations with median fallback",
        "sha256": hashlib.sha256(data).hexdigest(),
        "rows": rows,
        "distinct_profile_ids": len(durations),
        "repeated_rows": rows - len(durations),
        "duplicate_policy": "maximum per exact test ID",
        "matched_ids": len(matched),
        "unmatched_population_ids": sorted(population - matched),
        "unused_profile_ids": sorted(set(durations) - population),
        "fallback_seconds": fallback,
    }
    return weights, evidence


def main(argv=None):
    """Check a completed run's profile; write only a reviewed candidate digest.

    No discovery, test execution, global profile replacement or qualification.
    The optional baseline is evaluated with the completed run's method costs.
    """
    import argparse
    import importlib.util
    import json
    import os
    from pathlib import Path
    import tempfile

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--run', required=True, type=Path, help='completed native run directory')
    parser.add_argument('--output', required=True, type=Path, help='explicit advisory CSV destination')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='compare exact bytes (default)')
    mode.add_argument('--write', action='store_true', help='publish the reviewed candidate')
    parser.add_argument('--expected-sha256', help='candidate digest from check; required for write')
    parser.add_argument('--workers', type=int, help='default: source run worker count')
    parser.add_argument('--baseline-profile', type=Path)
    args = parser.parse_args(argv)
    try:
        spec = importlib.util.spec_from_file_location(
            'profile_launcher', Path(__file__).with_name('test-parallel.py'))
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        run = args.run.resolve(strict=True)
        output = Path(os.path.abspath(args.output))
        if output.resolve() != output or run == output or run in output.parents:
            raise ValueError('profile output must be ordinary and outside source run evidence')
        raw = (run / 'final.json').read_bytes()
        report = json.loads(raw)
        durations = completed_durations(report)
        census, plan = report['census'], report['plan']
        for name, expected in (
            ('request.json', {key: report[key] for key in ('mode', 'source', 'interpreter')}),
            ('census.json', census), ('plan.json', plan),
        ):
            if launcher.read_json(run / name) != expected:
                raise ValueError('profile persisted request, census or plan mismatch')
        for result in report['results']:
            if launcher.read_worker_result(run, result['key']) != result:
                raise ValueError('profile worker journal mismatch')
        children = report['children']
        expected_codes = {'discovery': 0, **{
            row['key']: 0 if row['successful'] else 1 for row in report['results']}}
        if (len(children) != len(expected_codes)
                or {row['key']: row['returncode'] for row in children} != expected_codes):
            raise ValueError('profile child process mismatch')
        data = profile_csv(durations)
        sha256 = hashlib.sha256(data).hexdigest()
        weights, audit = profile_weights(data, census)
        workers = args.workers if args.workers is not None else report['workers']
        refreshed = launcher.make_plan(census, workers, weights)
        # validate_plan proves exactly one occurrence of every original ID,
        # intact native units, original within-worker order and serial reasons.
        launcher.validate_plan(census, refreshed)
        proof = {
            'status': 'match' if output.is_file() and output.read_bytes() == data else 'drift',
            'sha256': sha256, 'run_sha256': hashlib.sha256(raw).hexdigest(),
            'source': report['source'], 'population_count': len(census['ids']),
            'population_digest': census['population_digest'],
            'refreshed_population_digest': refreshed['population_digest'],
            'measured_ids': len(durations),
            'unmeasured_ids': audit['unmatched_population_ids'],
            'fallback_seconds': audit['fallback_seconds'],
            'estimates': launcher.plan_estimates(refreshed, weights),
            'recorded_plan_estimates': launcher.plan_estimates(plan, weights),
            'serial_reasons': {unit['key']: unit['serial_reason'] for unit in census['units']
                               if unit['serial_reason'] is not None},
            'load_1_5_15': list(os.getloadavg()),
        }
        if args.baseline_profile is not None:
            baseline = args.baseline_profile.read_bytes()
            old_weights, _ = profile_weights(baseline, census)
            old_plan = launcher.make_plan(census, workers, old_weights)
            proof.update(baseline_profile_sha256=hashlib.sha256(baseline).hexdigest(),
                         baseline_reproduces_recorded_plan=old_plan == plan,
                         baseline_estimates=launcher.plan_estimates(old_plan, weights))
        if args.write:
            if args.expected_sha256 != sha256:
                raise ValueError('profile write requires the exact candidate --expected-sha256 from check')
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.profile-', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, output)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
            proof['status'] = 'written'
        print(json.dumps(proof, ensure_ascii=False, sort_keys=True))
        return 0 if proof['status'] in ('match', 'written') else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'refused', 'error': str(error)}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
