"""Explicit installed upgrade measurement. Run the native and bulk methods separately.

The native arm authors every historical envelope with the previous installed CLI.
The bulk arm measures a different case: old-reader-validated, fixture-authored
one-envelope frames. Only install, update and the measured sends are user actions
in both arms. File inventories and ledger verification are outside timed sends.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1]
# A pre-index Train 70 revision from the period of the study's quiet reference.
BEFORE_INDEX = '1bbd97a0b03fcaabada571eadb6922479e327378'
RELEASE_BASE = '65d800341c32a55fccec307ba352cae5f7c8e44e'
COMPACT_TRAIN = '94364cde1eb6bee1ce60e88241bc4b44b5e164b4'


def send_args(root, sha, key):
    return ['send', '--root', str(root), '--from', 'author', '--to', 'reader',
            '--repo', 'history', '--sha', sha, '--doc', 'note.md', '--note', 'History evidence',
            '--idempotency-key', key]


def seed_installed(destination, root, sha, count, mode):
    # A single isolated process amortizes startup in setup only. Every native
    # send still executes the installed CLI's Git proof and native transaction.
    sys.path.insert(0, str(destination))
    from floati import cli
    from floati.events import EVENT_KINDS, EventLog
    from floati.framing import encode_frame
    from floati.ids import uuid7_hex
    from floati.records import validate_record
    from floati.root import FloatiRoot
    if Path(cli.__file__).resolve() != destination / 'floati/cli.py':
        raise RuntimeError('previous installed CLI was not imported')
    opened = FloatiRoot.open_direct_home(root, create=False)
    def send(key):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(send_args(root, sha, key))
        artifact = json.loads(output.getvalue())
        if code or artifact['status'] != 'ok':
            raise ValueError(artifact)
        return artifact['evidence']['message']
    if mode == 'native':
        for ordinal in range(count):
            message = send('history-' + str(ordinal))
            if message['idempotency_key'] != 'history-' + str(ordinal):
                raise ValueError('native setup send lost its identity')
    else:
        template = send('bulk-template')
        with (root / 'events.jsonl').open('wb') as stream:
            for ordinal in range(count):
                row = dict(template, id='msg-' + uuid7_hex(), idempotency_key='bulk-' + str(ordinal))
                stream.write(encode_frame(validate_record(row, opened.tenant_id, EVENT_KINDS, integrity=False)))
    rows = EventLog(opened).strict_event_records()
    expected = 2 * count if mode == 'native' else count
    if len(rows) != expected or sum(row['kind'] == 'message_envelope' for row in rows) != count:
        raise ValueError('previous installed reader rejected the expected history population')
    print(json.dumps({'status': 'ok', 'evidence': {'events': len(rows), 'envelopes': count}}))


def observed(argv, *, cwd, environment):
    """Collect the launched process before reading its artifact. I/O is process-wide."""
    start_load = os.getloadavg()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        begin = time.monotonic()
        with subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                              stdout=output, stderr=errors) as child:
            counters = None
            if sys.platform.startswith('linux'):
                os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
                end = time.monotonic()
                try:
                    lines = Path('/proc', str(child.pid), 'io').read_text().splitlines()
                except PermissionError:
                    counters = {'unavailable': 'proc_io_permission_denied'}
                else:
                    counters = {key: int(value) for key, value in (line.split(':') for line in lines)}
                child.wait()
            else:
                child.wait()
                end = time.monotonic()
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        output.seek(0)
        errors.seek(0)
        raw = output.read()
        diagnostic = errors.read()
        if child.returncode:
            raise RuntimeError(f'{argv[0]} exited {child.returncode}: {raw[-1500:]!r} {diagnostic[-1500:]!r}')
    artifact = json.loads(raw)
    if artifact['status'] != 'ok':
        raise RuntimeError(artifact)
    return artifact['evidence'], {'wall_seconds': end - begin,
        'child_cpu_seconds': (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime),
        'load_before': start_load, 'load_after': os.getloadavg(), 'linux_process_io': counters,
        'stderr_bytes': len(diagnostic), 'stderr_tail': diagnostic[-1024:].decode(errors='replace')}


def bank_state(root):
    bank = root / '.send-prefix'
    if not bank.exists():
        return None
    payload = json.loads((bank / 'checkpoint.json').read_bytes())['payload']
    nodes = [path.stat().st_size for path in (bank / 'nodes').iterdir()]
    compact = bank / 'bank.bin'
    return {'generation': payload['generation'], 'frames': payload['count'], 'offset': payload['offset'],
            'node_files': len(nodes), 'node_bytes': sum(nodes),
            'compact_bank_bytes': compact.stat().st_size if compact.exists() else None}


def report(phase, **observation):
    print('COLD_UPGRADE ' + json.dumps(dict(phase=phase, **observation), sort_keys=True), flush=True)


class InstalledColdUpgradeSendTests(unittest.TestCase):
    def measure(self, mode, counts, *, target_ref='HEAD', shared_history=None, send_repository=None):
        from tests.banked_sha_fixture import banked_send_repository
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            base = Path(raw).resolve()
            env = dict(os.environ, HOME=str(base), PATH='/usr/bin:/bin',
                       FLOATI_PYTHON=str(Path(sys.executable).resolve()), PYTHONDONTWRITEBYTECODE='1')
            source = base / 'source'
            installed = base / 'installed'
            launcher = installed / 'scripts/floati'
            def git(cwd, *args):
                return subprocess.check_output(['/usr/bin/git', *args], cwd=cwd, env=env, text=True).strip()
            target = git(SOURCE, 'rev-parse', '--verify', target_ref + '^{commit}')
            git(base, 'clone', '--quiet', '--shared', '--no-checkout', str(SOURCE), str(source))
            git(source, 'checkout', '--quiet', '--detach', BEFORE_INDEX)
            workspace, sha = send_repository or banked_send_repository(base)
            def run(executable, *args):
                return observed([str(executable), *map(str, args)], cwd=workspace, environment=env)
            receipt, timing = run(source / 'scripts/floati', 'install', '--source', source,
                                  '--destination', installed, '--ref', BEFORE_INDEX, '--json')
            self.assertEqual(BEFORE_INDEX, receipt['source_sha'])
            self.assertEqual('installed', receipt['status'])
            self.assertFalse(launcher.is_symlink())
            self.assertEqual(launcher.read_bytes(), (source / 'scripts/floati').read_bytes())
            report('install', mode=mode, previous=BEFORE_INDEX, target=target, **timing)
            histories = []
            for count in counts:
                root = base / ('fleet-' + str(count))
                saved = shared_history / root.name if shared_history is not None else None
                if saved is not None and saved.exists():
                    shutil.copytree(saved, root)
                    seed = {'events': count, 'envelopes': count}
                    timing = {'source': 'restored_validated_history'}
                else:
                    run(launcher, 'init', '--root', root)
                    for node in ('author', 'reader'):
                        run(launcher, 'register', '--root', root, node, '--harness', 'Codex')
                    seed, timing = run(env['FLOATI_PYTHON'], '-I', '-B', __file__, 'seed', installed, root, sha, count, mode)
                    if saved is not None:
                        shutil.copytree(root, saved)
                data = (root / 'events.jsonl').read_bytes()
                if saved is not None:
                    self.assertEqual((saved / 'events.jsonl').read_bytes(), data)
                self.assertEqual(seed['events'], data.count(b'\n'))
                self.assertIsNone(bank_state(root))
                histories.append((root, seed, hashlib.sha256(data).hexdigest()))
                report('history', mode=mode, target=target, **seed, ledger_bytes=len(data), sha256=histories[-1][2], **timing)
            git(source, 'checkout', '--quiet', '--detach', target)
            receipt, timing = run(launcher, 'update', '--source', source, '--destination', installed,
                                  '--ref', target, '--json')
            self.assertEqual(target, receipt['source_sha'])
            self.assertEqual('updated', receipt['status'])
            self.assertFalse(launcher.is_symlink())
            self.assertEqual(launcher.read_bytes(), (source / 'scripts/floati').read_bytes())
            report('update', mode=mode, target=target, **timing)
            for root, seed, digest in histories:
                ledger = root / 'events.jsonl'
                prior = ledger.read_bytes()
                self.assertEqual(digest, hashlib.sha256(prior).hexdigest())
                self.assertIsNone(bank_state(root))
                previous_bank = None
                for phase in ('cold', 'warm'):
                    receipt, timing = run(launcher, *send_args(root, sha, 'after-upgrade-' + phase))
                    current = ledger.read_bytes()
                    self.assertTrue(current.startswith(prior))
                    message, readiness = map(json.loads, current[len(prior):].splitlines())
                    self.assertEqual(receipt['message'], message)
                    self.assertEqual(('send_readiness_observation', message['id']),
                                     (readiness['kind'], readiness['envelope_id']))
                    bank = bank_state(root)
                    self.assertEqual(current.count(b'\n'), bank['frames'])
                    self.assertEqual(len(current), bank['offset'])
                    if previous_bank is not None:
                        self.assertEqual(previous_bank['generation'], bank['generation'])
                    report(phase, mode=mode, target=target, history_events=seed['events'],
                           before_events=prior.count(b'\n'), before_bytes=len(prior), appended_bytes=len(current)-len(prior),
                           bank_before=previous_bank, bank_after=bank, **timing)
                    previous_bank, prior = bank, current

    def test_native_send_history(self):
        self.measure('native', (32, 320))

    def test_bulk_validated_history(self):
        self.measure('bulk', (9998, 99980))

    def test_release_base_and_compact_train_on_identical_histories(self):
        from tests.banked_sha_fixture import banked_send_repository
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            shared = Path(raw)
            repository = banked_send_repository(shared)
            for target in (RELEASE_BASE, COMPACT_TRAIN):
                with self.subTest(target=target):
                    self.measure('bulk', (9998, 99980), target_ref=target,
                                 shared_history=shared, send_repository=repository)


if __name__ == '__main__' and sys.argv[1:2] == ['seed']:
    seed_installed(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], int(sys.argv[5]), sys.argv[6])
