#!/usr/bin/env python3
"""Build and exercise the distribution outside the checkout.

Run with build, twine and a venv-capable Python. Pass --uv to also measure
the README's tool install against the same wheel without an index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def run(*argv, cwd, env, capture=False, request=None):
    start = time.monotonic()
    result = subprocess.run(
        argv, cwd=cwd, env=env, check=False, text=True, capture_output=capture, input=request,
        timeout=120,
    )
    print(f"{Path(argv[0]).name} {' '.join(map(str, argv[1:3]))}: "
          f"exit {result.returncode}, {time.monotonic() - start:.3f}s", flush=True)
    if result.returncode and capture:
        print(result.stdout + result.stderr, file=sys.stderr)
    result.check_returncode()
    return result.stdout


def quickstart(launcher, cwd, env):
    root = cwd / "demo"
    preview_argv = ("setup", "--root", str(root), "--seat", "codex",
                    "--session", "demo-codex", "--hook", "codex")

    def command(*args, directory=cwd):
        return json.loads(run(launcher, *args, cwd=directory, env=env, capture=True))

    preview = command(*preview_argv)["evidence"]
    assert preview["state"] == "preview" and not root.exists()
    installed = command(*preview_argv, "--confirm", preview["confirmation"])["evidence"]
    assert installed["state"] == "installed"
    assert installed["hooks"]["codex"]
    workspace = Path(installed["workspace"])
    command("register", "--root", str(root), "claude", "--harness", "Claude")
    command("who", "--root", str(root))
    command("wake", "arm", "--root", str(root), "--as", "codex", "--session", "demo-codex",
            "--workspace", str(workspace), "--idempotency-key", "demo-arm", directory=workspace)

    origin = cwd / "origin"
    origin.mkdir()
    (origin / "README.md").write_text("# Demo\n", encoding="utf-8")
    run("/usr/bin/git", "init", "-q", cwd=origin, env=env)
    run("/usr/bin/git", "add", "README.md", cwd=origin, env=env)
    run("/usr/bin/git", "-c", "user.name=Floati Demo", "-c",
        "user.email=demo@example.invalid", "commit", "-qm", "Add a demo README",
        cwd=origin, env=env)
    repository = cwd / "project"
    run("/usr/bin/git", "clone", "--quiet", "--no-local", origin, repository, cwd=cwd, env=env)
    sha = run("/usr/bin/git", "rev-parse", "HEAD", cwd=repository, env=env, capture=True).strip()
    sent = command("send", "--root", str(root), "--from", "claude", "--to", "codex",
                   "--repo", "demo", "--sha", sha, "--repository", str(repository),
                   "--doc", "README.md", "--note", "Review the README.",
                   "--idempotency-key", "demo-handoff", directory=repository)["evidence"]["message"]
    hooks_path = Path(installed["hooks"]["codex"]["hooks_path"])
    stop = json.loads(hooks_path.read_text())["hooks"]["Stop"]
    hook, = [item["command"] for block in stop for item in block["hooks"]]
    payload = json.dumps({"session_id": "demo-codex", "cwd": str(workspace),
                          "hook_event_name": "Stop", "stop_hook_active": False})
    decision = json.loads(run(*shlex.split(hook), cwd=workspace, env=env,
                              capture=True, request=payload))
    assert decision["decision"] == "block" and sent["id"] in decision["reason"], decision
    before_drain = command("receipts", "codex", "--root", str(root))["evidence"]
    assert [row["item_ids"] for row in before_drain["deliveries"]] == [[sent["id"]]], before_drain
    assert before_drain["acks"] == [], before_drain

    readme = (ROOT / "README.md").read_text()
    section = readme.split("### Send work from Claude Code to Codex", 1)[1].split(
        "### Install from a source checkout", 1)[0]
    for block in re.findall(r"```sh\n(floati inbox [\s\S]*?)\n```", section):
        substitutions = {"$HOME/floati-demo": str(root), "$CODEX_THREAD_ID": "demo-codex"}
        argv = shlex.split(block.replace("\\\n", " "))
        command(*(substitutions.get(arg, arg) for arg in argv[1:]), directory=workspace)
    history = command("receipts", "codex", "--root", str(root))["evidence"]
    for kind in ("deliveries", "acks"):
        assert any(sent["id"] in row["item_ids"] for row in history[kind]), history
    assert history["consumptions"] == [], history
    # Verify the installed MCP definition starts its own server from its
    # declared absolute command, with the seat's working directory.
    config = (workspace / ".codex/config.toml").read_text().splitlines()
    mcp_path = json.loads(config[1].partition(" = ")[2])
    mcp_args = json.loads(config[2].partition(" = ")[2])
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "distribution-check", "version": "1"},
    }}
    stdout = run(mcp_path, *mcp_args, cwd=workspace, env=env, capture=True,
                 request=json.dumps(request) + "\n")
    response = json.loads(stdout)
    assert response["id"] == request["id"] and response["result"]["serverInfo"], response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", type=Path, help="absolute uv executable for the tool-install arm")
    parser.add_argument("--output-dir", type=Path, help="retain the checked artifacts in a new directory")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="floati-pypi-") as scratch:
        bank = Path(scratch).resolve()
        artifacts = args.output_dir.resolve() if args.output_dir else bank / "dist"
        artifacts.mkdir()
        home = bank / "home"
        home.mkdir()
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
               "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(bank),
               "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        # The build inputs are resolved independently of the installed CLI.
        build_env = env
        installer = ()
        if args.uv:
            build_env = dict(env, PATH=f"{args.uv.parent}:/usr/bin:/bin")
            installer = ("--installer", "uv")
        run(sys.executable, "-m", "build", *installer, "--outdir", str(artifacts),
            cwd=ROOT, env=build_env)
        wheel, = artifacts.glob("*.whl")
        sdist, = artifacts.glob("*.tar.gz")
        run(sys.executable, "-m", "twine", "check", "--strict", str(wheel), str(sdist),
            cwd=bank, env=env)
        manifest = json.loads((ROOT / "bundle-manifest.v0.json").read_text())
        with ZipFile(wheel) as artifact:
            assert "floati/__init__.py" in artifact.namelist(), artifact.namelist()[:12]
            for item in manifest["files"]:
                payload = artifact.read("floati/_runtime/" + item["path"])
                assert hashlib.sha256(payload).hexdigest() == item["sha256"], item["path"]
        for artifact in (wheel, sdist):
            print(f"{artifact.name} sha256={hashlib.sha256(artifact.read_bytes()).hexdigest()}")
        python = bank / "venv/bin/python"
        if args.uv:
            run(sys.executable, "-m", "venv", "--without-pip", str(bank / "venv"), cwd=bank, env=env)
            run(args.uv, "pip", "install", "--offline", "--no-index", "--no-deps",
                "--python", python, wheel, cwd=bank, env=env)
        else:
            run(sys.executable, "-m", "venv", str(bank / "venv"), cwd=bank, env=env)
            run(python, "-m", "pip", "install", "--no-index", "--no-deps", wheel, cwd=bank, env=env)
        quickstart(bank / "venv/bin/floati", bank, env)
        if args.uv:
            user = bank / "tool-home"
            user.mkdir()
            bin_dir = user / ".local/bin"
            tool_env = dict(env, HOME=str(user), UV_TOOL_BIN_DIR=str(bin_dir),
                            PATH=f"{bin_dir}:/usr/bin:/bin", UV_PYTHON_DOWNLOADS="never",
                            UV_OFFLINE="1", UV_NO_INDEX="1", UV_FIND_LINKS=str(artifacts),
                            UV_PYTHON=sys.executable)
            run(args.uv, "tool", "install", "floati", cwd=user, env=tool_env)
            assert (bin_dir / "floati").is_file()
            quickstart("floati", user, tool_env)


if __name__ == "__main__":
    main()
