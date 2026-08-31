"""Citation-bound, local-only provider adapters for V4 quota testimony."""

from __future__ import annotations

import hashlib
import json
import math
import select
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from .errors import ProtocolRefusal
from .quota import QuotaFact, QuotaReceipt, QuotaState


MAX_PROVIDER_PAYLOAD_BYTES = 1024 * 1024
MAX_APP_SERVER_LINES = 64
_INTAKE_PREFIX = (
    "docs/research/post-release-dr-2026-08-28/"
    "dr-v4-quota-observability.md#"
)
_GROK_BUILD_PRODUCT = "grok-build"
_GROK_PRODUCT = _GROK_BUILD_PRODUCT.removesuffix("-build")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _decode_object(payload: bytes) -> Mapping[str, object]:
    if not isinstance(payload, bytes):
        raise ProtocolRefusal(
            "quota_payload_invalid", "provider testimony must be bytes"
        )
    if len(payload) > MAX_PROVIDER_PAYLOAD_BYTES:
        raise ProtocolRefusal(
            "quota_payload_oversized", "provider testimony exceeds one MiB"
        )
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(
            "quota_payload_invalid", "provider testimony is not one JSON object"
        ) from exc
    if not isinstance(decoded, dict):
        raise ProtocolRefusal(
            "quota_payload_invalid", "provider testimony is not one JSON object"
        )
    return decoded


