"""LEDGER-1 (b): the epoch roll becomes POLICY, surfaced by doctor.

The 08-29 roll into ``archive-2026-08-29/`` was performed by hand — no
threshold anywhere states when a roll is due. The policy is declared in
the root (``state/ledger-policy.json``, operator-declared) with shipped
defaults when absent; doctor measures the live ledger's SIZE and AGE
against it and names the governed roll verb as the remedy. Doctor never
rolls by itself: ``epoch roll`` stays governed, authority-gated, and
idempotent by key.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


def _policy(self: unittest.TestCase, payload: dict | None) -> None:
    policy_dir = self.root_path / "state"
    policy_dir.mkdir(exist_ok=True)
    if payload is not None:
        (policy_dir / "ledger-policy.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def _ledger(self: unittest.TestCase, *, bytes_: int = 0, first_age_days: float | None = None) -> None:
    line = json.dumps(
        {
            "id": "msg-seedrecord000000000000000000000000000",
            "kind": "message_envelope",
            "timestamp": (
                (
                    datetime.now(timezone.utc) - timedelta(days=first_age_days)
                ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if first_age_days is not None
                else "2026-09-05T00:00:00.000Z"
            ),
        }
    ) + "\n"
    payload = line + ("x" * max(0, bytes_ - len(line))) if bytes_ > len(line) else line
    (self.root_path / "events.jsonl").write_text(payload, encoding="utf-8")


class LedgerRollPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)

    def _finding(self):
        from floati.doctor import project_ledger_roll_policy_finding

        return project_ledger_roll_policy_finding(self.root)

    def test_oversized_ledger_produces_the_doctor_line(self) -> None:
        """RED: a ledger past the threshold produces no doctor line."""

        _policy(self, {"schema_version": 0, "max_bytes": 64, "max_age_days": 30})
        _ledger(self, bytes_=200, first_age_days=1.0)

        finding = self._finding()

        self.assertEqual("ledger_roll_policy", finding["code"])
        self.assertEqual("warning", finding["severity"])
        self.assertIn("200", finding["detail"])
        self.assertIn("64", finding["detail"])
        self.assertIn("epoch roll", finding["remediation"])

    def test_ledger_age_against_the_policy(self) -> None:
        _policy(self, {"schema_version": 0, "max_bytes": 1_000_000, "max_age_days": 30})
        _ledger(self, bytes_=400, first_age_days=40.0)

        finding = self._finding()

        self.assertEqual("warning", finding["severity"])
        self.assertIn("40", finding["detail"], "the age must be named in days")

    def test_fresh_small_ledger_is_ok(self) -> None:
        _policy(self, {"schema_version": 0, "max_bytes": 1_000_000, "max_age_days": 30})
        _ledger(self, bytes_=400, first_age_days=1.0)

        finding = self._finding()

        self.assertEqual("ledger_roll_policy", finding["code"])
        self.assertEqual("ok", finding["severity"])

    def test_shipped_defaults_apply_without_a_policy_file(self) -> None:
        _ledger(self, bytes_=400, first_age_days=1.0)

        finding = self._finding()

        self.assertEqual("ok", finding["severity"])
        self.assertIn("shipped default", finding["detail"])

    def test_malformed_policy_is_refused_typed(self) -> None:
        _policy(self, {"schema_version": 0, "max_bytes": -5, "max_age_days": 30})
        _ledger(self, bytes_=10, first_age_days=1.0)

        from floati.errors import ProtocolRefusal

        with self.assertRaises(ProtocolRefusal) as raised:
            self._finding()
        self.assertEqual("ledger_policy_invalid", raised.exception.code)

    def test_absent_ledger_is_a_typed_absence(self) -> None:
        _policy(self, {"schema_version": 0, "max_bytes": 64, "max_age_days": 30})

        finding = self._finding()

        self.assertEqual("ledger_roll_policy", finding["code"])
        self.assertEqual("ok", finding["severity"])
        self.assertIn("no live ledger", finding["detail"])


class LedgerRollPolicyWiringTests(unittest.TestCase):
    def test_doctor_artifact_carries_the_policy_row(self) -> None:
        from floati.doctor import Doctor

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            home = Path(temporary) / "fleet"
            FloatiRoot.open_direct_home(home, create=True)
            (home / "state").mkdir()
            (home / "state" / "ledger-policy.json").write_text(
                json.dumps(
                    {"schema_version": 0, "max_bytes": 64, "max_age_days": 30}
                ) + "\n",
                encoding="utf-8",
            )
            (home / "events.jsonl").write_text(
                json.dumps(
                    {
                        "id": "msg-seedrecord000000000000000000000000000",
                        "kind": "message_envelope",
                        "timestamp": "2026-09-05T00:00:00.000Z",
                    }
                )
                + ("x" * 200)
                + "\n",
                encoding="utf-8",
            )

            doctor = Doctor(
                Path(__file__).resolve().parents[1],
                home,
                ref="origin/main",
                no_sandbox=True,
            )
            artifact, _rc = doctor.artifact()

            codes = [row["code"] for row in artifact.get("findings", [])]
            self.assertIn("ledger_roll_policy", codes)


if __name__ == "__main__":
    unittest.main()


class LedgerRollPolicyAm1Tests(unittest.TestCase):
    """LEDGER-1 (b) Am.1: the symlink guard was DEAD CODE —
    resolve_relative dereferences the final symlink before any
    is_symlink check, so a symlinked policy was applied as
    operator_declared."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        (self.root_path / "state").mkdir()
        (self.root_path / "events.jsonl").write_text(
            json.dumps(
                {
                    "id": "msg-seedrecord000000000000000000000000000",
                    "kind": "message_envelope",
                    "timestamp": "2026-09-05T00:00:00.000Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _finding(self):
        from floati.doctor import project_ledger_roll_policy_finding

        return project_ledger_roll_policy_finding(self.root)

    def test_in_root_symlinked_policy_refuses_by_name(self) -> None:
        """RED: the symlinked policy was APPLIED as operator_declared."""

        target = self.root_path / "state" / "real-policy.json"
        target.write_text(
            json.dumps({"schema_version": 0, "max_bytes": 64, "max_age_days": 30}),
            encoding="utf-8",
        )
        policy = self.root_path / "state" / "ledger-policy.json"
        policy.symlink_to(target)

        from floati.errors import ProtocolRefusal

        with self.assertRaises(ProtocolRefusal) as raised:
            self._finding()
        self.assertEqual("ledger_policy_symlink", raised.exception.code)
        self.assertIsNotNone(raised.exception.remedy)

    def test_malformed_policy_refusal_names_a_remedy(self) -> None:
        (self.root_path / "state" / "ledger-policy.json").write_text(
            json.dumps({"schema_version": 0, "max_bytes": -5, "max_age_days": 30}),
            encoding="utf-8",
        )

        from floati.errors import ProtocolRefusal

        with self.assertRaises(ProtocolRefusal) as raised:
            self._finding()
        self.assertEqual("ledger_policy_invalid", raised.exception.code)
        self.assertIsInstance(
            raised.exception.remedy, str,
            "kind:none today — the unnamed placeholder is not a remedy",
        )

    def test_age_unknown_is_its_own_severity_word(self) -> None:
        """RED: an unreadable first entry read age unknown at severity ok."""

        (self.root_path / "events.jsonl").write_text("not json\n", encoding="utf-8")

        finding = self._finding()

        self.assertEqual("warning", finding["severity"])
        self.assertIn("age unknown", finding["detail"])
