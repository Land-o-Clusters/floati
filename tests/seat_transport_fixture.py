"""Build inert, manifest-pinned transport evidence inside a fixture's scratch root.

These ordinary files satisfy transport validation only. No installer or client
is invoked, and the placeholder interpreter refuses if accidentally executed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def create_transport(install_root: Path) -> dict:
    install_root.mkdir(mode=0o700)
    (install_root / "floati").mkdir(mode=0o700)
    (install_root / ".floati-install").mkdir(mode=0o700)
    contents = {name: b"# Inert scratch transport fixture; never invoke as a client.\n"
        for name in ("floati/__init__.py", "floati/__main__.py", "floati/cli.py",
            "floati/codex_hook_install.py", "floati/codex_wait.py")}
    contents["fixture-python"] = b"#!/bin/sh\nexit 99\n"
    entries = []
    for name, content in sorted(contents.items()):
        path = install_root / name
        path.write_bytes(content)
        path.chmod(0o700 if name == "fixture-python" else 0o600)
        entries.append({"path": name, "sha256": hashlib.sha256(content).hexdigest()})
    source_sha = "a" * 40
    manifest = (json.dumps({"schema_version": 0, "source_ref": "scratch-fixture",
        "source_sha": source_sha, "files": entries}, sort_keys=True) + "\n").encode()
    manifest_path = install_root / ".floati-install/manifest.v0.json"
    manifest_path.write_bytes(manifest)
    manifest_path.chmod(0o600)
    return {"kind": "floati-install-manifest-v0", "install_root": str(install_root),
        "manifest_path": str(manifest_path), "source_sha": source_sha,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "interpreter": str(install_root / "fixture-python"), "max_files": len(entries),
        "max_file_bytes": 4096, "max_total_bytes": 4096 * len(entries)}
