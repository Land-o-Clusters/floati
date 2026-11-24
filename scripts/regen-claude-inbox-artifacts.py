"""Regenerate the Claude row/version subprocess pin and validate README claims.

Uses the existing renderer and AST census; never starts a native client.
Unrelated subprocess drift refuses rather than advancing a broad baseline.
"""
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from tests.test_no_listener_fence import _subprocess_population
    kinds, shapes = Counter(), Counter()
    for path in sorted((ROOT / "floati").rglob("*.py")):
        observed_kinds, observed_shapes = _subprocess_population(ast.parse(path.read_text()))
        kinds.update(observed_kinds)
        shapes.update(observed_shapes)
    if dict(kinds) != {"run": 52, "Popen": 16, "os-family": 3} or dict(shapes) != {"mixed": 41, "dynamic": 30}:
        raise ValueError("subprocess drift outside the reviewed installed-client version read")
    path = ROOT / "tests/test_no_listener_fence.py"
    text = path.read_text()
    tree = ast.parse(text)
    owner, = [node for node in tree.body if isinstance(node, ast.ClassDef)
              and node.name == "WholeProductNoListenerFenceTests"]
    method, = [node for node in owner.body if isinstance(node, ast.FunctionDef)
               and node.name == "test_subprocess_population_is_pinned"]
    calls = [node.value for node in method.body if isinstance(node, ast.Expr)
             and isinstance(node.value, ast.Call)
             and isinstance(node.value.func, ast.Attribute)
             and node.value.func.attr == "assertEqual"]
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for call, observed in reversed(list(zip(calls, (dict(kinds), dict(shapes))))):
        node = call.args[0]
        if not isinstance(node, ast.Dict):
            raise ValueError("subprocess pin is no longer a literal dictionary")
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        text = text[:start] + json.dumps(observed) + text[end:]
    path.write_text(text)

    renderer = runpy.run_path(str(ROOT / "scripts/capability-matrix-render.py"))
    dataset = renderer["_load"](ROOT / "docs/capability-matrix.v0.json")
    rendered = renderer["render_compact"](dataset).strip()
    path = ROOT / "README.md"
    text = path.read_text()
    begin = text.index("capability-matrix:begin")
    begin = text.index("-->", begin) + len("-->\n")
    end = text.index("<!-- capability-matrix:end -->", begin)
    path.write_text(text[:begin] + rendered + "\n" + text[end:])

    from floati.readme_claims import readme_claim_census
    claims = readme_claim_census(ROOT)
    count, linked = len(claims), sum(claim["linked"] for claim in claims)
    digest = hashlib.sha256("\n".join(claim["claim_digest"] for claim in claims).encode()).hexdigest()
    if count != linked:
        raise ValueError("README claims must carry local receipt links before repinning")
    # Receipt coverage is enforced behaviorally. A new linked observation
    # must not require this producer to rewrite a test's count/digest literal.
    print(json.dumps({"status": "written", "subprocesses": dict(kinds),
        "argv_shapes": dict(shapes), "matrix_source": "docs/capability-matrix.v0.json",
        "readme_claims": count, "readme_linked": linked, "readme_digest": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
