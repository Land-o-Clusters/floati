"""Owner-run zcode bus/work measurement; discovery runs fakes only.

One explicit --live capture signs in through the owner's native OAuth flow,
then spends at most one measured native invocation in the same scratch HOME.
It never copies credentials or provider state from another HOME or store.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from floati.adapters.zcode import (
    ZcodeAdapter, resolve_zcode_command, zcode_entry_testimony,
    zcode_launch_environment,
)
from floati.admin_registry import RegistryAdminBackend
from floati.cursor import SparseCursor
from floati.events import EVENT_KINDS, EventLog
from floati.host_paths import HOST_TEMP_ROOT_VARIABLE, capture_temporary_parent
from floati.ids import uuid7_hex
from floati.jsonl import read_records_snapshot
from floati.node_wizard import NodeWizard
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts, WorkerRunner


DEFAULT_COMMAND = tuple(ZcodeAdapter._default_profile().command)
SIGNIN_ARGUMENTS = ("login", "zai", "--no-browser", "--json")
PREFLIGHT_ARGUMENTS = ("login", "floati-preflight", "--no-browser", "--json")


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ScratchCredentialRefusal(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_scratch_credentials(home, paths):
    """Inspect only metadata: no credential contents or external stores are read.

    Reported paths, HOME descendants, and all link targets must remain inside
    the freshly allocated HOME. Shared inodes are refused even without a
    discoverable external pathname. No foreign filesystem scan is performed.
    """
    home = Path(home).absolute()
    if home.is_symlink() or home.resolve() != home or not home.is_dir():
        raise ScratchCredentialRefusal("credential_home_not_canonical")
    checked = []
    reported = [Path(path) for path in paths]
    for candidate in [*reported, *home.rglob("*")]:
        try:
            candidate.absolute().relative_to(home)
            candidate.resolve().relative_to(home)
        except ValueError:
            raise ScratchCredentialRefusal("credential_outside_scratch_home")
        # A lexical '..' can disguise an escape even when the final path is
        # currently absent; canonical paths are the only accepted testimony.
        if ".." in candidate.parts or candidate.is_symlink():
            raise ScratchCredentialRefusal("credential_outside_scratch_home")
        metadata = candidate.lstat()
        if candidate in reported and not stat.S_ISREG(metadata.st_mode):
            raise ScratchCredentialRefusal("credential_file_not_ordinary")
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise ScratchCredentialRefusal("credential_shared_inode")
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise ScratchCredentialRefusal("credential_file_not_ordinary")
        checked.append(str(candidate))
    return sorted(set(checked))


def remove_scratch_credentials(home):
    """Remove the complete authentication HOME and retain a typed absence."""
    home = Path(home)
    try:
        shutil.rmtree(home)
        if home.exists() or home.is_symlink():
            raise OSError("scratch HOME still present")
    except OSError:
        return {"state": "cannot-measure", "code": "credential_removal_unproved"}
    return {"state": "removed", "home": str(home)}


def preflight_native_entry(home):
    """Prove provider bootstrap without OAuth, a prompt, or a model request.

    Installed ZCode's help/version flags bypass provider initialization. An
    invalid provider instead reaches runLoginCommand's usage refusal after
    initialization and before it imports or invokes the OAuth implementation.
    The declared paths and bundle version are observed anew for each use.
    """
    native = zcode_entry_testimony(DEFAULT_COMMAND)
    command = native["resolved_command"]
    result = {"state": "cannot-measure", "native_entry": native,
              "argv": [*command, *PREFLIGHT_ARGUMENTS], "exit_code": None}
    try:
        with clean_environment(home):
            environment = zcode_launch_environment(Path(command[1])) if len(command) == 2 else None
            process = subprocess.run(result["argv"], cwd=home, env=environment,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, timeout=15)
        result["exit_code"] = process.returncode
        # Only this known parser refusal establishes the no-auth path. Other
        # output remains an unqualified entry rather than triggering a login.
        if (process.returncode == 1 and not process.stdout.strip()
                and "Error: Usage: zcode login [zai|bigmodel] [--no-browser]" in process.stderr):
            result["state"] = "ready"
        else:
            result["code"] = "provider_or_entrypoint_failed"
            result["diagnostic"] = process.stderr[-2000:]
    except subprocess.TimeoutExpired:
        result["code"] = "native_preflight_deadline"
    except OSError:
        result["code"] = "provider_or_entrypoint_failed"
    return result


def native_owner_signin(home, *, command=None):
    """Native no-browser OAuth; authorization URL stays on the owner's terminal.

    The landed CLI prints its URL to stderr and final JSON to stdout. Neither
    stream is persisted. Only the ready status and path metadata leave here.
    The login command has no model prompt and creates no measured invocation.
    """
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ScratchCredentialRefusal("owner_signin_requires_terminal")
    command = resolve_zcode_command(DEFAULT_COMMAND if command is None else command)
    environment = zcode_launch_environment(Path(command[1])) if len(command) == 2 else None
    process = subprocess.Popen([*command, *SIGNIN_ARGUMENTS], cwd=home, env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=None, text=True)
    try:
        stdout, _ = process.communicate(timeout=360)
        if process.returncode != 0:
            raise ScratchCredentialRefusal("owner_signin_failed")
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError):
            raise ScratchCredentialRefusal("owner_signin_result_malformed")
        if not isinstance(payload, dict):
            raise ScratchCredentialRefusal("owner_signin_result_malformed")
        if payload.get("status") != "ready":
            raise ScratchCredentialRefusal("owner_signin_not_ready")
        if not all(isinstance(payload.get(key), str) and payload[key].strip()
                   for key in ("credentialsPath", "configPath")):
            raise ScratchCredentialRefusal("owner_signin_result_malformed")
        return {key: payload[key] for key in ("status", "credentialsPath", "configPath")}
    except subprocess.TimeoutExpired:
        raise ScratchCredentialRefusal("owner_signin_deadline")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout is not None:
            process.stdout.close()


def recorded_native_command(receipt):
    """Replay captured identity without consulting today's installed app.

    Old receipts predate native-entry testimony and keep their original
    declaration. New receipts bind both invocations to the observed canonical
    command; an upgrade's version or digest is never compared with a pin.
    """
    native = receipt.get("native_entry")
    if native is None:
        return list(DEFAULT_COMMAND)
    if not isinstance(native, dict) or native.get("declared_command") != list(DEFAULT_COMMAND):
        return None
    command = native.get("resolved_command")
    if (not isinstance(command, list) or len(command) != 2
            or not all(isinstance(part, str) and Path(part).is_absolute()
                       and ".." not in Path(part).parts for part in command)):
        return None
    return command


def owner_signin_proved(receipt):
    """Replay checks only retained metadata; it never accesses a removed HOME."""
    try:
        signin, invocation = receipt["owner_signin"], receipt["invocation"]
        home = Path(signin["home"])
        paths = signin["credential_paths"]
        return (signin["actor"] == "owner" and signin["home_kind"] == "scratch"
                and signin["flow"] == "native_login_zai"
                and recorded_native_command(receipt) is not None
                and signin["argv"] == [*recorded_native_command(receipt), *SIGNIN_ARGUMENTS]
                and signin["state"] == "ready" and signin["credential_copy"] == "none"
                and signin["path_validation"] == "canonical_metadata_only"
                and home.is_absolute() and ".." not in home.parts
                and home.name == "home" and home.parent.name.startswith("floati-zcode-conformance-")
                and str(home) == invocation["home"] and bool(paths)
                and all(Path(path).is_absolute() and ".." not in Path(path).parts
                        and Path(path).is_relative_to(home) for path in paths)
                and datetime.fromisoformat(signin["ended_at"]) <= datetime.fromisoformat(invocation["started_at"])
                and receipt["cleanup"]["credential_material"] == {"state": "removed", "home": str(home)})
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def classify_receipt(receipt):
    """Pure replay: every claim requires captured independent coordinates."""
    result = {"status": "cannot-measure", "surface": "cannot-measure:native_result_incomplete",
              "bus": "cannot-measure:bus_round_trip_unproved",
              "work": "cannot-measure:worker_round_trip_unproved", "live_conformance": False}
    try:
        invocation = receipt["invocation"]
        if invocation.get("exit_code") != 0:
            result["surface"] = "cannot-measure:provider_or_entrypoint_failed"
        else:
            payload = json.loads(invocation.get("stdout", ""))
            usage = payload.get("usage", {})
            if (all(isinstance(payload.get(key), str) and payload[key].strip()
                    for key in ("sessionId", "traceId", "turnId", "response"))
                    and usage.get("source") == "provider"
                    and type(usage.get("inputTokens")) is int and usage["inputTokens"] > 0
                    and type(usage.get("outputTokens")) is int and usage["outputTokens"] > 0):
                result["surface"] = "native_success"
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    try:
        bus = receipt["bus"]
        message, delivery, ack = bus["message"], bus["delivery"], bus["ack"]
        records = bus["ledger_records"]
        if (message["kind"] == "message_envelope" and message["sender"] == "zc-source"
                and message["recipient"] == "zc-recipient" and message["note"] == receipt["nonce"]
                and message["idempotency_key"] == receipt["nonce"]
                and message["sha"] == receipt["source_sha"]
                and delivery["kind"] in ("delivery_receipt", "delivered_unpresented")
                and delivery["recipient"] == ack["recipient"] == message["recipient"]
                and delivery["item_ids"] == ack["item_ids"] == [message["id"]]
                and ack["kind"] == "ack_receipt"
                and all(row in records for row in (message, delivery, ack))):
            result["bus"] = "send_delivery_ack_verified"
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    try:
        work = receipt["work"]
        item, terminal = work["item"], work["result"]
        records, artifacts = work["receipts"], work["artifacts"]
        bindings = terminal["artifact_bindings"]
        after, audit = work["work_after"], work["process_audit"]
        if (terminal["transition"] == "complete" and terminal.get("outcome_code") is None
                and after["id"] == item["id"] and after["state"] == "completed"
                and after["artifact_bindings"] == bindings and len(bindings) == 1
                and [r["transition"] for r in records] == ["claim", "spawn", "drive", "bind_artifact", "complete"]
                and all(r["node_id"] == item["owner"] and r["adapter"] == "zcode"
                        and r["work_item_id"] == item["id"] and r["session_id"] == terminal["session_id"]
                        and r["authority_subject"] == terminal["authority_subject"]
                        and r["authority_epoch"] == terminal["authority_epoch"]
                        and r.get("outcome_code") is None
                        for r in records)
                and all(after.get(key, expected) == expected for key, expected in (
                    ("holder", item["owner"]), ("authority_subject", terminal["authority_subject"]),
                    ("authority_epoch", terminal["authority_epoch"])))
                and records[-1] == terminal and records[-2]["artifact_bindings"] == bindings
                and [a["binding"] for a in artifacts] == bindings
                and all(a["content"].strip() == receipt["nonce"] and a["binding"]["doc"] == "answer.txt"
                        and re.fullmatch(r"[0-9a-f]{40}", a["binding"]["sha"]) for a in artifacts)
                and audit["adapter_pid"] is not None and audit["registered_process_groups"]
                and audit["alive_after_cleanup"] == []):
            result["work"] = "complete_with_bound_artifact"
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    try:
        clean = receipt["cleanup"]
        if (result["surface"] == "native_success" and result["bus"] == "send_delivery_ack_verified"
                and result["work"] == "complete_with_bound_artifact"
                and clean["scratch_removed"] is True and clean["workspaces_removed"] is True
                and receipt["home"] == "scratch" and receipt["invocation"]["pid"] > 0
                and receipt["invocation"]["pid"] in receipt["work"]["process_audit"]["registered_process_groups"]
                and receipt["invocation"]["workspace"] == receipt["work"]["item"]["workspace"]
                and (receipt["invocation"]["home"] == str(Path(receipt["invocation"]["workspace"]) / ".floati/home")
                     or receipt.get("owner_signin", {}).get("home") == receipt["invocation"]["home"])
                and receipt["invocation"]["owned_child_reaped"] is True):
            result["status"] = "ok"
            argv = receipt["invocation"]["argv"]
            native_command = recorded_native_command(receipt)
            result["live_conformance"] = (receipt["mode"] == "owner-live"
                and native_command is not None
                and isinstance(argv, list) and len(argv) == len(native_command) + 4
                and argv[:-1] == [*native_command, "--json", "--no-color", "--prompt"]
                and isinstance(argv[-1], str) and bool(argv[-1].strip()))
            if receipt["mode"] == "owner-live" and not owner_signin_proved(receipt):
                result["status"] = "cannot-measure"
                result["authentication"] = "cannot-measure:owner_signin_unproved"
                result["live_conformance"] = False
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return result


@contextmanager
def clean_environment(home):
    """The allowlist carries no provider, auth, Git, hook or config redirectors.

    It keeps Floati's own host temporary root override when one is set: this
    process bound its adapters' workspace parent at import, and the work items
    it derives inside this block must name the same parent.
    """
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(HOME=str(home), PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
                      PYTHONDONTWRITEBYTECODE="1", LANG="en_US.UTF-8")
    if HOST_TEMP_ROOT_VARIABLE in original:
        os.environ[HOST_TEMP_ROOT_VARIABLE] = original[HOST_TEMP_ROOT_VARIABLE]
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


class RecordingZcodeAdapter(ZcodeAdapter):
    """Observe inherited spawn/validation, preserving the landed protocol."""

    def __init__(self, command, *, prepared_home=None):
        super().__init__(command)
        self.prepared_home = prepared_home

    def _home(self, item):
        return self.prepared_home or Path(item["workspace"]) / ".floati/home"

    def _prompt_arguments(self, item):
        # Called by the landed spawn after it creates .floati, before Popen.
        home = self._home(item)
        if self.prepared_home is None:
            home.mkdir(mode=0o700)
        return super()._prompt_arguments(item)

    def spawn(self, item, *, deadline_seconds):
        home = self._home(item)
        with clean_environment(home):
            handle = super().spawn(item, deadline_seconds=deadline_seconds)
        self._capture_path = handle.filesystem_workspace / ".floati/native-capture.json"
        self._capture = {"argv": list(handle.process.args), "pid": handle.process.pid,
                         "home": str(home), "workspace": str(handle.workspace),
                         "started_at": timestamp(), "owned_child_reaped": False}
        self._capture_path.write_text(json.dumps(self._capture))
        return handle

    def _validate_result(self, payload, return_code, stderr_path):
        self._capture.update(stdout=payload.decode("utf-8", errors="replace"),
                             stderr=stderr_path.read_text(errors="replace"), exit_code=return_code,
                             ended_at=timestamp(), owned_child_reaped=True)
        self._capture_path.write_text(json.dumps(self._capture))
        return super()._validate_result(payload, return_code, stderr_path)


def bus_step(root_path, nonce, source_sha):
    """Native-invoked bounded scratch bus operation; no arbitrary root accepted."""
    expected = Path.cwd().resolve() / ".floati/conformance-bus"
    if Path(root_path) != expected or expected.exists() or expected.is_symlink():
        raise ValueError("bus step requires an absent exact workspace scratch root")
    if not (expected.parent.is_dir() and expected.parent.parent.name.startswith("work-")):
        raise ValueError("bus step requires a governed scratch workspace")
    if not re.fullmatch(r"zcode-[0-9a-f]{32}", nonce) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("bus step coordinates are malformed")
    root = FloatiRoot.open_direct_home(expected, create=True)
    registry = Registry(root)
    for node in ("zc-source", "zc-recipient"):
        registry.register(node, "worker")
    events = EventLog(root)
    message = events.send("zc-source", "zc-recipient", "floati", source_sha,
                          "README.md", nonce, idempotency_key=nonce)["message"]
    _, delivery = events.present("zc-recipient")
    ack = SparseCursor(root).ack("zc-recipient", [message["id"]], acting_session_id="zcode-conformance")
    print(json.dumps({"message": message, "delivery": delivery, "ack": ack}))


def read_bus(path):
    if not path.is_dir():
        return {}
    root = FloatiRoot.open_direct_home(path)
    events = read_records_snapshot(root, "events.jsonl", allowed_kinds=EVENT_KINDS)
    deliveries = read_records_snapshot(root, "receipts/deliveries/zc-recipient.jsonl",
                                      allowed_kinds={"delivery_receipt", "delivered_unpresented"})
    acks = read_records_snapshot(root, "receipts/acks/zc-recipient.jsonl", allowed_kinds={"ack_receipt"})
    messages = [row for row in events if row["kind"] == "message_envelope"]
    return {"message": messages[0] if len(messages) == 1 else None,
            "delivery": deliveries[0] if len(deliveries) == 1 else None,
            "ack": acks[0] if len(acks) == 1 else None,
            "ledger_records": events + deliveries + acks}


def capture(output, *, command=None, timeout=30, owner_signin=None):
    """Capture exactly one invocation; a custom command is always fixture-fake."""
    output = Path(output).absolute()
    # Reserve both before creating state or launching any model-capable process.
    human = output.with_suffix(".md")
    if output.exists() or output.is_symlink() or human.exists() or human.is_symlink():
        raise FileExistsError("capture output already exists; refusing invocation replay")
    if type(timeout) not in (int, float) or not 0.01 <= timeout <= 60:
        raise ValueError("timeout must be 0.01 through 60 seconds")
    with output.open("x") as handle:
        handle.write('{"status":"capture_started"}\n')
    human.touch(exist_ok=False)
    receipt = {"artifact_version": 0, "mode": "fixture-fake" if command is not None else "owner-live",
               "home": "scratch", "started_at": timestamp(), "nonce": "zcode-" + uuid7_hex(),
               "source_sha": subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
               "invocation": {}, "bus": {}, "work": {}, "cleanup": {}}
    scratch = Path(tempfile.mkdtemp(prefix="floati-zcode-conformance-", dir=capture_temporary_parent()))
    workspace = None
    signin_home = scratch / "home"
    try:
        signin_home.mkdir(mode=0o700)
        native_command = DEFAULT_COMMAND
        if command is None:
            receipt["preflight"] = preflight_native_entry(signin_home)
            receipt["native_entry"] = receipt["preflight"]["native_entry"]
            if receipt["preflight"]["state"] != "ready":
                raise ScratchCredentialRefusal("provider_or_entrypoint_failed")
            native_command = tuple(receipt["native_entry"]["resolved_command"])
        with clean_environment(signin_home):
            # Authentication is complete before authority is acquired and
            # before the measured worker's short deadline starts. No files
            # are copied: the worker consumes this exact prepared HOME.
            prepared_home = None
            if command is None or owner_signin is not None:
                receipt["owner_signin"] = {
                    "actor": "owner", "home_kind": "scratch", "home": str(signin_home),
                    "flow": "native_login_zai" if owner_signin is None else "fixture-fake",
                    "argv": [*native_command, *SIGNIN_ARGUMENTS] if owner_signin is None else [],
                    "credential_copy": "none", "started_at": timestamp(), "state": "started"}
                signed = (owner_signin(signin_home) if owner_signin is not None
                          else native_owner_signin(signin_home, command=native_command))
                if not isinstance(signed, dict) or signed.get("status") != "ready":
                    raise ScratchCredentialRefusal("owner_signin_not_ready")
                paths = [signed[key] for key in ("credentialsPath", "configPath") if key in signed]
                if not paths or not isinstance(signed.get("credentialsPath"), str):
                    raise ScratchCredentialRefusal("owner_signin_credentials_missing")
                validate_scratch_credentials(signin_home, paths)
                receipt["owner_signin"].update(
                    state="ready", ended_at=timestamp(), credential_paths=paths,
                    path_validation="canonical_metadata_only")
                prepared_home = signin_home
            root = FloatiRoot.open_direct_home(scratch / "fleet", create=True)
            node = "zc-worker"
            NodeWizard(root, RegistryAdminBackend(root), id_factory=uuid7_hex).add_from_keys(
                [node, "Zcode", "permanent"], io.StringIO())
            AuthorityGrantStore(root).claim("zc-claims", node, 300, 120, datetime.now(timezone.utc))
            work = WorkLog(root)
            item = work.add("scratch conformance", node, [], provision_workspace=True)
            workspace = Path(item["workspace"])
            request = {"nonce": receipt["nonce"], "bus_argv": ["/usr/bin/python3", "-B", str(Path(__file__).resolve()),
                "bus-step", "--root", str(workspace / ".floati/conformance-bus"), "--nonce", receipt["nonce"],
                "--source-sha", receipt["source_sha"]]}
            prompt = ("Run this exact scratch bus command once: " + shlex.join(request["bus_argv"])
                + ". Then write the nonce to answer.txt in your current workspace. Do nothing else.\n"
                + "FLOATI_CONFORMANCE_REQUEST=" + json.dumps(request))
            # The diagnostic request rides the landed measured --prompt form;
            # the durable work title remains the bounded human-readable task.
            adapter = RecordingZcodeAdapter(command or native_command, prepared_home=prepared_home)
            original_prompt = adapter._prompt_arguments
            # Assigning a per-instance callable is not pickled: WorkerRunner forks.
            adapter._prompt_arguments = lambda claimed: original_prompt({**claimed, "title": prompt})
            runner = WorkerRunner(root, {"zcode": adapter}, call_timeout=timeout)
            receipt["work"]["item"] = item
            try:
                receipt["work"]["result"] = runner.run(node, "zcode")
            except Exception as exc:
                receipt["work"]["refusal"] = {"code": getattr(exc, "code", type(exc).__name__)}
            receipt["work"].update(receipts=WorkerReceipts(root).records(), work_after=work.show(item["id"])[0],
                                   process_audit=runner.last_process_audit, artifacts=[])
            native = workspace / ".floati/native-capture.json"
            if native.is_file():
                receipt["invocation"] = json.loads(native.read_text())
            for binding in receipt["work"].get("result", {}).get("artifact_bindings", []):
                content = subprocess.check_output(["/usr/bin/git", "show", binding["sha"] + ":" + binding["doc"]],
                                                  cwd=workspace).decode()
                receipt["work"]["artifacts"].append({"binding": binding, "content": content})
            receipt["bus"] = read_bus(workspace / ".floati/conformance-bus")
    except Exception as exc:
        receipt["failure"] = {"code": getattr(exc, "code", type(exc).__name__)}
    finally:
        credential_removal = remove_scratch_credentials(signin_home)
        cleanup_failures = []
        for owned in (workspace, scratch):
            if owned is not None and owned.exists():
                try:
                    shutil.rmtree(owned)
                except OSError:
                    cleanup_failures.append("owned_scratch_removal_unproved")
        receipt["cleanup"] = {"scratch_removed": not scratch.exists(),
                              "workspaces_removed": workspace is None or not workspace.exists(),
                              "credential_material": credential_removal}
        if cleanup_failures:
            receipt["cleanup"]["absence"] = {"code": "owned_scratch_removal_unproved"}
        receipt["ended_at"] = timestamp()
        receipt["classification"] = classify_receipt(receipt)
        output.write_text(json.dumps(receipt, indent=2) + "\n")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        human.write_text("# Zcode conformance receipt\n\n"
            + "Mode: " + receipt["mode"] + ". Source: `" + receipt["source_sha"] + "`.\n\n"
            + "Replay JSON: `" + output.name + "`; SHA-256: `" + digest + "`.\n\n"
            + "```json\n" + json.dumps(receipt["classification"], indent=2) + "\n```\n\n"
            + "One measured native invocation maximum; owner native OAuth uses the same scratch HOME, no credentials copied. "
            + "Bus proof is a native-invoked scratch send/drain/ack, not a live hook wake. "
            + "Work proof requires a committed nonce artifact and complete governed receipts. "
            + "A fake capture does not establish live support. Missing provider access remains cannot-measure.\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    live = sub.add_parser("capture")
    mode = live.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--fixture-fake", action="store_true")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--timeout", type=float, default=30)
    replay = sub.add_parser("replay")
    replay.add_argument("--receipt", type=Path, required=True)
    sub.add_parser("preflight", help="check the declared native entry without sign-in or a model call")
    bus = sub.add_parser("bus-step")
    bus.add_argument("--root", type=Path, required=True)
    bus.add_argument("--nonce", required=True)
    bus.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    if args.phase == "bus-step":
        bus_step(args.root, args.nonce, args.source_sha)
        return 0
    if args.phase == "preflight":
        with tempfile.TemporaryDirectory(prefix="floati-zcode-preflight-", dir=capture_temporary_parent()) as temporary:
            result = preflight_native_entry(Path(temporary))
        print(json.dumps(result))
        return 0 if result["state"] == "ready" else 20
    if args.phase == "replay":
        result = classify_receipt(json.loads(args.receipt.read_text()))
    else:
        command = ["/usr/bin/python3", "-B", str(Path(__file__).resolve()), "fixture-harness"] if args.fixture_fake else None
        result = capture(args.output, command=command, timeout=args.timeout)["classification"]
    print(json.dumps(result))
    return 0 if result["status"] == "ok" else 20


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fixture-harness":
        request = json.loads(sys.argv[sys.argv.index("--prompt") + 1].split("FLOATI_CONFORMANCE_REQUEST=", 1)[1])
        subprocess.run(request["bus_argv"], check=True, stdout=subprocess.DEVNULL)
        Path("answer.txt").write_text(request["nonce"] + "\n")
        print(json.dumps({"sessionId": "fake-session", "traceId": "fake-trace", "turnId": "fake-turn",
                          "response": "done", "usage": {"source": "provider", "inputTokens": 1, "outputTokens": 1}}))
    else:
        raise SystemExit(main())
