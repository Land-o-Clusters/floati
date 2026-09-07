"""Explicit ordinary-update binding to one governed transport registry."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import ProtocolRefusal
from .fleet_update_registry import _decode, _pin_spans, _real_file, rewrite_transport_pins

SUPPORTED_PROFILE_OPERATIONS = frozenset({
    "board", "inbox", "ack", "send", "attach", "doctor-probe", "waiter-smoke",
})
PROFILE_REMEDY = "Pass --profile-registry as the canonical registry file and --fleet-profile as its declared profile; update only that profile's install destination."


def prepare_profile_update(registry, profile, destination):
    if registry is None and profile is None:
        return None
    if not registry or not profile:
        raise ProtocolRefusal("profile_update_binding_invalid", "profile registry and fleet profile must be supplied together", PROFILE_REMEDY)
    try:
        path = _real_file(Path(registry))
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("registry exceeds one MiB")
        document = _decode(raw)
        profiles = document["profiles"]
        selected = profiles[profile]
        # All profiles must be usable by the same installed gateway. Validate
        # before install writes, including operations in unselected profiles.
        for name, row in profiles.items():
            operations = row["allowed_operations"]
            if not isinstance(operations, list) or not all(isinstance(op, str) for op in operations):
                raise ValueError("profile operations must be a string list")
            unsupported = sorted(set(operations) - SUPPORTED_PROFILE_OPERATIONS)
            if unsupported:
                raise ProtocolRefusal(
                    "profile_operations_unsupported", f"profile {name} lists unsupported operations: {', '.join(unsupported)}",
                    "Replace the named unsupported operations with the vendored gateway operations: " + ", ".join(sorted(SUPPORTED_PROFILE_OPERATIONS)),
                )
        transport_name = selected["transport"]
        transport = document["transports"][transport_name]
        root = Path(transport["install_root"])
        metadata = Path(transport["manifest_path"])
        if not root.is_absolute() or root.is_symlink() or root.resolve() != Path(destination).resolve():
            raise ValueError("destination does not match selected transport")
        if metadata != root / ".floati-install/manifest.v0.json":
            raise ValueError("transport manifest is outside its install root")
        _pin_spans(raw, transport_name)
        st = path.stat()
    except ProtocolRefusal:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ProtocolRefusal("profile_update_binding_invalid", str(exc), PROFILE_REMEDY) from exc
    return {"path": path, "profile": profile, "transport": transport_name, "metadata": metadata,
            "before_digest": hashlib.sha256(raw).hexdigest(), "identity": (st.st_dev, st.st_ino)}


def finish_profile_update(binding, source_sha):
    """The existing writer owns atomicity, readback and unrelated-byte preservation."""
    metadata = binding["metadata"]
    raw = metadata.read_bytes()
    document = json.loads(raw)
    if document.get("source_sha") != source_sha:
        raise ProtocolRefusal("profile_update_binding_invalid", "installed source identity differs from the completed update", PROFILE_REMEDY)
    return rewrite_transport_pins(
        binding["path"], binding["transport"],
        manifest_sha256=hashlib.sha256(raw).hexdigest(), source_sha=source_sha,
        expected_registry_sha256=binding["before_digest"], expected_identity=binding["identity"],
    )
