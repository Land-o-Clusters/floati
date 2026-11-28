#!/usr/bin/env python3
"""Explicit README census; --seed-allowlist banks only current unlinked units.

This is a report/seed tool, not another release gate. Verification remains
python3 -m floati.release. The seed reason deliberately requests owner review.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from floati.readme_claims import ALLOWLIST, readme_claim_census


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed-allowlist', action='store_true')
    args = parser.parse_args()
    claims = readme_claim_census(ROOT)
    path = ROOT / ALLOWLIST
    if args.seed_allowlist:
        rows = {c['claim_digest']: {
            'claim_digest': c['claim_digest'], 'excerpt': c['text'][:80],
            'reason': 'pre-census: owner review',
        } for c in claims if not c['linked'] and not c.get('requires_replay')}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(list(rows.values()), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    allowed = {r['claim_digest'] for r in json.loads(path.read_text(encoding='utf-8'))} if path.exists() else set()
    print(json.dumps({
        'claims': claims, 'population': len(claims),
        'linked': sum(c['linked'] for c in claims),
        'allowlisted': sum(not c['linked'] and not c.get('requires_replay')
                           and c['claim_digest'] in allowed for c in claims),
        'population_digest': hashlib.sha256('\n'.join(c['claim_digest'] for c in claims).encode()).hexdigest(),
    }, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
