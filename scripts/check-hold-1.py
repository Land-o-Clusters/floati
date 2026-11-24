#!/usr/bin/python3
"""Fixed HOLD-1 readers with native logs and private temporary evidence."""
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
    parser.add_argument('phase', choices=('red', 'focused', 'fences'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if (not output.is_absolute() or output != Path(os.path.normpath(str(output)))
            or output.resolve() != output or Path('\x2fprivate/tmp') not in output.parents):
        parser.error('output must be a fresh private temporary descendant')
    output.mkdir(mode=0o700)
    modules = {
        'red': ['tests.test_holds'],
        'focused': ['tests.test_holds', 'tests.test_record_validation',
                    'tests.test_command_codegen', 'tests.test_command_metavars',
                    'tests.test_tui_render', 'tests.test_doctor',
                    'tests.test_doctor_remedy_schema'],
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
    receipt = {'phase': args.phase, 'modules': modules, 'tests_run': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'wall_seconds': time.monotonic() - started,
               'successful': result.wasSuccessful(), 'output': str(output)}
    (output / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
