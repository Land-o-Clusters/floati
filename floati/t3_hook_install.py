"""Preview and publish just the Claude delegate hook in explicit settings."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import stat
import sys

from . import t3_dispatch_hook
from .claude_hook_install import _decode, _settings_path, _write
from .codex_hook_install import _rewrite_hook_group
from .errors import DurabilityFailure, ProtocolRefusal
from .jsonl import _locked_path
from .root import FloatiRoot
from .t3_dispatch_post_hook import POST_EVENTS

EVENT = "PreToolUse"
MATCHER = t3_dispatch_hook.MATCHER
MAX_POLICY_BYTES = 16384


def _policy(path):
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise ProtocolRefusal("t3_gate_list_invalid", "Use an explicit canonical gate list file.")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_POLICY_BYTES + 1)
        if len(data) > MAX_POLICY_BYTES:
            raise ValueError
        # Share the installer decoder's duplicate-key and non-JSON checks.
        _, document = _decode(b'{"gates":' + data + b"}")
        gates = document["gates"]
        if not isinstance(gates, list) or len(gates) > 64:
            raise ValueError
        selectors = set()
        for gate in gates:
            if not isinstance(gate, dict) or not {"provider_instance"} <= gate.keys() <= {"provider_instance", "model"}:
                raise ValueError
            provider = t3_dispatch_hook.coordinate(gate["provider_instance"])
            model = gate.get("model")
            if not provider or (model is not None and not t3_dispatch_hook.coordinate(model)):
                raise ValueError
            if (provider, model) in selectors:
                raise ValueError
            selectors.add((provider, model))
        return [{"provider_instance": provider, "model": model}
                for provider, model in sorted(selectors, key=lambda pair: (pair[0], pair[1] or ""))]
    except (ValueError, OSError, RecursionError, ProtocolRefusal) as exc:
        raise ProtocolRefusal("t3_gate_list_invalid",
                              "List up to 64 unique provider instances and optional model qualifiers.") from exc


def _plan(path, runtime, root, gate_path):
    gates = _policy(gate_path)
    interpreter = Path(sys.executable).resolve(strict=True)
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise ProtocolRefusal("t3_hook_runtime_invalid", "Use a canonical Python executable.")
    adapter = Path(t3_dispatch_hook.__file__).read_text(encoding="utf-8")
    launch = [str(interpreter), "-I", "-S", "-B", "-c"]
    coordinates = [str(runtime), str(root.path), root.tenant_id]
    commands = {EVENT: shlex.join([*launch, adapter, *coordinates, json.dumps(gates)])}
    post_adapter = ("import sys;sys.path.insert(0,sys.argv.pop(1));"
                    "from floati.t3_dispatch_post_hook import main;main(*sys.argv[1:])")
    commands.update({event: shlex.join([*launch, post_adapter, *coordinates, event])
                     for event in POST_EVENTS})
    blocks = {event: {"matcher": MATCHER, "hooks": [{"type": "command", "command": command, "timeout": 5}]}
              for event, command in commands.items()}
    present = path.is_file()
    before = path.read_bytes() if present else b"{}\n"
    text, doc = _decode(before)
    if "hooks" in doc:
        after = before
        for event, block in blocks.items():
            groups = doc["hooks"].get(event, [])
            if not isinstance(groups, list):
                raise ProtocolRefusal("t3_hook_settings_invalid", f"Claude {event} must be an array.")
            exact = [group for group in groups if isinstance(group, dict) and group.get("matcher") == MATCHER]
            if exact == [block]:
                continue
            if exact:
                raise ProtocolRefusal("t3_hook_matcher_exists",
                                      "Review the existing T3 delegate hook before installing a different one.")
            after = _rewrite_hook_group(after.decode("utf-8"), event, block)
    else:
        start = text.index("{") + 1
        member = '"hooks":' + json.dumps({event: [block] for event, block in blocks.items()},
                                        separators=(",", ":"))
        after = (text[:start] + member + ("," if doc else "") + text[start:]).encode()
    evidence = {
        "settings": str(path), "runtime": str(runtime), "root": str(root.path),
        "tenant_id": root.tenant_id, "matcher": MATCHER, "gates": gates, "timeout_seconds": 5,
        "previous_digest": hashlib.sha256(before).hexdigest() if present else None,
        "proposed_digest": hashlib.sha256(after).hexdigest(),
    }
    return evidence, after


def install_t3_hook(*, root, runtime, settings, gate_path, confirmation):
    path = _settings_path(settings)
    runtime = Path(runtime)
    launcher = runtime / "scripts/floati"
    if not runtime.is_absolute() or launcher.resolve() != launcher or not launcher.is_file():
        raise ProtocolRefusal("t3_hook_runtime_invalid", "Name a canonical Floati runtime with its launcher.")
    root = FloatiRoot.open_direct_home(Path(root))
    try:
        evidence, after = _plan(path, runtime, root, Path(gate_path))
        token = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        if confirmation is None:
            return {"state": "preview", "confirmation": token, **evidence}
        if not secrets.compare_digest(confirmation.encode(), token.encode()):
            raise ProtocolRefusal("t3_hook_confirmation_changed", "Preview the T3 hook again and use its confirmation.")
        # Serialize this writer for the exact settings coordinate and re-read
        # the preview under the lock before touching settings bytes.
        lock = path.with_name(path.name + ".floati-t3.lock")
        def open_lock():
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("settings lock is not an ordinary file")
                return os.fdopen(fd, "a+b")
            except BaseException:
                os.close(fd)
                raise

        with _locked_path(lock, exclusive=True, relative="state/t3-hook-install.lock",
                          _open_handle=open_lock):
            _settings_path(path)
            current, _ = _plan(path, runtime, root, Path(gate_path))
            if current != evidence:
                raise ProtocolRefusal("t3_hook_confirmation_changed", "Preview the T3 hook again and use its confirmation.")
            if evidence["previous_digest"] != evidence["proposed_digest"]:
                _write(path, after)
        return {"state": "installed", **evidence}
    except OSError as exc:
        raise DurabilityFailure("t3_hook_install_io_failed", "Could not read or publish the T3 hook settings.") from exc
