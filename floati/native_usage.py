"""Small provider-reported token facts, separate from quota and billed money.

The native terminal owns identity and transport testimony. This module only
normalizes documented counters; it never reads history or credential stores.
"""
from copy import deepcopy

from .worker_errors import WorkerAdapterFailure

MAX_TOKENS = 9_223_372_036_854_775_807
_CONTRACTS = {
    "codex": ("thread/tokenUsage/updated.tokenUsage.total", "thread_cumulative", {
        "inputTokens": "input_tokens", "outputTokens": "output_tokens",
        "cachedInputTokens": "cache_read_input_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens"}),
    "claude": ("result.usage", "dispatch", {
        "input_tokens": "input_tokens", "output_tokens": "output_tokens",
        "cache_read_input_tokens": "cache_read_input_tokens",
        "cache_creation_input_tokens": "cache_creation_input_tokens"}),
    "pi": ("message_end.message.usage", "dispatch", {
        "input": "input_tokens", "output": "output_tokens",
        "cacheRead": "cache_read_input_tokens", "cacheWrite": "cache_creation_input_tokens",
        "reasoning": "reasoning_output_tokens"}),
}


def _invalid(code, detail):
    raise WorkerAdapterFailure(code)


def validate_usage(value, adapter, error=_invalid):
    """Validate the closed normalized fact, including its adapter-specific scope."""
    if adapter not in _CONTRACTS:
        error("protocol_error", "usage adapter has no reviewed token contract")
    source, scope, names = _CONTRACTS[adapter]
    if (type(value) is not dict or set(value) != {"status", "source", "scope", "tokens"}
            or value["status"] != "measured" or value["source"] != source or value["scope"] != scope):
        error("protocol_error", "usage requires its closed provider source and scope")
    tokens = value["tokens"]
    if (type(tokens) is not dict or not {"input_tokens", "output_tokens"} <= set(tokens)
            or not set(tokens) <= set(names.values())
            or any(type(count) is not int or not 0 <= count <= MAX_TOKENS for count in tokens.values())):
        error("protocol_error", "usage requires bounded provider-reported integer counters")
    if ("reasoning_output_tokens" in tokens
            and tokens["reasoning_output_tokens"] > tokens["output_tokens"]):
        error("protocol_error", "reasoning tokens must remain a subset of output")
    if adapter == "codex" and tokens.get("cache_read_input_tokens", 0) > tokens["input_tokens"]:
        error("protocol_error", "Codex cached input must remain a subset of input")
    return deepcopy(value)


def provider_usage(adapter, payload):
    """Normalize only explicit fields, retaining missing optional categories."""
    if type(payload) is not dict:
        raise WorkerAdapterFailure("protocol_error")
    source, scope, names = _CONTRACTS[adapter]
    result = {"status": "measured", "source": source, "scope": scope,
        "tokens": {target: payload[name] for name, target in names.items() if name in payload}}
    return validate_usage(result, adapter)


def add_pi_usage(previous, current):
    """Sum final assistant messages, never their repeated agent/turn summaries.

    Keep an optional category only when every included message reports it.
    This prevents a partial cache/reasoning count masquerading as a total.
    """
    current = validate_usage(current, "pi")
    if previous is None:
        return current
    previous = validate_usage(previous, "pi")
    current["tokens"] = {key: count + current["tokens"][key]
        for key, count in previous["tokens"].items() if key in current["tokens"]}
    return validate_usage(current, "pi")


def usage_observation(terminal):
    """Project only authenticated terminal metadata; old records stay readable."""
    if terminal is None or "usage" not in terminal:
        return {"status": "unsupported", "reason": "native_usage_not_reported"}
    adapter = {"codex_local": "codex", "claude_local": "claude", "pi_local": "pi"}[
        terminal["provider_coordinate"]["provider"]]
    return validate_usage(terminal["usage"], adapter)
