"""Host-boundary probe followed by a protocol-only provider fake; no live SDK."""
import json
import os
from pathlib import Path
import runpy
import sys


harness, plan_path = sys.argv[1:3]
plan = json.loads(Path(plan_path).read_text())
results = {}
for name, target in plan["forbidden"].items():
    try:
        with open(target, "wb") as stream:
            stream.write(b"provider escaped its lease\n")
    except OSError as exc:
        results[name] = {"errno": exc.errno, "written": False}
    else:
        results[name] = {"errno": None, "written": True}
allowed = {}
for name, target in plan["allowed"].items():
    try:
        Path(target).write_text("allowed native provider write\n")
    except OSError as exc:
        allowed[name] = {"errno": exc.errno, "written": False}
    else:
        allowed[name] = {"errno": None, "written": True}
report = {"forbidden": results, "allowed": allowed,
          "home": os.environ.get("HOME"), "tmpdir": os.environ.get("TMPDIR")}
Path(plan["report"]).write_text(json.dumps(report))
sys.argv = [harness, *sys.argv[3:]]
runpy.run_path(harness, run_name="__main__")
