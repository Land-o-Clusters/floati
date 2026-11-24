"""The parallel launcher must not qualify different or incomplete source."""

from __future__ import annotations

from tests.readiness import Readiness, wait_until

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.temp_roots import REAL_TEMP_ROOT


class ParallelSourceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self._git("init", "-q")
        (self.root / "subject.py").write_text("VALUE = 1\n")
        (self.root / "drafts").mkdir()
        (self.root / "drafts" / "case.py").write_text("VALUE = 2\n")
        self._git("add", ".")
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                  "commit", "-qm", "fixture")

    def _git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["/usr/bin/git", *arguments], cwd=self.root, text=True,
            stderr=subprocess.STDOUT,
        ).strip()

    def _launcher(self):
        source = Path(__file__).parents[1] / "scripts" / "test-parallel.py"
        self.assertTrue(source.is_file(), "parallel launcher has not been implemented")
        spec = importlib.util.spec_from_file_location("parallel_launcher_control", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_source_changes_in_drafts_change_the_fingerprint(self) -> None:
        launcher = self._launcher()
        before = launcher.source_identity(self.root)
        (self.root / "drafts" / "case.py").write_text("VALUE = 3\n")
        after = launcher.source_identity(self.root)
        self.assertEqual(before["head"], after["head"])
        self.assertNotEqual(before["digest"], after["digest"])

    def test_untracked_discoverable_python_is_refused(self) -> None:
        launcher = self._launcher()
        (self.root / "test_extra.py").write_text("import unittest\n")
        with self.assertRaisesRegex(ValueError, "untracked_python"):
            launcher.source_identity(self.root)

    def test_deleted_tracked_input_is_refused(self) -> None:
        launcher = self._launcher()
        (self.root / "subject.py").unlink()
        with self.assertRaisesRegex(ValueError, "source_missing"):
            launcher.source_identity(self.root)

    def test_absent_work_parent_is_allowed_but_symlink_is_refused(self) -> None:
        launcher = self._launcher()
        target = self.root / "work"
        launcher.check_work_parent(target)
        target.mkdir(mode=0o700)
        launcher.check_work_parent(target)
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "work_parent_not_ordinary"):
            launcher.check_work_parent(alias)

    def test_parent_preflight_leaves_existing_mode_policy_to_the_adapter(self) -> None:
        launcher = self._launcher()
        target = self.root / "work"
        target.mkdir()
        target.chmod(0o755)
        try:
            launcher.check_work_parent(target)
        except ValueError as error:
            self.fail("read-only preflight added an adapter mode refusal: " + str(error))
        self.assertEqual(0o755, target.stat().st_mode & 0o777)


