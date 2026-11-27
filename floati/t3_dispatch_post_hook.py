"""Record the outcome of an opted-in Claude delegate call."""
import hashlib
import json
import os
from pathlib import Path

from .errors import FloatiError
from .root import FloatiRoot
from .t3_dispatch import ChildLink, record_child_link
from .t3_dispatch_hook import MATCHER, _read_input, coordinate
from .t3_dispatch_records import T3_RESULT_ID, _match

POST_EVENTS = ("PostToolUse", "PostToolUseFailure")


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate response key")
    return result


def _response_link(response):
    if isinstance(response, str):
        try:
            result = json.loads(response, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            result = None
        if isinstance(result, dict):
            task_id, child_id = result.get("taskId"), result.get("childThreadId")
            if _match(task_id, T3_RESULT_ID) and _match(child_id, T3_RESULT_ID):
                return ChildLink("linked", task_id, child_id)
    return ChildLink("unparseable-response")


def main(root_path, tenant, event):
    try:
        payload = _read_input()
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        if payload.get("tool_name") != MATCHER or payload.get("hook_event_name") != event:
            return
        parent = coordinate(os.environ.get("T3CODE_THREAD_ID"))
        call = coordinate(payload.get("tool_use_id"))
        if not parent or not call:
            raise ValueError("missing dispatch coordinate")
        root = FloatiRoot.open_direct_home(Path(root_path))
        if root.path != Path(root_path) or root.tenant_id != tenant:
            raise ValueError("dispatch ledger identity changed")
        # Keep the installed PreToolUse adapter byte-identical. This is its
        # v1 dispatch key encoding for a known parent and per-call ID.
        key = hashlib.sha256(json.dumps([parent, call]).encode()).hexdigest()
        link = (ChildLink("call-failed") if event == "PostToolUseFailure"
                else _response_link(payload.get("tool_response")))
        record_child_link(root, dispatch_key=key, parent_thread_id=parent, link=link)
    except (OSError, FloatiError, RecursionError, ValueError):
        print(json.dumps({"systemMessage": "t3_child_link_unavailable. Floati could not save the child link."}))
