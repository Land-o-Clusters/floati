"""Canonical evidence for the dark journal's attended enable operation."""

from __future__ import annotations

import re
from ipaddress import ip_address
from pathlib import PurePosixPath

from .cross_coordinates import MAX_INTEGER, digest, enc, semantic
from .errors import ProtocolRefusal
from .framing import encode_frame


_HEX16 = re.compile(r"[0-9a-f]{32}\Z")
_LEGACY_ENABLE_PANEL = b"floati-enable-panel-v7"
_CURRENT_ENABLE_PANEL = b"floati-enable-panel-v12-am4"
_ENABLE_PANELS = (
    _CURRENT_ENABLE_PANEL, _LEGACY_ENABLE_PANEL, b"floati-enable-panel-v12",
    b"floati-enable-panel-v12-am1", b"floati-enable-panel-v12-am3",
)


def _refused():
    return ProtocolRefusal("cross_enable_approval_invalid", "enable requires the exact settled attended approval")


def endpoint_refusal():
    return ProtocolRefusal("cross_endpoint_unproved", "the attended bind and local endpoint owner are unproved")


def _endpoint_address(address, *, bound=False):
    if (not isinstance(address, dict) or set(address) != {"host", "port"}
        or not isinstance(address["host"], str) or not address["host"] or len(address["host"]) > 253
        or type(address["port"]) is not int or not int(bound) <= address["port"] <= 65535):
        raise endpoint_refusal()
    host = address["host"]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:%-]*|:[a-zA-Z0-9.:%_-]*", host):
        raise endpoint_refusal()
    try:
        numeric = ip_address(host)
    except ValueError:
        if bound or ":" in host or "%" in host:
            raise endpoint_refusal()
        numeric = None
    return host, address["port"], numeric


def endpoint_bind_spec(address) -> str:
    """Spell the exact approved LAN bind in an attended plan."""
    host, port, numeric = _endpoint_address(address)
    if numeric is not None and (numeric.is_unspecified or getattr(numeric, "ipv4_mapped", None) is not None):
        raise endpoint_refusal()
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def validate_endpoint_registration(record):
    required = {"kind", "schema_version", "id", "enable_id", "enable_generation", "approved", "bound",
                "dual_stack", "pid", "start_token", "socket_token"}
    if (set(record) != required or record["kind"] != "remote_endpoint_registered"
        or type(record["schema_version"]) is not int or record["schema_version"] != 1
        or not isinstance(record["id"], str) or not re.fullmatch(r"cross-endpoint-[0-9a-f]{32}", record["id"])
        or not isinstance(record["enable_id"], str) or not _HEX16.fullmatch(record["enable_id"])
        or type(record["enable_generation"]) is not int or not 1 <= record["enable_generation"] <= MAX_INTEGER
        or type(record["pid"]) is not int or not 1 <= record["pid"] <= 2**31 - 1
        or not isinstance(record["start_token"], str) or not re.fullmatch(r"[a-zA-Z0-9._:-]{1,256}", record["start_token"])
        or not isinstance(record["socket_token"], str) or not _HEX16.fullmatch(record["socket_token"])):
        raise endpoint_refusal()
    endpoint_bind_spec(record["approved"])
    host, port, numeric = _endpoint_address(record["approved"])
    _, bound_port, bound_ip = _endpoint_address(record["bound"], bound=True)
    wildcard = host == "any"
    if (record["dual_stack"] is not wildcard or (port != 0 and port != bound_port)
        or (bound_ip != ip_address("::") if wildcard else bound_ip.is_unspecified or
            getattr(bound_ip, "ipv4_mapped", None) is not None or (numeric is not None and numeric != bound_ip))):
        raise endpoint_refusal()


def _check_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"listen", "root", "store", "unit"}:
        raise _refused()
    root, listen = plan["root"], plan["listen"]
    if (not isinstance(root, str) or not root.startswith("/") or str(PurePosixPath(root)) != root
        or ".." in PurePosixPath(root).parts or not root.isascii() or any(ord(char) < 32 for char in root)
        or not isinstance(listen, list)
        or any(not isinstance(item, str) or not item.isascii() or not item or any(ord(char) <= 32 for char in item)
               for item in listen)
        or sorted(set(listen)) != listen
        or plan["store"] not in ("keychain", "secret-service")
        or plan["unit"] not in ("none", "launchd" if plan["store"] == "keychain" else "systemd-user")):
        raise _refused()