class ParallelLauncherIntegrationTests(unittest.TestCase):
    """Tiny committed repositories exercise the production CLI and recorder."""

    def setUp(self):
        import shutil
        self.scratch = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.scratch.cleanup)
        self.bank = Path(self.scratch.name)
        self.root = self.bank / 'repo'
        self.root.mkdir()
        scripts = self.root / 'scripts'
        scripts.mkdir()
        original = Path(__file__).parents[1] / 'scripts'
        for name in ('test-parallel.py', 'parallel_plan.py', 'parallel_unittest.py',
                     'parallel_isolation.py', 'parallel_processes.py', 'parallel_profile.py',
                     'fixture_finalizer.py'):
            shutil.copyfile(original / name, scripts / name)
        (scripts / 'parallel_serial_classes.json').write_text('{}')
        (self.root / 'subject.py').write_text('VALUE = 1\n')

    def fixture(self, text, rules=None):
        import json
        (self.root / 'test_cases.py').write_text(text)
        if rules:
            (self.root / 'scripts/parallel_serial_classes.json').write_text(json.dumps(rules))
        for args in [('init', '-q'), ('add', '.'),
                     ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                      'commit', '-qm', 'fixture')]:
            subprocess.run(['/usr/bin/git', *args], cwd=self.root, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def command(self, mode='run', output='out', extra=()):
        import sys
        return [sys.executable, str(self.root / 'scripts/test-parallel.py'), mode,
                '--output', str(self.bank / output), '--workers', '1', *extra]

    def run_cli(self, mode='run', output='out', extra=()):
        import json
        import shutil
        proc = subprocess.run(self.command(mode, output, extra), cwd=self.bank,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=30)
        self.assertTrue(proc.stdout.strip(), 'launcher must publish final JSON: ' + proc.stderr)
        summary = json.loads(proc.stdout.strip().splitlines()[-1])
        result = (json.loads(Path(summary['final_path']).read_text())
                  if summary.get('final_path') else summary)
        if result.get('scratch_root'):
            self.addCleanup(shutil.rmtree, result['scratch_root'], True)
        return proc, result

    def launcher(self):
        spec = importlib.util.spec_from_file_location('launcher_integration', self.root / 'scripts/test-parallel.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_plan_derives_population_without_executing(self):
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self):\n  raise RuntimeError("must not run")\n')
        proc, result = self.run_cli('plan')
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual('planned', result['status'])
        self.assertEqual(['test_cases.Case.test_one'], result['census']['ids'])
        self.assertTrue(result['cleanup']['successful'])
        self.assertFalse(Path(result['scratch_root']).exists())

    def test_final_receipt_records_own_load_at_both_suite_boundaries(self):
        import json
        from unittest.mock import patch
        from scripts.parallel_profile import completed_durations
        marker = self.bank / 'executed'
        self.fixture('import unittest\nfrom pathlib import Path\nclass Case(unittest.TestCase):\n'
                     ' def test_one(self): Path(%r).touch()\n' % str(marker))
        launcher = self.launcher()

        def sample():
            # A sample taken twice at either boundary cannot pass this control.
            return (4.0, 5.0, 6.0) if marker.exists() else (1.0, 2.0, 3.0)

        with patch.object(launcher.os, 'getloadavg', side_effect=sample):
            result = launcher.launch('serial', self.bank / 'out', 1)
        self.assertEqual('complete', result['status'], result.get('error'))
        persisted = json.loads((self.bank / 'out/final.json').read_text())
        self.assertEqual({'start': [1.0, 2.0, 3.0], 'end': [4.0, 5.0, 6.0]},
                         persisted.get('load_average'))
        self.assertEqual([1.0, 2.0, 3.0], persisted.get('load_before'))
        self.assertEqual([4.0, 5.0, 6.0], persisted.get('load_after'))
        expected_ids = ['test_cases.Case.test_one']
        self.assertEqual(expected_ids, persisted['census']['ids'])
        self.assertEqual(expected_ids, persisted['results'][0]['started_ids'])
        # Readers admit new evidence and historical receipts lacking the fields.
        old = {key: value for key, value in persisted.items()
               if key not in ('load_average', 'load_before', 'load_after')}
        for receipt in (persisted, old):
            with self.subTest(has_load='load_average' in receipt):
                self.assertEqual(expected_ids, list(completed_durations(receipt)))
                path = self.bank / 'out/reader-control.json'
                path.write_text(json.dumps(receipt))
                aggregate, durations, _ = launcher.reference_result(
                    path, receipt['source'], receipt['interpreter'])
                self.assertTrue(aggregate['successful'])
                self.assertEqual(expected_ids, list(durations))

    def test_load_failure_is_typed_independently_without_replacing_native_result(self):
        import json
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        launcher = self.launcher()
        absent = {'status': 'unavailable', 'code': 'getloadavg_failed',
                  'detail': 'controlled load failure'}
        for failed in ('start', 'end', 'both'):
            with self.subTest(failed=failed):
                samples = [OSError('controlled load failure') if failed in (endpoint, 'both')
                           else (1.0, 2.0, 3.0) for endpoint in ('start', 'end')]
                output = self.bank / failed
                # Exercise every final receipt, including early invalid runs,
                # without launching three redundant native populations.
                with patch.object(launcher.os, 'getloadavg', side_effect=samples),\
                        patch.object(launcher, 'source_identity', side_effect=ValueError('source refused')):
                    result = launcher.launch('run', output, 1)
                persisted = json.loads((output / 'final.json').read_text())
                self.assertEqual('invalid', result['status'])
                self.assertEqual('ValueError: source refused', result['error'])
                self.assertEqual({endpoint: absent if failed in (endpoint, 'both')
                                  else [1.0, 2.0, 3.0] for endpoint in ('start', 'end')},
                                 persisted.get('load_average'))
                for endpoint, alias in (('start', 'load_before'), ('end', 'load_after')):
                    self.assertEqual(None if failed in (endpoint, 'both') else [1.0, 2.0, 3.0],
                                     persisted.get(alias))

    def test_profile_balances_existing_units_and_retains_estimate_evidence(self):
        import hashlib
        self.fixture('''import unittest
from pathlib import Path
class A(unittest.TestCase):
 def test_a(self): Path('executed').touch()
class B(unittest.TestCase):
 def test_b(self): Path('executed').touch()
class C(unittest.TestCase):
 def test_c(self): Path('executed').touch()
class Tail(unittest.TestCase):
 def test_tail(self): Path('executed').touch()
''', {'test_cases.Tail': 'shared resource'})
        profile = self.bank / 'timings.csv'
        data = (b'test_id,seconds\ntest_cases.A.test_a,1\n'
                b'test_cases.B.test_b,20\ntest_cases.C.test_c,1\n'
                b'test_cases.Tail.test_tail,4\n')
        profile.write_bytes(data)
        plain, baseline = self.run_cli('plan', 'plain', extra=('--workers', '2'))
        self.assertEqual(0, plain.returncode, plain.stderr)
        weighted, planned = self.run_cli('plan', 'weighted', extra=(
            '--workers', '2', '--profile', str(profile)))
        self.assertEqual(0, weighted.returncode, weighted.stderr)
        import json
        self.assertEqual(planned['estimates'], json.loads(weighted.stdout)['estimates'])
        self.assertFalse((self.root / 'executed').exists())
        proc, result = self.run_cli(extra=('--workers', '2', '--profile', str(profile)))
        self.assertEqual(0, proc.returncode, proc.stderr + str(result))
        self.assertEqual(baseline['census'], planned['census'])
        self.assertEqual(planned['census'], result['census'])
        self.assertNotEqual(baseline['plan'], planned['plan'])
        self.assertEqual(planned['plan'], result['plan'])
        self.assertEqual([
            ['test_cases.B.test_b'],
            ['test_cases.A.test_a', 'test_cases.C.test_c'],
            ['test_cases.Tail.test_tail'],
        ], [item['ids'] for item in result['plan']['assignments']])
        self.assertEqual(4, result['aggregate']['tests_run'])
        self.assertEqual(str(profile), result['profile']['path'])
        self.assertEqual(hashlib.sha256(data).hexdigest(), result['profile']['sha256'])
        self.assertEqual(4, result['profile']['matched_ids'])
        self.assertEqual([], result['profile']['unmatched_population_ids'])
        self.assertTrue(result['profile']['provenance'].startswith('ESTIMATE'))
        self.assertEqual({
            'provenance': 'ESTIMATE',
            'assignment_seconds': {'worker-1': 20.0, 'worker-2': 2.0, 'serial': 4.0},
            'serial_tail_seconds': 4.0,
            'max_parallel_seconds': 20.0,
            'mean_parallel_seconds': 11.0,
            'parallel_max_mean_ratio': 20 / 11,
        }, result['estimates'])

    def test_completed_run_profile_refresh_is_checked_and_digest_guarded(self):
        import hashlib
        import json
        import sys
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, reference = self.run_cli()
        self.assertEqual(0, proc.returncode, proc.stderr)
        output = self.bank / 'refreshed.csv'
        command = [sys.executable, str(self.root / 'scripts/parallel_profile.py'),
                   '--run', str(self.bank / 'out'), '--output', str(output)]

        def refresh(*args):
            result = subprocess.run(command + list(args), cwd=self.root, text=True,
                                    capture_output=True, timeout=10)
            self.assertTrue(result.stdout.strip(), result.stderr)
            return result, json.loads(result.stdout)

        checked, audit = refresh()
        self.assertEqual(1, checked.returncode, checked.stdout)
        self.assertFalse(output.exists())
        self.assertEqual(reference['census']['population_digest'], audit['population_digest'])
        refused, _ = refresh('--write')
        self.assertEqual(2, refused.returncode)
        self.assertFalse(output.exists())
        refused, _ = refresh('--write', '--expected-sha256', '0' * 64)
        self.assertEqual(2, refused.returncode)
        self.assertFalse(output.exists())
        written, proof = refresh('--write', '--expected-sha256', audit['sha256'])
        self.assertEqual(0, written.returncode, written.stdout)
        self.assertEqual(audit['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(audit['sha256'], proof['sha256'])
        matched, _ = refresh('--check')
        self.assertEqual(0, matched.returncode)
        output.write_bytes(output.read_bytes().replace(b'\n', b'\r\n'))
        drift, _ = refresh()
        self.assertEqual(1, drift.returncode)
        repaired, _ = refresh('--write', '--expected-sha256', audit['sha256'])
        self.assertEqual(0, repaired.returncode)
        self.assertEqual(audit['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
        before = output.read_bytes()
        receipt = self.bank / 'out/worker-1.result.json'
        damaged = json.loads(receipt.read_text())
        damaged['test_durations'][reference['census']['ids'][0]] = -1
        receipt.write_text(json.dumps(damaged))
        refused, _ = refresh('--write', '--expected-sha256', audit['sha256'])
        self.assertEqual(2, refused.returncode)
        self.assertEqual(before, output.read_bytes())

    def test_malformed_profile_refuses_before_methods_execute(self):
        self.fixture('''import unittest
from pathlib import Path
class Case(unittest.TestCase):
 def test_never(self): Path('executed').touch()
''')
        profile = self.bank / 'timings.csv'
        profile.write_text('test_id,seconds\ntest_cases.Case.test_never,nan\n')
        proc, result = self.run_cli(extra=('--profile', str(profile)))
        self.assertNotEqual(0, proc.returncode)
        self.assertEqual('invalid', result['status'])
        self.assertIn('profile has invalid duration', result['error'])
        self.assertFalse((self.root / 'executed').exists())
        self.assertEqual(['discovery'], [child['key'] for child in result['children']])
        self.assertTrue(result['cleanup']['successful'])

    def test_profiled_serial_reference_keeps_order_and_needs_no_profile_in_workers(self):
        import json
        profile = self.bank / 'timings.csv'
        self.fixture('''import unittest
from pathlib import Path
class A(unittest.TestCase):
 def test_a(self):
  Path('order').write_text('a')
  Path(%r).unlink(missing_ok=True)
class B(unittest.TestCase):
 def test_b(self):
  with Path('order').open('a') as stream: stream.write('b')
''' % str(profile))
        data = b'test_id,seconds\ntest_cases.A.test_a,1\ntest_cases.B.test_b,20\n'
        profile.write_bytes(data)
        proc, reference = self.run_cli('serial', 'reference', extra=('--profile', str(profile)))
        self.assertEqual(0, proc.returncode, proc.stderr + str(reference))
        self.assertEqual('ab', (self.root / 'order').read_text())
        self.assertFalse(profile.exists())
        self.assertEqual(['test_cases.A.test_a', 'test_cases.B.test_b'],
                         reference['plan']['assignments'][0]['ids'])
        self.assertEqual(21.0, reference['estimates']['serial_tail_seconds'])
        self.assertEqual(0.0, reference['estimates']['max_parallel_seconds'])
        request = json.loads((self.bank / 'reference/request.json').read_text())
        self.assertEqual({'mode': 'serial', 'source': reference['source'],
                          'interpreter': reference['interpreter']}, request)
        profile.write_bytes(data)
        proc, result = self.run_cli(extra=(
            '--profile', str(profile), '--reference', str(self.bank / 'reference/final.json')))
        self.assertEqual(0, proc.returncode, proc.stderr + str(result))
        self.assertTrue(result['qualified'])
        self.assertTrue(result['comparison']['equivalent'])
        self.assertEqual(reference['source'], result['source'])

    def test_serial_reference_timings_balance_units_with_receipted_missing_fallback(self):
        import hashlib
        import statistics
        self.fixture('''import unittest, time
class A(unittest.TestCase):
 def test_a(self): pass
class B(unittest.TestCase):
 def test_b(self): time.sleep(.25)
class C(unittest.TestCase):
 def test_c(self): pass
class Tail(unittest.TestCase):
 @classmethod
 def setUpClass(cls): raise unittest.SkipTest('fixture unavailable')
 def test_tail(self): pass
''', {'test_cases.Tail': 'shared resource'})
        proc, reference = self.run_cli('serial', 'reference')
        self.assertEqual(0, proc.returncode, proc.stderr + str(reference))
        reference_path = self.bank / 'reference/final.json'
        durations = reference['results'][0]['test_durations']
        proc, result = self.run_cli(extra=(
            '--workers', '2', '--reference', str(reference_path)))
        self.assertEqual(0, proc.returncode, proc.stderr + str(result))
        self.assertTrue(result['qualified'])
        self.assertEqual(reference['census']['ids'], result['census']['ids'])
        self.assertEqual([
            ['test_cases.B.test_b'],
            ['test_cases.A.test_a', 'test_cases.C.test_c'],
            ['test_cases.Tail.test_tail'],
        ], [item['ids'] for item in result['plan']['assignments']])
        self.assertEqual('serial reference', result['profile']['source'])
        self.assertEqual(str(reference_path), result['profile']['path'])
        self.assertEqual(hashlib.sha256(reference_path.read_bytes()).hexdigest(),
                         result['profile']['reference_sha256'])
        self.assertEqual(3, result['profile']['matched_ids'])
        self.assertEqual(['test_cases.Tail.test_tail'], result['profile']['unmatched_population_ids'])
        self.assertEqual(statistics.median(durations.values()), result['profile']['fallback_seconds'])
        self.assertEqual(durations['test_cases.B.test_b'], result['estimates']['max_parallel_seconds'])
        self.assertEqual(statistics.median(durations.values()), result['estimates']['serial_tail_seconds'])
        self.assertEqual('ESTIMATE', result['estimates']['provenance'])
        profile = self.bank / 'explicit.csv'
        profile.write_text('test_id,seconds\ntest_cases.A.test_a,20\n'
                           'test_cases.B.test_b,1\ntest_cases.C.test_c,1\n'
                           'test_cases.Tail.test_tail,1\n')
        proc, explicit = self.run_cli(output='explicit', extra=(
            '--workers', '2', '--reference', str(reference_path), '--profile', str(profile)))
        self.assertEqual(0, proc.returncode, proc.stderr + str(explicit))
        self.assertEqual(['test_cases.A.test_a'], explicit['plan']['assignments'][0]['ids'])
        self.assertEqual(str(profile), explicit['profile']['path'])
        self.assertTrue(explicit['qualified'])

    def test_serial_reference_malformed_timings_refuse_before_execution(self):
        import json
        self.fixture('''import unittest
from pathlib import Path
class Case(unittest.TestCase):
 def test_one(self): Path('executed').touch()
''')
        proc, reference = self.run_cli('serial', 'reference')
        self.assertEqual(0, proc.returncode, proc.stderr)
        (self.root / 'executed').unlink()
        reference_path = self.bank / 'reference/final.json'
        receipt_path = self.bank / 'reference/serial.result.json'
        journal_path = self.bank / 'reference/serial.events.jsonl'
        journal = journal_path.read_text().splitlines()
        footer = json.loads(journal[-1])
        for index, durations in enumerate((
            {'test_cases.Case.test_one': '2'},
            {'test_cases.Case.test_one': True},
            {'test_cases.Case.test_one': float('nan')},
            {'test_cases.Case.test_one': -1},
            {'foreign.test': 1},
            [],
        )):
            with self.subTest(durations=durations):
                reference['results'][0]['test_durations'] = durations
                reference_path.write_text(json.dumps(reference))
                receipt_path.write_text(json.dumps(reference['results'][0]))
                footer['record']['result'] = reference['results'][0]
                journal_path.write_text('\n'.join(journal[:-1] + [json.dumps(footer)]) + '\n')
                proc, result = self.run_cli(output='malformed-' + str(index), extra=(
                    '--reference', str(reference_path)))
                self.assertNotEqual(0, proc.returncode)
                self.assertIn('reference test_durations', result['error'])
                self.assertFalse((self.root / 'executed').exists())
                self.assertEqual(['discovery'], [child['key'] for child in result['children']])

    def test_serial_reference_without_method_timings_receipts_assumed_costs(self):
        self.fixture('''import unittest
class Case(unittest.TestCase):
 @classmethod
 def setUpClass(cls): raise unittest.SkipTest('fixture unavailable')
 def test_one(self): pass
''')
        proc, reference = self.run_cli('serial', 'reference')
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual({}, reference['results'][0]['test_durations'])
        proc, result = self.run_cli(extra=(
            '--reference', str(self.bank / 'reference/final.json')))
        self.assertEqual(0, proc.returncode, proc.stderr + str(result))
        self.assertTrue(result['qualified'])
        self.assertIn('profile', result)
        self.assertEqual(0, result['profile']['matched_ids'])
        self.assertEqual(['test_cases.Case.test_one'], result['profile']['unmatched_population_ids'])
        self.assertEqual(1.0, result['profile']['fallback_seconds'])
        self.assertEqual('one second per test because no method durations were measured',
                         result['profile']['fallback_policy'])
        self.assertEqual({'worker-1': 1.0}, result['estimates']['assignment_seconds'])
        self.assertEqual('ESTIMATE', result['estimates']['provenance'])

    def test_parallel_join_precedes_serial_and_workers_have_distinct_homes(self):
        self.fixture('''import unittest, os, json, time, socket
from pathlib import Path
TRACE = Path(__file__).with_name('trace.jsonl')
def note(kind):
 with TRACE.open('a') as f:
  f.write(json.dumps([kind, time.monotonic(), os.environ['HOME'], os.environ['TMPDIR']]) + '\\n')
class First(unittest.TestCase):
 def test_one(self):
  endpoint = Path(os.environ['TMPDIR']) / 'pymp-xxxxxxxx/listener-xxxxxxxx'
  endpoint.parent.mkdir()
  with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
   listener.bind(str(endpoint))
  note('start1'); time.sleep(.05); note('stop1')
class Second(unittest.TestCase):
 def test_two(self):
  note('start2'); time.sleep(.05); note('stop2')
class Tail(unittest.TestCase):
 def test_tail(self): note('serial')
''', {'test_cases.Tail': 'shared trace assertion'})
        proc, result = self.run_cli(extra=('--workers', '2'))
        self.assertEqual(0, proc.returncode, proc.stderr + str(result))
        import json
        events = [json.loads(line) for line in (self.root / 'trace.jsonl').read_text().splitlines()]
        self.assertEqual('serial', events[-1][0])
        self.assertEqual(3, len({entry[2] for entry in events}))
        # AF_UNIX counts encoded bytes, including the trailing NUL, rather
        # than Python characters. Reserve the longest multiprocessing socket
        # suffix created under a worker TMPDIR, and prove a real bind fits.
        import os
        import sys
        suffix = 'pymp-xxxxxxxx/listener-xxxxxxxx'
        capacity = {'darwin': 104, 'linux': 108}[sys.platform]
        budget = capacity - 1 - len(os.fsencode('/' + suffix))
        self.assertTrue(all(len(os.fsencode(str(Path(entry[3]).resolve()))) <= budget
                            for entry in events), (budget, events))
        self.assertEqual(3, result['aggregate']['tests_run'])
        self.assertEqual(3, len(result['results']))
        self.assertFalse(result['qualified'])
        self.assertGreaterEqual(result['wall_seconds'], sum(result['phase_seconds'].values()))

    def test_crash_retains_started_event_without_claiming_success(self):
        self.fixture('import unittest, os\nclass Case(unittest.TestCase):\n def test_crash(self): os._exit(7)\n')
        proc, result = self.run_cli()
        self.assertNotEqual(0, proc.returncode)
        self.assertEqual('invalid', result['status'])
        self.assertTrue(result['cleanup']['successful'], result['cleanup'])
        self.assertFalse(Path(result['scratch_root']).exists())
        import json
        journal = [json.loads(line) for line in (self.bank / 'out/worker-1.events.jsonl').read_text().splitlines()]
        self.assertIn('start', [line['record']['kind'] for line in journal])
        self.assertNotIn('footer', [line['record']['kind'] for line in journal])

    def test_source_drift_is_authoritative_failure(self):
        self.fixture('import unittest\nfrom pathlib import Path\nclass Case(unittest.TestCase):\n def test_change(self): Path("subject.py").write_text("VALUE = 2\\n")\n')
        proc, result = self.run_cli()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn('source', result['error'])
        self.assertFalse(result['successful'])

    def test_fresh_whole_census_mismatch_refuses_before_execution(self):
        self.fixture('''import unittest, uuid
from pathlib import Path
class Case(unittest.TestCase):
 identity = str(uuid.uuid4())
 def id(self): return self.identity
 def test_never(self): Path('executed').touch()
''')
        proc, result = self.run_cli()
        self.assertNotEqual(0, proc.returncode)
        self.assertFalse((self.root / 'executed').exists())
        self.assertEqual('invalid', result['status'])

    def test_repeated_setup_cleanup_errors_reconcile_as_complete_failure(self):
        self.fixture('''import unittest
class Case(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  def cleanup(): raise RuntimeError('cleanup failed')
  cls.addClassCleanup(cleanup)
  raise RuntimeError('setup failed')
 def test_blocked(self): pass
''')
        proc, result = self.run_cli()
        self.assertEqual(1, proc.returncode)
        self.assertEqual('complete', result['status'])
        self.assertEqual(0, result['aggregate']['tests_run'])
        self.assertEqual(2, len(result['aggregate']['events']))
        self.assertEqual(['test_cases.Case.test_blocked'], result['aggregate']['fixture_blocked_ids'])

    def test_overridden_ids_use_actual_fixture_membership(self):
        self.fixture('''import unittest
class Error(unittest.TestCase):
 def id(self): return 'alien-error'
 @classmethod
 def setUpClass(cls): raise RuntimeError('blocked error')
 def test_blocked(self): pass
class Skip(unittest.TestCase):
 def id(self): return 'alien-skip'
 @classmethod
 def setUpClass(cls): raise unittest.SkipTest('blocked skip')
 def test_blocked(self): pass
''')
        proc, result = self.run_cli()
        self.assertEqual(1, proc.returncode)
        self.assertEqual('complete', result['status'])
        self.assertEqual({'alien-error', 'alien-skip'}, set(result['aggregate']['fixture_blocked_ids']))
        self.assertEqual([], result['aggregate']['started_ids'])

    def test_native_keyboard_interrupt_is_incomplete(self):
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_stop(self): raise KeyboardInterrupt()\n')
        proc, result = self.run_cli()
        self.assertNotEqual(0, proc.returncode)
        self.assertEqual('interrupted', result['status'])
        self.assertTrue(result['cleanup']['successful'], result['cleanup'])
        self.assertFalse(Path(result['scratch_root']).exists())

    def test_serial_reference_compares_same_source_and_interpreter(self):
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        serial, reference = self.run_cli('serial', 'reference')
        self.assertEqual(0, serial.returncode, serial.stderr)
        proc, result = self.run_cli(extra=('--reference', str(self.bank / 'reference/final.json')))
        self.assertEqual(0, proc.returncode, proc.stderr + str(result))
        self.assertTrue(result['qualified'])
        self.assertTrue(result['comparison']['equivalent'])
        self.assertEqual(reference['source'], result['source'])

    def test_missing_or_invalid_footer_is_refused(self):
        import json
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, result = self.run_cli()
        self.assertEqual(0, proc.returncode)
        journal = self.bank / 'out/worker-1.events.jsonl'
        lines = journal.read_text().splitlines()
        journal.write_text('\n'.join(lines[:-1]) + '\n')
        with self.assertRaisesRegex(ValueError, 'footer'):
            self.launcher().read_worker_result(self.bank / 'out', 'worker-1')
        damaged = json.loads(lines[-1]); damaged['sequence'] += 1
        journal.write_text('\n'.join(lines[:-1] + [json.dumps(damaged)]) + '\n')
        with self.assertRaisesRegex(ValueError, 'sequence'):
            self.launcher().read_worker_result(self.bank / 'out', 'worker-1')

    def test_existing_output_is_not_reused(self):
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        (self.bank / 'out').mkdir()
        proc, result = self.run_cli()
        self.assertNotEqual(0, proc.returncode)
        self.assertEqual('invalid', result['status'])

    def test_worker_count_is_bounded(self):
        runner = self.root / 'scripts/test-parallel.py'
        runner.write_text(runner.read_text().replace('import os\n',
                          "import os\nif __name__ == '__main__':\n    os.cpu_count = lambda: 10\n"))
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        for workers in (0, 11):
            with self.subTest(workers=workers):
                proc, result = self.run_cli(output=f'bound-{workers}', extra=('--workers', str(workers)))
                self.assertNotEqual(0, proc.returncode)
                self.assertEqual('invalid', result['status'])
                self.assertIn('parallel_workers_out_of_range', result['error'])
                self.assertIn('10', result['error'])
                self.assertFalse((self.bank / f'bound-{workers}').exists())

    def test_ten_workers_keep_profile_keys_scratch_and_cleanup_distinct(self):
        import json, os, sys
        from unittest import mock
        runner = self.root / 'scripts/test-parallel.py'
        runner.write_text(runner.read_text().replace('import os\n',
                          "import os\nif __name__ == '__main__':\n    os.cpu_count = lambda: 10\n"))
        case = '''
class Case{index}(unittest.TestCase):
 def test_one(self):
  temporary = Path(os.environ['TMPDIR'])
  endpoint = temporary / 'pymp-xxxxxxxx/listener-xxxxxxxx'
  endpoint.parent.mkdir()
  with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
   listener.bind(str(endpoint))
  Path(__file__).with_name('trace-{index}.json').write_text(json.dumps({{
   'home': os.environ['HOME'], 'tmp': str(temporary.resolve())}}))
'''
        self.fixture('import unittest, os, json, socket\nfrom pathlib import Path\n' +
                     ''.join(case.format(index=index) for index in range(10)))
        profile = self.bank / 'timings.csv'
        profile.write_text('test_id,seconds\n' + ''.join(
            f'test_cases.Case{index}.test_one,{index + 1}\n' for index in range(10)))
        proc, result = self.run_cli(extra=('--workers', '10', '--profile', str(profile)))
        self.assertEqual(0, proc.returncode, proc.stderr + str(result))
        keys = {f'worker-{index}' for index in range(1, 11)}
        self.assertEqual(10, result['workers'])
        self.assertEqual(keys, {item['key'] for item in result['plan']['assignments']})
        self.assertEqual(keys, {item['key'] for item in result['results']})
        self.assertEqual(keys | {'discovery'}, {item['key'] for item in result['children']})
        self.assertTrue(all(item['returncode'] == 0 for item in result['children']))
        self.assertEqual(10, result['aggregate']['tests_run'])
        self.assertEqual(keys, set(result['estimates']['assignment_seconds']))
        self.assertEqual(list(range(1, 11)), sorted(result['estimates']['assignment_seconds'].values()))
        traces = [json.loads((self.root / f'trace-{index}.json').read_text()) for index in range(10)]
        scratch = Path(result['scratch_root'])
        for field in ('home', 'tmp'):
            self.assertEqual(10, len({trace[field] for trace in traces}))
            self.assertTrue(all(scratch in Path(trace[field]).parents for trace in traces))
        self.assertTrue(all(len(os.fsencode(trace['tmp'])) <= result['worker_tmp_budget_bytes']
                            for trace in traces))
        self.assertTrue(result['cleanup']['successful'])
        self.assertFalse(scratch.exists())
        self.launcher().validate_ownership_receipt(result['cleanup']['ownership'], result['children'], sys.platform)
        with mock.patch('os.cpu_count', return_value=10):
            default = self.launcher().launch('plan', self.bank / 'default')
        self.assertEqual('planned', default['status'], default)
        self.assertEqual(8, default['workers'])
        self.assertTrue(default['cleanup']['successful'])

    def test_parent_interrupt_reaps_running_worker_and_preserves_journal(self):
        import json, os, signal, time
        self.fixture('''import unittest, time
from pathlib import Path
class Case(unittest.TestCase):
 def test_wait(self):
  Path('started').touch()
  time.sleep(60)
''')
        proc = subprocess.Popen(self.command(), cwd=self.bank, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_until(lambda: (self.root / 'started').exists() or proc.poll() is not None,
                'parallel worker started publication')
            self.assertTrue((self.root / 'started').exists(), 'worker exited before started publication')
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=10)
            result = json.loads((self.bank / 'out/final.json').read_text())
            import shutil
            self.addCleanup(shutil.rmtree, result['scratch_root'], True)
            self.assertEqual(130, proc.returncode, err)
            self.assertEqual('interrupted', result['status'])
            self.assertTrue(result['cleanup']['successful'], result['cleanup'])
            self.assertFalse(Path(result['scratch_root']).exists())
            for child in result['children']:
                with self.assertRaises(ProcessLookupError):
                    os.kill(child['pid'], 0)
            journal = (self.bank / 'out/worker-1.events.jsonl').read_text()
            self.assertIn('"kind": "start"', journal)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.communicate(timeout=10)

    def test_empty_receipt_refuses_success(self):
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, result = self.run_cli()
        self.assertEqual(0, proc.returncode, result)
        (self.bank / 'out/worker-1.result.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'receipt mismatch'):
            self.launcher().read_worker_result(self.bank / 'out', 'worker-1')

    def test_cleanup_failure_overrides_complete_success(self):
        import shutil
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        launcher = self.launcher()
        with patch.object(launcher, 'cleanup_path', side_effect=OSError('controlled cleanup refusal')):
            result = launcher.launch('run', self.bank / 'out', 1)
        try:
            self.assertEqual('invalid', result['status'])
            self.assertFalse(result['successful'])
            self.assertFalse(result['cleanup']['successful'])
            self.assertTrue(result['cleanup']['scratch_retained'])
            self.assertTrue(Path(result['scratch_root']).is_dir())
            self.assertIn('controlled cleanup refusal', result['error'])
            self.assertEqual(1, result['aggregate']['tests_run'])
        finally:
            shutil.rmtree(result['scratch_root'])

    def test_header_selection_must_match_receipt(self):
        import json
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, result = self.run_cli()
        self.assertEqual(0, proc.returncode)
        journal = self.bank / 'out/worker-1.events.jsonl'
        lines = journal.read_text().splitlines()
        first = json.loads(lines[0]); first['record']['selected_ids'] = ['wrong-id']
        journal.write_text('\n'.join([json.dumps(first)] + lines[1:]) + '\n')
        with self.assertRaisesRegex(ValueError, 'header'):
            self.launcher().read_worker_result(self.bank / 'out', 'worker-1')

    def test_serial_reference_rejects_worker_identity_drift(self):
        import json
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, result = self.run_cli('serial', 'reference')
        self.assertEqual(0, proc.returncode)
        result['results'][0]['source']['digest'] = 'other source'
        target = self.bank / 'reference/final.json'
        target.write_text(json.dumps(result))
        proc, result = self.run_cli(extra=('--reference', str(target)))
        self.assertNotEqual(0, proc.returncode)
        self.assertFalse(result['successful'])

    def test_damaged_plan_refuses_before_running_any_test(self):
        import json, os, site, sys
        self.fixture('''import unittest
from pathlib import Path
class Case(unittest.TestCase):
 def test_never(self): Path('executed').touch()
''')
        proc, result = self.run_cli('plan')
        self.assertEqual(0, proc.returncode)
        plan = result['plan']; plan['assignments'][0]['ids'] = []
        (self.bank / 'out/plan.json').write_text(json.dumps(plan))
        isolated = self.bank / 'child'; isolated.mkdir()
        env = self.launcher().make_worker_environment(isolated, os.environ, site.getusersitepackages())
        proc = subprocess.run([sys.executable, str(self.root / 'scripts/test-parallel.py'),
                               '_child', '--output', str(self.bank / 'out'), '--key', 'worker-1'],
                              cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn('empty assignment', proc.stderr)
        self.assertFalse((self.root / 'executed').exists())


    def test_serial_reference_missing_footer_cannot_qualify(self):
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, result = self.run_cli('serial', 'reference')
        self.assertEqual(0, proc.returncode)
        journal = self.bank / 'reference/serial.events.jsonl'
        lines = journal.read_text().splitlines()
        journal.write_text('\n'.join(lines[:-1]) + '\n')
        proc, result = self.run_cli(extra=('--reference', str(self.bank / 'reference/final.json')))
        self.assertNotEqual(0, proc.returncode)
        self.assertFalse(result['qualified'])

    def test_serial_reference_process_failure_cannot_qualify(self):
        import json
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, result = self.run_cli('serial', 'reference')
        self.assertEqual(0, proc.returncode)
        result['children'][-1]['returncode'] = 1
        target = self.bank / 'reference/final.json'
        target.write_text(json.dumps(result))
        proc, result = self.run_cli(extra=('--reference', str(target)))
        self.assertNotEqual(0, proc.returncode)
        self.assertFalse(result['qualified'])

    def test_parallel_receipt_with_serial_label_cannot_qualify(self):
        import json
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, result = self.run_cli('run', 'reference')
        self.assertEqual(0, proc.returncode)
        result['mode'] = 'serial'
        target = self.bank / 'reference/final.json'
        target.write_text(json.dumps(result))
        proc, result = self.run_cli(extra=('--reference', str(target)))
        self.assertNotEqual(0, proc.returncode)
        self.assertFalse(result['qualified'])

    def test_serial_reference_requires_matching_persisted_request(self):
        import json
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, result = self.run_cli('serial', 'reference')
        self.assertEqual(0, proc.returncode)
        request_path = self.bank / 'reference/request.json'
        request = json.loads(request_path.read_text())
        request['mode'] = 'run'
        request_path.write_text(json.dumps(request))
        proc, result = self.run_cli(extra=('--reference', str(self.bank / 'reference/final.json')))
        self.assertNotEqual(0, proc.returncode)
        self.assertFalse(result['qualified'])

    def test_sigint_during_constructor_adopts_and_reaps_child(self):
        import os, shutil, signal
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        launcher = self.launcher()
        constructor = launcher.subprocess.Popen.__init__
        spawned = []

        def interrupted(process, *args, **kwargs):
            constructor(process, *args, **kwargs)
            if kwargs.get('start_new_session'):
                spawned.append(process)
                os.kill(os.getpid(), signal.SIGINT)

        with patch.object(launcher.subprocess.Popen, '__init__', interrupted):
            result = launcher.launch('run', self.bank / 'out', 1)
        try:
            self.assertEqual('interrupted', result['status'])
            self.assertEqual([p.pid for p in spawned], [c['pid'] for c in result['children']])
            for process in spawned:
                self.assertIsNotNone(process.poll())
        finally:
            for process in spawned:
                if process.poll() is None:
                    process.send_signal(signal.SIGKILL)
                    process.wait(timeout=5)
            shutil.rmtree(result['scratch_root'], ignore_errors=True)

    def test_constructor_failure_after_spawn_still_reaps_child(self):
        import shutil, signal
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        launcher = self.launcher()
        constructor = launcher.subprocess.Popen.__init__
        spawned = []

        def failed(process, *args, **kwargs):
            constructor(process, *args, **kwargs)
            if kwargs.get('start_new_session'):
                spawned.append(process)
                raise OSError('controlled failure after child creation')

        with patch.object(launcher.subprocess.Popen, '__init__', failed):
            result = launcher.launch('run', self.bank / 'out', 1)
        try:
            self.assertEqual('invalid', result['status'])
            self.assertEqual([p.pid for p in spawned], [c['pid'] for c in result['children']])
            for process in spawned:
                self.assertIsNotNone(process.poll())
        finally:
            for process in spawned:
                if process.poll() is None:
                    process.send_signal(signal.SIGKILL)
                    process.wait(timeout=5)
            shutil.rmtree(result['scratch_root'], ignore_errors=True)

    def test_sigterm_persists_interruption_and_reaps_children(self):
        import json, os, shutil, signal, time
        self.fixture('''import unittest, time
from pathlib import Path
class Case(unittest.TestCase):
 def test_wait(self):
  Path('started').touch()
  time.sleep(180)
''')
        proc = subprocess.Popen(self.command(), cwd=self.bank, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_until(lambda: (self.root / 'started').exists() or proc.poll() is not None,
                'parallel worker started publication')
            self.assertTrue((self.root / 'started').exists(), 'worker exited before started publication')
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=15)
            receipt = self.bank / 'out/final.json'
            self.assertTrue(receipt.exists(), err)
            result = json.loads(receipt.read_text())
            self.addCleanup(shutil.rmtree, result['scratch_root'], True)
            self.assertEqual(143, proc.returncode, err)
            self.assertEqual('interrupted', result['status'])
            self.assertEqual('SIGTERM', result['interruption'])
            self.assertTrue(result['cleanup']['successful'], result['cleanup'])
            self.assertFalse(Path(result['scratch_root']).exists())
            for child in result['children']:
                with self.assertRaises(ProcessLookupError):
                    os.kill(child['pid'], 0)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.communicate(timeout=15)

    def test_crash_removes_scratch_after_owned_processes_are_reaped(self):
        import shutil
        self.fixture('import unittest, os\nclass Case(unittest.TestCase):\n def test_crash(self): os._exit(7)\n')
        proc, result = self.run_cli()
        try:
            self.assertNotEqual(0, proc.returncode)
            self.assertEqual('invalid', result['status'])
            self.assertTrue(result['cleanup']['successful'], result['cleanup'])
            self.assertFalse(Path(result['scratch_root']).exists())
            self.assertFalse(result['cleanup']['ownership']['exhaustive'])
        finally:
            if Path(result['scratch_root']).exists():
                shutil.rmtree(result['scratch_root'])

    def test_crashed_worker_cleans_observed_detached_child_and_scratch(self):
        import os, shutil
        self.fixture('''import unittest, os, subprocess, sys, time
from pathlib import Path
class Case(unittest.TestCase):
 def test_crash(self):
  child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], start_new_session=True)
  Path('descendant').write_text(str(child.pid))
  time.sleep(.25)
  os._exit(7)
''')
        proc, result = self.run_cli()
        pid = int((self.root / 'descendant').read_text())
        try:
            self.assertTrue(result['cleanup']['successful'], result['cleanup'])
            self.assertFalse(Path(result['scratch_root']).exists())
            retained = result['cleanup']['ownership']['observed']
            self.assertIn(pid, [item['pid'] for item in retained], result['cleanup'])
            self.assertTrue(all(item['birth'] for item in retained))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if Path(result['scratch_root']).exists():
                shutil.rmtree(result['scratch_root'])

    def test_interrupt_cleans_observed_setsid_child_and_scratch(self):
        import json, os, shutil, signal, time
        self.fixture('''import unittest, os, time
from pathlib import Path
class Case(unittest.TestCase):
 def test_wait(self):
  pid = os.fork()
  if pid == 0:
   os.setsid()
   Path('setsid-ready').touch()
   time.sleep(180)
   os._exit(0)
  Path('descendant').write_text(str(pid))
  time.sleep(180)
''')
        proc = subprocess.Popen(self.command(), cwd=self.bank, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = None
        try:
            wait_until(lambda: (self.root / 'descendant').exists() or proc.poll() is not None,
                'parallel worker descendant publication')
            self.assertTrue((self.root / 'descendant').exists(), 'worker exited before descendant publication')
            pid = int((self.root / 'descendant').read_text())
            journal = self.bank / 'out/processes.jsonl'
            def observed_setsid_child():
                if not (self.root / 'setsid-ready').exists() or not journal.exists():
                    return False
                rows = [json.loads(line) for line in journal.read_text().splitlines(keepends=True)
                        if line.endswith('\n')]
                return any(row['pid'] == pid for row in rows)
            wait_until(observed_setsid_child, 'launcher observation of owned setsid child')
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=10)
            result = json.loads((self.bank / 'out/final.json').read_text())
            self.assertEqual('interrupted', result['status'])
            self.assertTrue(result['cleanup']['successful'], result['cleanup'])
            self.assertFalse(Path(result['scratch_root']).exists())
            self.assertIn(pid, [item['pid'] for item in result['cleanup']['ownership']['observed']], result['cleanup'])
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.communicate(timeout=15)
            if result and Path(result['scratch_root']).exists():
                shutil.rmtree(result['scratch_root'])

    def test_child_cleanup_error_does_not_abandon_later_children(self):
        import signal, site
        from unittest.mock import patch
        self.fixture('import time\ntime.sleep(1)\n')
        launcher = self.launcher()
        output, scratch = self.bank / 'out', self.bank / 'scratch'
        output.mkdir(); scratch.mkdir()
        launcher.write_json(output / 'request.json', {
            'mode': 'run', 'source': launcher.source_identity(self.root),
            'interpreter': launcher.interpreter_identity()})
        owner = launcher.Children(self.root, output, scratch, site.getusersitepackages())
        first, second = owner.spawn('worker-1'), owner.spawn('worker-2')
        def refusal(*args, **kwargs):
            raise OSError('controlled first-child wait refusal')

        try:
            with patch.object(first['process'], 'wait', refusal):
                receipts = owner.close()
            self.assertEqual(2, len(receipts))
            self.assertIsNotNone(second['process'].poll())
            self.assertTrue(any('wait refusal' in error for error in owner.cleanup_errors))
        finally:
            for child in (first, second):
                proc = child['process']
                if proc.poll() is None:
                    proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=5)
                child['log'].close()

    def test_cli_summary_keeps_full_population_in_persisted_receipt(self):
        import json
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        proc, result = self.run_cli()
        self.assertEqual(0, proc.returncode, result)
        summary = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertNotIn('census', summary)
        self.assertNotIn('results', summary)
        self.assertLess(len(proc.stdout), 2000)
        self.assertEqual(str(self.bank / 'out/final.json'), summary['final_path'])
        retained = json.loads(Path(summary['final_path']).read_text())
        self.assertEqual(['test_cases.Case.test_one'], retained['census']['ids'])
        self.assertEqual(1, summary['counts']['tests_run'])

    def test_observation_parent_exit_discards_unproven_edge(self):
        import os
        from types import SimpleNamespace
        output = self.bank / 'observed'; output.mkdir()
        tree = self.launcher().ProcessTree(output)
        self.addCleanup(tree.journal.close)
        parent = {'pid': 100, 'birth': [1], 'ppid': 1, 'uid': os.getuid(), 'zombie': False}
        child = {'pid': 101, 'birth': [2], 'ppid': 100, 'uid': os.getuid(), 'zombie': False}
        tree.retain(parent, 'worker-1')
        snapshots = iter((parent, None))
        tree.query = SimpleNamespace(identity=lambda pid: next(snapshots) if pid == 100 else child,
                                     children=lambda pid: [101])
        tree.observe()
        self.assertEqual([100], [item['pid'] for item in tree.known.values()])
        # A vanished parent is a documented snapshot limit, not a read error.
        # Cleanup keeps this bounded-observation limit in its receipt.
        self.assertEqual([], tree.errors)
        self.assertNotIn(101, [row['pid'] for row in tree.receipt()['departed']])

    # HARBOR-OBSERVE-BOUND-1: a child enumeration that outlasts the real
    # 0.25s owned-observation bound must not become a cleanup error. The
    # enumerated child is still retained; remaining parents are retried.
    def test_slow_child_enumeration_under_real_bound_is_not_a_cleanup_error(self):
        import os, time
        from types import SimpleNamespace
        output = self.bank / 'observe-bound'; output.mkdir()
        tree = self.launcher().ProcessTree(output)
        self.addCleanup(tree.journal.close)
        parent = {'pid': 100, 'birth': [1], 'ppid': 1, 'uid': os.getuid(), 'zombie': False}
        child = {'pid': 101, 'birth': [2], 'ppid': 100, 'uid': os.getuid(), 'zombie': False}
        tree.retain(parent, 'worker-1')

        def identity(pid):
            return parent if pid == 100 else child if pid == 101 else None

        def children(pid):
            if pid == 100:
                time.sleep(.26)
                return [101]
            return []

        tree.query = SimpleNamespace(identity=identity, children=children)
        tree.observe()
        self.assertEqual([], tree.errors)
        self.assertEqual([100, 101], sorted(item['pid'] for item in tree.known.values()))
        self.assertNotIn('limited_observation', tree.receipt())

    # HARBOR-OBSERVE-BOUND-1: an enumeration that never finishes must
    # return inside a named sweep cap with a typed limited-observation
    # receipt, never an OSError cleanup error. The clock is the module
    # monotonic observe() uses. A safety stop keeps a missing cap from
    # retaining without bound.
    def test_unbounded_child_enumeration_returns_within_cap_as_limited_observation(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        output = self.bank / 'observe-cap'; output.mkdir()
        tree = self.launcher().ProcessTree(output)
        self.addCleanup(tree.journal.close)
        uid = os.getuid()
        parent = {'pid': 100, 'birth': [100], 'ppid': 1, 'uid': uid, 'zombie': False}
        tree.retain(parent, 'worker-1')
        nxt = {'n': 100, 'calls': 0}
        now = {'t': 0.0}
        module = tree.__class__.__init__.__globals__
        step = module.get('OBSERVATION_PASS_SECONDS', .25)

        def identity(pid):
            return {'pid': pid, 'birth': [pid],
                    'ppid': 1 if pid == 100 else pid - 1,
                    'uid': uid, 'zombie': False}

        def children(pid):
            now['t'] += step
            nxt['calls'] += 1
            if nxt['calls'] > 50:
                return []
            nxt['n'] += 1
            return [nxt['n']]

        tree.query = SimpleNamespace(identity=identity, children=children)
        with patch.object(module['time'], 'monotonic', lambda: now['t']):
            tree.observe()
        self.assertEqual([], tree.errors)
        record = tree.receipt()['limited_observation']
        self.assertEqual('owned observation time cap reached', record['reason'])
        self.assertGreater(record['remaining'], 0)
        self.assertLessEqual(nxt['calls'], 50)

    # HARBOR-PROCESS-ESRCH-1: an enumerated child may disappear before
    # identity is read. Exercise the real launcher verdict, not just errno.
    def test_vanished_enumerated_pid_is_departed_without_failing_the_run(self):
        import errno, shutil
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        launcher = self.launcher()
        query_type = launcher.ProcessTree.__init__.__globals__['ProcessQuery']
        identity, children = query_type.identity, query_type.children
        vanished = 100_000_001
        for index, failure in enumerate((
                OSError(errno.ESRCH, 'vanished during identity read'),
                FileNotFoundError(errno.ENOENT, 'vanished proc entry'))):
            with self.subTest(errno=failure.errno):
                state = {'enumerated': False}

                def enumerate_then_vanish(query, pid):
                    actual = children(query, pid)
                    if not state['enumerated']:
                        state['enumerated'] = True
                        return [vanished, *actual]
                    return actual

                def disappearing_identity(query, pid):
                    if pid == vanished:
                        raise failure
                    return identity(query, pid)

                with patch.object(query_type, 'children', enumerate_then_vanish),\
                        patch.object(query_type, 'identity', disappearing_identity):
                    result = launcher.launch('run', self.bank / ('gone-%d' % index), 1)
                self.addCleanup(shutil.rmtree, result['scratch_root'], True)
                self.assertTrue(state['enumerated'])
                self.assertEqual([], result['cleanup']['errors'], result)
                self.assertTrue(result['successful'], result)
                self.assertFalse(Path(result['scratch_root']).exists(), result)
                rows = [row for row in result['cleanup']['ownership']['departed']
                        if row['pid'] == vanished]
                self.assertEqual(1, len(rows), rows)
                self.assertIsNone(rows[0]['birth'])
                self.assertEqual('unestablished', rows[0]['ownership'])
                self.assertEqual('exited before identity read', rows[0]['reason'])
                self.assertIn('parent', rows[0])
                self.assertNotIn(vanished, [row['pid'] for row in
                                           result['cleanup']['ownership']['observed']])

    def test_vanished_retained_identity_has_one_departure_and_is_not_signaled(self):
        import errno, signal
        from types import SimpleNamespace
        from unittest.mock import patch
        for index, failure in enumerate((
                OSError(errno.ESRCH, 'vanished during identity read'),
                FileNotFoundError(errno.ENOENT, 'vanished proc entry'), None)):
            with self.subTest(failure=repr(failure)):
                tree, module, uid, parent, _ = self._eperm_tree('gone-retained-%d' % index)
                child = {'pid': 101, 'birth': [11, 2], 'ppid': 100,
                         'uid': uid, 'zombie': False}
                tree.retain(child, 'worker-1', {'pid': 100, 'birth': [10, 1]})

                def gone(pid):
                    if failure is not None:
                        raise failure
                    return None

                tree.query = SimpleNamespace(identity=gone, children=lambda pid: [])
                signaled = []
                with patch.object(module['os'], 'kill',
                                  lambda pid, number: signaled.append(pid)):
                    tree.signal(signal.SIGKILL)
                    self.assertEqual([], tree.alive())
                    tree.observe()
                self.assertEqual([], tree.errors)
                self.assertEqual([], signaled)
                self.assertEqual([(101, [11, 2], 'worker-1', 'exited')],
                                 [(row['pid'], row['birth'], row['root'], row['reason'])
                                  for row in tree.receipt()['departed']])

    def test_vanished_candidate_does_not_hide_a_surviving_owned_sibling(self):
        import errno
        from types import SimpleNamespace
        tree, _, uid, parent, _ = self._eperm_tree('gone-sibling')
        child = {'pid': 102, 'birth': [12, 3], 'ppid': 100,
                 'uid': uid, 'zombie': False}
        tree.retain(parent, 'worker-1')
        def identity(pid):
            if pid == 101:
                raise OSError(errno.ESRCH, 'vanished during identity read')
            return parent if pid == 100 else child
        tree.query = SimpleNamespace(identity=identity,
                                     children=lambda pid: [101, 102] if pid == 100 else [])
        tree.observe()
        self.assertEqual([], tree.errors)
        self.assertEqual([100, 102], sorted(row['pid'] for row in tree.alive('worker-1')))
        launcher = self.launcher()
        owner = object.__new__(launcher.Children)
        owner.tree, owner.cleanup_errors = tree, []
        owner.cancellation = SimpleNamespace(check=lambda: None)
        # The leader has now exited; the retained descendant remains alive.
        tree.query.identity = lambda pid: None if pid == 100 else child
        tree.query.children = lambda pid: []
        leader = {'key': 'worker-1', 'process': SimpleNamespace(wait=lambda **kw: 0),
                  'log': SimpleNamespace(close=lambda: None), 'joined': False}
        self.assertEqual(0, owner.join(leader))
        self.assertIn('worker-1: descendants survived leader', owner.cleanup_errors)
        self.assertEqual([102], [row['pid'] for row in tree.alive('worker-1')])
        self.assertNotIn(102, [row['pid'] for row in tree.receipt()['departed']])

    def test_identity_errors_other_than_departure_still_refuse(self):
        import errno
        from types import SimpleNamespace
        for index, code in enumerate((errno.EIO, errno.EACCES)):
            tree, _, uid, parent, _ = self._eperm_tree('gone-other-error-%d' % index)
            tree.retain(parent, 'worker-1')
            def bad_identity(pid):
                raise OSError(code, 'identity unavailable')
            tree.query = SimpleNamespace(identity=bad_identity, children=lambda pid: [])
            tree.observe()
            self.assertTrue(tree.errors, code)
            self.assertEqual([], tree.receipt()['departed'])

    def test_linux_proc_identity_read_disappearance_is_normalized_on_mac(self):
        import errno
        tree, module, _, child = self._linux_tree('gone-linux-proc')
        for code in (errno.ESRCH, errno.ENOENT):
            with self.subTest(errno=code):
                # Disappear while reading /proc/PID/stat.
                with self._linux_proc_files(module, {101: child}, fault=('stat-before', 101, code)):
                    self.assertEqual((None, None), tree.read(101))
                # Or after stat is captured, while reading the effective UID.
                # Directory ownership is no longer an identity input.
                with self._linux_proc_files(module, {101: child}, fault=('status', 101, code)):
                    self.assertEqual((None, None), tree.read(101))

    # HARBOR-PROCESS-ESRCH-2: inject the Linux filesystem boundary, leaving
    # ProcessQuery and ProcessTree real. No host process or signal is used.
    def _linux_tree(self, name):
        import os
        from unittest.mock import patch
        tree, module, _, _, _ = self._eperm_tree(name)
        with patch.object(module['sys'], 'platform', 'linux'):
            tree.query = module['ProcessQuery']()
        parent = {'pid': 100, 'birth': [10], 'ppid': os.getpid(),
                  'uid': os.getuid(), 'zombie': False}
        child = {'pid': 101, 'birth': [11], 'ppid': 100,
                 'uid': os.getuid(), 'zombie': False}
        return tree, module, parent, child

    def _linux_proc_files(self, module, identities, *, fault=None, tasks=None,
                          children=None, births=None, status=None):
        from contextlib import ExitStack
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        tasks, children, births = tasks or {}, children or {}, births or {}
        counts = {}

        def coordinate(path):
            parts = path.parts
            if len(parts) < 3 or parts[:2] != ('/', 'proc'):
                raise AssertionError('unexpected filesystem read: ' + str(path))
            return parts, int(parts[2])

        def fail(site, pid, task_pid=None):
            if fault is not None and fault[:2] == (site, pid):
                if len(fault) == 3 or fault[3] == task_pid:
                    raise OSError(fault[2], 'fixture proc read failed', pid)

        def read_text(path, *args, **kwargs):
            parts, pid = coordinate(path)
            item = identities[pid]
            if parts[-1] == 'stat':
                counts[pid] = counts.get(pid, 0) + 1
                site = 'stat-before' if counts[pid] % 2 else 'stat-after'
                fail(site, pid)
                sequence = births.get(pid, item['birth'])
                birth = sequence[min(counts[pid] - 1, len(sequence) - 1)]
                fields = ['Z' if item['zombie'] else 'R', str(item['ppid'])]
                fields += ['0'] * 17 + [str(birth)]
                return '%d (fixture process) %s' % (pid, ' '.join(fields))
            if parts[-1] == 'status':
                fail('status', pid)
                if status is not None:
                    return status
                return 'Name:\tfixture\nUid:\t%d\t%d\t%d\t%d\n' % (
                    os.getuid(), item['uid'], item['uid'], item['uid'])
            if len(parts) == 6 and parts[3] == 'task' and parts[5] == 'children':
                task_pid = int(parts[4])
                fail('thread-children', pid, task_pid)
                return ' '.join(str(child) for child in children.get((pid, task_pid), []))
            raise AssertionError('unexpected proc file: ' + str(path))

        def directory_stat(path, *args, **kwargs):
            _, pid = coordinate(path)
            fail('directory-stat', pid)
            # /proc directory ownership is not the process's effective UID.
            return SimpleNamespace(st_uid=os.getuid())

        def iterdir(path):
            parts, pid = coordinate(path)
            if len(parts) != 4 or parts[3] != 'task':
                raise AssertionError('unexpected proc enumeration: ' + str(path))
            fail('task-directory', pid)
            return iter(path / str(task_pid) for task_pid in tasks.get(pid, []))

        stack = ExitStack()
        stack.enter_context(patch.object(module['sys'], 'platform', 'linux'))
        stack.enter_context(patch.object(module['Path'], 'read_text', read_text))
        stack.enter_context(patch.object(module['Path'], 'stat', directory_stat))
        stack.enter_context(patch.object(module['Path'], 'iterdir', iterdir))
        return stack

    def test_linux_each_identity_read_departure_is_typed_and_not_signaled(self):
        import errno, signal
        from unittest.mock import patch
        for site in ('stat-before', 'status', 'stat-after'):
            for code in (errno.ESRCH, errno.ENOENT):
                with self.subTest(site=site, errno=code):
                    tree, module, _, child = self._linux_tree('linux-gone-%s-%d' % (site, code))
                    tree.retain(child, 'worker-1')
                    signaled = []
                    with self._linux_proc_files(module, {101: child}, fault=(site, 101, code)),\
                            patch.object(module['os'], 'kill', lambda pid, number: signaled.append(pid)):
                        tree.observe()
                        tree.signal(signal.SIGKILL)
                        self.assertEqual([], tree.alive())
                    self.assertEqual([], signaled)
                    self.assertEqual([], tree.errors)
                    self.assertEqual([(101, [11], 'exited')], [
                        (row['pid'], row['birth'], row['reason'])
                        for row in tree.receipt()['departed']])
                    self.assertIn({'pid': 101, 'site': site, 'errno': code,
                                   'reason': 'proc entry disappeared'},
                                  tree.receipt().get('observations', []))

    def test_linux_identity_read_errors_are_not_process_departures(self):
        import errno
        for site in ('stat-before', 'status', 'stat-after'):
            for code in (errno.EIO, errno.EACCES):
                with self.subTest(site=site, errno=code):
                    tree, module, _, child = self._linux_tree('linux-error-%s-%d' % (site, code))
                    tree.retain(child, 'worker-1')
                    with self._linux_proc_files(module, {101: child}, fault=(site, 101, code)):
                        tree.observe()
                    self.assertTrue(tree.errors, (site, code))
                    self.assertEqual([], tree.receipt()['departed'])
                    self.assertEqual([], tree.receipt().get('observations', []))

    def test_linux_task_directory_loss_does_not_claim_a_live_parent_departed(self):
        import errno
        for code in (errno.ESRCH, errno.ENOENT, errno.EIO, errno.EACCES):
            with self.subTest(errno=code):
                tree, module, parent, _ = self._linux_tree('linux-task-%d' % code)
                tree.retain(parent, 'worker-1')
                with self._linux_proc_files(module, {100: parent}, fault=('task-directory', 100, code)):
                    tree.observe()
                    self.assertEqual([100], [row['pid'] for row in tree.alive()])
                self.assertEqual([], tree.receipt()['departed'])
                if code in (errno.ESRCH, errno.ENOENT):
                    self.assertEqual([], tree.errors)
                    self.assertIn({'pid': 100, 'site': 'task-directory', 'errno': code,
                                   'reason': 'proc entry disappeared'},
                                  tree.receipt().get('observations', []))
                else:
                    self.assertTrue(tree.errors)
                    self.assertEqual([], tree.receipt().get('observations', []))

    def test_linux_thread_loss_keeps_surviving_sibling_edges(self):
        import errno
        for code in (errno.ESRCH, errno.ENOENT, errno.EIO, errno.EACCES):
            with self.subTest(errno=code):
                tree, module, parent, child = self._linux_tree('linux-thread-%d' % code)
                sibling = dict(child, pid=102, birth=[12])
                tree.retain(parent, 'worker-1')
                with self._linux_proc_files(module, {100: parent, 101: child, 102: sibling},
                        tasks={100: [100, 103]}, children={(100, 100): [101], (100, 103): [102]},
                        fault=('thread-children', 100, code, 100)):
                    tree.observe()
                    alive = sorted(row['pid'] for row in tree.alive())
                self.assertEqual([], tree.receipt()['departed'])
                if code in (errno.ESRCH, errno.ENOENT):
                    self.assertEqual([], tree.errors)
                    self.assertEqual([100, 102], alive)
                    self.assertIn({'pid': 100, 'site': 'thread-children', 'task_pid': 100,
                                   'errno': code, 'reason': 'proc entry disappeared'},
                                  tree.receipt().get('observations', []))
                else:
                    self.assertTrue(tree.errors)
                    self.assertEqual([], tree.receipt().get('observations', []))

    def test_linux_effective_uid_change_preserves_start_ownership_without_signaling(self):
        import os, signal
        from unittest.mock import patch
        tree, module, _, child = self._linux_tree('linux-euid-change')
        tree.retain(child, 'worker-1')
        privileged = dict(child, uid=os.getuid() + 1000)
        signaled = []
        with self._linux_proc_files(module, {101: privileged}),\
                patch.object(module['os'], 'kill', lambda pid, number: signaled.append(pid)):
            tree.observe()
            self.assertEqual([101], [row['pid'] for row in tree.alive()])
            tree.signal(signal.SIGKILL)
        self.assertEqual([], signaled)
        self.assertEqual([], tree.errors)
        self.assertEqual([], tree.receipt()['departed'])
        # Privilege loss makes the same retained process safe to signal again.
        with self._linux_proc_files(module, {101: child}),\
                patch.object(module['os'], 'kill', lambda pid, number: signaled.append(pid)):
            tree.signal(signal.SIGKILL)
        self.assertEqual([101], signaled)

    def test_linux_changed_birth_has_one_reuse_departure_and_no_signal(self):
        import signal
        from unittest.mock import patch
        tree, module, _, child = self._linux_tree('linux-reused-birth')
        tree.retain(child, 'worker-1')
        replacement = dict(child, birth=[99], ppid=1)
        signaled = []
        with self._linux_proc_files(module, {101: replacement}),\
                patch.object(module['os'], 'kill', lambda pid, number: signaled.append(pid)):
            tree.signal(signal.SIGKILL)
            tree.observe()
            self.assertEqual([], tree.alive())
        self.assertEqual([], signaled)
        self.assertEqual([], tree.errors)
        self.assertEqual([(101, [11], 'pid reused')], [
            (row['pid'], row['birth'], row['reason']) for row in tree.receipt()['departed']])

    def test_linux_mixed_birth_snapshot_is_recaptured_or_refused(self):
        import os
        for index, births in enumerate(([11, 99, 99, 99], [11, 99, 11, 99])):
            with self.subTest(births=births):
                tree, module, _, child = self._linux_tree('linux-mixed-birth-%d' % index)
                privileged = dict(child, uid=os.getuid() + 1000)
                with self._linux_proc_files(module, {101: privileged}, births={101: births}):
                    try:
                        identity = tree.query.identity(101)
                    except OSError:
                        self.assertEqual(1, index, 'a coherent retry should recover')
                    else:
                        self.assertEqual(0, index, 'persistent mixed births must fail closed')
                        self.assertEqual([99], identity['birth'])
                        self.assertEqual(privileged['uid'], identity['uid'])

    def test_linux_status_uid_shape_is_refused_without_inventing_departure(self):
        for index, status in enumerate(('Name:\tmissing uid\n', 'Uid:\t1 2 3\n',
                'Uid:\t1 -2 3 4\n', 'Uid:\t1 2 3 4\nUid:\t1 2 3 4\n')):
            with self.subTest(status=status):
                tree, module, _, child = self._linux_tree('linux-status-shape-%d' % index)
                tree.retain(child, 'worker-1')
                with self._linux_proc_files(module, {101: child}, status=status):
                    tree.observe()
                self.assertTrue(tree.errors, status)
                self.assertEqual([], tree.receipt()['departed'])

    def test_linux_foreign_effective_uid_first_adoption_keeps_verified_parentage(self):
        import os, signal
        from types import SimpleNamespace
        from unittest.mock import patch
        for mode in ('leader', 'descendant'):
            with self.subTest(mode=mode):
                tree, module, parent, child = self._linux_tree('linux-foreign-first-' + mode)
                privileged = dict(child, uid=os.getuid() + 1000)
                if mode == 'leader':
                    privileged['ppid'] = os.getpid()
                else:
                    tree.retain(parent, 'worker-1')
                signaled = []
                with self._linux_proc_files(module, {100: parent, 101: privileged},
                        tasks={100: [100]}, children={(100, 100): [101]}),\
                        patch.object(module['os'], 'kill', lambda pid, number: signaled.append(pid)):
                    if mode == 'leader':
                        tree.root(SimpleNamespace(pid=101, returncode=None, _child_created=True), 'worker-1')
                    else:
                        tree.observe()
                    self.assertIn(101, [row['pid'] for row in tree.alive()])
                    tree.signal(signal.SIGKILL)
                self.assertNotIn(101, signaled)
                self.assertEqual([], tree.errors)
                self.assertEqual([], tree.receipt()['departed'])

    def test_linux_leader_departure_before_first_read_has_unestablished_receipt(self):
        import errno, signal
        from types import SimpleNamespace
        from unittest.mock import patch
        for code in (errno.ESRCH, errno.ENOENT):
            with self.subTest(errno=code):
                tree, module, _, child = self._linux_tree('linux-leader-gone-%d' % code)
                signaled = []
                with self._linux_proc_files(module, {101: child}, fault=('stat-before', 101, code)),\
                        patch.object(module['os'], 'kill', lambda pid, number: signaled.append(pid)):
                    tree.root(SimpleNamespace(pid=101, returncode=None, _child_created=True), 'worker-1')
                    tree.signal(signal.SIGKILL)
                self.assertEqual([], tree.errors)
                self.assertEqual([], tree.receipt()['observed'])
                self.assertEqual([], signaled)
                self.assertEqual([{'pid': 101, 'birth': None, 'root': 'worker-1',
                    'ownership': 'unestablished', 'reason': 'exited before leader identity read'}],
                    tree.receipt()['departed'])

    def test_eperm_witness_disappearance_is_departed_but_other_witness_errors_refuse(self):
        import errno
        from types import SimpleNamespace
        for index, failure in enumerate((OSError(errno.ESRCH, 'witness vanished'),
                                         FileNotFoundError('witness vanished'))):
            tree, _, uid, _, unreadable = self._eperm_tree('gone-witness-%d' % index)
            child = {'pid': 101, 'birth': [11, 2], 'ppid': 100,
                     'uid': uid, 'zombie': False}
            tree.retain(child, 'worker-1')
            def vanished_witness(pid):
                raise failure
            tree.query = SimpleNamespace(identity=unreadable, witness=vanished_witness,
                                         children=lambda pid: [])
            self.assertEqual([], tree.alive())
            self.assertEqual([], tree.errors)
            self.assertEqual(['exited'], [row['reason'] for row in tree.receipt()['departed']])

    # HARBOR-PARALLEL-EPERM-1. A process this runner owns can stop being
    # readable through the per-user identity call while it is still alive:
    # a test that runs setuid-root /bin/ps leaves a child of ours with
    # euid 0 for a few milliseconds, and proc_pidinfo answers EPERM. One
    # such snapshot used to be an ownership error that invalidated the
    # whole run. The verdict now comes from start identity (pid + start
    # time) read through an unprivileged kernel witness: gone is gone with
    # a typed reason; alive-and-ours-but-unreadable is refused, by pid, at
    # the moment survivors are judged.

    def _eperm_tree(self, name):
        import errno, os
        output = self.bank / name; output.mkdir()
        tree = self.launcher().ProcessTree(output)
        self.addCleanup(tree.journal.close)
        module = tree.__class__.__init__.__globals__
        uid = os.getuid()
        parent = {'pid': 100, 'birth': [10, 1], 'ppid': 1, 'uid': uid, 'zombie': False}

        def unreadable(pid):
            raise OSError(errno.EPERM, 'cannot read owned process identity', pid)
        return tree, module, uid, parent, unreadable

    def test_eperm_on_owned_process_that_exited_is_gone_not_fatal(self):
        from types import SimpleNamespace
        tree, _, uid, parent, unreadable = self._eperm_tree('eperm-exited')
        child = {'pid': 101, 'birth': [11, 2], 'ppid': 100, 'uid': uid, 'zombie': False}
        tree.retain(parent, 'worker-1')
        tree.retain(child, 'worker-1', {'pid': 100, 'birth': [10, 1]})
        tree.query = SimpleNamespace(
            identity=lambda pid: parent if pid == 100 else unreadable(pid),
            children=lambda pid: [], witness=lambda pid: None if pid == 101 else parent)
        tree.observe()
        self.assertEqual([], tree.errors)
        self.assertEqual([100], [item['pid'] for item in tree.alive('worker-1')])
        self.assertEqual([(101, [11, 2], 'worker-1', 'exited')],
                         [(item['pid'], item['birth'], item['root'], item['reason'])
                          for item in tree.receipt()['departed']])

    def test_eperm_on_reused_pid_is_gone_and_never_signaled(self):
        import signal
        from types import SimpleNamespace
        from unittest.mock import patch
        tree, module, uid, parent, unreadable = self._eperm_tree('eperm-reused')
        child = {'pid': 101, 'birth': [11, 2], 'ppid': 100, 'uid': uid, 'zombie': False}
        stranger = {'pid': 101, 'birth': [99, 7], 'ppid': 1, 'uid': 0, 'zombie': False}
        tree.retain(parent, 'worker-1')
        tree.retain(child, 'worker-1', {'pid': 100, 'birth': [10, 1]})
        tree.query = SimpleNamespace(
            identity=lambda pid: parent if pid == 100 else unreadable(pid),
            children=lambda pid: [], witness=lambda pid: stranger if pid == 101 else parent)
        signaled = []
        with patch.object(module['os'], 'kill', lambda pid, number: signaled.append(pid)):
            tree.signal(signal.SIGKILL)
        self.assertEqual([], tree.errors)
        self.assertEqual([100], signaled)
        self.assertEqual(['pid reused'], [item['reason'] for item in tree.receipt()['departed']])

    def test_eperm_on_zombie_is_gone(self):
        from types import SimpleNamespace
        tree, _, uid, parent, unreadable = self._eperm_tree('eperm-zombie')
        child = {'pid': 101, 'birth': [11, 2], 'ppid': 100, 'uid': uid, 'zombie': False}
        tree.retain(child, 'worker-1')
        tree.query = SimpleNamespace(identity=unreadable, children=lambda pid: [],
                                     witness=lambda pid: dict(child, uid=0, zombie=True))
        self.assertEqual([], tree.alive())
        self.assertEqual([], tree.errors)
        self.assertEqual(['zombie'], [item['reason'] for item in tree.receipt()['departed']])

    def test_eperm_on_unretained_candidate_that_is_gone_is_not_adopted(self):
        from types import SimpleNamespace
        tree, _, uid, parent, unreadable = self._eperm_tree('eperm-candidate')
        reaped_and_reused = {'pid': 102, 'birth': [12, 3], 'ppid': 1, 'uid': 0, 'zombie': False}
        tree.retain(parent, 'worker-1')
        tree.query = SimpleNamespace(
            identity=lambda pid: parent if pid == 100 else unreadable(pid),
            children=lambda pid: [101, 102] if pid == 100 else [],
            witness=lambda pid: reaped_and_reused if pid == 102 else None)
        tree.observe()
        self.assertEqual([], tree.errors)
        self.assertEqual([100], [item['pid'] for item in tree.known.values()])

    def test_eperm_on_alive_owned_process_is_refused_by_pid_and_not_signaled(self):
        import signal
        from types import SimpleNamespace
        from unittest.mock import patch
        tree, module, uid, parent, unreadable = self._eperm_tree('eperm-alive')
        child = {'pid': 101, 'birth': [11, 2], 'ppid': 100, 'uid': uid, 'zombie': False}
        setuid = dict(child, uid=0)
        tree.retain(parent, 'worker-1')
        tree.retain(child, 'worker-1', {'pid': 100, 'birth': [10, 1]})
        tree.query = SimpleNamespace(
            identity=lambda pid: parent if pid == 100 else unreadable(pid),
            children=lambda pid: [], witness=lambda pid: setuid if pid == 101 else parent)
        tree.observe()
        survivors = tree.alive('worker-1')
        # Control: the alive-and-unreadable process is still a refusal.
        self.assertTrue(tree.errors or 101 in [item['pid'] for item in survivors])
        self.assertIn(101, [item['pid'] for item in survivors])
        refusals = tree.unreadable_refusals(survivors)
        self.assertEqual(1, len(refusals), refusals)
        self.assertIn('worker-1: owned process alive but unreadable: pid 101 start [11, 2] euid 0',
                      refusals[0])
        self.assertIn('[Errno 1]', refusals[0])
        signaled = []
        with patch.object(module['os'], 'kill', lambda pid, number: signaled.append(pid)):
            tree.signal(signal.SIGKILL)
        self.assertEqual([100], signaled)
        self.assertEqual([], tree.receipt()['departed'])

    def test_eperm_on_alive_candidate_of_owned_parent_is_retained_and_refused(self):
        from types import SimpleNamespace
        tree, _, uid, parent, unreadable = self._eperm_tree('eperm-alive-candidate')
        setuid = {'pid': 101, 'birth': [11, 2], 'ppid': 100, 'uid': 0, 'zombie': False}
        tree.retain(parent, 'worker-1')
        tree.query = SimpleNamespace(
            identity=lambda pid: parent if pid == 100 else unreadable(pid),
            children=lambda pid: [101] if pid == 100 else [],
            witness=lambda pid: setuid if pid == 101 else parent)
        tree.observe()
        survivors = tree.alive('worker-1')
        self.assertTrue(tree.errors or 101 in [item['pid'] for item in survivors])
        self.assertEqual([100, 101], sorted(item['pid'] for item in survivors))
        self.assertEqual(['worker-1: owned process alive but unreadable: pid 101 start [11, 2] euid 0'],
                         [line.split(': [Errno')[0] for line in tree.unreadable_refusals(survivors)])

    def test_unreadable_child_that_drops_privilege_stays_owned(self):
        import signal
        from types import SimpleNamespace
        from unittest.mock import patch
        tree, module, uid, parent, unreadable = self._eperm_tree('eperm-regained')
        setuid = {'pid': 101, 'birth': [11, 2], 'ppid': 100, 'uid': 0, 'zombie': False}
        tree.retain(parent, 'worker-1')
        state = {'readable': False}
        tree.query = SimpleNamespace(
            identity=lambda pid: (parent if pid == 100 else dict(setuid, uid=uid)
                                  if state['readable'] else unreadable(pid)),
            children=lambda pid: [101] if pid == 100 else [],
            witness=lambda pid: setuid if pid == 101 else parent)
        tree.observe()
        state['readable'] = True
        for _ in range(2):
            survivors = tree.alive('worker-1')
            self.assertEqual([100, 101], sorted(item['pid'] for item in survivors))
            self.assertEqual([], tree.unreadable_refusals(survivors))
        signaled = []
        with patch.object(module['os'], 'kill', lambda pid, number: signaled.append(pid)):
            tree.signal(signal.SIGKILL)
        self.assertEqual([100, 101], sorted(signaled))
        self.assertEqual([], tree.errors)
        self.assertEqual([], tree.receipt()['departed'])

    def test_unreadable_witness_failure_and_other_errors_still_refuse(self):
        import errno
        from types import SimpleNamespace
        tree, _, uid, parent, unreadable = self._eperm_tree('eperm-witness-fails')
        child = {'pid': 101, 'birth': [11, 2], 'ppid': 100, 'uid': uid, 'zombie': False}
        tree.retain(child, 'worker-1')

        def witness_refuses(pid):
            raise OSError('controlled witness refusal')

        def other_error(pid):
            raise OSError(errno.EIO, 'cannot read owned process identity', pid)
        tree.query = SimpleNamespace(identity=unreadable, children=lambda pid: [],
                                     witness=witness_refuses)
        tree.alive()
        self.assertTrue(any('controlled witness refusal' in error for error in tree.errors), tree.errors)
        tree.errors.clear()
        tree.query = SimpleNamespace(identity=other_error, children=lambda pid: [],
                                     witness=lambda pid: None)
        tree.alive()
        self.assertTrue(any('[Errno 5]' in error for error in tree.errors), tree.errors)

    def _setuid_top(self):
        import os, stat, sys
        path = '/usr/bin/top'
        if (sys.platform != 'darwin' or not os.path.exists(path)
                or not os.stat(path).st_mode & stat.S_ISUID):
            self.skipTest('no setuid-root /usr/bin/top on this host; an owned child the runner cannot read is unobservable here')
        return path

    def _spawn_real_setuid_child(self, name):
        """A shell we own, whose child is a REAL setuid-root process."""
        import errno, os, signal, subprocess, time
        top = self._setuid_top()
        output = self.bank / name; output.mkdir()
        tree = self.launcher().ProcessTree(output)
        self.addCleanup(tree.journal.close)
        shell = subprocess.Popen(['/bin/sh', '-c', top + ' -l 0 -s 1 >/dev/null & echo $!; wait'],
                                 stdout=subprocess.PIPE, text=True, start_new_session=True)
        setuid_pid = int(shell.stdout.readline())

        def stop():
            # Only pids this test started: the setuid child, then its shell.
            try:
                os.kill(setuid_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            shell.wait(timeout=30)
            shell.stdout.close()
        self.addCleanup(lambda: shell.poll() is not None or stop())
        tree.root(shell, 'worker-1')
        self.assertEqual([], tree.errors)
        readiness = Readiness('owned setuid child unreadable identity')
        while True:
            # The real condition: our own child, alive, unreadable (EPERM).
            try:
                tree.query.identity(setuid_pid)
            except OSError as exc:
                if exc.errno == errno.EPERM:
                    break
                raise
            readiness.remaining()
            time.sleep(.01)
        return tree, shell, setuid_pid, stop

    def test_real_setuid_child_that_exits_leaves_no_ownership_error(self):
        tree, shell, setuid_pid, stop = self._spawn_real_setuid_child('real-exited')
        tree.observe()
        stop()
        tree.observe()
        self.assertEqual([], tree.errors)
        self.assertEqual([], tree.alive('worker-1'))
        departed = [item for item in tree.receipt()['departed'] if item['pid'] == setuid_pid]
        self.assertEqual(['exited'], [item['reason'] for item in departed])

    def test_real_setuid_child_alive_at_verdict_is_refused_by_pid(self):
        tree, shell, setuid_pid, stop = self._spawn_real_setuid_child('real-alive')
        tree.observe()
        survivors = tree.alive('worker-1')
        self.assertTrue(tree.errors or setuid_pid in [item['pid'] for item in survivors])
        self.assertIn(setuid_pid, [item['pid'] for item in survivors])
        refusals = tree.unreadable_refusals(survivors)
        self.assertEqual(1, len(refusals), refusals)
        self.assertIn('owned process alive but unreadable: pid %d' % setuid_pid, refusals[0])
        self.assertIn('euid 0', refusals[0])

    def test_run_whose_test_spawns_a_setuid_child_is_not_invalidated(self):
        top = self._setuid_top()
        self.fixture('import unittest, subprocess\nclass Case(unittest.TestCase):\n'
                     ' def test_setuid_child(self):\n'
                     '  subprocess.run([%r, "-l", "2", "-s", "1"], stdout=subprocess.DEVNULL, check=True)\n'
                     % top)
        import shutil
        result = self.launcher().launch('run', self.bank / 'out', 1)
        if result.get('scratch_root'):
            self.addCleanup(shutil.rmtree, result['scratch_root'], True)
        self.assertEqual([], result['cleanup']['errors'], result.get('error'))
        self.assertEqual('complete', result['status'], result.get('error'))
        self.assertTrue(result['successful'])
        self.assertIn('exited', [item['reason'] for item in result['cleanup']['ownership']['departed']])

    def test_exec_failure_does_not_adopt_or_signal_reused_pid(self):
        import os
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        launcher = self.launcher()
        constructor = launcher.subprocess.Popen.__init__
        query_type = launcher.ProcessTree.__init__.__globals__['ProcessQuery']
        identity, children, kill = query_type.identity, query_type.children, os.kill
        reaped, signaled = [], []

        def exec_failure(process, *args, **kwargs):
            if not kwargs.get('start_new_session'):
                return constructor(process, *args, **kwargs)
            try:
                # Exercise CPython's real fork/exec error-pipe and internal reap.
                constructor(process, [str(self.bank / 'missing-executable')], **kwargs)
            except FileNotFoundError:
                reaped.append(process)
                raise

        def replacement(query, pid):
            if reaped and pid == reaped[0].pid:
                return {'pid': pid, 'birth': [999999999, 1], 'ppid': os.getpid(),
                        'uid': os.getuid(), 'zombie': False}
            return identity(query, pid)

        def replacement_children(query, pid):
            return [] if reaped and pid == reaped[0].pid else children(query, pid)

        def record_signal(pid, number):
            if reaped and pid == reaped[0].pid:
                signaled.append((pid, number))
            else:
                kill(pid, number)

        with patch.object(launcher.subprocess.Popen, '__init__', exec_failure),\
                patch.object(query_type, 'identity', replacement),\
                patch.object(query_type, 'children', replacement_children),\
                patch.object(os, 'kill', record_signal):
            result = launcher.launch('run', self.bank / 'out', 1)
        import shutil
        self.addCleanup(shutil.rmtree, result['scratch_root'], True)
        self.assertEqual(1, len(reaped))
        self.assertIsNotNone(reaped[0].returncode)
        self.assertEqual(reaped[0].pid, result['children'][0]['pid'])
        self.assertEqual(reaped[0].returncode, result['children'][0]['returncode'])
        self.assertEqual([], result['cleanup']['ownership']['observed'])
        self.assertEqual([], signaled)

    def test_spawn_root_requires_current_direct_parent(self):
        import os, signal, site
        from unittest.mock import patch
        launcher = self.launcher()
        output, scratch = self.bank / 'out', self.bank / 'scratch'
        output.mkdir(); scratch.mkdir()
        owner = launcher.Children(self.root, output, scratch, site.getusersitepackages())
        identity = owner.tree.query.identity

        def foreign_parent(pid):
            result = identity(pid)
            return dict(result, ppid=os.getpid() + 1) if result is not None else result

        try:
            with patch.object(owner.tree.query, 'identity', foreign_parent):
                child = owner.spawn('discovery')
            self.assertEqual([], owner.tree.receipt()['observed'])
            self.assertTrue(any('parent' in error for error in owner.tree.errors))
        finally:
            for child in owner.children:
                proc = child['process']
                if proc.poll() is None:
                    proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=5)
            owner.close()

    def _cancel_at_final_stage(self, stage, number):
        import contextlib, io, json, os, shutil, signal
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        launcher = self.launcher()
        reference = ()
        if stage == 'comparison':
            proc, result = self.run_cli('serial', 'reference')
            self.assertEqual(0, proc.returncode, result)
            reference = ('--reference', str(self.bank / 'reference/final.json'))
        if stage == 'cleanup':
            target, name = launcher.Children, 'close'
        elif stage in ('source', 'pending'):
            target, name = launcher, 'source_identity'
        else:
            target, name = launcher, 'compare_results' if stage == 'comparison' else 'reconcile'
        original = getattr(target, name)
        calls = []

        def cancel_after(*args, **kwargs):
            value = original(*args, **kwargs)
            calls.append(stage)
            if stage not in ('source', 'pending') or len(calls) == 2:
                if stage == 'pending':
                    signal.pthread_sigmask(signal.SIG_BLOCK, (number,))
                os.kill(os.getpid(), number)
            return value

        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        try:
            with patch.object(target, name, cancel_after), contextlib.redirect_stdout(io.StringIO()):
                code = launcher.main(['run', '--output', str(self.bank / 'out'), '--workers', '1', *reference])
        finally:
            with launcher.cancellation_handlers(signal.SIG_IGN):
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
        result = json.loads((self.bank / 'out/final.json').read_text())
        self.addCleanup(shutil.rmtree, result['scratch_root'], True)
        self.assertEqual(128 + number, code, {key: result[key] for key in ('status', 'successful', 'qualified')})
        self.assertEqual('interrupted', result['status'])
        self.assertFalse(result['successful'])
        self.assertFalse(result['qualified'])
        self.assertTrue(result['cleanup']['successful'], result['cleanup'])
        self.assertFalse(result['cleanup']['scratch_retained'])
        self.assertFalse(Path(result['scratch_root']).exists())

    def test_cancellation_during_reconciliation_cannot_publish_success(self):
        import signal
        self._cancel_at_final_stage('reconcile', signal.SIGINT)

    def test_cancellation_during_comparison_cannot_qualify(self):
        import signal
        self._cancel_at_final_stage('comparison', signal.SIGTERM)

    def test_first_cancellation_during_cleanup_removes_scratch(self):
        import signal
        self._cancel_at_final_stage('cleanup', signal.SIGTERM)

    def test_cancellation_during_final_source_check_removes_scratch(self):
        import signal
        self._cancel_at_final_stage('source', signal.SIGINT)

    def test_pending_signal_before_commitment_refuses_success(self):
        import signal
        self._cancel_at_final_stage('pending', signal.SIGTERM)

    def test_final_commitment_masks_signals_during_removal_and_receipt_write(self):
        import contextlib, io, json, os, signal
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        launcher = self.launcher()
        remove, write = launcher.cleanup_path, launcher.write_json
        stages = []

        def during_remove(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertIn(signal.SIGTERM, signal.sigpending())
            stages.append('scratch removal')
            return remove(*args, **kwargs)

        def during_write(path, value):
            if path.name == 'final.json':
                os.kill(os.getpid(), signal.SIGINT)
                self.assertIn(signal.SIGINT, signal.sigpending())
                self.assertTrue(value['final_commitment']['begun'])
                stages.append('receipt write')
            return write(path, value)

        with patch.object(launcher, 'cleanup_path', during_remove),\
                patch.object(launcher, 'write_json', during_write),\
                contextlib.redirect_stdout(io.StringIO()):
            code = launcher.main(['run', '--output', str(self.bank / 'out'), '--workers', '1'])
        self.assertEqual(0, code, 'final commitment must mask both supported signals')
        result = json.loads((self.bank / 'out/final.json').read_text())
        self.assertEqual(0, code, result.get('error'))
        self.assertEqual(['scratch removal', 'receipt write'], stages)
        self.assertEqual('complete', result['status'])
        self.assertFalse(result['cleanup']['scratch_retained'])
        self.assertTrue(result['final_commitment']['begun'])

    def test_repeated_cleanup_signal_preserves_spawn_interruption(self):
        import os, shutil, signal
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        launcher = self.launcher()
        constructor, close = launcher.subprocess.Popen.__init__, launcher.Children.close

        def first_interrupt(process, *args, **kwargs):
            constructor(process, *args, **kwargs)
            if kwargs.get('start_new_session'):
                os.kill(os.getpid(), signal.SIGINT)

        def repeated_interrupt(owner):
            os.kill(os.getpid(), signal.SIGTERM)
            return close(owner)

        with patch.object(launcher.subprocess.Popen, '__init__', first_interrupt),\
                patch.object(launcher.Children, 'close', repeated_interrupt):
            result = launcher.launch('run', self.bank / 'out', 1)
        self.addCleanup(shutil.rmtree, result['scratch_root'], True)
        self.assertEqual('SIGINT', result['interruption'])
        self.assertEqual(130, result['exit_code'])
        self.assertTrue(result['cleanup']['successful'], result['cleanup'])
        self.assertFalse(Path(result['scratch_root']).exists())

    def test_constructor_exception_preserves_deferred_first_signal(self):
        import os, shutil, signal
        from unittest.mock import patch
        self.fixture('import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n')
        for later_signal in (False, True):
            with self.subTest(later_cleanup_signal=later_signal):
                launcher = self.launcher()
                constructor, close = launcher.subprocess.Popen.__init__, launcher.Children.close
                spawned = []

                def interrupted_failure(process, *args, **kwargs):
                    constructor(process, *args, **kwargs)
                    if kwargs.get('start_new_session'):
                        spawned.append(process)
                        os.kill(os.getpid(), signal.SIGINT)
                        raise OSError('constructor failed after deferred SIGINT')

                def cleanup(owner):
                    if later_signal:
                        os.kill(os.getpid(), signal.SIGTERM)
                    return close(owner)

                with patch.object(launcher.subprocess.Popen, '__init__', interrupted_failure),\
                        patch.object(launcher.Children, 'close', cleanup):
                    result = launcher.launch('run', self.bank / ('later' if later_signal else 'first'), 1)
                self.addCleanup(shutil.rmtree, result['scratch_root'], True)
                self.assertEqual('interrupted', result['status'])
                self.assertEqual('SIGINT', result['interruption'])
                self.assertEqual(130, result['exit_code'])
                self.assertFalse(result['successful'])
                self.assertFalse(result['qualified'])
                self.assertTrue(result['cleanup']['successful'], result['cleanup'])
                self.assertFalse(Path(result['scratch_root']).exists())
                self.assertEqual([process.pid for process in spawned],
                                 [child['pid'] for child in result['children']])
                self.assertTrue(all(process.poll() is not None for process in spawned))


if __name__ == '__main__':
    unittest.main()
