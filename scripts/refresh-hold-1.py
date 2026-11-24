#!/usr/bin/python3
"""Refresh registry help/copy, bundle manifest and derived H1-F1 reader maps."""
from pathlib import Path
import argparse
import json
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--census', action='store_true')
    args = parser.parse_args()
    if args.census:
        from tests.test_rem_2 import census, _set_digest, PINNED_BOUND, PINNED_MIXED, PINNED_UNBOUND
        derived = census(ROOT)
        counts = {key: (value if isinstance(value, int) else len(value)) for key, value in derived.items()}
        digests = {key: _set_digest(derived[key]) for key in ('protocol', 'kind_none', 'other', 'integrity', 'durability')}
        print(json.dumps({'counts': counts, 'digests': digests,
                          'new_bound': sorted(derived['bound'] - PINNED_BOUND),
                          'new_unbound': sorted(derived['unbound'] - PINNED_UNBOUND),
                          'new_mixed': sorted(derived['mixed'] - PINNED_MIXED)}, indent=2))
        return 0
    from floati import command_codegen
    from floati.copy import copy_ledger_markdown
    from scripts import regen_bundle_manifest, regen_h1_f1_reader_maps
    command_codegen.main(['--write'])
    (ROOT / 'docs/COPY-LEDGER.md').write_text(copy_ledger_markdown(), encoding='utf-8')
    sys.argv = ['regen_bundle_manifest.py', '--root', str(ROOT), '--write']
    if regen_bundle_manifest.main():
        return 1
    sys.argv = ['regen_h1_f1_reader_maps.py', '--root', str(ROOT), '--write']
    return regen_h1_f1_reader_maps.main()


if __name__ == '__main__':
    raise SystemExit(main())
