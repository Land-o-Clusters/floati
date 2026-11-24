"""Exec canonical unittest with a bounded owned-process descriptor budget.

Python 3.9's fork/exec close_fds path can scan an oversized inherited soft
limit before each fixture Git process starts. Bound only this test process;
leave the hard limit and the operator's parent/sibling processes unchanged.
The exec preserves the terminal's original process-control handle.
"""
from pathlib import Path
import json
import os
import re
import resource
import sys


root = Path(__file__).resolve().parents[1]
if Path.cwd().resolve() != root:
    raise SystemExit("qualification must run from its own worktree root")
arguments = sys.argv[1:]
modules = [argument for argument in arguments if argument not in {"-v", "-f"}]
if not modules or any(not re.fullmatch(r"tests\.test_[a-z0-9_]+(?:\.[A-Za-z0-9_]+)*", module)
                      for module in modules):
    raise SystemExit("name explicit repository unittest modules or test selectors")
for module in modules:
    path = root / ("/".join(module.split(".")[:2]) + ".py")
    if not path.is_file() or path.is_symlink():
        raise SystemExit("qualification selector is not a repository test module")
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
bounded = 4096 if soft == resource.RLIM_INFINITY else min(soft, 4096)
resource.setrlimit(resource.RLIMIT_NOFILE, (bounded, hard))
print(json.dumps({"test_descriptor_budget": {"inherited_soft": soft,
      "effective_soft": bounded, "hard_unchanged": hard}}), file=sys.stderr, flush=True)
os.execv("/usr/bin/python3", ["/usr/bin/python3", "-B", "-m", "unittest", *arguments])
