"""Hostile fake provider: report each filesystem attempt without model calls.

Each request entry is a target path (write its bytes) or an object
{"path": target, "rename_from": source}: write the source, then rename it
onto the target, which replaces the target's directory entry.
"""
import json
import os
from pathlib import Path
import sys


def attempt(request):
    try:
        if isinstance(request, dict):
            Path(request["rename_from"]).write_bytes(b"provider replacement")
            os.rename(request["rename_from"], request["path"])
        else:
            Path(request).write_bytes(b"provider write")
    except OSError as exc:
        return {"status": "refused", "errno": exc.errno}
    return {"status": "ok"}


request = json.load(sys.stdin)
result = {name: attempt(target) for name, target in request.items()}
print(json.dumps(result))
