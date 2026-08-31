from __future__ import annotations

import json
import shutil
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

from floati import copy as copy_module
from floati.doctor import Doctor
from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from floati.update_consent import UpdateConsentLedger
from tests import test_au1_s2 as au1_s2


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UPDATE_CODES = (
    "update_ownership",
    "update_consent",
    "update_last_check",
    "update_last_apply",
)
COPY_KEYS = (
    "doctor.update.ownership",
    "doctor.update.consent",
    "doctor.update.consent_invalid",
    "doctor.update.never_checked",
    "doctor.update.last_check",
    "doctor.update.never_applied",
    "doctor.update.last_apply",
    "doctor.update.observation_invalid",
    "doctor.update.application_invalid",
)


def tree_bytes(root: Path) -> dict[Path, tuple[str, bytes]]:
    return {
        path.relative_to(root): (
            "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
            b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
        )
        for path in root.rglob("*")
    }


class AU1S3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = au1_s2.AU1S2Tests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fleet_home = self.fixture.base / "doctor-fleet"
        FloatiRoot.open_direct_home(self.fleet_home, create=True)

    def _doctor(self) -> tuple[dict[str, object], int]:
        return Doctor(
            self.fixture.source,
            self.fleet_home,
            ref="HEAD",
            destination=self.fixture.destination,
        ).artifact()

    @staticmethod
    def _update_findings(artifact: dict[str, object]) -> list[dict[str, object]]:
        findings = artifact["findings"]
        assert isinstance(findings, list)
        return [row for row in findings if row.get("code") in UPDATE_CODES]

    def _require_update_findings(
        self, artifact: dict[str, object]
    ) -> dict[str, dict[str, object]]:
        findings = self._update_findings(artifact)
        self.assertEqual(list(UPDATE_CODES), [row["code"] for row in findings])
        return {str(row["code"]): row for row in findings}

    def test_s3_00_doctor_projects_never_checked_without_network_or_writes(self) -> None:
        """Catches doctor creating an update lock, opening transport, or nagging before a check."""

        sys.modules.pop("floati.update_transport", None)
        before = tree_bytes(self.fixture.destination)
        with mock.patch.object(
            socket,
            "socket",
            side_effect=AssertionError("doctor must not create a network socket"),
        ):
            artifact, _ = self._doctor()

        self.assertEqual(before, tree_bytes(self.fixture.destination))
        self.assertNotIn("floati.update_transport", sys.modules)
        findings = list(self._require_update_findings(artifact).values())
        self.assertEqual(
            "updates have never been checked for this installation",
            findings[2]["detail"],
        )
        self.assertIsNone(findings[2]["remediation"])
        self.assertEqual(
            "no update has been applied or rolled back for this installation",
            findings[3]["detail"],
        )
        self.assertIsNone(findings[3]["remediation"])

    def test_s3_01_doctor_projects_active_consent_and_signed_check_receipts(self) -> None:
        """Catches doctor dropping exact consent/check coordinates or inventing freshness."""

        observation = self.fixture._observe()
        before = tree_bytes(self.fixture.destination)

        artifact, _ = self._doctor()

        self.assertEqual(before, tree_bytes(self.fixture.destination))
        findings = self._require_update_findings(artifact)
        consent = findings["update_consent"]["detail"]
        self.assertIn("state=active", consent)
        self.assertIn("epoch=1", consent)
        self.assertIn("predecessor=none", consent)
        self.assertIn(str(observation["consent_receipt_id"]), consent)
        checked = findings["update_last_check"]["detail"]
        self.assertIn(str(observation["id"]), checked)
        self.assertIn(str(observation["timestamp"]), checked)
        self.assertIn(str(observation["observed_version"]), checked)
        self.assertIn(str(observation["latest_source_sha"]), checked)
        self.assertIn("signature=verified", checked)
        self.assertNotIn("stale", checked.lower())

    def test_s3_02_cold_fixture_acceptance_projects_the_rollback_chain(self) -> None:
        """Catches fixture verification, A-to-B-to-A, or the final wiring join being omitted."""

        observation = self.fixture._observe()
        applied_b = self.fixture._run_apply(
            self.fixture.bundle_b,
            idempotency_key="s3-apply-b",
        )
        rolled_back_a = self.fixture._run_apply(
            self.fixture.bundle_a,
            version="1.0.0",
            idempotency_key="s3-rollback-a",
        )

        artifact, _ = self._doctor()

        self.assertEqual(self.fixture.bytes_a, self.fixture._destination_managed_bytes())
        self.assertEqual(self.fixture.sha_a, rolled_back_a["source_sha"])
        self.assertEqual(self.fixture.sha_b, rolled_back_a["previous_source_sha"])
        self.assertEqual(observation["id"], applied_b["check_observation_id"])
        self.assertEqual(observation["id"], rolled_back_a["check_observation_id"])
        last_apply = self._require_update_findings(artifact)["update_last_apply"][
            "detail"
        ]
        for value in (
            rolled_back_a["id"],
            rolled_back_a["version"],
            rolled_back_a["previous_source_sha"],
            rolled_back_a["source_sha"],
            rolled_back_a["check_observation_id"],
            rolled_back_a["wiring_journal"],
        ):
            self.assertIn(str(value), last_apply)

    def test_s3_03_every_visible_doctor_string_is_registered_without_marker(self) -> None:
        """Catches S3 copy bypassing the ledger, and any provenance marker
        surviving into shipped copy after the 2026-08-29 voice pass."""

        self._doctor()
        self.assertEqual([], [key for key in COPY_KEYS if key not in copy_module._ENTRIES])
        self.assertEqual(
            [],
            [
                key
                for key in COPY_KEYS
                if copy_module._ENTRIES[key][0].startswith("DRAFT")
            ],
        )
        self.assertEqual(
            copy_module.copy_ledger_markdown(),
            (REPOSITORY_ROOT / "docs" / "COPY-LEDGER.md").read_text(encoding="utf-8"),
        )

    def test_s3_04_public_trust_absence_refuses_before_transport(self) -> None:
        """Catches fixture success being mistaken for an installation with public trust."""

        shutil.rmtree(self.fixture.destination / "trust")
        sys.modules.pop("floati.update_transport", None)
        before = tree_bytes(self.fixture.destination)
        with (
            mock.patch.object(
                socket,
                "socket",
                side_effect=AssertionError("missing trust must refuse before a socket"),
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            UpdateConsentLedger(self.fixture.destination).consent(
                channel=self.fixture.channel,
                epoch=1,
                idempotency_key="s3-public-trust-absent",
            )

        self.assertEqual("update_trust_unprovisioned", caught.exception.code)
        self.assertNotIn("floati.update_transport", sys.modules)
        self.assertEqual(before, tree_bytes(self.fixture.destination))

    def test_s3_05_malformed_update_truth_is_typed_and_physically_read_only(self) -> None:
        """Catches doctor skipping malformed update testimony or repairing it during diagnosis."""

        observations = (
            self.fixture.destination
            / ".floati-install"
            / "update-observations.v0.jsonl"
        )
        observations.write_text('{"kind":"update_observation"}\n', encoding="utf-8")
        before = tree_bytes(self.fixture.destination)

        artifact, rc = self._doctor()

        self.assertEqual(33, rc)
        self.assertEqual(before, tree_bytes(self.fixture.destination))
        finding = next(
            row for row in artifact["findings"]
            if row["code"] == "update_ledger_record_invalid"
        )
        self.assertEqual("error", finding["severity"])
        self.assertIsNone(finding["remediation"])

    def test_s3_06_foreign_destination_consent_cannot_project_as_local_truth(self) -> None:
        """Catches a copied shape-valid consent row being displayed for this installation."""

        self.fixture._observe()
        consent_path = (
            self.fixture.destination
            / ".floati-install"
            / "update-consent.v0.jsonl"
        )
        consent = json.loads(consent_path.read_text(encoding="utf-8"))
        consent["destination"] = str(self.fixture.base / "foreign-install")
        consent_path.write_text(
            json.dumps(consent, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        before = tree_bytes(self.fixture.destination)

        artifact, rc = self._doctor()

        self.assertEqual(33, rc)
        self.assertEqual(before, tree_bytes(self.fixture.destination))
        finding = next(
            row for row in artifact["findings"]
            if row["code"] == "update_consent_record_invalid"
        )
        self.assertEqual("error", finding["severity"])
        self.assertIsNone(finding["remediation"])

    def test_s3_07_foreign_update_observation_cannot_project_as_local_truth(self) -> None:
        """Catches a copied shape-valid check observation being displayed as local."""

        self.fixture._observe()
        observations_path = (
            self.fixture.destination
            / ".floati-install"
            / "update-observations.v0.jsonl"
        )
        observation = json.loads(observations_path.read_text(encoding="utf-8"))
        observation["destination"] = str(self.fixture.base / "foreign-install")
        observations_path.write_text(
            json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        before = tree_bytes(self.fixture.destination)

        artifact, rc = self._doctor()

        self.assertEqual(33, rc)
        self.assertEqual(before, tree_bytes(self.fixture.destination))
        finding = next(
            row for row in artifact["findings"]
            if row["code"] == "update_observation_invalid"
        )
        self.assertEqual("error", finding["severity"])
        self.assertIsNone(finding["remediation"])

    def test_s3_08_application_must_link_one_local_check_observation(self) -> None:
        """Catches an orphan shape-valid application receipt being displayed as verified."""

        self.fixture._observe()
        self.fixture._run_apply(
            self.fixture.bundle_b,
            idempotency_key="s3-orphan-application",
        )
        observations_path = (
            self.fixture.destination
            / ".floati-install"
            / "update-observations.v0.jsonl"
        )
        rows = [
            json.loads(line)
            for line in observations_path.read_text(encoding="utf-8").splitlines()
        ]
        rows[-1]["check_observation_id"] = "update-observation-orphan"
        observations_path.write_text(
            "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
        before = tree_bytes(self.fixture.destination)

        artifact, rc = self._doctor()

        self.assertEqual(33, rc)
        self.assertEqual(before, tree_bytes(self.fixture.destination))
        finding = next(
            row for row in artifact["findings"]
            if row["code"] == "update_application_invalid"
        )
        self.assertEqual("error", finding["severity"])
        self.assertIsNone(finding["remediation"])


if __name__ == "__main__":
    unittest.main()
