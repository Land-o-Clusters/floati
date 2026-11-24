"""One fresh adapter interpreter for the fixture-only resume contract."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from floati.adapters.claude import ClaudeHeadlessAdapter
from floati.worker_errors import WorkerAdapterFailure

context = json.loads(Path(sys.argv[1]).read_text())
adapter = ClaudeHeadlessAdapter((sys.executable, str(Path(__file__).with_name("claude_provider.py")),
                                "--fixture-mode", sys.argv[2]))
events = []
try:
    setter = getattr(adapter, "set_native_thread_context", None)
    if setter is not None:
        setter(context, events.append)
    item = {"id": context["work_item_id"], "workspace": context["workspace_binding"]["workspace"],
            "title": "Record the next fixture proof"}
    handle = adapter.spawn(item, deadline_seconds=3)
    adapter.drive(handle, item, deadline_seconds=3)
    print(json.dumps({"code": "ok", "events": events, "reaped": handle.process.poll() is not None}))
except WorkerAdapterFailure as exc:
    print(json.dumps({"code": exc.code, "events": events}))
finally:
    adapter.cancel()
