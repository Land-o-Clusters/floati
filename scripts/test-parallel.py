#!/usr/bin/env python3
"""Governed native unittest execution; source and process ownership helpers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(
        ["/usr/bin/git", *arguments], cwd=root, stderr=subprocess.PIPE,
    )


def source_identity(root: Path) -> dict:
    """Bind HEAD to actual tracked bytes, including draft and bundle inputs."""
    root = root.resolve(strict=True)
    top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip())
    if top.resolve() != root:
        raise ValueError("source_not_repository_root")
    unknown = _git(root, "ls-files", "--others", "-z").split(b"\0")
    python_additions = sorted(name.decode() for name in unknown if name.endswith(b".py"))
    if python_additions:
        raise ValueError("untracked_python: " + ", ".join(python_additions))
    digest = hashlib.sha256()
    tracked = sorted(name for name in _git(root, "ls-files", "-z").split(b"\0") if name)
    for name in tracked:
        path = root / os.fsdecode(name)
        try:
            info = path.lstat()
        except FileNotFoundError as error:
            raise ValueError("source_missing: " + os.fsdecode(name)) from error
        if not stat.S_ISREG(info.st_mode) or path.resolve() != path:
            raise ValueError("source_not_ordinary: " + os.fsdecode(name))
        digest.update(name + b"\0")
        digest.update(str(stat.S_IMODE(info.st_mode)).encode() + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return {
        "head": _git(root, "rev-parse", "HEAD").decode().strip(),
        "digest": digest.hexdigest(),
        "tracked_files": len(tracked),
    }


def check_work_parent(path: Path) -> None:
    """Allow cold serial creation; refuse a foreign or nonordinary parent."""
    if path.resolve(strict=False) != path:
        raise ValueError("work_parent_not_ordinary")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("work_parent_not_ordinary")
    if info.st_uid != os.getuid():
        raise ValueError("work_parent_foreign_owner")


# Sibling-only infrastructure; no product imports before the child HOME fence.
import argparse
from contextlib import contextmanager
import json
import math
import platform
import signal
import site
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_finalizer import cleanup_path, owned_mkdtemp, retain_path
from parallel_isolation import make_worker_environment, apply_serial_rules, _platform_temp_root, worker_tmp_budget, require_clean_test_environment
from parallel_plan import make_plan, validate_plan, reconcile, compare_results, worker_limit, validate_worker_count, plan_estimates
from parallel_plan import shard_census, shard_unit_keys
from parallel_profile import profile_weights, profile_csv
from parallel_unittest import discover_suite, select_suite, run_suite
from parallel_processes import ProcessTree, validate_ownership_receipt


class Cancelled(KeyboardInterrupt):
    def __init__(self, number):
        self.number = number


class Cancellation:
    """Signal handlers only record intent; safe boundaries deliver it."""
    def __init__(self):
        self.number = None
        self.committed = False

    def record(self, number, frame):
        if self.number is None and not self.committed:
            self.number = number

    def check(self):
        if self.number is not None:
            raise Cancelled(self.number)


@contextmanager
def cancellation_handlers(handler):
    numbers = (signal.SIGINT, signal.SIGTERM)
    prior = {}
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
    try:
        for number in numbers:
            prior[number] = signal.signal(number, handler)
    except BaseException:
        for number, original in prior.items():
            signal.signal(number, original)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    try:
        yield
    finally:
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
        try:
            for number, original in prior.items():
                signal.signal(number, original)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


@contextmanager
def defer_cancellation(cancellation):
    # Record into shared state immediately, including when the body raises.
    with cancellation_handlers(cancellation.record):
        yield
    cancellation.check()


@contextmanager
def final_commitment(cancellation):
    """Close cancellation before scratch removal and final receipt publication.

    The masked pending-signal snapshot is the commitment boundary. Previously
    recorded or pending requests settle into interruption; signals after that
    snapshot cannot cancel the committed result. Removal and receipt I/O have
    no time bound and remain protected until they return or fail.
    """
    numbers = (signal.SIGINT, signal.SIGTERM)
    prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
    try:
        pending = signal.sigpending()
        for number in numbers:
            if number in pending:
                cancellation.record(number, None)
        cancellation.committed = True
        yield
    finally:
        # Consume queued signals belonging to this committed run, including
        # when the caller had already blocked one of the supported signals.
        try:
            for number in signal.sigpending().intersection(numbers):
                signal.sigwait((number,))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)


def interpreter_identity() -> dict:
    return {"executable": str(Path(sys.executable).resolve()), "version": sys.version,
            "platform": platform.platform(), "implementation": sys.implementation.name}


def write_json(path: Path, value: dict) -> None:
    with path.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path: Path) -> dict:
    with path.open() as stream:
        return json.load(stream)


def sample_load_average():
    """This host's 1/5/15-minute load, or a typed endpoint-local absence."""
    try:
        return list(os.getloadavg())
    except (OSError, AttributeError) as exc:
        return {'status': 'unavailable', 'code': 'getloadavg_failed', 'detail': str(exc)}


