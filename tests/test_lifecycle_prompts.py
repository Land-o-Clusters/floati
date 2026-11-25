"""LC-GEN: the per-seat lifecycle command files are GENERATED, never hand-written.

Ruling `docs/rulings/2026-09-05-lc-1-lifecycle-taxonomy-ruling.md` §LC-1d:
`node prompts` projects the per-seat command files from the role template,
the harness adapter table and the LC-1 parity table; every projection opens
with a header naming the generator, the harbor SHA and "hand edits are
overwritten"; regeneration is byte-stable; the LC-R3 reserved-name fence
refuses product verbs and other bus families' prompt names (`board` is on
the list); and the verb×harness parity table derived from the output REDs
on any cell that is neither realized nor an honest dash with a receipt.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import tests  # Pin HOME before fixtures or product imports resolve account paths.

from floati.ids import uuid7_hex
from floati.mcp import run_cli_artifact
from floati.registry import Registry
from floati.role_assignment import RoleStepWizard
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

ARCHITECT_ANSWERS = ("floati", "foreign-project", "owner-tier")

#: LC-1 §1 verb set (closed by LC-1a).
LIFECYCLE_VERBS = ("boot", "board", "drain", "pause", "resume", "prep-clear", "turnover")

#: The boot and teardown pair is shared; other projections retain seat suffixes.
EXPECTED_FILES = {
    "codex": ("board", "drain", "pause", "resume", "prep-clear"),
    "claude": ("boot", "prep-clear", "board"),
    "zcode": ("prep-clear",),
}

# F2-REFUSAL-REMEDY-1: measured at base 1d616d90918f4347420eb00bea16472dc45cea6c.
# F2-GATEWAY-TAKEOVER-1: only the Codex/Claude board files move for explicit
# takeover and verified displacement reporting; all other file pins stay exact.
# Only the scratch fleet path and generated harbor header are normalized.
PROJECTION_DIGESTS = {
    "codex": {
        "board-codex-a.md": "6a6b8ed3e5934b432ee69b65cf888cec0c0e0579067117ed3bd5a38581f0c740",
        "drain-codex-a.md": "aee80f449b6607cbf9d09b086bf1be74a1c1a01c12f23cdd31e5eaa719ffbcd7",
        "pause-codex-a.md": "1e751f09dbe619b206f477a454969fdba6e912840a0933d37a4204b41c634123",
        "prep-clear-codex-a.md": "a48dc262eb9985fff1bff9aa74704d685649fda096b3ea8a08ce09e8bb19fb5e",
        "resume-codex-a.md": "74d2e2ce6891e9a8d7c2f63df3dd9ba82edb01acde72ae6358d04f90aa122c5e",
    },
    "claude": {
        "board-claude-a.md": "3049d3ecd8774f520df87c174e5793a0c38e9f2b80b7b5ea9c27f4a6fd2ad476",
        # Claude boot now uses the identity-free shared pair, pinned above.
        "prep-clear-claude-a.md": "6ee226af071b40d3cbe126dfac6c67fb00915c719d7746f94e3b60d9727c8c88",
    },
}


class LifecyclePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        self.backend = None
        for node, harness in (("codex-a", "Codex"), ("claude-a", "Claude"), ("zcode-a", "zcode")):
            Registry(self.root).register(node, harness)
            self._assign(node, "architect", ARCHITECT_ANSWERS)
        self.out = Path(self.temporary.name) / "out"
        self.templates = self._templates()

    def _templates(self) -> dict:
        from floati.role_library import RoleTemplateLibrary

        return RoleTemplateLibrary(self.root).templates()

    def _assign(self, node: str, template: str, answers: tuple[str, ...]) -> None:
        wizard = RoleStepWizard(
            self.root,
            self._backend(),
            self._templates(),
            id_factory=uuid7_hex,
        )
        wizard.assign_from_keys([node, template, *answers], io.StringIO())

    def _backend(self):
        from floati.admin_registry import RegistryAdminBackend

        if self.backend is None:
            self.backend = RegistryAdminBackend(self.root)
        return self.backend

    def _prompts(self, harness: str, node: str | None = None) -> tuple[int, dict]:
        seats = {"codex": "codex-a", "claude": "claude-a", "zcode": "zcode-a"}
        seat = node or seats.get(harness, "codex-a")
        return run_cli_artifact([
            "node", "prompts",
            "--root", str(self.root_path),
            "--as", seat,
            "--harness", harness,
            "--out", str(self.out),
        ])

    def test_supported_harnesses_project_one_shared_seat_command_pair(self) -> None:
        """Missing shared commands must fail before a fresh seat can boot.

        A projection that embeds a node or root would bind another fresh
        session to the previous seat instead of resolving its declaration.
        """

        Registry(self.root).register("cursor-a", "Cursor")
        self._assign("cursor-a", "architect", ARCHITECT_ANSWERS)
        for harness, node in (("cursor", "cursor-a"), ("codex", "codex-a"),
                              ("zcode", "zcode-a"), ("claude", "claude-a")):
            with self.subTest(harness=harness):
                self.out = Path(self.temporary.name) / harness
                exit_code, artifact = self._prompts(harness, node)
                self.assertEqual(0, exit_code, artifact)
                files = {row["path"] for row in artifact["evidence"]["files"]}
                self.assertTrue({"boot-seat.md", "teardown-seat.md"} <= files,
                                "a fresh seat needs both shared commands")
                self.assertNotIn(f"boot-{node}.md", files)
                self.assertNotIn(f"teardown-{node}.md", files)
                for verb in ("boot", "teardown"):
                    content = (self.out / f"{verb}-seat.md").read_text()
                    self.assertIn(f"floati node {verb}", content)
                    self.assertIn("SEAT.json", content)
                    self.assertNotIn(node, content)
                    self.assertNotIn(str(self.root_path), content)
                parity = artifact["evidence"]["parity"][harness]
                self.assertEqual("file:boot-seat.md", parity["boot"])
                self.assertEqual("file:teardown-seat.md", parity["teardown"])

    def test_installer_projects_shared_commands_without_a_registry_or_role(self) -> None:
        """A governed hook install must make commands available before boot.

        Requiring an assigned role here would leave a fresh session without
        its boot command; writing outside the fixed directory leaks scope.
        """

        from floati import lifecycle_prompts

        installer = getattr(lifecycle_prompts, "install_seat_commands", None)
        self.assertTrue(callable(installer), "governed hook installers need the shared pair")
        for harness, relative in (("cursor", ".cursor/commands"),
                                  ("codex", ".agents/skills"),
                                  ("claude", ".claude/commands"),
                                  ("zcode", ".")):
            with self.subTest(harness=harness):
                workspace = Path(self.temporary.name) / (harness + "-seat")
                workspace.mkdir()
                result = installer(workspace, harness)
                directory = workspace / relative
                self.assertEqual(str(directory), result["out"])
                expected_files = (["boot-seat/SKILL.md", "teardown-seat/SKILL.md"]
                                  if harness == "codex" else
                                  ["BOOT.md", "boot-seat.md", "teardown-seat.md"]
                                  if harness == "zcode"
                                  else ["boot-seat.md", "teardown-seat.md"])
                self.assertEqual(expected_files,
                                 sorted(str(path.relative_to(directory))
                                        for path in directory.rglob('*') if path.is_file()))
                first = {str(path.relative_to(directory)): path.read_bytes()
                         for path in directory.rglob('*') if path.is_file()}
                self.assertEqual(result, installer(workspace, harness))
                self.assertEqual(first, {str(path.relative_to(directory)): path.read_bytes()
                                         for path in directory.rglob('*') if path.is_file()})
                self.assertEqual(set(first), {str(path.relative_to(directory))
                                             for path in workspace.rglob("*")
                                             if path.is_file()})

    def test_shared_command_installer_preserves_foreign_files_before_any_write(self) -> None:
        """A foreign second target cannot permit a partial pair installation."""

        from floati import lifecycle_prompts
        from floati.errors import ProtocolRefusal

        installer = getattr(lifecycle_prompts, "install_seat_commands", None)
        self.assertTrue(callable(installer), "governed hook installers need the shared pair")
        for harness, relative, boot, teardown in (
                ('cursor', '.cursor/commands', 'boot-seat.md', 'teardown-seat.md'),
                ('codex', '.agents/skills', 'boot-seat/SKILL.md', 'teardown-seat/SKILL.md')):
            with self.subTest(harness=harness):
                workspace = Path(self.temporary.name) / harness
                commands = workspace / relative
                foreign = commands / teardown
                foreign.parent.mkdir(parents=True)
                foreign.write_text("owner command\n")
                with self.assertRaises(ProtocolRefusal) as caught:
                    installer(workspace, harness)
                self.assertEqual("lifecycle_prompts_target_foreign", caught.exception.code)
                self.assertEqual("owner command\n", foreign.read_text())
                self.assertFalse((commands / boot).exists())

    def test_codex_command_reaches_a_runtime_with_spaces_without_path_lookup(self) -> None:
        """Generated shell argv must preserve the installed runtime as one path."""
        import os
        import subprocess
        from floati import lifecycle_prompts

        workspace = Path(self.temporary.name) / 'workspace'
        workspace.mkdir()
        runtime = Path(self.temporary.name) / 'runtime with spaces'
        launcher = runtime / 'scripts/floati'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        launcher.chmod(0o700)
        with patch.object(lifecycle_prompts, 'REPOSITORY_ROOT', runtime):
            receipt = lifecycle_prompts.install_seat_commands(workspace, 'codex')
        commands = Path(receipt['out'])
        for verb in ('boot', 'teardown'):
            with self.subTest(verb=verb):
                path = commands / f'{verb}-seat/SKILL.md'
                if not path.exists():
                    path = commands / f'{verb}-seat.md'
                invocation = re.search(r'Run `([^`]+)` with', path.read_text()).group(1)
                self.assertEqual(str(launcher), shlex.split(invocation)[0])
                result = subprocess.run(shlex.split(invocation), cwd=workspace,
                    env={**os.environ, 'PATH': '/usr/bin:/bin'}, capture_output=True,
                    text=True, check=False)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(['node', verb], result.stdout.splitlines()[:2])

    def test_shared_command_installer_refuses_symlinked_command_parent(self) -> None:
        """A linked harness directory must never route writes outside the seat."""

        from floati import lifecycle_prompts
        from floati.errors import ProtocolRefusal

        installer = getattr(lifecycle_prompts, "install_seat_commands", None)
        self.assertTrue(callable(installer), "governed hook installers need the shared pair")
        workspace = Path(self.temporary.name) / "fresh-seat"
        workspace.mkdir()
        foreign = Path(self.temporary.name) / "foreign-commands"
        foreign.mkdir()
        (workspace / ".cursor").symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(ProtocolRefusal) as caught:
            installer(workspace, "cursor")
        self.assertEqual("lifecycle_prompts_target_invalid", caught.exception.code)
        self.assertEqual([], list(foreign.iterdir()))

    def test_node_prompts_projects_the_command_files(self) -> None:
        """RED: no verb parses — the projections are hand-written today."""

        exit_code, artifact = self._prompts("codex")
        self.assertEqual(0, exit_code, artifact)
        self.assertEqual("ok", artifact["status"])
        written = sorted(path.name for path in self.out.iterdir())
        self.assertEqual(
            sorted([f"{verb}-codex-a.md" for verb in EXPECTED_FILES["codex"]]
                   + ["boot-seat.md", "teardown-seat.md"]),
            written,
        )
        for path in self.out.iterdir():
            header = path.read_text(encoding="utf-8").splitlines()[:3]
            joined = "\n".join(header)
            self.assertIn("hand edits are overwritten", joined)
            self.assertIn("node prompts", joined)
            self.assertRegex(joined, r"harbor [0-9a-f]{40}",
                             "the header names the harbor SHA")

    def test_prep_clear_projection_derives_the_landed_verb_line(self) -> None:
        """Am.1: the wind-down line comes from the live parser, never prose.

        The landed `node prep-clear` requires --workspace --repo --doc
        --note; a projection that omits them sends the seat into
        arguments_invalid.
        """

        exit_code, artifact = self._prompts("codex")
        self.assertEqual(0, exit_code, artifact)
        content = (self.out / "prep-clear-codex-a.md").read_text(encoding="utf-8")
        for required in ("--workspace", "--repo", "--doc", "--note", "--session"):
            self.assertIn(required, content, "prep-clear line misses " + required)

    def test_generation_is_byte_stable_and_out_only(self) -> None:
        """Catches nondeterministic projections and stray writes."""

        self._prompts("codex")
        first = {
            path.name: path.read_bytes() for path in sorted(self.out.iterdir())
        }
        self._prompts("codex")
        second = {
            path.name: path.read_bytes() for path in sorted(self.out.iterdir())
        }
        self.assertEqual(first, second)

    def test_warm_projection_uses_latest_role_without_replaying_registry_history(self) -> None:
        """A seat's current prompts need its selected role, not retained history."""
        from floati.registry_prefix import warm

        Registry(self.root).register("bounded-seat", "Codex")
        self.assertIsNotNone(warm(self.root))
        self._assign("bounded-seat", "builder", ("floati", "shared-core", "claude-a"))
        exit_code, artifact = self._prompts("codex", "bounded-seat")
        self.assertEqual(0, exit_code, artifact)
        expected = {path.name: path.read_bytes() for path in self.out.iterdir()}
        self.assertIn(b"role `builder`", expected["board-bounded-seat.md"])
        registry_path = self.root.resolve_relative("registry/entries.jsonl")
        native_open = Path.open

        def opening(path, mode="r", *args, **kwargs):
            if path == registry_path and mode == "rb":
                self.fail("warm prompt projection replayed verified registry history")
            return native_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", opening):
            exit_code, artifact = self._prompts("codex", "bounded-seat")
        self.assertEqual(0, exit_code, artifact)
        actual = {path.name: path.read_bytes() for path in self.out.iterdir()}
        self.assertEqual(expected, actual)

    def test_missing_role_prints_case_and_executable_role_interview(self) -> None:
        """Catch an unassigned seat being refused without a usable next step."""

        Registry(self.root).register("unassigned-a", "Codex")
        exit_code, artifact = self._prompts("codex", node="unassigned-a")
        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("role_assignment_missing", evidence["code"])
        self.assertIn("unassigned-a", evidence["detail"])
        self.assertIsInstance(evidence["remedy"], str)
        command = shlex.split(evidence["remedy"])
        self.assertEqual(
            ["floati", "node", "role", "--root", str(self.root_path), "--node", "unassigned-a"], command
        )
        self.assertFalse(self.out.exists(), "a refusal writes no projection")
        from floati.cli import main
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO("builder\nfloati\nforeign-bus\narchitect-a\n")), redirect_stdout(output), redirect_stderr(errors):
            remedy_exit = main(command[1:])
        roles = json.loads(output.getvalue())
        self.assertEqual(0, remedy_exit, roles)
        self.assertEqual("builder", roles["evidence"]["records"][0]["template_role"])

    def test_harness_mismatch_prints_case_and_executable_corrected_projection(self) -> None:
        """Catch a corrected command retaining the mismatched harness or paths."""

        self.out = self.out.parent / "prompt output's"
        exit_code, artifact = self._prompts("claude", node="codex-a")
        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("lifecycle_prompts_harness_mismatch", evidence["code"])
        for case in ("codex-a", "Codex", "claude"):
            self.assertIn(case, evidence["detail"])
        self.assertIsInstance(evidence["remedy"], str)
        command = shlex.split(evidence["remedy"])
        self.assertEqual([
            "floati", "node", "prompts", "--root", str(self.root_path),
            "--as", "codex-a", "--harness", "codex", "--out", str(self.out),
        ], command)
        self.assertFalse(self.out.exists(), "a refusal writes no projection")
        remedy_exit, projection = run_cli_artifact(command[1:])
        self.assertEqual(0, remedy_exit, projection)
        self.assertEqual("codex", projection["evidence"]["harness"])
        self.assertEqual(7, len(projection["evidence"]["files"]))

    def test_matching_harnesses_preserve_ruled_projection_bytes(self) -> None:
        """Pin exact projections after the ruled takeover board amendment."""

        self.maxDiff = None
        for harness, expected in PROJECTION_DIGESTS.items():
            with self.subTest(harness=harness):
                self.out = self.out.parent / harness
                exit_code, artifact = self._prompts(harness)
                self.assertEqual(0, exit_code, artifact)
                actual = {}
                for path in sorted(self.out.iterdir()):
                    if path.name in ("boot-seat.md", "teardown-seat.md"):
                        continue
                    normalized = path.read_text(encoding="utf-8").replace(
                        str(self.root_path), "ROOT"
                    )
                    normalized = re.sub(
                        r"at harbor [0-9a-f]{40}", "at harbor HARBOR_SHA", normalized
                    )
                    actual[path.name] = hashlib.sha256(normalized.encode()).hexdigest()
                self.assertEqual(expected, actual)

    def test_unsupported_registered_harness_prints_executable_switch_inspection(self) -> None:
        """Catch a stale label sending the operator into harness_unknown."""

        from contextlib import redirect_stdout
        from floati.cli import main

        Registry(self.root).register("legacy-a", "opencode-grok")
        exit_code, artifact = self._prompts("codex", node="legacy-a")
        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("lifecycle_prompts_harness_mismatch", evidence["code"])
        for case in ("legacy-a", "opencode-grok", "codex", "no lifecycle adapter",
                     "node switch", "re-register"):
            self.assertIn(case, evidence["detail"])
        self.assertEqual("floati node switch --help", evidence["remedy"])
        self.assertFalse(self.out.exists(), "a refusal writes no projection")
        output = io.StringIO()
        with redirect_stdout(output):
            remedy_exit = main(shlex.split(evidence["remedy"])[1:])
        self.assertEqual(0, remedy_exit)
        for option in ("--root", "--node", "--harness", "--model"):
            self.assertIn(option, output.getvalue())
        self.assertEqual("opencode-grok", Registry(self.root).require_active("legacy-a")["role"],
                         "help inspects the switch contract; it never changes the node")

    def test_unknown_harness_name_refuses_lifecycle_prompts_harness_unknown(self) -> None:
        """LIFECYCLE-UNKNOWN-HARNESS-1: a name not in HARNESS_ADAPTERS.

        Production change that would make this fail: deleting the
        `adapter is None` branch so `--harness` of a name that does not
        exist never emits `lifecycle_prompts_harness_unknown`.
        """

        from floati.errors import UNNAMED_REMEDY
        from floati.lifecycle_prompts import HARNESS_ADAPTERS

        unknown = "not-a-harness"
        self.assertNotIn(unknown, HARNESS_ADAPTERS)
        exit_code, artifact = self._prompts(unknown, node="codex-a")
        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("lifecycle_prompts_harness_unknown", evidence["code"])
        self.assertIn(repr(unknown), evidence["detail"])
        for known in sorted(HARNESS_ADAPTERS):
            self.assertIn(known, evidence["detail"])
        self.assertEqual(UNNAMED_REMEDY, evidence["remedy"])
        self.assertFalse(self.out.exists(), "a refusal writes no projection")

    def test_parity_table_realizes_or_dashes_every_cell(self) -> None:
        """Catches a verb×harness cell neither realized nor an honest dash."""

        parity = {}
        for harness in EXPECTED_FILES:
            exit_code, artifact = self._prompts(harness)
            self.assertEqual(0, exit_code, artifact)
            parity[harness] = artifact["evidence"]["parity"][harness]
        for harness, verbs in EXPECTED_FILES.items():
            for verb in verbs:
                cell = parity[harness][verb]
                self.assertTrue(
                    cell.startswith("file:") or cell.startswith("dash:"),
                    f"{harness}/{verb} is neither realized nor an honest dash: {cell!r}",
                )
        for verb in LIFECYCLE_VERBS:
            for harness in EXPECTED_FILES:
                self.assertIn(verb, parity[harness], f"{harness}/{verb} is missing")

    def test_reserved_names_and_harness_mismatch_refuse(self) -> None:
        """LC-R3: the reserved set is DERIVED from the live registry and pinned."""

        from floati.lifecycle_prompts import reserved_names

        reserved = reserved_names()
        # Derived from describe's live registry plus the bus families — the
        # hand-written first draft missed 11 of the live commands.
        # LANES-1 on train BA: reserved_names() measures 216 entries.
        # SN-R1 Am.4 on this base adds chart timings (218).
        # CUR-2 on landing I adds hook, hook-install and wake-wait (221) —
        # re-derived at the composition with reserved_names() itself.
        # BL-10 on this base adds seat migrate (223: migrate, seat-migrate) —
        # re-derived at the composition with reserved_names() itself.
        # SEAT-ONBOARD-1 adds seat-add and seat-remove (225); their leaf
        # names were already reserved by the existing command families.
        # Harbor train 14 (seat 25) adds hold, hold-release, hold-start, reclaim and
        # seat-retire (230), re-derived at the composition with reserved_names() itself.
        # BUS-CONSUMED-RECEIPT-1 adds consume (231), measured from the live registry.
        # F2-SEAT-TEARDOWN-1 adds seat-teardown (232); teardown was already reserved.
        # FLEET-ADHOC-THREADS-1: re-derived from merged reserved_names();
        # main's hook-uninstall, ask and backup join seven ad-hoc stems.
        # Person membership adds person and the three node-person path stems.
        # Claude's internal relay reserves relay and hook-relay.
        # Work queue notifications reserve notify and work-notify.
        # Checkout liveness reserves its top-level command name.
        # Network setup reserves setup.
        # Ask receipt replay reserves the top-level replay verb.
        # Upgrade preparation reserves prepare and update-prepare.
        # Inbound mail holds reserve inbound, hold-inbound, hold-inbound-start and hold-inbound-show.
        # NEEDS-YOU-OBSERVED-1 reserves needs-observe; observe was already reserved.
        # NET-MCP-ASK-1 reserves the top-level who verb.
        # Fleet topology reserves role-clear and the net route command family.
        self.assertEqual(len(reserved), 305, sorted(reserved))
        self.assertTrue({"node-role-clear", "net-route-add", "net-route-remove",
                         "net-route-list"} <= reserved)
        self.assertIn("checkout-liveness", reserved)
        self.assertIn("setup", reserved)
        self.assertIn("replay", reserved)
        self.assertIn("who", reserved)
        self.assertIn("notify", reserved)
        self.assertIn("work-notify", reserved)
        self.assertIn("relay", reserved)
        self.assertIn("hook-relay", reserved)
        for name in ("adhoc", "profile", "worker-adhoc", "worker-adhoc-profile",
                     "worker-adhoc-run", "worker-adhoc-show", "worker-adhoc-teardown"):
            self.assertIn(name, reserved)
        for name in ("board", "prompts", "prep-clear", "wait", "pausebus", "resumebus", "seat-add", "seat-remove", "consume", "seat-teardown", "hook-uninstall", "ask", "replay", "backup"):
            self.assertIn(name, reserved)

        exit_code, artifact = self._prompts("cursor")
        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])

    def test_cursor_adapter_projects_boot_board_drain_teardown(self) -> None:
        """F2-CURSOR-ADAPTER-1: a Cursor node emits the four command files.

        Production change that would make this fail: leaving `cursor` out of
        HARNESS_ADAPTERS, or realizing a verb set other than boot, board,
        drain and teardown.
        """

        Registry(self.root).register("cursor-a", "Cursor")
        self._assign("cursor-a", "architect", ARCHITECT_ANSWERS)
        exit_code, artifact = self._prompts("cursor", node="cursor-a")
        self.assertEqual(0, exit_code, artifact)
        self.assertEqual("ok", artifact["status"])
        written = sorted(path.name for path in self.out.iterdir())
        self.assertEqual(
            [
                "board-cursor-a.md",
                "boot-seat.md",
                "drain-cursor-a.md",
                "teardown-seat.md",
            ],
            written,
        )
        for path in self.out.iterdir():
            header = path.read_text(encoding="utf-8").splitlines()[:3]
            joined = "\n".join(header)
            self.assertIn("hand edits are overwritten", joined)
            self.assertIn("node prompts", joined)
        parity = artifact["evidence"]["parity"]["cursor"]
        self.assertIn(
            "teardown",
            parity,
            "parity report excludes teardown even though cursor realizes it",
        )
        for verb in ("boot", "board", "drain", "teardown"):
            self.assertTrue(parity[verb].startswith("file:"), parity[verb])
        for verb in ("pause", "resume", "prep-clear", "turnover"):
            self.assertTrue(parity[verb].startswith("dash:"), parity[verb])

    def test_cursor_request_against_stale_label_names_switch_or_reregister(self) -> None:
        """F2-CURSOR-ADAPTER-1: a stale harness label refuses with a remedy.

        Production change that would make this fail: `--harness cursor`
        remaining `lifecycle_prompts_harness_unknown` (remedy none) before
        the registry row is judged, so grok labelled `opencode-grok` never
        hears `floati node switch` or re-register.
        """

        Registry(self.root).register("legacy-cursor", "opencode-grok")
        exit_code, artifact = self._prompts("cursor", node="legacy-cursor")
        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("lifecycle_prompts_harness_mismatch", evidence["code"])
        for case in (
            "legacy-cursor", "opencode-grok", "cursor",
            "node switch", "re-register",
        ):
            self.assertIn(case, evidence["detail"])
        self.assertEqual("floati node switch --help", evidence["remedy"])
        self.assertFalse(self.out.exists(), "a refusal writes no projection")

    def test_reserved_stem_refusal_is_reachable_through_project_prompts(self) -> None:
        """LC-GEN-R1 (a): the reserved-name fence fires on a REAL write path.

        A fixture harness adapter whose file pattern is the BARE verb makes
        the collision shape real: the projection would write ``board.md``
        over the product's own command vocabulary, and the refusal must
        come out of project_prompts naming it — never out of a helper the
        test calls by hand.
        """

        from floati.lifecycle_prompts import DASH_RECEIPTS, HARNESS_ADAPTERS

        Registry(self.root).register("fixture-a", "fixture")
        self._assign("fixture-a", "architect", ARCHITECT_ANSWERS)
        # A well-formed adapter: every lifecycle cell realized or an honest
        # dash — the pattern alone is hostile (the bare verb collides).
        HARNESS_ADAPTERS["fixture"] = {
            "prompts_dir": None,
            "verbs": ("board", "boot", "drain", "pause", "resume", "prep-clear"),
            "file_pattern": "{verb}",
        }
        self.addCleanup(HARNESS_ADAPTERS.pop, "fixture", None)
        DASH_RECEIPTS[("fixture", "turnover")] = (
            "dash: fixture turnover (test receipt)"
        )
        self.addCleanup(DASH_RECEIPTS.pop, ("fixture", "turnover"), None)

        exit_code, artifact = self._prompts("fixture", node="fixture-a")
        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "lifecycle_prompts_name_reserved", artifact["evidence"]["code"]
        )
        self.assertIn("board", artifact["evidence"]["detail"])
        if self.out.exists():
            self.assertEqual(
                [], sorted(path.name for path in self.out.iterdir()),
                "no file lands when the reserved-name fence fires",
            )

    def test_foreign_file_under_out_refuses_by_path(self) -> None:
        """LC-GEN-R1 (b): a file under --out without the generator header
        is someone else's command file; the projection refuses by path.
        Files that DO carry the header are overwritten, as the header says.
        """

        exit_code, artifact = self._prompts("codex")
        self.assertEqual(0, exit_code, artifact)
        foreign = self.out / "board-codex-a.md"
        foreign.write_text(
            "# my own command file, not the generator's\n", encoding="utf-8"
        )

        exit_code, artifact = self._prompts("codex")
        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "lifecycle_prompts_target_foreign", artifact["evidence"]["code"]
        )
        self.assertIn("board-codex-a.md", artifact["evidence"]["detail"])
        self.assertEqual(
            "# my own command file, not the generator's\n",
            foreign.read_text(encoding="utf-8"),
            "the foreign file is left untouched",
        )

        foreign.write_text(
            "<!-- generated by floati node prompts (codex board prompt) at harbor "
            + "0" * 40 + "\n     hand edits are overwritten -->\n",
            encoding="utf-8",
        )
        exit_code, artifact = self._prompts("codex")
        self.assertEqual(0, exit_code, artifact)
        self.assertIn(
            "hand edits are overwritten",
            (self.out / "board-codex-a.md").read_text(encoding="utf-8"),
            "a generator-headed file is overwritten",
        )
