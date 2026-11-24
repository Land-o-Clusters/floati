"""F2-ROLE-EVIDENCE-LEGACY-1: six proved historical pins are readable evidence.

Pins measured from the shipped templates immediately before and at
e2e1863dd42cdcbee2d561df5c18d553944071bc. That migration changed only
template_version 1 -> 2 and added ack_sla_minutes=45. No compatibility
constants from the implementation are imported here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.admin_registry import RegistryAdminBackend
from floati.doctor import Doctor, _role_ack_slas, _role_cadences
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.registry import Registry, read_registry_compatible
from floati.role_assignment import RoleAssignmentPlan
from floati.role_templates import load_role_template
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_cli import LAUNCHER


SOURCE = Path(__file__).resolve().parents[1]
# role: (historical v1 pin, declared v2 pin). The architect pair reproduces
# the September 5 live incident, without reading or changing its live root.
PROVED_PAIRS = {
    "architect": (
        "7ac392ebe9f68676e117282d81e65cccfb7d19cf249e1159f012bfa89b689d24",
        "28131d93807d83a8f42f7ce5fb89507f1ddd3579c617cad28941a9f0ba36bc10",
    ),
    "builder": (
        "ae6ccfa31c8ef96b2f378830333c45ea5390ba3d70924f6d0b56319a7c97573b",
        "12a1177a569f629e44795e1b1538f735cc9ad0ca93ce62901ca8093a5d699846",
    ),
    "github-manager": (
        "43b55bbdafc21a5b93d803673e3f656ba29888aa066e779c0ddd34d862af2bdf",
        "eea7a59ff550efd817a5ca12add836533c7e4fbee6ee535c8b99a18953bf1929",
    ),
    "researcher": (
        "cf3e039f7a6b14ee3eea1edf3cca859ea131638c50c05a974db9822e413a8e82",
        "2f2b5905e6e1ac7d8df23e426c8e4410956033d3aa7f0f5ba4f53a2870197a24",
    ),
    "reviewer": (
        "477135125e53df3a5820d347577b011453c0bf0ed87e04ba09075179f58cd9a1",
        "a5599b1881daecf2fbd8987d4ed27da7857ebce9104ac62beb905e0ba99f8c46",
    ),
    "sre": (
        "3fed4d1e92b21a4a12bff60f7e574e32afc19e0c6d702fca34406c42ed7fc516",
        "802f929e75099ee1b15a5f3e7a02ca675dda7572c0d16045f976accbfaa63448",
    ),
}


def _canonical_digest(record: dict) -> str:
    encoded = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _v1_record(current: dict) -> dict:
    historical = dict(current, template_version=1)
    historical.pop("ack_sla_minutes", None)
    return historical


class RoleEvidenceLegacyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def _root(self, label: str) -> FloatiRoot:
        return FloatiRoot.open_direct_home(self.base / label, create=True)

    def _assign(
        self, root: FloatiRoot, role: str, version: int, pin: str,
        *, node: str = "assigned-node", answers: dict | None = None,
    ) -> tuple[str, dict]:
        Registry(root).register(node, "Codex")
        record = {
            "schema_version": 0,
            "id": "registry-role-" + uuid7_hex(),
            "tenant_id": root.tenant_id,
            "timestamp": "2026-09-05T12:00:00.000Z",
            "kind": "registry_role_record",
            "node_id": node,
            "template_role": role,
            "template_version": version,
            "template_sha256": pin,
            "answers": answers if answers is not None else {
                "repo": "floati",
                "never_touch": "foreign-project",
                "reports_to": "owner-tier",
            },
            "state": "active",
            "predecessor_role_record_id": None,
        }
        RegistryAdminBackend(root).commit_role(
            RoleAssignmentPlan(node_id=node, template_role=role, record=record)
        )
        return node, record

    def _bytes(self, root: FloatiRoot, source: Path = SOURCE) -> dict[Path, bytes]:
        paths = [root.resolve_relative("registry/entries.jsonl")]
        paths.extend(source / "roles/shipped" / (role + ".json") for role in PROVED_PAIRS)
        custom = root.resolve_relative("roles/custom")
        if custom.is_dir():
            paths.extend(custom.glob("*.json"))
        return {path: path.read_bytes() for path in paths}

    def _read_evidence(
        self, reader, root: FloatiRoot, node: str, source: Path = SOURCE
    ) -> dict:
        before = self._bytes(root, source)
        try:
            rows, _unknown, _versions = read_registry_compatible(root)
            return reader(source, [node], rows, root=root)
        finally:
            self.assertEqual(before, self._bytes(root, source), "role reads must preserve durable bytes")

    def _doctor(self, root: FloatiRoot) -> tuple[dict, int]:
        before = self._bytes(root)
        try:
            return Doctor(SOURCE, root.path, ref="HEAD").artifact()
        finally:
            self.assertEqual(before, self._bytes(root), "Doctor must preserve registry/template bytes")

    def _legacy_root(self, role: str) -> tuple[FloatiRoot, str]:
        root = self._root(role)
        node, _record = self._assign(root, role, 1, PROVED_PAIRS[role][0])
        return root, node

    def _legacy_findings(self, artifact: dict) -> list[dict]:
        return [row for row in artifact["findings"] if row["code"] == "role_evidence_legacy"]

    def _assert_refused_readers(self, root: FloatiRoot, node: str, source: Path = SOURCE) -> None:
        for reader in (_role_cadences, _role_ack_slas):
            with self.subTest(reader=reader.__name__):
                with self.assertRaises(IntegrityFailure) as caught:
                    self._read_evidence(reader, root, node, source)
                self.assertEqual("role_cadence_invalid", caught.exception.code)

    def _explicit_source(self, label: str) -> Path:
        source = self.base / label
        shipped = source / "roles/shipped"
        shipped.mkdir(parents=True)
        for role in PROVED_PAIRS:
            filename = role + ".json"
            (shipped / filename).write_bytes((SOURCE / "roles/shipped" / filename).read_bytes())
        return source

    def test_measured_pairs_audit_exact_two_field_migration(self) -> None:
        self.assertEqual(6, len(PROVED_PAIRS))
        for role, (historical_pin, current_pin) in PROVED_PAIRS.items():
            with self.subTest(role=role):
                current = json.loads((SOURCE / "roles/shipped" / (role + ".json")).read_text())
                self.assertEqual(role, current["role"])
                self.assertEqual(2, current["template_version"])
                self.assertEqual(45, current["ack_sla_minutes"])
                self.assertEqual(current_pin, _canonical_digest(current))
                historical = _v1_record(current)
                self.assertEqual(historical_pin, _canonical_digest(historical))
                self.assertEqual("envelope-per-row", historical["cadence"])
                self.assertNotIn("ack_sla_minutes", historical)

    def test_all_six_historical_pins_preserve_the_known_cadence(self) -> None:
        for role in PROVED_PAIRS:
            with self.subTest(role=role):
                root, node = self._legacy_root(role)
                try:
                    evidence = self._read_evidence(_role_cadences, root, node)
                except IntegrityFailure as exc:
                    self.fail(f"proved shipped v1 cadence refused: {exc.code}: {exc.detail}")
                self.assertEqual({node: "envelope-per-row"}, evidence)

    def test_all_six_historical_pins_have_no_declared_ack_sla(self) -> None:
        for role in PROVED_PAIRS:
            with self.subTest(role=role):
                root, node = self._legacy_root(role)
                try:
                    evidence = self._read_evidence(_role_ack_slas, root, node)
                except IntegrityFailure as exc:
                    self.fail(f"proved shipped v1 SLA absence refused: {exc.code}: {exc.detail}")
                self.assertIsNone(evidence.get(node), "the historical template never declared 45")
                self.assertEqual({}, evidence)

    def test_native_doctor_does_not_call_any_proved_legacy_pin_malformed(self) -> None:
        for role in PROVED_PAIRS:
            with self.subTest(role=role):
                root, _node = self._legacy_root(role)
                artifact, exit_code = self._doctor(root)
                self.assertNotEqual(33, exit_code, [
                    row["code"] for row in artifact["findings"] if row["severity"] == "error"
                ])

    def test_native_doctor_emits_one_typed_info_finding_for_each_legacy_node(self) -> None:
        for role in PROVED_PAIRS:
            with self.subTest(role=role):
                root, node = self._legacy_root(role)
                artifact, _exit_code = self._doctor(root)
                findings = self._legacy_findings(artifact)
                self.assertEqual(1, len(findings), [row["code"] for row in artifact["findings"]])
                self.assertEqual("info", findings[0]["severity"])
                self.assertEqual(node, findings[0]["subject"])

    def test_all_six_legacy_findings_name_a_pending_answer_preserving_migration(self) -> None:
        for role, (historical_pin, _current_pin) in PROVED_PAIRS.items():
            with self.subTest(role=role):
                root = self._root(role)
                node, _record = self._assign(
                    root, role, 1, historical_pin, node="legacy-" + role,
                    answers={
                        "repo": "floati",
                        "never_touch": "foreign-project",
                        "owner_stops" if role == "architect" else "reports_to": "owner-tier",
                    },
                )
                artifact, _exit_code = self._doctor(root)
                findings = self._legacy_findings(artifact)
                self.assertEqual(1, len(findings))
                self.assertEqual(node, findings[0]["subject"])
                remedy = findings[0]["remediation"]
                self.assertIsInstance(remedy, str)
                self.assertIn("`floati node role ", remedy)
                argv = shlex.split(remedy.split("`")[1])
                expected = [
                    "floati", "node", "role", "--root", str(root.path),
                    "--node", node, "--template", role,
                    "--expected-predecessor", _record["id"],
                ]
                for key in sorted(_record["answers"]):
                    expected.extend(["--answer", key + "=" + _record["answers"][key]])
                self.assertEqual(expected, argv)
                for key, value in _record["answers"].items():
                    self.assertIn(key + "=" + value, argv)
                    self.assertNotIn(key + "=VALUE", argv)
                self.assertIn("role " + role + " version 1", remedy)
                self.assertIn("same role version 2", remedy)
                self.assertIn("preserving every recorded answer", remedy)
                self.assertIn("compare-and-swaps the current row", remedy)

    def test_all_six_exact_current_pins_keep_the_declared_cadence_and_sla(self) -> None:
        for role, (_historical_pin, current_pin) in PROVED_PAIRS.items():
            with self.subTest(role=role):
                root = self._root(role)
                node, _record = self._assign(root, role, 2, current_pin)
                self.assertEqual({node: "envelope-per-row"}, self._read_evidence(_role_cadences, root, node))
                self.assertEqual({node: 45}, self._read_evidence(_role_ack_slas, root, node))
                artifact, exit_code = self._doctor(root)
                self.assertNotEqual(33, exit_code, artifact)
                self.assertEqual([], self._legacy_findings(artifact))

    def test_unknown_hash_and_unknown_version_remain_refused(self) -> None:
        for label, version, pin in (
            ("unknown-hash", 1, "0" * 64),
            ("unknown-version", 3, PROVED_PAIRS["architect"][0]),
            ("current-pin-wrong-version", 1, PROVED_PAIRS["architect"][1]),
            ("historical-pin-wrong-version", 2, PROVED_PAIRS["architect"][0]),
        ):
            with self.subTest(case=label):
                root = self._root(label)
                node, _record = self._assign(root, "architect", version, pin)
                self._assert_refused_readers(root, node)
                artifact, exit_code = self._doctor(root)
                self.assertEqual(33, exit_code, artifact)
                self.assertEqual([], self._legacy_findings(artifact))

    def test_a_proved_historical_pin_cannot_bind_a_different_role(self) -> None:
        root = self._root("wrong-role")
        node, _record = self._assign(root, "builder", 1, PROVED_PAIRS["architect"][0])
        self._assert_refused_readers(root, node)
        artifact, exit_code = self._doctor(root)
        self.assertEqual(33, exit_code, artifact)
        self.assertEqual([], self._legacy_findings(artifact))

    def test_custom_predecessor_is_not_a_proved_shipped_migration(self) -> None:
        root = self._root("custom-predecessor")
        custom = root.resolve_relative("roles/custom/custom-builder.json")
        custom.parent.mkdir(parents=True)
        payload = json.loads((SOURCE / "roles/shipped/builder.json").read_text())
        payload.update(role="custom-builder", cadence="on-demand", ack_sla_minutes=17)
        custom.write_text(json.dumps(payload))
        node, _record = self._assign(root, "custom-builder", 1, _canonical_digest(_v1_record(payload)))
        self._assert_refused_readers(root, node)
        artifact, exit_code = self._doctor(root)
        self.assertEqual(33, exit_code, artifact)
        self.assertEqual([], self._legacy_findings(artifact))

    def test_altered_explicit_v2_source_does_not_make_its_computed_v1_pin_legacy(self) -> None:
        source = self._explicit_source("altered-source")
        target = source / "roles/shipped/architect.json"
        payload = json.loads(target.read_text())
        payload["cadence"] = "on-demand"
        target.write_text(json.dumps(payload))
        computed_predecessor = _canonical_digest(_v1_record(payload))
        self.assertNotEqual(PROVED_PAIRS["architect"][0], computed_predecessor)
        for label, pin in (
            ("computed-predecessor", computed_predecessor),
            ("proved-pin-changed-source", PROVED_PAIRS["architect"][0]),
        ):
            with self.subTest(case=label):
                root = self._root(label)
                node, _record = self._assign(root, "architect", 1, pin)
                self._assert_refused_readers(root, node, source)

    def test_sla_only_modified_v2_source_refuses_v1_but_accepts_its_exact_pin(self) -> None:
        source = self._explicit_source("sla-only-source")
        target = source / "roles/shipped/builder.json"
        payload = json.loads(target.read_text())
        payload["ack_sla_minutes"] = 17
        target.write_text(json.dumps(payload))
        historical_pin, shipped_current_pin = PROVED_PAIRS["builder"]
        self.assertEqual(historical_pin, _canonical_digest(_v1_record(payload)))
        template = load_role_template(target)
        self.assertNotEqual(shipped_current_pin, template.digest)

        legacy_root = self._root("sla-only-legacy")
        legacy_node, _record = self._assign(
            legacy_root, "builder", 1, historical_pin
        )
        self._assert_refused_readers(legacy_root, legacy_node, source)

        current_root = self._root("sla-only-current")
        current_node, _record = self._assign(
            current_root, "builder", 2, template.digest
        )
        self.assertEqual(
            {current_node: "envelope-per-row"},
            self._read_evidence(_role_cadences, current_root, current_node, source),
        )
        self.assertEqual(
            {current_node: 17},
            self._read_evidence(_role_ack_slas, current_root, current_node, source),
        )

    def test_exact_custom_current_source_keeps_its_own_cadence_and_sla(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version):
                root = self._root(f"exact-custom-{version}")
                target = root.resolve_relative("roles/custom/custom-builder.json")
                target.parent.mkdir(parents=True)
                payload = json.loads((SOURCE / "roles/shipped/builder.json").read_text())
                payload.update(role="custom-builder", template_version=version,
                               cadence="on-demand", ack_sla_minutes=17)
                target.write_text(json.dumps(payload))
                template = load_role_template(target)
                node, _record = self._assign(root, template.role, version, template.digest)
                self.assertEqual({node: "on-demand"}, self._read_evidence(_role_cadences, root, node))
                self.assertEqual({node: 17}, self._read_evidence(_role_ack_slas, root, node))
                artifact, exit_code = self._doctor(root)
                self.assertNotEqual(33, exit_code, artifact)
                self.assertEqual([], self._legacy_findings(artifact))

    def test_exact_modified_explicit_source_still_passes_its_current_contract(self) -> None:
        source = self._explicit_source("exact-source")
        target = source / "roles/shipped/builder.json"
        payload = json.loads(target.read_text())
        payload.update(cadence="on-demand", ack_sla_minutes=17)
        target.write_text(json.dumps(payload))
        template = load_role_template(target)
        root = self._root("exact-explicit")
        node, _record = self._assign(root, "builder", template.template_version, template.digest)
        self.assertEqual({node: "on-demand"}, self._read_evidence(_role_cadences, root, node, source))
        self.assertEqual({node: 17}, self._read_evidence(_role_ack_slas, root, node, source))
        self._assert_refused_readers(root, node)

    def test_unassigned_registered_node_is_missing_role_evidence_not_legacy(self) -> None:
        root = self._root("unassigned")
        node = "unassigned-node"
        Registry(root).register(node, "Codex")
        rows, _unknown, _versions = read_registry_compatible(root)
        self.assertFalse(any(row["kind"] == "registry_role_record" for row in rows))
        self.assertEqual({}, self._read_evidence(_role_cadences, root, node))
        self.assertEqual({}, self._read_evidence(_role_ack_slas, root, node))
        artifact, exit_code = self._doctor(root)
        self.assertNotEqual(33, exit_code, artifact)
        self.assertEqual([], self._legacy_findings(artifact))

    def _node_role(self, root: FloatiRoot, node: str, role: str, answers: dict) -> subprocess.CompletedProcess[str]:
        argv = [
            str(LAUNCHER), "node", "role", "--root", str(root.path),
            "--node", node, "--template", role,
        ]
        for key, value in answers.items():
            argv.extend(["--answer", key + "=" + value])
        return subprocess.run(
            argv, cwd=str(SOURCE), env=dict(os.environ),
            text=True, capture_output=True, check=False,
        )

    def _role_rows(self, root: FloatiRoot, node: str) -> list[dict]:
        rows, _unknown, _versions = read_registry_compatible(root)
        return [
            row for row in rows
            if row.get("kind") == "registry_role_record" and row.get("node_id") == node
        ]

    def test_filled_node_role_succeeds_against_existing_v1_assignment(self) -> None:
        """Catches node role treating the current v1 row as a null predecessor."""
        answers = {
            "repo": "floati",
            "never_touch": "foreign-project",
            "owner_stops": "owner-tier",
        }
        root = self._root("migrate-architect")
        node, record = self._assign(
            root, "architect", 1, PROVED_PAIRS["architect"][0],
            node="legacy-architect", answers=answers,
        )
        result = self._node_role(root, node, "architect", answers)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        rows = self._role_rows(root, node)
        self.assertEqual(2, len(rows), rows)
        self.assertEqual(record["id"], rows[1]["predecessor_role_record_id"])
        self.assertEqual(answers, rows[1]["answers"])
        self.assertEqual(2, rows[1]["template_version"])
        self.assertEqual(PROVED_PAIRS["architect"][1], rows[1]["template_sha256"])
        self.assertEqual([], self._legacy_findings(self._doctor(root)[0]))

    def test_node_role_still_refuses_a_stale_predecessor(self) -> None:
        """Catches compare-and-swap accepting a predecessor that is no longer current."""
        answers = {
            "repo": "floati",
            "never_touch": "foreign-project",
            "owner_stops": "owner-tier",
        }
        root = self._root("stale-predecessor")
        node, first = self._assign(
            root, "architect", 1, PROVED_PAIRS["architect"][0],
            node="legacy-architect", answers=answers,
        )
        current = dict(first)
        current.update({
            "id": "registry-role-" + uuid7_hex(),
            "template_version": 2,
            "template_sha256": PROVED_PAIRS["architect"][1],
            "predecessor_role_record_id": first["id"],
        })
        RegistryAdminBackend(root).commit_role(
            RoleAssignmentPlan(node_id=node, template_role="architect", record=current)
        )
        stale = dict(current)
        stale.update({
            "id": "registry-role-" + uuid7_hex(),
            "predecessor_role_record_id": first["id"],
        })
        with self.assertRaises(ProtocolRefusal) as caught:
            RegistryAdminBackend(root).commit_role(
                RoleAssignmentPlan(node_id=node, template_role="architect", record=stale)
            )
        self.assertEqual("role_preview_invalid", caught.exception.code)
        self.assertEqual("role predecessor is stale", caught.exception.detail)

    def test_genuinely_malformed_durable_role_record_still_returns_33(self) -> None:
        root = self._root("malformed-record")
        self._assign(root, "architect", 2, PROVED_PAIRS["architect"][1])
        registry = root.resolve_relative("registry/entries.jsonl")
        rows = decode_frames(registry.read_bytes())
        role = next(row for row in rows if row["kind"] == "registry_role_record")
        role["template_sha256"] = "not-a-sha256"
        registry.write_bytes(b"".join(encode_frame(row) for row in rows))
        with self.assertRaises(IntegrityFailure):
            read_registry_compatible(root)
        artifact, exit_code = self._doctor(root)
        self.assertEqual(33, exit_code, artifact)
        self.assertEqual([], self._legacy_findings(artifact))

    def test_printed_writer_does_not_overwrite_a_newer_row(self) -> None:
        """R3 RED: doctor command replayed after another writer overwrote the row."""

        answers = {
            "repo": "floati",
            "never_touch": "foreign-project",
            "owner_stops": "owner-tier",
        }
        root = self._root("stale-overwrite")
        node, _record = self._assign(
            root, "architect", 1, PROVED_PAIRS["architect"][0],
            node="legacy-architect", answers=answers,
        )
        artifact, _exit_code = self._doctor(root)
        argv = shlex.split(self._legacy_findings(artifact)[0]["remediation"].split("`")[1])
        first = self._node_role(
            root, node, "architect", dict(answers, repo="OTHER-WRITER"),
        )
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        self.assertEqual(
            "OTHER-WRITER", self._role_rows(root, node)[-1]["answers"]["repo"]
        )
        replay = subprocess.run(
            [str(LAUNCHER), *argv[1:]],
            cwd=str(SOURCE), env=dict(os.environ),
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(0, replay.returncode, replay.stdout + replay.stderr)
        self.assertEqual(
            "OTHER-WRITER", self._role_rows(root, node)[-1]["answers"]["repo"]
        )

    def test_node_role_refuses_demoting_the_only_architect(self) -> None:
        """R3 RED: unguarded node role demotes the only architect without transfer-architect."""

        answers = {
            "repo": "floati",
            "never_touch": "foreign-project",
            "owner_stops": "owner-tier",
        }
        root = self._root("demote-architect")
        node, _record = self._assign(
            root, "architect", 2, PROVED_PAIRS["architect"][1],
            node="sole-architect", answers=answers,
        )
        result = self._node_role(
            root, node, "builder",
            {"repo": "floati", "never_touch": "foreign-project", "reports_to": node},
        )
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("role predecessor is stale", combined)
        self.assertEqual("architect", self._role_rows(root, node)[-1]["template_role"])


if __name__ == "__main__":
    unittest.main()