def write_process_cost(output: Path, report: dict, tree) -> None:
    """Separate informational evidence; never alter native receipt fields."""
    request = output / 'request.json'
    row = {'schema_version': 1, 'kind': 'parallel_process_cost',
           'launcher_pid': os.getpid(), 'source': report.get('source'),
           'interpreter': report.get('interpreter'),
           'final_path': str(output / 'final.json'),
           'request_sha256': hashlib.sha256(request.read_bytes()).hexdigest() if request.is_file() else None,
           'native_status': report['status'],
           'availability': 'available' if tree is not None else 'unavailable',
           'cost': tree.cost() if tree is not None else None}
    # The output directory is newly owned by this run. Refuse a preexisting
    # coordinate rather than following an alias or replacing another artifact.
    with (output / 'process-cost.json').open('x') as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def census_for(root: Path, mode: str):
    suite, census = discover_suite(root)
    census = apply_serial_rules(census, read_json(root / 'scripts/parallel_serial_classes.json'))
    if mode == 'serial':
        for unit in census['units']:
            unit['serial_reason'] = 'complete serial reference'
    return suite, census


class Journal:
    """An append-only, sequenced callback log outside disposable child scratch."""
    def __init__(self, path: Path):
        self.stream = path.open('x')
        self.sequence = 0

    def emit(self, record):
        self.stream.write(json.dumps({'sequence': self.sequence, 'record': record},
                                     ensure_ascii=False) + '\n')
        self.stream.flush()
        self.sequence += 1

    def close(self):
        self.stream.close()


def read_worker_result(output: Path, key: str) -> dict:
    try:
        rows = [json.loads(line) for line in (output / (key + '.events.jsonl')).read_text().splitlines()]
    except (OSError, ValueError) as exc:
        raise ValueError(f'{key}: invalid or missing event journal: {exc}') from exc
    for sequence, row in enumerate(rows):
        if (type(row.get('sequence')) is not int or row['sequence'] != sequence
                or not isinstance(row.get('record'), dict)):
            raise ValueError(f'{key}: invalid journal sequence')
    records = [row['record'] for row in rows]
    if (len(records) < 2 or records[0].get('kind') != 'header'
            or records[-1].get('kind') != 'footer'
            or any(record.get('kind') in ('header', 'footer') for record in records[1:-1])):
        raise ValueError(f'{key}: missing or invalid journal footer')
    result = read_json(output / (key + '.result.json'))
    if records[-1].get('result') != result or records[0].get('key') != key or result.get('key') != key:
        raise ValueError(f'{key}: footer receipt mismatch')
    if (records[0].get('selected_ids') != result.get('selected_ids')
            or records[0].get('population_digest') != result.get('population_digest')):
        raise ValueError(f'{key}: header receipt mismatch')
    callbacks = records[1:-1]
    for kind, field in [('start', 'started_ids'), ('stop', 'stopped_ids')]:
        if [event.get('test_id') for event in callbacks if event.get('kind') == kind] != result.get(field):
            raise ValueError(f'{key}: journal {kind} mismatch')
    if [event for event in callbacks if event.get('kind') not in ('start', 'stop')] != result.get('events'):
        raise ValueError(f'{key}: journal outcomes mismatch')
    return result


