from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from dataclasses import replace
from pathlib import Path

from floati.errors import ProtocolRefusal


OBSERVED_AT = "2026-08-29T12:00:00Z"
PROVIDERS = (
    "anthropic_claude_code",
    "openai_codex",
    "google_gemini",
    "cursor_individual",
    "xai_grok",
    "github_copilot",
)


class QuotaAdapterTests(unittest.TestCase):
    def quota_adapters(self):
        try:
            import floati.quota_adapters as quota_adapters
        except ModuleNotFoundError:
            self.fail("V4 quota adapter module must exist")
        return quota_adapters

    def test_roster_is_derived_in_exact_six_provider_order_and_every_adapter_is_cited(self) -> None:
        module = self.quota_adapters()
        roster = module.adapter_roster()
        intake_prefix = (
            "docs/research/post-release-dr-2026-08-28/"
            "dr-v4-quota-observability.md#"
        )

        self.assertEqual(PROVIDERS, tuple(adapter.provider for adapter in roster))
        self.assertEqual(roster, module.validate_adapter_roster(roster))
        for adapter in roster:
            self.assertTrue(adapter.intake_citation.startswith(intake_prefix))
            self.assertTrue(adapter.primary_citation.startswith("https://"))
            self.assertIn(adapter.endpoint_id, type(adapter).__doc__ or "")
            self.assertIn(adapter.intake_citation, type(adapter).__doc__ or "")
            self.assertIn(adapter.primary_citation, type(adapter).__doc__ or "")
            self.assertIs(adapter, module.adapter_for(adapter.provider))

    def test_missing_adapter_citation_refuses_roster_construction(self) -> None:
        module = self.quota_adapters()
        roster = module.adapter_roster()
        for field in ("intake_citation", "primary_citation"):
            with self.subTest(field=field):
                perturbed = (replace(roster[0], **{field: ""}),) + roster[1:]
                with self.assertRaises(ProtocolRefusal) as raised:
                    module.validate_adapter_roster(perturbed)
                self.assertEqual("quota_adapter_citation_missing", raised.exception.code)

    def test_claude_statusline_maps_two_documented_windows_to_measured_facts(self) -> None:
        module = self.quota_adapters()
        payload = json.dumps(
            {
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 25,
                        "resets_at": 1788022800,
                    },
                    "seven_day": {
                        "used_percentage": 40.5,
                        "resets_at": 1788609600,
                    },
                }
            },
            separators=(",", ":"),
        ).encode()

        receipt = module.adapter_for("anthropic_claude_code").observe(
            payload,
            observed_at=OBSERVED_AT,
            idempotency_key="claude-observation-1",
        )

        self.assertEqual("anthropic_claude_code", receipt.provider)
        self.assertEqual(
            ("rate_limits.five_hour", "rate_limits.seven_day"),
            tuple(fact.surface for fact in receipt.facts),
        )
        self.assertEqual(("0.250000", "0.405000"), tuple(fact.state.value for fact in receipt.facts))
        self.assertEqual(("MEASURED", "MEASURED"), tuple(fact.stamp for fact in receipt.facts))
        self.assertEqual(
            ("2026-08-29T17:00:00Z", "2026-09-05T12:00:00Z"),
            tuple(fact.resets_at for fact in receipt.facts),
        )
        self.assertEqual(
            {hashlib.sha256(payload).hexdigest()},
            {fact.evidence_digest for fact in receipt.facts},
        )

    def test_codex_rate_limit_response_maps_named_buckets_without_duplication(self) -> None:
        module = self.quota_adapters()
        codex = {
            "limitId": "codex",
            "primary": {
                "usedPercent": 25,
                "windowDurationMins": 15,
                "resetsAt": 1788022800,
            },
            "secondary": None,
            "rateLimitReachedType": None,
        }
        payload = json.dumps(
            {
                "id": 6,
                "result": {
                    "rateLimits": codex,
                    "rateLimitsByLimitId": {
                        "codex": codex,
                        "codex_other": {
                            "limitId": "codex_other",
                            "primary": {
                                "usedPercent": 42,
                                "windowDurationMins": 60,
                                "resetsAt": 1788609600,
                            },
                            "secondary": None,
                            "rateLimitReachedType": None,
                        },
                    },
                },
            },
            separators=(",", ":"),
        ).encode()

        receipt = module.adapter_for("openai_codex").observe(
            payload,
            observed_at=OBSERVED_AT,
            idempotency_key="codex-observation-1",
        )

        self.assertEqual(
            (
                "account/rateLimits/read:codex.primary",
                "account/rateLimits/read:codex_other.primary",
            ),
            tuple(fact.surface for fact in receipt.facts),
        )
        self.assertEqual(("0.250000", "0.420000"), tuple(fact.state.value for fact in receipt.facts))
        self.assertEqual(("MEASURED", "MEASURED"), tuple(fact.stamp for fact in receipt.facts))

    def test_gemini_without_documented_quota_fields_is_typed_unknown(self) -> None:
        module = self.quota_adapters()
        payload = b'{"response":"ok","stats":{"models":{"gemini":{"tokens":{"prompt":7}}}}}'

        receipt = module.adapter_for("google_gemini").observe(
            payload,
            observed_at=OBSERVED_AT,
            idempotency_key="gemini-observation-1",
        )

        self.assertEqual(1, len(receipt.facts))
        fact = receipt.facts[0]
        self.assertEqual("account_quota", fact.surface)
        self.assertEqual(("unknown", None), (fact.state.kind, fact.state.value))
        self.assertEqual("DERIVED", fact.stamp)
        self.assertIn("undocumented", fact.source)

    def test_cursor_grok_and_copilot_local_cells_are_typed_unknown(self) -> None:
        module = self.quota_adapters()
        for provider in ("cursor_individual", "xai_grok", "github_copilot"):
            with self.subTest(provider=provider):
                receipt = module.adapter_for(provider).observe(
                    b"",
                    observed_at=OBSERVED_AT,
                    idempotency_key=provider + "-observation-1",
                )
                self.assertEqual(1, len(receipt.facts))
                fact = receipt.facts[0]
                self.assertEqual("account_quota", fact.surface)
                self.assertEqual(("unknown", None), (fact.state.kind, fact.state.value))
                self.assertEqual("DERIVED", fact.stamp)
                self.assertEqual(hashlib.sha256(b"").hexdigest(), fact.evidence_digest)

    def test_zero_observable_surface_is_typed_unknown_for_entire_roster(self) -> None:
        module = self.quota_adapters()
        receipts = tuple(
            adapter.observe(
                b"",
                observed_at=OBSERVED_AT,
                idempotency_key="zero-surface-" + adapter.provider,
            )
            for adapter in module.adapter_roster()
        )

        self.assertEqual(PROVIDERS, tuple(receipt.provider for receipt in receipts))
        for receipt in receipts:
            self.assertEqual(1, len(receipt.facts))
            fact = receipt.facts[0]
            self.assertEqual(("unknown", None), (fact.state.kind, fact.state.value))
            self.assertEqual(hashlib.sha256(b"").hexdigest(), fact.evidence_digest)

    def test_malformed_and_oversized_provider_payloads_are_refused(self) -> None:
        module = self.quota_adapters()
        adapter = module.adapter_for("anthropic_claude_code")
        for payload in (b"not-json", b"{" + (b"x" * (1024 * 1024)) + b"}"):
            with self.subTest(size=len(payload)):
                with self.assertRaises(ProtocolRefusal) as raised:
                    adapter.observe(
                        payload,
                        observed_at=OBSERVED_AT,
                        idempotency_key="invalid-observation-1",
                    )
                self.assertIn(
                    raised.exception.code,
                    {"quota_payload_invalid", "quota_payload_oversized"},
                )

    def test_codex_stdio_collector_uses_real_jsonl_handshake_and_no_listener(self) -> None:
        module = self.quota_adapters()
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "codex-fixture"
            methods = executable.with_suffix(".methods")
            arguments = executable.with_suffix(".arguments")
            executable.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys
                    from pathlib import Path

                    here = Path(__file__)
                    here.with_suffix('.arguments').write_text(json.dumps(sys.argv[1:]))
                    seen = []
                    for line in sys.stdin:
                        message = json.loads(line)
                        seen.append(message['method'])
                        if message['method'] == 'initialize':
                            print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
                        elif message['method'] == 'account/rateLimits/read':
                            print(json.dumps({
                                'id': message['id'],
                                'result': {
                                    'rateLimits': {
                                        'limitId': 'codex',
                                        'primary': {
                                            'usedPercent': 25,
                                            'windowDurationMins': 15,
                                            'resetsAt': 1788022800,
                                        },
                                        'secondary': None,
                                        'rateLimitReachedType': None,
                                    }
                                },
                            }), flush=True)
                            break
                    here.with_suffix('.methods').write_text(json.dumps(seen))
                    """
                ),
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            receipt = module.collect_codex_app_server(
                executable,
                observed_at=OBSERVED_AT,
                idempotency_key="codex-stdio-1",
                timeout_seconds=2.0,
            )

            self.assertEqual(
                ["initialize", "initialized", "account/rateLimits/read"],
                json.loads(methods.read_text(encoding="utf-8")),
            )
            self.assertEqual(
                ["app-server", "--listen", "stdio://"],
                json.loads(arguments.read_text(encoding="utf-8")),
            )
            self.assertEqual("0.250000", receipt.facts[0].state.value)

    def test_codex_stdio_timeout_digests_stderr_without_disclosure_and_reaps_child(self) -> None:
        module = self.quota_adapters()
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "codex-hang"
            pid_path = executable.with_suffix(".pid")
            executable.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    import time
                    from pathlib import Path

                    here = Path(__file__)
                    here.with_suffix('.pid').write_text(str(os.getpid()))
                    print('sk-test-secret \x2fUsers/private-account', file=sys.stderr, flush=True)
                    initialize = json.loads(sys.stdin.readline())
                    print(json.dumps({'id': initialize['id'], 'result': {}}), flush=True)
                    sys.stdin.readline()
                    sys.stdin.readline()
                    time.sleep(5)
                    """
                ),
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            with self.assertRaises(ProtocolRefusal) as raised:
                module.collect_codex_app_server(
                    executable,
                    observed_at=OBSERVED_AT,
                    idempotency_key="codex-timeout-1",
                    timeout_seconds=0.5,
                )

            self.assertEqual("quota_provider_timeout", raised.exception.code)
            diagnostic = b"sk-test-secret \x2fUsers/private-account\n"
            self.assertNotIn("sk-test-secret", raised.exception.detail)
            self.assertNotIn("\x2fUsers/private-account", raised.exception.detail)
            self.assertIn(
                "stderr_sample_sha256=" + hashlib.sha256(diagnostic).hexdigest(),
                raised.exception.detail,
            )
            self.assertIn(
                "stderr_sample_bytes=" + str(len(diagnostic)), raised.exception.detail
            )
            self.assertIn("stderr_truncated=false", raised.exception.detail)
            pid = int(pid_path.read_text(encoding="utf-8"))
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail("timed-out Codex app-server child survived collector cleanup")

    def test_claude_statusline_collector_appends_one_receipt_and_emits_one_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "quota-fleet"
            root.mkdir()
            payload = json.dumps({
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 25,
                        "resets_at": 1788022800,
                    }
                }
            })
            process = subprocess.run(
                [
                    sys.executable,
                    "scripts/floati-quota-statusline",
                    "--root",
                    str(root),
                    "--observed-at",
                    OBSERVED_AT,
                    "--idempotency-key",
                    "claude-script-1",
                ],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, process.returncode, process.stderr)
            self.assertEqual("", process.stderr)
            self.assertEqual(1, len(process.stdout.splitlines()))
            artifact = json.loads(process.stdout)
            self.assertEqual(
                (0, "quota-statusline", "ok"),
                (
                    artifact["artifact_version"],
                    artifact["command"],
                    artifact["status"],
                ),
            )
            self.assertEqual("anthropic_claude_code", artifact["evidence"]["provider"])
            ledger = root / "receipts" / "quota" / "anthropic_claude_code.jsonl"
            self.assertEqual(1, len(ledger.read_text(encoding="utf-8").splitlines()))


if __name__ == "__main__":
    unittest.main()