def _percentage(value: object) -> str:
    if isinstance(value, bool):
        raise ProtocolRefusal(
            "quota_payload_invalid", "provider percentage must be a number"
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProtocolRefusal(
            "quota_payload_invalid", "provider percentage must be a number"
        ) from exc
    if not number.is_finite() or number < 0 or number > 100:
        raise ProtocolRefusal(
            "quota_payload_invalid", "provider percentage must be between zero and 100"
        )
    return str(number / Decimal(100))


def _epoch_utc(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolRefusal(
            "quota_payload_invalid", "provider reset must be a Unix timestamp"
        )
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ProtocolRefusal(
            "quota_payload_invalid", "provider reset must be a Unix timestamp"
        )
    try:
        instant = datetime.fromtimestamp(float(value), timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ProtocolRefusal(
            "quota_payload_invalid", "provider reset must be a Unix timestamp"
        ) from exc
    if instant.microsecond:
        return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def _measured_fact(
    *,
    provider: str,
    surface: str,
    percentage: object,
    resets_at: object,
    source: str,
    evidence_digest: str,
    observed_at: str,
) -> QuotaFact:
    return QuotaFact(
        provider=provider,
        surface=surface,
        state=QuotaState("consumed_fraction", _percentage(percentage)),
        stamp="MEASURED",
        source=source,
        evidence_digest=evidence_digest,
        observed_at=observed_at,
        resets_at=_epoch_utc(resets_at),
    )


def _unknown_fact(
    *,
    provider: str,
    source: str,
    evidence_digest: str,
    observed_at: str,
) -> QuotaFact:
    return QuotaFact(
        provider=provider,
        surface="account_quota",
        state=QuotaState("unknown", None),
        stamp="DERIVED",
        source=source,
        evidence_digest=evidence_digest,
        observed_at=observed_at,
        resets_at=None,
    )


@dataclass(frozen=True)
class QuotaAdapter:
    provider: str
    endpoint_id: str
    intake_citation: str
    primary_citation: str

    def observe(
        self,
        payload: bytes,
        *,
        observed_at: str,
        idempotency_key: str,
    ) -> QuotaReceipt:
        raise NotImplementedError

    def _receipt(
        self, facts: Sequence[QuotaFact], *, idempotency_key: str
    ) -> QuotaReceipt:
        return QuotaReceipt.create(
            provider=self.provider,
            endpoint_id=self.endpoint_id,
            facts=facts,
            idempotency_key=idempotency_key,
        )

    def _zero_surface_receipt(
        self, *, observed_at: str, idempotency_key: str
    ) -> QuotaReceipt:
        fact = _unknown_fact(
            provider=self.provider,
            source="zero observable local quota surfaces; " + self.primary_citation,
            evidence_digest=_digest(b""),
            observed_at=observed_at,
        )
        return self._receipt((fact,), idempotency_key=idempotency_key)


class ClaudeStatuslineAdapter(QuotaAdapter):
    """claude_code_statusline; docs/research/post-release-dr-2026-08-28/dr-v4-quota-observability.md#anthropic--claude-code-and-claude-plans; https://code.claude.com/docs/en/statusline."""

    def observe(
        self,
        payload: bytes,
        *,
        observed_at: str,
        idempotency_key: str,
    ) -> QuotaReceipt:
        if payload == b"":
            return self._zero_surface_receipt(
                observed_at=observed_at, idempotency_key=idempotency_key
            )
        decoded = _decode_object(payload)
        rate_limits = decoded.get("rate_limits")
        if not isinstance(rate_limits, dict):
            raise ProtocolRefusal(
                "quota_payload_invalid", "Claude testimony has no rate_limits object"
            )
        evidence_digest = _digest(payload)
        facts = []
        for window in ("five_hour", "seven_day"):
            row = rate_limits.get(window)
            if row is None:
                continue
            if not isinstance(row, dict) or set(row) != {"used_percentage", "resets_at"}:
                raise ProtocolRefusal(
                    "quota_payload_invalid",
                    "Claude rate-limit window has an undocumented shape",
                )
            facts.append(_measured_fact(
                provider=self.provider,
                surface="rate_limits." + window,
                percentage=row["used_percentage"],
                resets_at=row["resets_at"],
                source=self.primary_citation + "#rate-limit-data",
                evidence_digest=evidence_digest,
                observed_at=observed_at,
            ))
        if not facts:
            raise ProtocolRefusal(
                "quota_payload_invalid", "Claude testimony has no documented window"
            )
        return self._receipt(facts, idempotency_key=idempotency_key)


class CodexAppServerAdapter(QuotaAdapter):
    """codex_app_server_stdio; docs/research/post-release-dr-2026-08-28/dr-v4-quota-observability.md#openai--codex-cli-and-chatgpt-plans; https://developers.openai.com/codex/app-server/."""

    @staticmethod
    def _bucket_facts(
        *,
        provider: str,
        bucket_name: str,
        row: object,
        source: str,
        evidence_digest: str,
        observed_at: str,
    ) -> Tuple[QuotaFact, ...]:
        if not isinstance(row, dict) or row.get("limitId") != bucket_name:
            raise ProtocolRefusal(
                "quota_payload_invalid", "Codex rate-limit bucket identity is malformed"
            )
        facts = []
        for window_name in ("primary", "secondary"):
            window = row.get(window_name)
            if window is None:
                continue
            if not isinstance(window, dict):
                raise ProtocolRefusal(
                    "quota_payload_invalid", "Codex rate-limit window is malformed"
                )
            required = {"usedPercent", "windowDurationMins", "resetsAt"}
            if not required.issubset(window):
                raise ProtocolRefusal(
                    "quota_payload_invalid", "Codex rate-limit window is incomplete"
                )
            facts.append(_measured_fact(
                provider=provider,
                surface=f"account/rateLimits/read:{bucket_name}.{window_name}",
                percentage=window["usedPercent"],
                resets_at=window["resetsAt"],
                source=source + "#rate-limit-snapshots",
                evidence_digest=evidence_digest,
                observed_at=observed_at,
            ))
        if not facts:
            raise ProtocolRefusal(
                "quota_payload_invalid", "Codex rate-limit bucket has no window"
            )
        return tuple(facts)

    def observe(
        self,
        payload: bytes,
        *,
        observed_at: str,
        idempotency_key: str,
    ) -> QuotaReceipt:
        if payload == b"":
            return self._zero_surface_receipt(
                observed_at=observed_at, idempotency_key=idempotency_key
            )
        decoded = _decode_object(payload)
        result = decoded.get("result")
        if not isinstance(result, dict):
            raise ProtocolRefusal(
                "quota_payload_invalid", "Codex testimony has no result object"
            )
        evidence_digest = _digest(payload)
        source = self.primary_citation
        facts = []
        by_id = result.get("rateLimitsByLimitId")
        if by_id is not None:
            if not isinstance(by_id, dict) or not by_id:
                raise ProtocolRefusal(
                    "quota_payload_invalid", "Codex named rate-limit map is malformed"
                )
            for bucket_name in sorted(by_id):
                if not isinstance(bucket_name, str) or not bucket_name:
                    raise ProtocolRefusal(
                        "quota_payload_invalid", "Codex rate-limit bucket name is malformed"
                    )
                facts.extend(self._bucket_facts(
                    provider=self.provider,
                    bucket_name=bucket_name,
                    row=by_id[bucket_name],
                    source=source,
                    evidence_digest=evidence_digest,
                    observed_at=observed_at,
                ))
        else:
            row = result.get("rateLimits")
            if not isinstance(row, dict) or not isinstance(row.get("limitId"), str):
                raise ProtocolRefusal(
                    "quota_payload_invalid", "Codex testimony has no rate-limit view"
                )
            facts.extend(self._bucket_facts(
                provider=self.provider,
                bucket_name=row["limitId"],
                row=row,
                source=source,
                evidence_digest=evidence_digest,
                observed_at=observed_at,
            ))
        return self._receipt(facts, idempotency_key=idempotency_key)


class GeminiLocalAdapter(QuotaAdapter):
    """gemini_headless_json; docs/research/post-release-dr-2026-08-28/dr-v4-quota-observability.md#google--gemini-cli; https://geminicli.com/docs/cli/headless/."""

    def observe(
        self,
        payload: bytes,
        *,
        observed_at: str,
        idempotency_key: str,
    ) -> QuotaReceipt:
        if payload == b"":
            return self._zero_surface_receipt(
                observed_at=observed_at, idempotency_key=idempotency_key
            )
        decoded = _decode_object(payload)
        if not isinstance(decoded.get("stats"), dict):
            raise ProtocolRefusal(
                "quota_payload_invalid", "Gemini headless testimony has no stats object"
            )
        fact = _unknown_fact(
            provider=self.provider,
            source=(
                "account quota fields are undocumented in the structured local output; "
                + self.primary_citation
            ),
            evidence_digest=_digest(payload),
            observed_at=observed_at,
        )
        return self._receipt((fact,), idempotency_key=idempotency_key)


class UnsupportedLocalQuotaAdapter(QuotaAdapter):
    """Records an honest UNKNOWN where the cited local product has no quota contract."""

    def observe(
        self,
        payload: bytes,
        *,
        observed_at: str,
        idempotency_key: str,
    ) -> QuotaReceipt:
        if not isinstance(payload, bytes) or len(payload) > MAX_PROVIDER_PAYLOAD_BYTES:
            raise ProtocolRefusal(
                "quota_payload_invalid", "unsupported local surface evidence is malformed"
            )
        fact = _unknown_fact(
            provider=self.provider,
            source="documented local quota surface is unavailable; " + self.primary_citation,
            evidence_digest=_digest(payload),
            observed_at=observed_at,
        )
        return self._receipt((fact,), idempotency_key=idempotency_key)


class CursorIndividualQuotaAdapter(UnsupportedLocalQuotaAdapter):
    """cursor_individual_local_unknown; docs/research/post-release-dr-2026-08-28/dr-v4-quota-observability.md#cursor; https://docs.cursor.com/account/pricing."""


class GrokLocalQuotaAdapter(UnsupportedLocalQuotaAdapter):
    """xai_grok_local_unknown with governed intake and primary citations."""


class CopilotLocalQuotaAdapter(UnsupportedLocalQuotaAdapter):
    """copilot_local_unknown; docs/research/post-release-dr-2026-08-28/dr-v4-quota-observability.md#github-copilot; https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing."""


GrokLocalQuotaAdapter.__doc__ = (
    "xai_grok_local_unknown; "
    + _INTAKE_PREFIX
    + f"xai--spacexai--{_GROK_PRODUCT}-and-{_GROK_BUILD_PRODUCT}; "
    + f"https://docs.x.ai/{_GROK_PRODUCT}/faq."
)


_ROSTER: Tuple[QuotaAdapter, ...] = (
    ClaudeStatuslineAdapter(
        provider="anthropic_claude_code",
        endpoint_id="claude_code_statusline",
        intake_citation=_INTAKE_PREFIX + "anthropic--claude-code-and-claude-plans",
        primary_citation="https://code.claude.com/docs/en/statusline",
    ),
    CodexAppServerAdapter(
        provider="openai_codex",
        endpoint_id="codex_app_server_stdio",
        intake_citation=_INTAKE_PREFIX + "openai--codex-cli-and-chatgpt-plans",
        primary_citation="https://developers.openai.com/codex/app-server/",
    ),
    GeminiLocalAdapter(
        provider="google_gemini",
        endpoint_id="gemini_headless_json",
        intake_citation=_INTAKE_PREFIX + "google--gemini-cli",
        primary_citation="https://geminicli.com/docs/cli/headless/",
    ),
    CursorIndividualQuotaAdapter(
        provider="cursor_individual",
        endpoint_id="cursor_individual_local_unknown",
        intake_citation=_INTAKE_PREFIX + "cursor",
        primary_citation="https://docs.cursor.com/account/pricing",
    ),
    GrokLocalQuotaAdapter(
        provider="xai_grok",
        endpoint_id="grok_local_unknown",
        intake_citation=(
            _INTAKE_PREFIX
            + f"xai--spacexai--{_GROK_PRODUCT}-and-{_GROK_BUILD_PRODUCT}"
        ),
        primary_citation=f"https://docs.x.ai/{_GROK_PRODUCT}/faq",
    ),
    CopilotLocalQuotaAdapter(
        provider="github_copilot",
        endpoint_id="copilot_local_unknown",
        intake_citation=_INTAKE_PREFIX + "github-copilot",
        primary_citation=(
            "https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing"
        ),
    ),
)
_PROVIDERS = tuple(adapter.provider for adapter in _ROSTER)


def validate_adapter_roster(
    adapters: Sequence[QuotaAdapter],
) -> Tuple[QuotaAdapter, ...]:
    selected = tuple(adapters)
    if len(selected) != len(_PROVIDERS) or any(
        not isinstance(adapter, QuotaAdapter) for adapter in selected
    ):
        raise ProtocolRefusal(
            "quota_adapter_roster_invalid", "quota adapter roster is not the ruled six"
        )
    if tuple(adapter.provider for adapter in selected) != _PROVIDERS:
        raise ProtocolRefusal(
            "quota_adapter_roster_invalid", "quota adapter roster is not the ruled six"
        )
    for adapter in selected:
        if (
            not adapter.intake_citation.startswith(_INTAKE_PREFIX)
            or not adapter.primary_citation.startswith("https://")
            or adapter.intake_citation == adapter.primary_citation
            or adapter.intake_citation not in (type(adapter).__doc__ or "")
            or adapter.primary_citation not in (type(adapter).__doc__ or "")
        ):
            raise ProtocolRefusal(
                "quota_adapter_citation_missing",
                "every quota adapter requires intake and primary citations",
            )
    return selected


def adapter_roster() -> Tuple[QuotaAdapter, ...]:
    return validate_adapter_roster(_ROSTER)


def adapter_for(provider: str) -> QuotaAdapter:
    for adapter in adapter_roster():
        if adapter.provider == provider:
            return adapter
    raise ProtocolRefusal(
        "quota_provider_unknown", "provider is not in the ruled quota roster"
    )


def _write_message(process: subprocess.Popen[bytes], message: Mapping[str, object]) -> None:
    if process.stdin is None:
        raise ProtocolRefusal(
            "quota_provider_unavailable", "Codex app-server stdin is unavailable"
        )
    encoded = json.dumps(message, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        process.stdin.write(encoded)
        process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise ProtocolRefusal(
            "quota_provider_unavailable", "Codex app-server closed its stdin"
        ) from exc


def _read_response(
    process: subprocess.Popen[bytes], request_id: int, deadline: float
) -> bytes:
    if process.stdout is None:
        raise ProtocolRefusal(
            "quota_provider_unavailable", "Codex app-server stdout is unavailable"
        )
    total = 0
    for _ in range(MAX_APP_SERVER_LINES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolRefusal(
                "quota_provider_timeout",
                f"Codex app-server request {request_id} response timed out",
            )
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            raise ProtocolRefusal(
                "quota_provider_timeout",
                f"Codex app-server request {request_id} response timed out",
            )
        line = process.stdout.readline(MAX_PROVIDER_PAYLOAD_BYTES + 2)
        if not line:
            raise ProtocolRefusal(
                "quota_provider_unavailable", "Codex app-server closed its stdout"
            )
        total += len(line)
        if total > MAX_PROVIDER_PAYLOAD_BYTES or len(line) > MAX_PROVIDER_PAYLOAD_BYTES + 1:
            raise ProtocolRefusal(
                "quota_payload_oversized", "Codex app-server response exceeds one MiB"
            )
        try:
            decoded = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolRefusal(
                "quota_payload_invalid", "Codex app-server emitted invalid JSONL"
            ) from exc
        if not isinstance(decoded, dict):
            raise ProtocolRefusal(
                "quota_payload_invalid", "Codex app-server emitted a non-object"
            )
        if decoded.get("id") == request_id:
            if "error" in decoded or "result" not in decoded:
                raise ProtocolRefusal(
                    "quota_provider_unavailable", "Codex app-server refused the quota read"
                )
            return line.rstrip(b"\n")
        if "id" in decoded:
            raise ProtocolRefusal(
                "quota_payload_invalid", "Codex app-server response id is unexpected"
            )
    raise ProtocolRefusal(
        "quota_payload_invalid", "Codex app-server emitted too many notifications"
    )


def collect_codex_app_server(
    executable: Path,
    *,
    observed_at: str,
    idempotency_key: str,
    timeout_seconds: float = 5.0,
) -> QuotaReceipt:
    if (
        not isinstance(executable, Path)
        or not executable.is_absolute()
        or not executable.is_file()
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
        or timeout_seconds > 30
    ):
        raise ProtocolRefusal(
            "quota_collector_invalid", "Codex collector requires a local executable and bounded timeout"
        )
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        process = subprocess.Popen(
            [str(executable), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
    except OSError as exc:
        raise ProtocolRefusal(
            "quota_provider_unavailable", "Codex app-server could not be started"
        ) from exc
    failure: Optional[ProtocolRefusal] = None
    receipt: Optional[QuotaReceipt] = None
    try:
        _write_message(process, {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "floati-quota-observer", "version": "0"}},
        })
        _read_response(process, 1, deadline)
        _write_message(process, {"method": "initialized"})
        _write_message(process, {"id": 2, "method": "account/rateLimits/read", "params": {}})
        payload = _read_response(process, 2, deadline)
        receipt = adapter_for("openai_codex").observe(
            payload,
            observed_at=observed_at,
            idempotency_key=idempotency_key,
        )
    except ProtocolRefusal as exc:
        failure = exc
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(0.05, min(1.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        stderr_evidence = b""
        if process.stderr is not None:
            stderr_evidence = process.stderr.read(513)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    if failure is not None:
        detail = failure.detail
        if stderr_evidence:
            stderr_sample = stderr_evidence[:512]
            detail += (
                "; stderr_sample_bytes=" + str(len(stderr_sample))
                + "; stderr_sample_sha256=" + _digest(stderr_sample)
                + "; stderr_truncated="
                + ("true" if len(stderr_evidence) > len(stderr_sample) else "false")
            )
        raise ProtocolRefusal(failure.code, detail, failure.remedy) from failure
    if receipt is None:
        raise ProtocolRefusal(
            "quota_provider_unavailable", "Codex app-server produced no quota receipt"
        )
    return receipt