def child_main(root: Path, output: Path, key: str) -> int:
    require_clean_test_environment(os.environ)
    request = read_json(output / 'request.json')
    if interpreter_identity() != request['interpreter']:
        raise ValueError('interpreter drift')
    if source_identity(root) != request['source']:
        raise ValueError('source drift before discovery')
    suite, census = census_for(root, request['mode'])
    census.update(source=request['source'], interpreter=request['interpreter'])
    if source_identity(root) != request['source']:
        raise ValueError('source drift during discovery')
    if key == 'discovery':
        write_json(output / 'census.json', census)
        return 0
    expected = read_json(output / 'census.json')
    if census != expected:
        raise ValueError('whole-population census mismatch')
    if request.get('shard_units'):
        census = shard_census(census, request['shard_units'])
    plan = read_json(output / 'plan.json')
    validate_plan(census, plan)
    choices = [assignment for assignment in plan['assignments'] if assignment['key'] == key]
    if len(choices) != 1:
        raise ValueError('unknown worker assignment')
    assignment = choices[0]
    selected = select_suite(suite, assignment['ids'])
    require_clean_test_environment(os.environ)
    journal = Journal(output / (key + '.events.jsonl'))
    try:
        journal.emit({'kind': 'header', 'key': key, 'selected_ids': assignment['ids'],
                      'population_digest': census['population_digest']})
        result = run_suite(selected, assignment, census['population_digest'], sys.stderr,
                           event_sink=journal.emit)
        result.update(source=source_identity(root), interpreter=interpreter_identity())
        write_json(output / (key + '.result.json'), result)
        journal.emit({'kind': 'footer', 'result': result})
    finally:
        journal.close()
    if result['status'] == 'interrupted':
        return 130
    return 0 if result['successful'] else 1


class Children:
    """Own direct handles and observe descendants across session changes."""
    def __init__(self, root, output, scratch, user_site, cancellation=None):
        self.root, self.output, self.scratch = root, output, scratch
        self.user_site = user_site
        self.children = []
        self.cleanup_errors = []
        self.tree = ProcessTree(output)
        self.cancellation = cancellation or Cancellation()

    def spawn(self, key):
        self.cancellation.check()
        short_key = {'discovery': 'd', 'serial': 's'}.get(key, key.replace('worker-', 'w'))
        worker_root = self.scratch / short_key
        worker_root.mkdir(mode=0o700)
        env = make_worker_environment(worker_root, os.environ, self.user_site)
        log = (self.output / (key + '.log')).open('x')
        # Retain the Popen object before construction: its PID remains reachable
        # even if initialization fails after the OS creates the child. Handled
        # cancellation is deferred until construction and adoption both finish.
        with defer_cancellation(self.cancellation):
            proc = subprocess.Popen.__new__(subprocess.Popen)
            child = {'key': key, 'process': proc, 'log': log, 'joined': False}
            self.children.append(child)
            try:
                proc.__init__(
                    [sys.executable, '-B', str(self.root / 'scripts/test-parallel.py'), '_child',
                     '--output', str(self.output), '--key', key], cwd=self.root,
                    env=env, stdout=log, stderr=log, start_new_session=True,
                )
            except BaseException:
                if getattr(proc, 'pid', None) is None:
                    self.children.remove(child)
                    log.close()
                else:
                    self.tree.root(proc, key)
                raise
            self.tree.root(proc, key)
        return child

    def join(self, child):
        while True:
            self.cancellation.check()
            self.tree.observe()
            try:
                code = child['process'].wait(timeout=.02)
                break
            except subprocess.TimeoutExpired:
                pass
        self.tree.observe()
        child['joined'] = True
        child['log'].close()
        survivors = self.tree.alive(child['key'])
        if survivors:
            self.cleanup_errors.append(child['key'] + ': descendants survived leader')
            self.cleanup_errors.extend(self.tree.unreadable_refusals(survivors))
        self.cleanup_errors.extend(error for error in self.tree.errors
                                   if error not in self.cleanup_errors)
        return code

    def close(self):
        self.tree.observe()
        self.tree.signal(signal.SIGINT)
        deadline = time.monotonic() + 1
        while self.tree.alive() and time.monotonic() < deadline:
            self.tree.observe()
            time.sleep(.02)
        self.tree.signal(signal.SIGKILL)
        # Every root gets its own bounded reap attempt; one failure cannot skip
        # later roots. Observed descendants remain tracked after leader reaping.
        for child in self.children:
            proc = child['process']
            try:
                proc.wait(timeout=2)
            except Exception as exc:
                self.cleanup_errors.append(child['key'] + ': direct child reap failed: ' + str(exc))
            try:
                child['log'].close()
            except Exception as exc:
                self.cleanup_errors.append(child['key'] + ': log close failed: ' + str(exc))
        deadline = time.monotonic() + 1
        while self.tree.alive() and time.monotonic() < deadline:
            self.tree.observe()
            self.tree.signal(signal.SIGKILL)
            time.sleep(.02)
        survivors = self.tree.alive()
        if survivors:
            self.cleanup_errors.append('observed owned descendants remain')
            self.cleanup_errors.extend(self.tree.unreadable_refusals(survivors))
        else:
            # A descendant surviving its leader already invalidated normal
            # execution at join(). Once teardown proves its departure, that
            # earlier observation no longer prevents owned scratch cleanup.
            departed_leaders = {child['key'] + ': descendants survived leader'
                                for child in self.children}
            self.cleanup_errors[:] = [error for error in self.cleanup_errors
                                      if error not in departed_leaders]
        self.cleanup_errors.extend(self.tree.errors)
        self.tree.journal.close()
        return [{'key': child['key'], 'pid': child['process'].pid,
                 'returncode': child['process'].returncode,
                 'log': str(self.output / (child['key'] + '.log'))}
                for child in self.children]


