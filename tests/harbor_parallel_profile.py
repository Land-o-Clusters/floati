"""Measured scheduling hints never change the discovered test population."""

import hashlib
import importlib.util
import copy
from pathlib import Path
import unittest


_SPEC = importlib.util.spec_from_file_location(
    "parallel_profile", Path(__file__).resolve().parents[1] / "scripts/parallel_profile.py"
)
_PROFILE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PROFILE)


class ProfileWeightsTests(unittest.TestCase):
    def setUp(self):
        self.census = {
            "ids": ["a", "b", "new"],
            "units": [{"key": "ab", "ids": ["a", "b"]},
                      {"key": "new", "ids": ["new"]}],
        }

    def test_profile_costs_cover_units_without_changing_population(self):
        data = b"test_id,seconds\na,2\nb,3\nold,8\n"
        weights, evidence = _PROFILE.profile_weights(data, self.census)
        self.assertEqual(weights, {"ab": 5.0, "new": 2.5})
        self.assertEqual(evidence["matched_ids"], 2)
        self.assertEqual(evidence["unmatched_population_ids"], ["new"])
        self.assertEqual(evidence["unused_profile_ids"], ["old"])
        self.assertEqual(evidence["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.census["ids"], ["a", "b", "new"])

    def test_repeated_alias_measurements_use_maximum_not_sum(self):
        weights, evidence = _PROFILE.profile_weights(
            b"test_id,seconds\na,2\na,4\nb,3\n", self.census
        )
        self.assertEqual(weights, {"ab": 7.0, "new": 3.5})
        self.assertEqual(evidence["repeated_rows"], 1)
        self.assertEqual(evidence["duplicate_policy"], "maximum per exact test ID")

    def test_profile_with_no_matching_ids_refuses(self):
        with self.assertRaisesRegex(ValueError, "no matching"):
            _PROFILE.profile_weights(b"test_id,seconds\nold,2\n", self.census)

    def test_invalid_or_missing_durations_refuse(self):
        for duration in ("nan", "inf", "-1", "", "fast"):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ValueError, "duration"):
                    _PROFILE.profile_weights(
                        ("test_id,seconds\na," + duration + "\n").encode(), self.census
                    )

    def test_missing_columns_empty_ids_and_empty_profile_refuse(self):
        for data in (b"id,time\na,2\n", b"test_id,seconds\n,2\n", b"test_id,seconds\n"):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    _PROFILE.profile_weights(data, self.census)

    def test_zero_duration_remains_a_valid_measurement(self):
        weights, evidence = _PROFILE.profile_weights(
            b"test_id,seconds\na,0\nb,0\n", self.census
        )
        self.assertEqual(weights, {"ab": 0.0, "new": 0.0})
        self.assertEqual(evidence["fallback_seconds"], 0.0)

    def test_malformed_csv_shape_and_quoting_refuse(self):
        for data in (
            b"test_id,seconds,seconds\na,bad,2\n",
            b"test_id,seconds\na,2,unclaimed\n",
            b"test_id,seconds,detail\na,2\n",
            b'test_id,seconds\na,"2\n',
        ):
            with self.subTest(data=data):
                with self.assertRaisesRegex(ValueError, "profile|CSV"):
                    _PROFILE.profile_weights(data, self.census)

    def test_canonical_profile_is_order_independent_and_rejects_bad_measurements(self):
        self.assertTrue(callable(getattr(_PROFILE, 'profile_csv', None)),
                        'completed runs need the existing canonical CSV producer')
        self.assertEqual(b'test_id,seconds\na,2.0\nb,0.0\n',
                         _PROFILE.profile_csv({'b': 0.0, 'a': 2.0}))
        for durations in ({'a': -1}, {'a': float('nan')}, {'a': True}, {'': 1}):
            with self.subTest(durations=durations), self.assertRaises(ValueError):
                _PROFILE.profile_csv(durations)

    def test_unmeasured_units_spread_without_changing_population(self):
        from scripts.parallel_plan import make_plan
        ids = ['m.C%d.test_one' % i for i in range(9)]
        census = {'ids': ids, 'units': [
            {'key': test_id, 'ids': [test_id], 'classes': [test_id.rsplit('.', 1)[0]],
             'serial_reason': None} for test_id in ids],
            'membership': {test_id: {'class': test_id.rsplit('.', 1)[0],
                                    'module': 'm', 'origin': 'm'} for test_id in ids}}
        import json
        census['population_digest'] = hashlib.sha256(json.dumps(
            ids, separators=(',', ':')).encode()).hexdigest()
        original = copy.deepcopy(census)
        for seconds in (0, 2):
            with self.subTest(seconds=seconds):
                weights, audit = _PROFILE.profile_weights(
                    ('test_id,seconds\n%s,%s\n' % (ids[0], seconds)).encode(), census)
                plan = make_plan(census, 3, weights)
                self.assertEqual([3, 3, 3], [len(a['ids']) for a in plan['assignments']])
                self.assertEqual(ids[1:], audit['unmatched_population_ids'])
                self.assertEqual(original, census)

    def test_completed_timings_rebalance_a_stale_profile_without_losing_ids(self):
        import json
        from scripts.parallel_plan import make_plan
        self.assertTrue(callable(getattr(_PROFILE, 'profile_csv', None)),
                        'refresh must reuse canonical measured profile production')
        ids = ['m.C%02d.test_one' % i for i in range(64)]
        census = {'ids': ids, 'units': [
            {'key': tid, 'ids': [tid], 'classes': [tid.rsplit('.', 1)[0]],
             'serial_reason': None} for tid in ids],
            'membership': {tid: {'class': tid.rsplit('.', 1)[0],
                                 'module': 'm', 'origin': 'm'} for tid in ids},
            'population_digest': hashlib.sha256(json.dumps(
                ids, separators=(',', ':')).encode()).hexdigest()}
        durations = {tid: 1.6 if i % 8 == 0 else 1.0 for i, tid in enumerate(ids)}
        current, _ = _PROFILE.profile_weights(_PROFILE.profile_csv(durations), census)
        stale, _ = _PROFILE.profile_weights(
            _PROFILE.profile_csv(dict.fromkeys(ids, 1.0)), census)
        plans = [make_plan(census, 8, weights) for weights in (stale, current)]
        ratios = []
        for plan in plans:
            loads = [sum(current[key] for key in a['unit_keys']) for a in plan['assignments']]
            ratios.append(max(loads) / (sum(loads) / len(loads)))
            self.assertEqual(census['population_digest'], plan['population_digest'])
            self.assertCountEqual(ids, [tid for a in plan['assignments'] for tid in a['ids']])
        self.assertGreater(ratios[0], 1.45)
        self.assertLessEqual(ratios[1], 1.15)

    def test_completed_profile_requires_consistent_receipts_and_measured_started_ids(self):
        from tests.harbor_parallel_plan import census, receipt
        from scripts.parallel_plan import make_plan, reconcile
        self.assertTrue(callable(getattr(_PROFILE, 'completed_durations', None)),
                        'profile refresh must validate completed-run timing evidence')
        population = census()
        plan = make_plan(population, 2)
        results = [receipt(a) for a in plan['assignments']]
        for result in results:
            result.update(source={'head': 'source'}, interpreter={'executable': 'python'},
                          test_durations=dict.fromkeys(result['started_ids'], 1.0))
        report = {'mode': 'run', 'status': 'complete', 'cleanup': {'successful': True},
                  'source': {'head': 'source'}, 'interpreter': {'executable': 'python'},
                  'census': population, 'plan': plan, 'results': results,
                  'aggregate': reconcile(population, plan, results), 'successful': True}
        population.update(source=report['source'], interpreter=report['interpreter'])
        self.assertEqual(dict.fromkeys(population['ids'], 1.0),
                         _PROFILE.completed_durations(report))
        # A fully observed failed run is still timing evidence.
        failed = copy.deepcopy(report)
        failed['results'][0]['events'][0]['kind'] = 'failure'
        failed['results'][0]['successful'] = False
        failed['successful'] = False
        failed['aggregate'] = reconcile(population, plan, failed['results'])
        self.assertEqual(dict.fromkeys(population['ids'], 1.0),
                         _PROFILE.completed_durations(failed))
        mutations = [
            lambda r: r.update(status='interrupted'),
            lambda r: r['cleanup'].update(successful=False),
            lambda r: r['aggregate'].update(tests_run=999),
            lambda r: r['results'][0].update(source={'head': 'foreign'}),
            lambda r: r['results'][0]['test_durations'].update(foreign=1),
            lambda r: r['results'][0]['test_durations'].update({'m.A.a': -1}),
            lambda r: r['results'][0]['test_durations'].update({'m.A.a': True}),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                damaged = copy.deepcopy(report)
                mutate(damaged)
                with self.assertRaises(ValueError):
                    _PROFILE.completed_durations(damaged)
