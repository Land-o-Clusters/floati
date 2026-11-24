from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


SCHEMA = Path("schemas/v1/doctor-artifact.schema.json")
TRANSIENT = {
    "kind": "transient", "action": "retry_once", "delay_seconds": 1,
    "holder": "codex-stop-holder",
}


class DoctorRemedySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.finding = {
            "code": "ledger_lock_timeout", "severity": "error",
            "subject": "root", "detail": "The declared ledger lock timed out.",
            "remediation": None,
        }
        self.base = {
            "schema_version": 1, "diagnostic_version": "0", "state": "degraded",
            "root": str(self.home / "fleet"), "source": str(self.home / "source"),
            "ref": "main", "findings": [dict(self.finding)],
            "unrecognized_kinds": [], "fleet_update": {"state": "absent", "actors": []},
        }

    def validate(self, artifact: dict) -> None:
        validate_json_schema(artifact, SCHEMA)

    def refused_probe(self, remedy: object) -> dict:
        finding = dict(self.finding, remedy=deepcopy(remedy))
        return {
            "schema_version": 1, "probe_version": "0", "state": "refused",
            "root": self.base["root"], "detail": "Root opening refused.",
            "code": "ledger_lock_timeout", "remedy": deepcopy(remedy),
            "findings": [finding],
        }

    def test_old_finding_fleet_and_probe_artifacts_keep_their_acceptance(self) -> None:
        self.validate(self.base)
        for fleet in (
            {"state": "absent", "actors": []},
            {"state": "unavailable"},
            {"state": "integrity_failure", "code": "record_fields_invalid"},
        ):
            artifact = dict(self.base, fleet_update=fleet)
            self.validate(artifact)
        for probe in (
            {"probe_version": "0", "state": "refused", "root": self.base["root"], "detail": "old refusal"},
            {
                "schema_version": 1, "probe_version": "0", "state": "healthy",
                "root": self.base["root"], "budget_seconds": 1, "total_budget_seconds": 0,
                "nodes": [], "findings": [{
                    "code": "delivery_probe", "severity": "ok", "subject": "builder",
                    "detail": "probe delivered",
                }],
            },
        ):
            self.validate(dict(self.base, probe=probe))

    def test_captured_remedy_shapes_are_accepted_in_direct_and_transformed_findings(self) -> None:
        for remedy in ("retry the named command", {"kind": "none", "why": "no action was named"}, TRANSIENT):
            for transformed in (False, True):
                with self.subTest(remedy=remedy, transformed=transformed):
                    finding = dict(self.finding, remedy=deepcopy(remedy))
                    if transformed:
                        finding.update(code="wake_daemon_health", refusal_code="ledger_lock_timeout")
                    self.validate(dict(self.base, findings=[finding]))

    def test_integrity_update_can_retain_wrapped_refusal_remedy_and_original_code(self) -> None:
        for remedy in ("retry the named command", {"kind": "none", "why": "no action was named"}, TRANSIENT):
            with self.subTest(remedy=remedy):
                fleet = {
                    "state": "integrity_failure", "code": "fleet_update_receipt_invalid",
                    "refusal_code": "ledger_lock_timeout", "remedy": deepcopy(remedy),
                }
                self.validate(dict(self.base, fleet_update=fleet))

    def test_refused_root_probe_accepts_original_code_remedy_and_error_finding(self) -> None:
        for remedy in ("retry the named command", {"kind": "none", "why": "no action was named"}, TRANSIENT):
            with self.subTest(remedy=remedy):
                self.validate(dict(self.base, probe=self.refused_probe(remedy)))

    def test_transient_remedy_is_bound_to_original_lock_timeout_on_every_surface(self) -> None:
        wrong_findings = (
            dict(self.finding, code="root_symlink", remedy=TRANSIENT),
            dict(self.finding, refusal_code="root_symlink", remedy=TRANSIENT),
            dict(self.finding, code="wake_daemon_health", refusal_code="root_symlink", remedy=TRANSIENT),
        )
        for finding in wrong_findings:
            with self.subTest(finding=finding), self.assertRaises(SchemaValidationError):
                self.validate(dict(self.base, findings=[finding]))
        for fleet in (
            {"state": "integrity_failure", "code": "root_symlink", "remedy": TRANSIENT},
            {"state": "integrity_failure", "code": "ledger_lock_timeout", "refusal_code": "root_symlink", "remedy": TRANSIENT},
        ):
            with self.subTest(fleet=fleet), self.assertRaises(SchemaValidationError):
                self.validate(dict(self.base, fleet_update=fleet))
        probe = self.refused_probe(TRANSIENT)
        probe["code"] = "root_symlink"
        with self.assertRaises(SchemaValidationError):
            self.validate(dict(self.base, probe=probe))

    def test_malformed_remedy_objects_stay_closed_and_typed(self) -> None:
        malformed = (
            "", None, [],
            {"kind": "none"}, {"kind": "none", "why": "none", "extra": "body"},
            {"kind": "none", "why": 1},
            dict(TRANSIENT, action="retry_forever"), dict(TRANSIENT, delay_seconds=True),
            dict(TRANSIENT, delay_seconds=0), dict(TRANSIENT, delay_seconds=3601),
            dict(TRANSIENT, delay_seconds="1"), dict(TRANSIENT, holder=""),
            dict(TRANSIENT, holder="holder with space"), dict(TRANSIENT, holder="holder\n"),
            dict(TRANSIENT, holder="Aholder"), dict(TRANSIENT, holder="h" * 129),
            dict(TRANSIENT, body="private message"),
        )
        for remedy in malformed:
            with self.subTest(remedy=remedy):
                finding = dict(self.finding, remedy=remedy)
                with self.assertRaises(SchemaValidationError):
                    self.validate(dict(self.base, findings=[finding]))
                fleet = {"state": "integrity_failure", "code": "ledger_lock_timeout", "remedy": remedy}
                with self.assertRaises(SchemaValidationError):
                    self.validate(dict(self.base, fleet_update=fleet))
                with self.assertRaises(SchemaValidationError):
                    self.validate(dict(self.base, probe=self.refused_probe(remedy)))

    def test_nonrefused_probe_keeps_original_delivery_finding_contract(self) -> None:
        probe = {
            "probe_version": "0", "state": "degraded", "root": self.base["root"],
            "budget_seconds": 1, "total_budget_seconds": 0, "nodes": [],
            "findings": [dict(self.finding, remedy=TRANSIENT)],
        }
        with self.assertRaises(SchemaValidationError):
            self.validate(dict(self.base, probe=probe))

    def test_old_shape_enum_and_required_field_fences_remain(self) -> None:
        for field in self.base:
            artifact = deepcopy(self.base)
            del artifact[field]
            with self.subTest(missing=field), self.assertRaises(SchemaValidationError):
                self.validate(artifact)
        for case_index, artifact in enumerate((
            dict(self.base, schema_version=2), dict(self.base, state="recoverable"),
            dict(self.base, extra="body"),
            dict(self.base, findings=[dict(self.finding, severity="fatal")]),
            dict(self.base, findings=[dict(self.finding, extra="body")]),
        )):
            with self.subTest(case=(
                "version-2",
                "recoverable-state-enum",
                "top-level-extra",
                "fatal-finding-severity",
                "finding-extra",
            )[case_index]), self.assertRaises(SchemaValidationError):
                self.validate(artifact)

    def test_nonrefused_probe_rejects_refusal_metadata_at_report_level(self) -> None:
        for state in ("healthy", "degraded"):
            for metadata in ({"code": "ledger_lock_timeout"}, {"remedy": "retry once"},
                             {"code": "ledger_lock_timeout", "remedy": TRANSIENT}):
                probe = {
                    "probe_version": "0", "state": state, "root": self.base["root"],
                    "budget_seconds": 1, "total_budget_seconds": 0,
                    "nodes": [], "findings": [], **metadata,
                }
                with self.subTest(state=state, metadata=metadata), self.assertRaises(SchemaValidationError):
                    self.validate(dict(self.base, probe=probe))

    def test_transient_bounds_and_original_refusal_code_are_accepted(self) -> None:
        for delay, holder in ((1, "h"), (3600, "h" * 128), (1, "holder.1:waiter-2")):
            with self.subTest(delay=delay, holder=holder):
                remedy = dict(TRANSIENT, delay_seconds=delay, holder=holder)
                finding = dict(self.finding, code="delivery_health_unavailable", refusal_code="ledger_lock_timeout", remedy=remedy)
                self.validate(dict(self.base, findings=[finding]))
        # The additive original-code coordinate is independent of whether
        # a refusal has actionable advice; legacy fields remain sufficient.
        self.validate(dict(self.base, findings=[dict(self.finding, refusal_code="root_symlink")]))
        for code in ("", "root symlink", "root_symlink\n", 3):
            with self.subTest(refusal_code=code), self.assertRaises(SchemaValidationError):
                self.validate(dict(self.base, findings=[dict(self.finding, refusal_code=code)]))


if __name__ == "__main__":
    unittest.main()