def reference_result(path: Path, source: dict, interpreter: dict) -> tuple:
    reference_data = path.read_bytes()
    reference = json.loads(reference_data)
    if (reference.get('mode') != 'serial' or reference.get('status') != 'complete'
            or not reference.get('cleanup', {}).get('successful')
            or reference.get('source') != source or reference.get('interpreter') != interpreter):
        raise ValueError('reference must be complete serial evidence on identical source and interpreter')
    census, plan = reference['census'], reference['plan']
    if (read_json(path.parent / 'request.json') != {
            'mode': 'serial', 'source': source, 'interpreter': interpreter}
            or read_json(path.parent / 'census.json') != census
            or read_json(path.parent / 'plan.json') != plan):
        raise ValueError('reference persisted request, census or plan mismatch')
    expected_assignment = {'key': 'serial', 'phase': 'serial', 'ids': census['ids'],
                           'unit_keys': [unit['key'] for unit in census['units']]}
    if (plan.get('assignments') != [expected_assignment]
            or any(unit.get('serial_reason') != 'complete serial reference'
                   for unit in census['units'])
            or len(reference['results']) != 1
            or reference['results'][0].get('key') != 'serial'):
        raise ValueError('reference must execute the complete native population in one serial process')
    if (reference['census'].get('source') != source
            or reference['census'].get('interpreter') != interpreter
            or any(result.get('source') != source or result.get('interpreter') != interpreter
                   for result in reference['results'])):
        raise ValueError('reference worker source or interpreter drift')
    aggregate = reconcile(reference['census'], reference['plan'], reference['results'])
    if aggregate != reference['aggregate']:
        raise ValueError('reference aggregate mismatch')
    child_codes = {child['key']: child['returncode'] for child in reference['children']}
    expected_codes = {'discovery': 0}
    expected_codes.update({result['key']: 0 if result['successful'] else 1
                           for result in reference['results']})
    if len(child_codes) != len(reference['children']) or child_codes != expected_codes:
        raise ValueError('reference child process failure')
    for result in reference['results']:
        if read_worker_result(path.parent, result['key']) != result:
            raise ValueError('reference journal receipt mismatch')
    durations = reference['results'][0].get('test_durations')
    if (not isinstance(durations, dict)
            or set(durations) - set(reference['results'][0]['started_ids'])
            or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in durations.values())):
        raise ValueError('reference test_durations must map started IDs to finite nonnegative numbers')
    return aggregate, durations, hashlib.sha256(reference_data).hexdigest()


