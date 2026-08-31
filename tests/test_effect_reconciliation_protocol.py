from __future__ import annotations

import hashlib
import json
import math
import struct
import unittest

from floati.errors import ProtocolRefusal


class ReconciliationProtocolTests(unittest.TestCase):
    """Closed request/result values used by the fresh reconciliation observer."""

    def setUp(self) -> None:
        # This fixture intentionally does not import the module under test: the
        # RED bank must prove the new module is the sole missing dependency.
        self.target = {
            "kind": "git_ref",
            "coordinate": "\x2fprivate/tmp/reconciliation-fixture",
            "identity_digest": "a" * 64,
        }
        self.expected = {
            "kind": "git_ref_equals",
            "locator": "refs/heads/main",
            "expected_digest": "b" * 64,
        }
        self.request_values = {
            "operation_id": "effect-op-018f7e9b3c117abc8def0123456789ab",
            "current_evidence_id": "effect-unknown-018f7e9b3c117abc8def0123456789ab",
            "adapter": "git_local",
            "target": self.target,
            "expected_confirmation": self.expected,
            "budget_claim": {"git": 1, "network": 2},
            "local_repository_identity": (17, 23),
        }

    def protocol(self):
        from floati.effect_reconciliation_protocol import (
            MAX_FRAME_BYTES,
            ReconciliationRequest,
            ReconciliationResult,
            build_request,
            build_result,
            decode_request_frame,
            decode_result_frame,
            encode_frame,
            validate_request,
            validate_result,
        )

        return {
            "MAX_FRAME_BYTES": MAX_FRAME_BYTES,
            "ReconciliationRequest": ReconciliationRequest,
            "ReconciliationResult": ReconciliationResult,
            "build_request": build_request,
            "build_result": build_result,
            "decode_request_frame": decode_request_frame,
            "decode_result_frame": decode_result_frame,
            "encode_frame": encode_frame,
            "validate_request": validate_request,
            "validate_result": validate_result,
        }

    def request(self, **changes: object):
        values = dict(self.request_values)
        values.update(changes)
        return self.protocol()["build_request"](**values)

    def confirmed(self, request, **changes: object):
        values = {
            "outcome": "confirmed",
            "reason_code": "exact_ref_and_object",
            "observation": {"observed_ref_digest": "b" * 64},
            "confirmation": self.expected,
            "spend_status": "complete",
            "measured_spend": {"git": 1, "network": 2},
        }
        values.update(changes)
        return self.protocol()["build_result"](request, **values)

    def adapter_request(
        self, adapter: str, *, coordinate: str = "https://example.invalid/repository",
    ):
        if adapter == "git_local":
            return self.request(request_id="1" * 32)
        if adapter == "git_remote_explicit":
            return self.request(
                request_id="1" * 32,
                adapter=adapter,
                target={
                    "kind": "git_remote_ref",
                    "coordinate": coordinate,
                    "identity_digest": hashlib.sha256(coordinate.encode()).hexdigest(),
                },
                expected_confirmation={
                    "kind": "git_remote_ref_equals",
                    "locator": "refs/heads/main",
                    "expected_digest": "b" * 64,
                },
                local_repository_identity=None,
            )
        if adapter == "github_explicit":
            target = {
                "kind": "github_resource",
                "coordinate": "owner/repository#1",
                "identity_digest": "c" * 64,
            }
            expected = {
                "kind": "github_idempotency_marker",
                "locator": "marker",
                "expected_digest": "d" * 64,
            }
        elif adapter == "deployment_explicit":
            target = {
                "kind": "deployment_target",
                "coordinate": "deployment/production",
                "identity_digest": "c" * 64,
            }
            expected = {
                "kind": "deployment_artifact_equals",
                "locator": "artifact",
                "expected_digest": "d" * 64,
            }
        else:
            target = {
                "kind": "shell_environment",
                "coordinate": "workspace",
                "identity_digest": "c" * 64,
            }
            expected = {
                "kind": "none",
                "locator": "none",
                "expected_digest": "d" * 64,
            }
        return self.request(
            request_id="1" * 32,
            adapter=adapter,
            target=target,
            expected_confirmation=expected,
            local_repository_identity=None,
        )

    @staticmethod
    def framed(value: object) -> bytes:
        payload = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return struct.pack(">I", len(payload)) + payload

    @staticmethod
    def sha256(value: object) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def evidence_payload(self, request, result) -> dict[str, object]:
        return {
            "request": {
                "schema_version": 1,
                "operation_id": request.operation_id,
                "current_evidence_id": request.current_evidence_id,
                "adapter": request.adapter,
                "target": dict(request.target),
                "expected_confirmation": dict(request.expected_confirmation),
                "budget_claim": dict(request.budget_claim),
                "local_repository_identity": (
                    None if request.local_repository_identity is None
                    else list(request.local_repository_identity)
                ),
            },
            "outcome": result.outcome,
            "reason_code": result.reason_code,
            "observation": result.observation,
            "confirmation": result.confirmation,
            "spend_status": result.spend_status,
            "measured_spend": result.measured_spend,
        }

    def test_request_round_trip_is_canonical_and_retry_digest_is_stable(self) -> None:
        """Catches correlation identity leaking into a retry's semantic digest."""
        protocol = self.protocol()
        first = self.request(request_id="1" * 32)
        retried = self.request(request_id="2" * 32)
        self.assertNotEqual(first.request_id, retried.request_id)
        self.assertEqual(first.request_digest, retried.request_digest)
        frame = protocol["encode_frame"](first)
        self.assertEqual(first, protocol["decode_request_frame"](frame))
        self.assertEqual(frame, protocol["encode_frame"](first))

    def test_result_round_trip_recomputes_exact_evidence_digest(self) -> None:
        """Catches evidence digests omitting a result semantic field."""
        protocol = self.protocol()
        request = self.request(request_id="1" * 32)
        confirmed = self.confirmed(request)
        failed = protocol["build_result"](
            request, outcome="failed", reason_code="ref_digest_mismatch",
            observation={"observed_ref_digest": "c" * 64},
        )
        unknown = protocol["build_result"](
            request, outcome="unknown", reason_code="git_observation_unavailable",
        )
        for result in (confirmed, failed, unknown):
            with self.subTest(outcome=result.outcome):
                frame = protocol["encode_frame"](result, request=request)
                self.assertEqual(result, protocol["decode_result_frame"](frame, request))
                self.assertEqual(frame, protocol["encode_frame"](result, request=request))

        # These confirmed and failed baselines retain one fixed semantic request.
        # Confirmation, complete spend, and non-confirmed state are invariants,
        # so coupled reason/observation changes cannot vary lawfully in place.
        # The one-field invalid rows below remain digest-domain probes: they
        # demonstrate the exact bytes the validator rejects before truth.
        def one_field_result(baseline, field: str, replacement: object):
            values = dict(baseline.__dict__)
            values[field] = replacement
            unsigned = protocol["ReconciliationResult"](
                **dict(values, evidence_digest="0" * 64),
            )
            values["evidence_digest"] = self.sha256(
                self.evidence_payload(request, unsigned),
            )
            return protocol["ReconciliationResult"](**values)

        confirmed_matrix = (
            ("outcome", "failed", False),
            ("reason_code", "exact_remote_ref", False),
            ("observation", {"observed_ref_digest": "c" * 64}, False),
            ("confirmation", dict(self.expected, locator="refs/heads/other"), False),
            ("spend_status", "unknown", False),
            ("measured_spend", {"git": 1, "network": 1}, False),
        )
        for field, replacement, lawful in confirmed_matrix:
            with self.subTest(baseline="confirmed", field=field):
                candidate = one_field_result(confirmed, field, replacement)
                self.assertEqual(
                    {field},
                    {
                        name for name in (
                            "outcome", "reason_code", "observation", "confirmation",
                            "spend_status", "measured_spend",
                        )
                        if getattr(candidate, name) != getattr(confirmed, name)
                    },
                )
                self.assertEqual(
                    self.sha256(self.evidence_payload(request, candidate)),
                    candidate.evidence_digest,
                )
                if lawful:
                    self.assertEqual(
                        candidate, protocol["validate_result"](candidate, request),
                    )
                else:
                    with self.assertRaises(ProtocolRefusal):
                        protocol["validate_result"](candidate, request)

        failed_reason = one_field_result(failed, "reason_code", "expected_object_absent")
        failed_observation = one_field_result(
            failed, "observation", {"observed_ref_digest": "d" * 64},
        )
        for field, candidate in (("reason_code", failed_reason), ("observation", failed_observation)):
            with self.subTest(baseline="failed", field=field):
                self.assertEqual(candidate, protocol["validate_result"](candidate, request))
                self.assertEqual(
                    self.sha256(self.evidence_payload(request, candidate)),
                    candidate.evidence_digest,
                )

    def test_encoder_refuses_direct_result_without_exact_request_validation(self) -> None:
        """Catches direct result construction bypassing its request-bound digest."""
        protocol = self.protocol()
        request = self.request(request_id="1" * 32)
        result = self.confirmed(request)
        forged = protocol["ReconciliationResult"](
            schema_version=result.schema_version,
            request_id=result.request_id,
            request_digest=result.request_digest,
            outcome="confirmed",
            evidence_digest="0" * 64,
            reason_code=result.reason_code,
            observation=result.observation,
            confirmation=dict(self.expected, locator="refs/heads/other"),
            spend_status="complete",
            measured_spend={"git": 1, "network": 1},
        )
        with self.assertRaises(ProtocolRefusal):
            protocol["encode_frame"](forged)
        with self.assertRaises(ProtocolRefusal):
            protocol["encode_frame"](forged, request=request)

    def test_request_and_result_shapes_are_exact_closed_builtins(self) -> None:
        """Catches subclasses, aliases, or spare members entering the channel."""
        protocol = self.protocol()
        request = self.request(request_id="1" * 32)
        result = self.confirmed(request)
        request_value = dict(request.__dict__)
        result_value = dict(result.__dict__)
        for value in (
            dict(request_value, extra=True),
            {key: value for key, value in request_value.items() if key != "adapter"},
            dict(result_value, extra=True),
            {key: value for key, value in result_value.items() if key != "reason_code"},
            dict(result_value, observation=[]),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ProtocolRefusal):
                    if "operation_id" in value:
                        protocol["validate_request"](value)
                    else:
                        protocol["validate_result"](value, request)

    def test_confirmed_requires_exact_confirmation_and_complete_measured_spend(self) -> None:
        """Catches a confirmed child selecting confirmation or partial spend."""
        request = self.request(request_id="1" * 32)
        for changes in (
            {"confirmation": dict(self.expected, locator="refs/heads/other")},
            {"confirmation": None},
            {"spend_status": "unknown", "measured_spend": None},
            {"measured_spend": {"git": 1}},
            {"measured_spend": {"git": 1, "network": 3}},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ProtocolRefusal):
                    self.confirmed(request, **changes)

    def test_failed_and_unknown_cannot_carry_confirmation_or_complete_spend(self) -> None:
        """Catches non-confirmed outcomes laundering confirmation or complete spend."""
        protocol = self.protocol()
        request = self.request(request_id="1" * 32)
        for outcome, reason in (
            ("failed", "confirmation_absent"),
            ("unknown", "git_observation_unavailable"),
        ):
            with self.subTest(outcome=outcome):
                lawful = protocol["build_result"](request, outcome=outcome, reason_code=reason)
                self.assertIsNone(lawful.confirmation)
                self.assertEqual("unknown", lawful.spend_status)
                self.assertIsNone(lawful.measured_spend)
                for changes in (
                    {"confirmation": self.expected},
                    {"spend_status": "complete", "measured_spend": {"git": 1, "network": 2}},
                    {"measured_spend": {"git": 1}},
                ):
                    with self.assertRaises(ProtocolRefusal):
                        protocol["build_result"](request, outcome=outcome, reason_code=reason, **changes)

    def test_exact_git_observation_without_measurement_is_truthful_unknown(self) -> None:
        """Catches a ref observation being relabeled as resource measurement."""
        protocol = self.protocol()
        for request, observation in (
            (
                self.adapter_request("git_local"),
                {"observed_ref_digest": "b" * 64},
            ),
            (
                self.adapter_request("git_remote_explicit"),
                {
                    "observed_ref_digest": "b" * 64,
                    "evidence_scope": "explicit_remote",
                },
            ),
        ):
            with self.subTest(adapter=request.adapter):
                result = protocol["build_result"](
                    request,
                    outcome="unknown",
                    reason_code="reconciliation_inconclusive",
                    observation=observation,
                )
                self.assertEqual("unknown", result.outcome)
                self.assertEqual(observation, result.observation)
                self.assertIsNone(result.confirmation)
                self.assertEqual("unknown", result.spend_status)
                self.assertIsNone(result.measured_spend)

    def test_result_adapter_outcome_reason_and_observation_cross_product_is_closed(self) -> None:
        """Catches adapter-incompatible dispositions or arbitrary observation maps reaching truth."""
        protocol = self.protocol()
        lawful = (
            (
                self.adapter_request("git_local"), "confirmed", "exact_ref_and_object",
                {"observed_ref_digest": "b" * 64},
            ),
            (
                self.adapter_request("git_local"), "failed", "expected_object_absent",
                {"observed_ref_digest": "a" * 64},
            ),
            (
                self.adapter_request("git_remote_explicit"), "confirmed", "exact_remote_ref",
                {"observed_ref_digest": "b" * 64, "evidence_scope": "explicit_remote"},
            ),
            (
                self.adapter_request("git_remote_explicit"), "failed", "confirmation_absent",
                None,
            ),
            (
                self.adapter_request("github_explicit"), "unknown", "adapter_unavailable",
                {"adapter": "github_explicit"},
            ),
            (
                self.adapter_request("deployment_explicit"), "unknown", "adapter_unavailable",
                {"adapter": "deployment_explicit"},
            ),
            (
                self.adapter_request("none"), "unknown", "reconciliation_inconclusive",
                {"adapter": "none"},
            ),
        )
        for request, outcome, reason, observation in lawful:
            with self.subTest(lawful=(request.adapter, outcome, reason)):
                extra = {}
                if outcome == "confirmed":
                    extra = {
                        "confirmation": request.expected_confirmation,
                        "spend_status": "complete",
                        "measured_spend": request.budget_claim,
                    }
                result = protocol["build_result"](
                    request, outcome=outcome, reason_code=reason,
                    observation=observation, **extra,
                )
                self.assertEqual(result, protocol["validate_result"](result, request))

        hostile = (
            ("none", "confirmed", "exact_ref_and_object", {"observed_ref_digest": "b" * 64}),
            ("none", "unknown", "adapter_unavailable", {"adapter": "none"}),
            ("github_explicit", "failed", "confirmation_absent", None),
            ("github_explicit", "unknown", "adapter_unavailable", {"adapter": "deployment_explicit"}),
            ("deployment_explicit", "unknown", "reconciliation_inconclusive", {"adapter": "none"}),
            ("git_local", "confirmed", "exact_remote_ref", {"observed_ref_digest": "b" * 64, "evidence_scope": "explicit_remote"}),
            ("git_local", "failed", "confirmation_absent", {"forged": True}),
            ("none", "unknown", "observer_timeout", {"forged": True}),
            ("git_local", "unknown", "observer_child_died", {"signal": "9"}),
            ("git_remote_explicit", "confirmed", "exact_ref_and_object", {"observed_ref_digest": "b" * 64}),
            ("git_remote_explicit", "failed", "ref_digest_mismatch", {"observed_ref_digest": "a" * 64, "evidence_scope": "forged"}),
        )
        for adapter, outcome, reason, observation in hostile:
            request = self.adapter_request(adapter)
            extra = {}
            if outcome == "confirmed":
                extra = {
                    "confirmation": request.expected_confirmation,
                    "spend_status": "complete",
                    "measured_spend": request.budget_claim,
                }
            unsigned = {
                "schema_version": 1,
                "request_id": request.request_id,
                "request_digest": request.request_digest,
                "outcome": outcome,
                "evidence_digest": "0" * 64,
                "reason_code": reason,
                "observation": observation,
                "confirmation": extra.get("confirmation"),
                "spend_status": extra.get("spend_status", "unknown"),
                "measured_spend": extra.get("measured_spend"),
            }
            candidate = protocol["ReconciliationResult"](**unsigned)
            unsigned["evidence_digest"] = self.sha256(
                self.evidence_payload(request, candidate),
            )
            with self.subTest(hostile=(adapter, outcome, reason, observation)):
                with self.assertRaises(ProtocolRefusal):
                    protocol["decode_result_frame"](self.framed(unsigned), request)

    def test_remote_observation_scope_requires_canonical_coordinate_grammar(self) -> None:
        """Catches noncanonical fixture or endpoint spellings claiming scoped proof."""
        protocol = self.protocol()
        lawful_cases = (
            (
                "\x2fprivate/tmp/reconciliation-fixture.git",
                "filesystem_fixture",
            ),
            (
                "https://example.invalid/org/repository.git",
                "explicit_remote",
            ),
            (
                "ssh://git@host.example:2222/org/repository.git",
                "explicit_remote",
            ),
        )
        for coordinate, lawful_scope in lawful_cases:
            request = self.adapter_request(
                "git_remote_explicit", coordinate=coordinate,
            )
            with self.subTest(control=(coordinate, lawful_scope)):
                result = protocol["build_result"](
                    request, outcome="confirmed", reason_code="exact_remote_ref",
                    observation={
                        "observed_ref_digest": "b" * 64,
                        "evidence_scope": lawful_scope,
                    },
                    confirmation=request.expected_confirmation,
                    spend_status="complete", measured_spend=request.budget_claim,
                )
                frame = protocol["encode_frame"](result, request=request)
                self.assertEqual(result, protocol["decode_result_frame"](frame, request))
            forged_scope = (
                "explicit_remote"
                if lawful_scope == "filesystem_fixture"
                else "filesystem_fixture"
            )
            hostile = dict(result.__dict__)
            hostile["observation"] = {
                "observed_ref_digest": "b" * 64,
                "evidence_scope": forged_scope,
            }
            unsigned = protocol["ReconciliationResult"](
                **dict(hostile, evidence_digest="0" * 64),
            )
            hostile["evidence_digest"] = self.sha256(
                self.evidence_payload(request, unsigned),
            )
            with self.subTest(scope_mismatch=(coordinate, forged_scope)):
                with self.assertRaises(ProtocolRefusal):
                    protocol["decode_result_frame"](self.framed(hostile), request)

        hostile_cases = (
            ("\x2ftmp/a/../fixture", "filesystem_fixture"),
            ("\x2fprivate/tmp//fixture", "filesystem_fixture"),
            ("https://host/a/../repo", "explicit_remote"),
            ("https://HOST/repository.git", "explicit_remote"),
            ("ssh://git@host.example/org/../repo", "explicit_remote"),
            ("ssh://git@host.example:22/org/repository.git", "explicit_remote"),
        )
        for coordinate, forged_scope in hostile_cases:
            request = self.adapter_request(
                "git_remote_explicit", coordinate=coordinate,
            )
            hostile = {
                "schema_version": 1,
                "request_id": request.request_id,
                "request_digest": request.request_digest,
                "outcome": "confirmed",
                "evidence_digest": "0" * 64,
                "reason_code": "exact_remote_ref",
                "observation": {
                    "observed_ref_digest": "b" * 64,
                    "evidence_scope": forged_scope,
                },
                "confirmation": dict(request.expected_confirmation),
                "spend_status": "complete",
                "measured_spend": dict(request.budget_claim),
            }
            unsigned = protocol["ReconciliationResult"](**hostile)
            hostile["evidence_digest"] = self.sha256(
                self.evidence_payload(request, unsigned),
            )
            frame = self.framed(hostile)
            with self.subTest(hostile=(coordinate, forged_scope)):
                with self.assertRaises(ProtocolRefusal):
                    protocol["decode_result_frame"](frame, request)

    def test_unicode_controls_surrogates_nan_and_unbounded_containers_refuse(self) -> None:
        """Catches non-I-JSON or terminal-unsafe observer input."""
        protocol = self.protocol()
        cases = (
            {"target": dict(self.target, coordinate="\x2ftmp/\u202e")},
            {"target": dict(self.target, coordinate="\x2ftmp/\ud800")},
            {"budget_claim": {"git": math.nan}},
            {"budget_claim": {"budget-%03d" % index: index for index in range(65)}},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ProtocolRefusal):
                    self.request(**changes)
        request = self.request(request_id="1" * 32)
        with self.assertRaises(ProtocolRefusal):
            protocol["build_result"](
                request, outcome="unknown", reason_code="adapter_unavailable",
                observation={"item-%03d" % index: index for index in range(65)},
            )

    def test_frame_rejects_partial_duplicate_trailing_and_noncanonical_json(self) -> None:
        """Catches any wire spelling other than one complete canonical object."""
        protocol = self.protocol()
        request = self.request(request_id="1" * 32)
        frame = protocol["encode_frame"](request)
        payload = frame[4:]
        malformed = (
            b"",
            frame[:3],
            b"\x00\x00\x00\x00",
            struct.pack(">I", len(payload) + 1) + payload,
            frame + b"x",
            struct.pack(">I", 2) + b"\xff\xff",
            struct.pack(">I", len(b'{\"schema_version\":1,\"schema_version\":1}')) + b'{\"schema_version\":1,\"schema_version\":1}',
            struct.pack(">I", len(json.dumps(dict(request.__dict__), ensure_ascii=False).encode("utf-8"))) + json.dumps(dict(request.__dict__), ensure_ascii=False).encode("utf-8"),
        )
        for candidate in malformed:
            with self.subTest(candidate=candidate[:32]):
                with self.assertRaises(ProtocolRefusal):
                    protocol["decode_request_frame"](candidate)

        request_value = dict(request.__dict__)
        result = self.confirmed(request)
        result_value = dict(result.__dict__)
        wire_cases = (
            (dict(request_value, schema_version=True), protocol["decode_request_frame"], None),
            (dict(request_value, schema_version=2), protocol["decode_request_frame"], None),
            (dict(request_value, request_digest="0" * 64), protocol["decode_request_frame"], None),
            (dict(request_value, target=dict(self.target, coordinate="/mutated")), protocol["decode_request_frame"], None),
            (dict(request_value, extra=True), protocol["decode_request_frame"], None),
            ({key: value for key, value in request_value.items() if key != "adapter"}, protocol["decode_request_frame"], None),
            (dict(result_value, request_id="2" * 32), protocol["decode_result_frame"], request),
            (dict(result_value, request_digest="0" * 64), protocol["decode_result_frame"], request),
            (dict(result_value, evidence_digest="0" * 64), protocol["decode_result_frame"], request),
            (dict(result_value, measured_spend={"git": True, "network": 2}), protocol["decode_result_frame"], request),
            (dict(result_value, confirmation=dict(self.expected, locator="refs/heads/other")), protocol["decode_result_frame"], request),
            (dict(result_value, extra=True), protocol["decode_result_frame"], request),
            ({key: value for key, value in result_value.items() if key != "reason_code"}, protocol["decode_result_frame"], request),
        )
        for value, decoder, bound_request in wire_cases:
            with self.subTest(value=value):
                with self.assertRaises(ProtocolRefusal):
                    if bound_request is None:
                        decoder(self.framed(value))
                    else:
                        decoder(self.framed(value), bound_request)

    def test_frame_size_is_bounded_at_65536_encoded_bytes(self) -> None:
        """Catches length headers accepting a payload beyond the fixed observer cap."""
        protocol = self.protocol()
        self.assertEqual(65_536, protocol["MAX_FRAME_BYTES"])
        request = self.request(request_id="1" * 32)
        unavailable_request = self.adapter_request("github_explicit")
        near_limit = protocol["build_result"](
            unavailable_request, outcome="unknown", reason_code="adapter_unavailable",
            observation={"adapter": "github_explicit"},
        )
        encoded_near_limit = protocol["encode_frame"](
            near_limit, request=unavailable_request,
        )
        self.assertGreater(len(encoded_near_limit) - 4, 256)
        self.assertLessEqual(len(encoded_near_limit) - 4, protocol["MAX_FRAME_BYTES"])
        oversized = struct.pack(">I", protocol["MAX_FRAME_BYTES"] + 1) + b"x" * (protocol["MAX_FRAME_BYTES"] + 1)
        with self.assertRaises(ProtocolRefusal):
            protocol["decode_request_frame"](oversized)

    def test_result_refuses_wrong_request_binding_and_child_selected_types(self) -> None:
        """Catches a result copied from another request or using Python coercions."""
        protocol = self.protocol()
        request = self.request(request_id="1" * 32)
        result = self.confirmed(request)
        value = dict(result.__dict__)
        cases = (
            dict(value, request_id="2" * 32),
            dict(value, request_digest="0" * 64),
            dict(value, schema_version=True),
            dict(value, measured_spend={"git": True, "network": 2}),
            dict(value, outcome="confirmed", confirmation=dict(self.expected, kind="none")),
            dict(value, observation={"nested": {"bad": object()}}),
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ProtocolRefusal):
                    protocol["validate_result"](candidate, request)


if __name__ == "__main__":
    unittest.main()
