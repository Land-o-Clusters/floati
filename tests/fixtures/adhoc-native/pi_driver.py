"""Fresh interpreter for fixture-only native Pi adapter qualification."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from floati.adapters.pi import PiRpcAdapter
from floati.errors import ProtocolRefusal
from floati.worker_errors import WorkerAdapterFailure

context = json.loads(Path(sys.argv[1]).read_text())
store = Path(sys.argv[3])
adapter = PiRpcAdapter((str(Path(sys.executable).resolve()),
    str(Path(__file__).with_name("pi_provider.py")), "--fixture-mode", sys.argv[2],
    "--fixture-store", str(store)))
events = []


def emit(event):
    events.append(event)
    if event["event"] == "opened":
        (store / "opened-ack").write_text("durably accepted")
        return {**event, "event": "opened_ack",
                "opened_record_id": "adhoc-native-opened-0199e000000070008000000000000001"}


handle = None
try:
    setter = getattr(adapter, "set_native_thread_context", None)
    if setter is not None:
        setter(context, emit)
    item = {"id": context["work_item_id"],
        "workspace": context["workspace_binding"]["workspace"],
        "adhoc_binding": context["workspace_binding"],
        "title": "Record the next fixture proof"}
    handle = adapter.spawn(item, deadline_seconds=3)
    adapter.drive(handle, item, deadline_seconds=3)
    result = {"code": "ok", "events": events}
except (WorkerAdapterFailure, ProtocolRefusal) as failure:
    result = {"code": failure.code, "events": events}
finally:
    adapter.cancel()
print(json.dumps(result))