def reference_profile(durations: dict, census: dict) -> tuple:
    """Use measured serial method times as advisory costs for the next run."""
    data = profile_csv(durations)
    if durations:
        costs, audit = profile_weights(data, census)
        audit['provenance'] = 'ESTIMATE: matching serial method durations with median fallback'
        audit['fallback_policy'] = 'median of measured durations for matching population IDs'
        return costs, audit
    # A complete reference can consist entirely of fixture-blocked tests.
    # With no measured method time, make the one-second assumption explicit.
    return {unit['key']: float(len(unit['ids'])) for unit in census['units']}, {
        'provenance': 'ESTIMATE: no method durations available; one second per test',
        'sha256': hashlib.sha256(data).hexdigest(), 'rows': 0,
        'distinct_profile_ids': 0, 'repeated_rows': 0,
        'matched_ids': 0, 'unmatched_population_ids': sorted(census['ids']),
        'unused_profile_ids': [], 'fallback_seconds': 1.0,
        'fallback_policy': 'one second per test because no method durations were measured',
    }


def committed_platform_profile(root: Path, source_head: str):
    platform_name = {'darwin': 'macos', 'linux': 'linux'}.get(sys.platform)
    if platform_name is None:
        return None
    relative = f'scripts/profiles/harbor-{platform_name}.csv'
    if not _git(root, 'ls-tree', '--name-only', source_head, '--', relative):
        return None
    return root / relative, _git(root, 'show', f'{source_head}:{relative}')


def launch(mode: str, output: Path, workers=None, reference=None, profile=None, *, shard=None) -> dict:
    cancellation = Cancellation()
    with cancellation_handlers(cancellation.record):
        return _launch(mode, output, workers, reference, cancellation, profile, shard)


