#!/usr/bin/python3
"""Fixed BL-10-F2 readers; pin HOME before repository tests, retain native output."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('red', 'boundary', 'focused', 'fences'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if (not output.is_absolute() or output != Path(os.path.normpath(str(output)))
            or output.resolve() != output or Path('\x2fprivate/tmp') not in output.parents):
        parser.error('output must be a fresh private temporary descendant')
    output.mkdir(mode=0o700)
    modules = {
        'red': ['tests.test_fl1_census_attribution.NativeAttributionTests'],
        'boundary': ['tests.test_fl1_census_attribution.ObservationBoundaryTests'],
        'focused': ['tests.test_fl1_census_attribution', 'tests.test_gate_z2_of_fl_1',
                    'tests.test_g1_trace_witness', 'tests.test_g1_trace_contract',
                    'tests.test_g1_trace_witness_availability'],
        'fences': ['tests.test_rem_2', 'tests.test_source_scrub', 'tests.test_name_sweep',
                   'tests.test_manifest', 'tests.test_h1_f1', 'tests.test_skip_census_1'],
    }[args.phase]
    started = time.monotonic()
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    with (output / 'unittest.log').open('w') as stream:
        original_out, original_err = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = stream
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        finally:
            sys.stdout, sys.stderr = original_out, original_err
    controls = sys.modules.get('tests.test_fl1_census_attribution')
    retained = getattr(controls, '_RETAINED_MAINTENANCE', {})
    owned_cleanup = [{'pid': pid, 'bank': str(bank)}
                     for pid, (process, bank) in retained.items()]
    successful = result.wasSuccessful() and not owned_cleanup
    receipt = {'phase': args.phase, 'modules': modules, 'tests_run': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'wall_seconds': time.monotonic() - started,
               'owned_cleanup_pending': owned_cleanup,
               'successful': successful, 'output': str(output)}
    (output / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))
    return 0 if successful else 1


if __name__ == '__main__':
    raise SystemExit(main())
