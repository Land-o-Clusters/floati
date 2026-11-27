"""Small Claude hook adapter, embedded in the confirmed settings preview.

Keep this module standard-library only. The installed adapter can classify the
requested target even if the Floati launcher is missing or its check times out.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
import uuid

MATCHER = "mcp__t3-code__delegate_task"
IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}"
NODE = r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?"
HOLD_REASONS = {"t3_project_hold", "t3_quiet_window"}
BRIEF = r"(?<![A-Za-z0-9_./~+-])(?:~?/|\.{1,2}/)?[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)+\.md(?![A-Za-z0-9_./+-])"
MAX_INPUT = 1024 * 1024
CHECK_SECONDS = 1.5
GAP_SECONDS = 1.5
GRANT = "Run python3 -m floati.t3_gate grant from a controlling terminal."
REASONS = {
    "t3_grant_required": GRANT,
    "t3_check_unavailable": "The Floati dispatch check is unavailable. " + GRANT,
    "t3_receipt_unavailable": "Floati could not save the dispatch receipt.",
    "t3_input_unavailable": "Floati could not identify the requested dispatch.",
}


def coordinate(value):
    return value if isinstance(value, str) and re.fullmatch(IDENTIFIER, value) else None


def _object(value):
    return value if isinstance(value, dict) else {}


def _declaration(title, field, pattern, default=None):
    values = re.findall(r"\[floati:" + field + r"=([^\]]*)\]", title)
    if not values:
        return default
    if len(values) == 1 and re.fullmatch(pattern, values[0]):
        return values[0]
    return None


def _read_input():
    deadline = time.monotonic() + 0.5
    data = bytearray()
    while len(data) <= MAX_INPUT:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
            raise TimeoutError
        chunk = os.read(0, min(65536, MAX_INPUT + 1 - len(data)))
        if not chunk:
            return json.loads(data)
        data.extend(chunk)
    raise ValueError("oversize hook input")


def _facts(payload, gates):
    call = _object(payload.get("tool_input"))
    target = _object(call.get("target"))
    provider = coordinate(target.get("providerInstanceId"))
    model = coordinate(target.get("model"))
    gated = any(provider == gate["provider_instance"]
                and (gate["model"] is None or model is None or gate["model"] == model)
                for gate in gates)
    parent = coordinate(os.environ.get("T3CODE_THREAD_ID"))
    use_id = coordinate(payload.get("tool_use_id"))
    key = json.dumps([parent, use_id]) if parent and use_id else uuid.uuid4().hex
    task = call.get("task")
    encoded = task.encode("utf-8", "surrogatepass") if isinstance(task, str) else None
    briefs = set(re.findall(BRIEF, task)) if encoded else set()
    brief = briefs.pop() if len(briefs) == 1 else None
    options = target.get("options")
    title = call.get("title")
    title = title if isinstance(title, str) else ""
    role = coordinate(call.get("role"))
    dispatch_class = _declaration(title, "class", r"load|measurement",
                                  "load" if role in ("implementation", "test") else None)
    base_ref = _declaration(title, "base", IDENTIFIER, coordinate(call.get("baseRef")))
    valid = (isinstance(payload.get("tool_input"), dict) and encoded is not None
             and isinstance(call.get("target", {}), dict)
             and (options is None or isinstance(options, (dict, list)))
             and (target.get("providerInstanceId") is None or provider is not None)
             and (target.get("model") is None or model is not None))
    return {
        "dispatch_key": hashlib.sha256(key.encode()).hexdigest(),
        "parent_thread_id": parent,
        "provider_instance": provider,
        "requested_model": model,
        "role": role,
        "isolation": coordinate(call.get("isolation")),
        "task_digest": hashlib.sha256(encoded).hexdigest() if encoded is not None else None,
        "brief_path": brief if brief is not None and len(brief) <= 256 else None,
        "base_ref": base_ref,
        "dispatch_class": dispatch_class,
        "hold_node": _declaration(title, "node", NODE),
        "gated": gated,
        "valid": valid,
    }


def _worker(argv, request, timeout):
    try:
        result = subprocess.run(argv, input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=True)
        verdict = json.loads(result.stdout)
        if verdict in (
            ["allowed", "t3_ungated"], ["allowed", "t3_grant_consumed"],
            ["denied", "t3_grant_required"], ["denied", "t3_check_unavailable"],
            ["allowed_with_gap", "t3_check_unavailable"], ["allowed_with_gap", "t3_target_unresolved"],
        ):
            return verdict
        if (isinstance(verdict, list) and len(verdict) == 3 and verdict[0] == "denied"
                and isinstance(verdict[1], str) and verdict[1] in HOLD_REASONS
                and isinstance(verdict[2], dict)):
            return verdict
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _reply(decision, reason, hold=None, *, receipt_missing=False):
    output = {}
    if decision == "denied":
        if hold is None:
            detail = REASONS[reason]
        else:
            # Quote the operator's reason so control characters stay in the receipt.
            label = "Quiet window" if reason == "t3_quiet_window" else "Project hold"
            detail = f"{label} {hold['id']} covers this load dispatch."
            if hold["detail"] is not None:
                detail += " " + json.dumps(hold["detail"], ensure_ascii=True) + "."
            if hold["expected_release"] is not None:
                detail += f" Expected release {hold['expected_release']}."
        output["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": reason + ". " + detail,
        }
    if receipt_missing:
        code = reason if reason == "t3_input_unavailable" else "t3_receipt_unavailable"
        output["systemMessage"] = code + ". " + REASONS[code]
    if output:
        print(json.dumps(output))


def main():
    runtime, root, tenant, policy = sys.argv[1:]
    try:
        payload = _read_input()
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        if (payload.get("tool_name") != MATCHER
                or payload.get("hook_event_name") != "PreToolUse"):
            return
        facts = _facts(payload, json.loads(policy))
    except (OSError, ValueError, RecursionError):
        _reply("allowed_with_gap", "t3_input_unavailable", receipt_missing=True)
        return

    request = json.dumps(facts).encode()
    launcher = os.path.join(runtime, "scripts", "floati")
    verdict = None
    if facts["valid"]:
        verdict = _worker([launcher, "hook", "t3-dispatch", root, tenant], request, CHECK_SECONDS)
    if verdict is None:
        # Separate entry so a broken launcher can still leave governed evidence.
        recovery = ("import sys;sys.path.insert(0,sys.argv.pop(1));"
                    "from floati.t3_dispatch import worker;worker(sys.argv[1:],gap=True)")
        verdict = _worker([sys.executable, "-I", "-S", "-B", "-c", recovery, runtime, root, tenant],
                          request, GAP_SECONDS)
    if verdict is None:
        _reply("denied" if facts["gated"] else "allowed_with_gap",
               "t3_check_unavailable", receipt_missing=True)
    else:
        _reply(*verdict)


if __name__ == "__main__":
    main()
