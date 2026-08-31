from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


class WholeProductNoListenerFenceTests(unittest.TestCase):
    """Pin the audited local-only surface named by docs/TRUTH-GUARANTEES.md."""

    def test_network_capable_imports_are_confined_to_ruled_local_transports(self) -> None:
        allowed_socket_imports = {
            "floati/adapters/herdr.py",
            "floati/adapters/herdr_host.py",
            "floati/adapters/t3.py",
            "floati/effect_reconciliation_exec.py",
            "floati/sequencer.py",
            "floati/sequencer_scale.py",
            "floati/worker_bootstrap.py",
            "floati/worker_bootstrap_protocol.py",
            "floati/worker_exec.py",
            "floati/workers.py",
        }
        forbidden_modules = {
            "aiohttp", "ftplib", "http.client", "http.server", "requests",
            "smtplib", "telnetlib", "urllib.request", "urllib3", "websockets",
        }
        allowed_network_imports = {
            "floati/update_transport.py": {"http.client"},
        }
        seen_socket_imports = set()
        seen_network_imports: dict[str, set[str]] = {}
        violations: list[str] = []
        for path in sorted(Path("floati").rglob("*.py")):
            relative = path.as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name == "socket":
                        seen_socket_imports.add(relative)
                        if relative not in allowed_socket_imports:
                            violations.append(f"{relative}:socket")
                    if (
                        name in forbidden_modules
                    ):
                        seen_network_imports.setdefault(relative, set()).add(name)
                        if name not in allowed_network_imports.get(relative, set()):
                            violations.append(f"{relative}:{name}")
        self.assertEqual(allowed_socket_imports, seen_socket_imports)
        self.assertEqual(allowed_network_imports, seen_network_imports)
        self.assertEqual([], violations)

    def test_bind_and_listen_calls_exist_only_in_the_ruled_af_unix_supervisor(self) -> None:
        calls: list[str] = []
        pattern = re.compile(r"\.(?:bind|listen)\s*\(")
        for path in sorted(Path("floati").rglob("*.py")):
            if pattern.search(path.read_text(encoding="utf-8")):
                calls.append(path.as_posix())
        self.assertEqual(["floati/sequencer.py"], calls)
        source = Path("floati/sequencer.py").read_text(encoding="utf-8")
        self.assertIn("socket.AF_UNIX", source)

    def test_mcp_module_imports_only_local_code_and_non_network_stdlib(self) -> None:
        """Catches an stdio server acquiring a listener, URL client, or dependency."""

        path = Path("floati/mcp.py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        allowed_stdlib = {
            "__future__",
            "argparse",
            "contextlib",
            "io",
            "json",
            "sys",
            "typing",
        }
        absolute_imports: set[str] = set()
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                absolute_imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                absolute_imports.add(node.module.split(".")[0])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"bind", "connect", "listen", "open_connection"}:
                    violations.append(node.func.attr)
        self.assertLessEqual(absolute_imports, allowed_stdlib)
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