def approved_start(enable_id: str, generation: int, token: str, plan: dict, *, schema_version: int = 1) -> dict:
    """Encode a plan that the local entry point has already approved."""
    # Version 1 construction remains available for the historical fixtures.
    panel = _CURRENT_ENABLE_PANEL if schema_version == 2 else _LEGACY_ENABLE_PANEL
    return _start_for_panel(enable_id, generation, token, plan, schema_version, panel)


def _start_for_panel(enable_id, generation, token, plan, schema_version, panel):
    if (type(schema_version) is not int or schema_version not in (1, 2)
        or not isinstance(enable_id, str) or not _HEX16.fullmatch(enable_id)
        or not isinstance(token, str) or not _HEX16.fullmatch(token) or token == enable_id
        or type(generation) is not int or not 1 <= generation <= MAX_INTEGER):
        raise _refused()
    _check_plan(plan)
    rendered = digest(enc(panel), enc(bytes.fromhex(enable_id)),
                      generation.to_bytes(8, "big"), enc(bytes.fromhex(token)), enc(encode_frame(plan)[:-1]))
    return {
        "schema_version": schema_version, "kind": "cross_optin", "phase": "enable_started",
        "id": f"cross-optin-{enable_id}-enable-started", "enable_id": enable_id,
        "enable_generation": generation, "token": token, "cause": None,
        "plan": dict(plan, listen=list(plan["listen"])),
        "human_act": {"channel": "controlling_terminal", "rendered_digest": rendered},
    }


def verified_start(start: dict) -> dict:
    """Reconstruct the exact sealed body using its issuing panel commitment."""
    coordinates = tuple(start.get(key) for key in ("enable_id", "enable_generation", "token", "plan", "schema_version"))
    for panel in _ENABLE_PANELS:
        expected = _start_for_panel(*coordinates, panel)
        if expected == start:
            return expected
    raise _refused()


def approved_completion(start: dict) -> dict:
    start = verified_start(semantic(start))
    enable_id, generation, token = (start[key] for key in ("enable_id", "enable_generation", "token"))
    return {
        "schema_version": start["schema_version"], "kind": "cross_optin", "phase": "enable_complete",
        "id": f"cross-optin-{enable_id}-enable-complete", "enable_id": enable_id,
        "enable_generation": generation, "token": token, "cause": None,
        "approval_sha256": digest(encode_frame(start)[:-1]),
    }


def _review_id(enable_id, generation, transition_id):
    identifier = digest(enc(b"floati-attended-review-v7"), enc(bytes.fromhex(enable_id)),
                        generation.to_bytes(8, "big"), enc(transition_id.encode("ascii")))
    return f"cross-review-{identifier}"


def review_item(start: dict, complete: dict) -> dict:
    complete = semantic(complete)
    if approved_completion(start) != complete:
        raise _refused()
    enable_id, generation, transition_id = (complete[k] for k in ("enable_id", "enable_generation", "id"))
    return {
        "id": _review_id(enable_id, generation, transition_id), "kind": "cross_attended_review", "schema_version": 1,
        "act": "enable", "pair_id": None, "role": None, "enable_id": enable_id, "enable_generation": generation,
        "token": complete["token"], "channel": "controlling_terminal",
        "rendered_digest": start["human_act"]["rendered_digest"], "transition_id": transition_id,
        "transition_sha256": digest(encode_frame(complete)[:-1]), "approval_sha256": complete["approval_sha256"],
    }


def activation_review_item(activation: dict) -> dict:
    """Project the immutable review from a verified attended activation."""
    body = semantic(activation)
    enable_id, generation, transition_id = (body[key] for key in ("enable_id", "enable_generation", "id"))
    commitment = digest(encode_frame(body)[:-1])
    return {
        "id": _review_id(enable_id, generation, transition_id), "kind": "cross_attended_review", "schema_version": 1,
        "act": "activate", "pair_id": body["pair_id"], "role": body["role"], "enable_id": enable_id,
        "enable_generation": generation, "token": body["ceremony_token"], "channel": "controlling_terminal",
        "rendered_digest": body["human_act"]["rendered_digest"], "transition_id": transition_id,
        "transition_sha256": commitment, "approval_sha256": commitment,
    }
