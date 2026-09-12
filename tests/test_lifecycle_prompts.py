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

import io
import tempfile
import unittest
from pathlib import Path

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

#: LC-1 §1 realized file projections per harness (LC-1d); the zcode section
#: file realizes both BOOT and WIND-DOWN (§1: the BOOT.md §clear calls it).
EXPECTED_FILES = {
    "codex": ("boot", "board", "drain", "pause", "resume", "prep-clear"),
    "claude": ("boot", "prep-clear", "board"),
    "zcode": ("boot", "prep-clear"),
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

    def test_node_prompts_projects_the_command_files(self) -> None:
        """RED: no verb parses — the projections are hand-written today."""

        exit_code, artifact = self._prompts("codex")
        self.assertEqual(0, exit_code, artifact)
        self.assertEqual("ok", artifact["status"])
        written = sorted(path.name for path in self.out.iterdir())
        self.assertEqual(
            sorted(f"{verb}-codex-a.md" for verb in EXPECTED_FILES["codex"]),
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
        self.assertEqual(len(reserved), 221, sorted(reserved))
        for name in ("board", "prompts", "prep-clear", "wait", "pausebus", "resumebus"):
            self.assertIn(name, reserved)

        exit_code, artifact = self._prompts("cursor")
        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])

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