def _launch(mode: str, output: Path, workers, reference, cancellation, profile=None, shard=None) -> dict:
    start = time.monotonic()
    load_start = sample_load_average()
    root = Path(__file__).resolve().parent.parent
    user_site = site.getusersitepackages()  # Capture before any discovery HOME pin.
    report = {'mode': mode, 'status': 'invalid', 'successful': False, 'qualified': False,
              'results': [], 'phase_seconds': {}, 'cleanup': {'successful': False},
              'load_average': {'start': load_start},
              'load_before': load_start if isinstance(load_start, list) else None}
    owned_output = False
    scratch = None
    children = None
    try:
        if workers is not None:
            validate_worker_count(workers)
        if reference is not None and mode != 'run':
            raise ValueError('reference is supported only for run')
        if shard is not None:
            index, shard_count = shard
            if not 1 <= index <= shard_count or mode not in ('plan', 'run') or reference is not None:
                raise ValueError('invalid shard index/count or incompatible mode')
        output = Path(os.path.abspath(output))
        if output.resolve() != output:
            raise ValueError('output must have ordinary directory ancestors')
        output.mkdir(mode=0o700)  # Deliberately refuse reuse, even an empty directory.
        owned_output = True
        report.update(source=source_identity(root), interpreter=interpreter_identity())
        check_work_parent(Path('\x2fprivate/tmp/floati-work' if sys.platform == 'darwin' else '\x2ftmp/floati-work'))
        platform_tmp = _platform_temp_root()
        scratch = Path(owned_mkdtemp(prefix='hp-', dir=platform_tmp))
        report['scratch_root'] = str(scratch)
        # Bound the actual generated worker paths in bytes before launching
        # any child. The same platform root is used at every nesting depth.
        longest_key = 'w' + str(workers or 8)
        longest_tmp = scratch / longest_key / 'tmp'
        budget = worker_tmp_budget()
        report['worker_tmp_budget_bytes'] = budget
        if len(os.fsencode(str(longest_tmp))) > budget:
            raise ValueError('parallel_worker_tmpdir_exceeds_unix_socket_bound')
        request = {'mode': mode, 'source': report['source'], 'interpreter': report['interpreter']}
        write_json(output / 'request.json', request)
        children = Children(root, output, scratch, user_site, cancellation)
        phase_start = time.monotonic()
        discovery = children.spawn('discovery')
        if children.join(discovery) != 0 or children.cleanup_errors:
            raise ValueError('discovery failed; see discovery.log')
        census = read_json(output / 'census.json')
        if census.get('source') != report['source'] or census.get('interpreter') != report['interpreter']:
            raise ValueError('discovery identity mismatch')
        reference_aggregate = None
        if reference is not None:
            reference_path = Path(reference).resolve(strict=True)
            reference_aggregate, durations, reference_hash = reference_result(
                reference_path, report['source'], report['interpreter'])
        costs = None
        weight_source = 'test counts'
        profile_input = None
        if profile is not None:
            profile_path = Path(profile).resolve(strict=True)
            profile_input = profile_path, profile_path.read_bytes()
        elif reference is None and mode in ('plan', 'run'):
            profile_input = committed_platform_profile(root, report['source']['head'])
        if profile_input is not None:
            profile_path, profile_data = profile_input
            costs, audit = profile_weights(profile_data, census)
            report['profile'] = {'path': str(profile_path), **audit}
            weight_source = 'profile seconds'
        elif reference is not None:
            costs, audit = reference_profile(durations, census)
            report['profile'] = {'source': 'serial reference', 'path': str(reference_path),
                                 'reference_sha256': reference_hash,
                                 'sha256_format': 'canonical test_id,seconds CSV', **audit}
            weight_source = 'serial reference seconds'
        if shard is not None:
            keys = shard_unit_keys(census, shard_count, costs)[index - 1]
            report['shard'] = {'index': index, 'count': shard_count,
                               'population_ids': census['ids'],
                               'population_digest': census['population_digest']}
            request['shard_units'] = keys
            write_json(output / 'request.json', request)
            census = shard_census(census, keys)
            if costs is not None:
                costs = {key: costs[key] for key in keys}
        count = sum(unit['serial_reason'] is None for unit in census['units'])
        effective_workers = workers if workers is not None else min(8, os.cpu_count() or 1, max(1, count))
        plan = make_plan(census, effective_workers, weights=costs)
        validate_plan(census, plan)
        report.update(census=census, plan=plan, workers=effective_workers,
                      weights=weight_source)
        if costs is not None:
            report['estimates'] = plan_estimates(plan, costs)
        write_json(output / 'plan.json', plan)
        report['phase_seconds']['discovery_and_plan'] = time.monotonic() - phase_start
        cancellation.check()
        if mode == 'plan':
            report.update(status='planned', successful=True)
        else:
            for phase in ('parallel', 'serial'):
                phase_start = time.monotonic()
                # Re-prove immediately before launch; serial begins only after all joins.
                validate_plan(census, plan)
                scheduled = [children.spawn(item['key']) for item in plan['assignments'] if item['phase'] == phase]
                codes = {child['key']: children.join(child) for child in scheduled}
                for child in scheduled:
                    result = read_worker_result(output, child['key'])
                    report['results'].append(result)
                    if result.get('source') != report['source'] or result.get('interpreter') != report['interpreter']:
                        raise ValueError(child['key'] + ': source or interpreter drift')
                    if result['status'] == 'interrupted':
                        raise KeyboardInterrupt()
                    expected_code = 0 if result['successful'] else 1
                    if codes[child['key']] != expected_code:
                        raise ValueError(child['key'] + ': process status mismatch')
                if children.cleanup_errors:
                    raise ValueError('child cleanup failed')
                report['phase_seconds'][phase] = time.monotonic() - phase_start
            phase_start = time.monotonic()
            cancellation.check()
            aggregate = reconcile(census, plan, report['results'])
            report.update(aggregate=aggregate, status='complete', successful=aggregate['successful'])
            if reference_aggregate is not None:
                comparison = compare_results(reference_aggregate, aggregate)
                report['comparison'] = comparison
                if not comparison['equivalent']:
                    raise ValueError('serial reference outcomes differ')
                report['qualified'] = aggregate['successful']
            report['phase_seconds']['reconciliation'] = time.monotonic() - phase_start
    except KeyboardInterrupt as exc:
        number = exc.number if isinstance(exc, Cancelled) else signal.SIGINT
        cancellation.record(number, None)
        report.update(status='interrupted', successful=False, qualified=False,
                      interruption=signal.Signals(number).name,
                      error=signal.Signals(number).name, exit_code=128 + number)
    except Exception as exc:
        report.update(status='invalid', successful=False, qualified=False,
                      error=type(exc).__name__ + ': ' + str(exc))
    finally:
        phase_start = time.monotonic()
        errors = []
        # First and repeated signals record intent without interrupting cleanup.
        with cancellation_handlers(cancellation.record):
            if children is not None:
                try:
                    report['children'] = children.close()
                    errors.extend(children.cleanup_errors)
                except Exception as exc:
                    errors.append('child cleanup: ' + str(exc))
            if 'source' in report:
                try:
                    if source_identity(root) != report['source']:
                        errors.append('source drift at final boundary')
                except Exception as exc:
                    errors.append('source verification: ' + str(exc))
        with final_commitment(cancellation):
            if cancellation.number is not None:
                number = cancellation.number
                report.update(status='interrupted', successful=False, qualified=False,
                              interruption=signal.Signals(number).name,
                              error=signal.Signals(number).name, exit_code=128 + number)
            report['final_commitment'] = {
                'begun': True,
                'cancellation_boundary': 'masked pending-signal snapshot before scratch removal and receipt publication',
            }
            if scratch is not None and not errors:
                try:
                    # Children.close() has reaped every direct child and
                    # refused any observed survivor. Incomplete test receipts
                    # still fail the run, but do not invalidate this cleanup.
                    cleanup_path(scratch)
                except Exception as exc:
                    errors.append('scratch cleanup: ' + str(exc))
            if scratch is not None and errors:
                # Ownership or teardown refusals must also fence the shared
                # atexit finalizer from removing a still-unsafe scratch tree.
                retain_path(scratch)
            report['cleanup'] = {'successful': not errors, 'errors': errors,
                                 'ownership': children.tree.receipt() if children is not None else None,
                                 'scratch_retained': scratch is not None and scratch.exists()}
            if errors:
                report.update(status='interrupted' if report['status'] == 'interrupted' else 'invalid',
                              successful=False, qualified=False,
                              error='; '.join(errors + ([report['error']] if 'error' in report else [])))
            if owned_output:
                try:
                    write_process_cost(output, report, children.tree if children is not None else None)
                except Exception as exc:
                    try:
                        print('process-cost artifact unavailable: ' + str(exc), file=sys.stderr)
                    except Exception:
                        # Neither informational sink may replace the native receipt.
                        pass
            report['phase_seconds']['cleanup_and_final_source'] = time.monotonic() - phase_start
            load_end = sample_load_average()
            report['load_average']['end'] = load_end
            # Compatibility projection for existing test-cost gate readers.
            report['load_after'] = load_end if isinstance(load_end, list) else None
            report['wall_seconds'] = time.monotonic() - start
            if report['qualified'] and report['wall_seconds'] > 600:
                report['qualified'] = False
            if owned_output:
                try:
                    report['final_path'] = str(output / 'final.json')
                    write_json(output / 'final.json', report)
                except Exception as exc:
                    report.pop('final_path', None)
                    report.update(status='invalid', successful=False, qualified=False,
                                  error='final receipt persistence: ' + str(exc))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('plan', 'serial', 'run', '_child'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--workers', type=int)
    parser.add_argument('--reference', type=Path,
                        help='matching serial receipt; supplies estimated costs unless --profile is set')
    parser.add_argument('--profile', type=Path,
                        help='override the committed platform timing CSV for estimated unit costs')
    parser.add_argument('--shard-index', type=int, help='one-based VM shard index')
    parser.add_argument('--shard-count', type=int, help='number of independent VM shards')
    parser.add_argument('--key', choices=('discovery', 'serial', *(f'worker-{i}' for i in range(1, worker_limit() + 1))), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (args.shard_index is None) != (args.shard_count is None):
        parser.error('--shard-index and --shard-count must be given together')
    if args.mode == '_child':
        try:
            return child_main(Path(__file__).resolve().parent.parent, args.output, args.key)
        except BaseException:
            traceback.print_exc()
            return 2
    shard = None if args.shard_index is None else (args.shard_index, args.shard_count)
    report = launch(args.mode, args.output, args.workers, args.reference, args.profile, shard=shard)
    summary = {key: report[key] for key in (
        'mode', 'status', 'successful', 'qualified', 'wall_seconds', 'error',
        'interruption', 'final_path', 'estimates') if key in report}
    summary['cleanup'] = {key: value for key, value in report['cleanup'].items()
                          if key != 'ownership'}
    summary['counts'] = {'population': len(report.get('census', {}).get('ids', [])),
                         'results': len(report['results']),
                         'tests_run': report.get('aggregate', {}).get('tests_run')}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if report['status'] == 'interrupted':
        return report['exit_code']
    return 0 if report['successful'] else 1


if __name__ == '__main__':
    sys.exit(main())
