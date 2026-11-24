"""Regenerate only the reviewed Codex identity-check subprocess population.

The existing fence owns classification. Exactly one local --version site in
the new repair module may advance its pins; all unrelated drift refuses.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    from tests.test_no_listener_fence import _subprocess_population

    kinds, shapes = Counter(), Counter()
    reviewed = ROOT / 'floati/codex_binding_repair.py'
    for path in sorted((ROOT / 'floati').rglob('*.py')):
        observed_kinds, observed_shapes = _subprocess_population(ast.parse(path.read_text()))
        kinds.update(observed_kinds)
        shapes.update(observed_shapes)
        if path == reviewed:
            delta_kinds, delta_shapes = Counter(observed_kinds), Counter(observed_shapes)
    if delta_kinds != {'run': 1} or delta_shapes != {'mixed': 1}:
        raise ValueError('reviewed_codex_version_site_changed')
    if (kinds != {'Popen': 16, 'run': 53, 'os-family': 3}
            or shapes != {'mixed': 42, 'dynamic': 30}):
        raise ValueError('subprocess_drift_outside_reviewed_codex_version_site')
    target = ROOT / 'tests/test_no_listener_fence.py'
    text = target.read_text()
    owner, = [node for node in ast.parse(text).body if isinstance(node, ast.ClassDef)
        and node.name == 'WholeProductNoListenerFenceTests']
    method, = [node for node in owner.body if isinstance(node, ast.FunctionDef)
        and node.name == 'test_subprocess_population_is_pinned']
    pins = [node.value.args[0] for node in method.body if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == 'assertEqual']
    if len(pins) != 2 or any(not isinstance(node, ast.Dict) for node in pins):
        raise ValueError('subprocess_pin_shape_changed')
    pinned = tuple(Counter(ast.literal_eval(node)) for node in pins)
    observed = (kinds, shapes)
    if pinned == observed:
        print(json.dumps({'status': 'unchanged', 'kinds': kinds, 'argv_shapes': shapes}, sort_keys=True))
        return 0
    if pinned != (kinds - delta_kinds, shapes - delta_shapes):
        raise ValueError('subprocess_drift_outside_reviewed_codex_version_site')
    if not args.write:
        print('DRIFT: reviewed Codex --version subprocess population; use --write')
        return 1
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    for node, value in reversed(list(zip(pins, observed))):
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        text = text[:start] + json.dumps(dict(value), sort_keys=True) + text[end:]
    target.write_text(text)
    print(json.dumps({'status': 'written', 'kinds': kinds, 'argv_shapes': shapes}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
