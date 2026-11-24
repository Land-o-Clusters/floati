"""Bounded, creation-only native CLI proof, isolated from operator state.

This is an explicitly invoked host probe, not a portable discovery test.
It invokes no model turn and uses only scratch HOME, CODEX_HOME and fleet.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    executable = Path(args.executable)
    destination = Path(args.output)
    if not executable.is_absolute() or executable.resolve() != executable or not executable.is_file():
        parser.error("executable must be an absolute canonical ordinary file")
    if not destination.is_absolute() or destination.exists() or destination.parent.resolve() != destination.parent:
        parser.error("output must be an absent file in an existing canonical directory")
    with tempfile.TemporaryDirectory(prefix="native-seat-home-", dir="\x2fprivate/tmp") as isolated_home:
        os.environ["HOME"] = isolated_home
        os.environ["CODEX_HOME"] = isolated_home
        os.environ.pop("CODEX_THREAD_ID", None)
        os.environ.pop("CODEX_TURN_ID", None)
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
                "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "AZURE_OPENAI_API_KEY"):
            os.environ.pop(name, None)
        from tests.test_seat_onboard import SeatOnboardingTests
        fixture = SeatOnboardingTests()
        # The baseline creates inert scratch transport files and their pinned
        # manifest; this probe never selects an operator installation/client.
        fixture.setUp()
        try:
            # Leave the native default in place; no model turn is requested.
            config = fixture.config.read_text().replace('model = "fixture"\n', '')
            fixture.config.write_text(config)
            before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (fixture.config, fixture.profiles)}
            env = dict(os.environ, CODEX_HOME=str(fixture.base))
            version = subprocess.run([str(executable), "--version"], env=env,
                cwd=fixture.source, capture_output=True, text=True, timeout=10)
            code, output, errors = fixture.add("--mode", "cli", "--codex-executable", str(executable))
            logs = {}
            for name in ("cli-start.jsonl", "cli-start.stderr"):
                path = fixture.home / "scratch" / name
                if path.exists():
                    logs[name] = path.read_text(errors="replace")[:65536]
            methods = []
            for line in logs.get("cli-start.jsonl", "").splitlines():
                frame = json.loads(line)
                if frame.get("direction") == "out":
                    methods.append(frame["message"]["method"])
            artifact = {
                "executable": str(executable),
                "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "version": {"exit_code": version.returncode, "stdout": version.stdout, "stderr": version.stderr},
                "isolated_home": isolated_home,
                "provider_home": str(fixture.base),
                "selected_transport": fixture.transport,
                "add_exit_code": code, "add_stdout": output, "add_stderr": errors,
                "provider_logs": logs, "request_methods": methods,
                "turn_start_requested": "turn/start" in methods,
            }
            remove_code, remove_output, remove_errors = fixture.cli("seat", "remove", "--root",
                str(fixture.root.path), "--home", str(fixture.home))
            artifact["remove"] = {"exit_code": remove_code, "stdout": remove_output, "stderr": remove_errors}
            artifact["configuration_inventory"] = {"before": before, "after": {
                str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (fixture.config, fixture.profiles)}}
            encoded = (json.dumps(artifact, indent=2, sort_keys=True) + "\n").encode()
            staged = destination.with_name(destination.name + ".staging")
            with staged.open("xb") as result:
                os.fchmod(result.fileno(), 0o600)
                result.write(encoded)
                result.flush()
                os.fsync(result.fileno())
            os.link(staged, destination)  # no replacement of an earlier receipt
            staged.unlink()
            directory = os.open(str(destination.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if destination.read_bytes() != encoded:
                raise RuntimeError("native proof readback differs")
            print(json.dumps({"output": str(destination), "version": version.stdout.strip(),
                "add_exit_code": code, "remove_exit_code": remove_code,
                "turn_start_requested": artifact["turn_start_requested"]}, sort_keys=True))
            restored = before == artifact["configuration_inventory"]["after"]
            return 0 if code == 0 and remove_code == 0 and restored and not artifact["turn_start_requested"] else 1
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    raise SystemExit(main())
