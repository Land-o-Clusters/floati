"""Read-only testimony for the fixed, portable ZCode bridge store.

No launcher, bank package, registration command or authority checkout is executed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


STABLE_LAUNCHER_SHA256 = "6d15802045f974366789df5b1c9b94c488787af21325a36aef78c21de58dacde"
BANK_LAUNCHER_SHA256 = "4b271127e891a622bd5225337317189c8d4b417385317a2e78ebfd72f6cdcd0a"
PRIVATE_BRIDGE_SHA256 = "a821fc4e6f1b6d44733600292f4d074d251871b6891298302b7705bb68370717"
BRIDGE_PROVIDER_GENERATIONS = frozenset({
    (STABLE_LAUNCHER_SHA256, BANK_LAUNCHER_SHA256, PRIVATE_BRIDGE_SHA256),
    ("0af69635725761783536264af206159c9e03e2175a02af722244977543e3ed35",
     "feea3777e97abe04451b0fdf36c21c1e09d86191ca9e1d65975163fd43d80e4f",
     "6af17469311cc8bb08fa29d82b4ae386edd1169d2043df4f39847c35bb3ce037"),
    ("6e64b1e7d47be62c010ffc1fedffb1b82d16674d650318b43b301577101ef177",
     "e0ecb345b27d9f993294dbd7c42df4ba58c638de6c146b054263d392bc023484",
     "a701bc07607ff02d6c6cf736235939072ded3bc0caeb792d69f82cd423bfbef1"),
    ("963816d24619e31bb03dae1715375ec300ba29aca4f5b897d2c3268b2f673fb9",
     "d13244ccbc542778aa8293f9553e722ae2d9dc21e49c1da6919397b73db14ea2",
     "f12c80959ed086a61e1f19d36b04e6c3087426adc9b46da7d527252a95c52c99"),
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAX_FILE = 64 * 1024 * 1024
_MAX_TOTAL = 256 * 1024 * 1024


class BridgeBankUnavailable(ValueError):
    """A named store coordinate cannot testify to the selected runtime."""


def _provider_generation(stable_digest, launcher_path=None):
    """An entrypoint names exactly one reviewed bank/bridge pair."""
    matches = [row for row in BRIDGE_PROVIDER_GENERATIONS if row[0] == stable_digest]
    if len(matches) != 1:
        expected = ",".join(sorted({row[0] for row in BRIDGE_PROVIDER_GENERATIONS}))
        subject = "" if launcher_path is None else f": {launcher_path}"
        raise BridgeBankUnavailable(
            f"bridge entrypoint generation is unapproved or ambiguous{subject} "
            f"expected={expected} observed={stable_digest}")
    return matches[0]


def _ordinary(path, *, directory=False, private_file=True):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise BridgeBankUnavailable(f"noncanonical bridge coordinate: {path}")
    try:
        for parent in path.parents:
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise BridgeBankUnavailable(f"linked or non-directory bridge parent: {parent}")
        item = path.lstat()
    except OSError as exc:
        raise BridgeBankUnavailable(f"bridge coordinate unavailable: {path}: {type(exc).__name__}") from exc
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(item.st_mode) or item.st_uid != os.getuid()
            or item.st_mode & 0o022
            or (not directory and private_file and stat.S_IMODE(item.st_mode) != 0o600)):
        raise BridgeBankUnavailable(f"bridge coordinate is not ordinary and private: {path}")
    return item


def _read(path, maximum=_MAX_FILE, *, private_file=True):
    item = _ordinary(path, private_file=private_file)
    if item.st_size > maximum:
        raise BridgeBankUnavailable(f"bridge coordinate exceeds byte bound: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (item.st_dev, item.st_ino):
            raise BridgeBankUnavailable(f"bridge coordinate changed during open: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or len(payload) != before.st_size):
            raise BridgeBankUnavailable(f"bridge coordinate changed during read: {path}")
        return payload
    finally:
        os.close(descriptor)


def _digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _checked(path, expected, maximum=_MAX_FILE):
    payload = _read(path, maximum)
    observed = _digest(payload)
    if observed != expected:
        raise BridgeBankUnavailable(f"digest changed: {path} expected={expected} observed={observed}")
    return payload


def _json(payload, path):
    try:
        return json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise BridgeBankUnavailable(f"unreadable bridge JSON: {path}") from exc


def _reader_version(payload, path):
    try:
        tree = ast.parse(payload, filename=str(path))
    except (SyntaxError, ValueError, UnicodeError) as exc:
        raise BridgeBankUnavailable(f"bridge reader version is unnameable: {path}") from exc
    values = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "READER_VERSION" for target in targets):
            value = statement.value
            if (not isinstance(value, ast.Constant) or not isinstance(value.value, str)
                    or re.fullmatch(r"[0-9]+", value.value) is None):
                raise BridgeBankUnavailable(f"bridge reader version is not a numeric string literal: {path}")
            values.append(value.value)
    if len(values) != 1:
        raise BridgeBankUnavailable(f"bridge reader version is absent or ambiguous: {path}")
    return values[0]


def _installed_manifest(home, source_sha):
    """Anchor runtime testimony in the one fixed installed transport."""
    registry_path = home / ".codex/fleet-bus-profiles.json"
    registry = _json(_read(registry_path, 1024 * 1024), registry_path)
    if (not isinstance(registry, dict)
            or set(registry) != {"schema_version", "transports", "profiles"}
            or type(registry["schema_version"]) is not int or registry["schema_version"] != 2
            or not isinstance(registry["transports"], dict)
            or not isinstance(registry["profiles"], dict)):
        raise BridgeBankUnavailable(f"installed transport registry shape changed: {registry_path}")
    transport = registry["transports"].get("floati-installed-v0")
    install = home / ".local/share/floati"
    path = install / ".floati-install/manifest.v0.json"
    fields = {"kind", "install_root", "manifest_path", "manifest_sha256", "source_sha",
              "interpreter", "max_files", "max_file_bytes", "max_total_bytes"}
    if (not isinstance(transport, dict) or set(transport) != fields
            or transport["kind"] != "floati-install-manifest-v0"
            or transport["install_root"] != str(install) or transport["manifest_path"] != str(path)
            or transport["interpreter"] != "/usr/bin/python3"
            or not isinstance(transport["manifest_sha256"], str)
            or _DIGEST.fullmatch(transport["manifest_sha256"]) is None
            or transport["source_sha"] != source_sha
            or any(type(transport[name]) is not int or not 1 <= transport[name] <= bound
                   for name, bound in (("max_files", 4096), ("max_file_bytes", _MAX_FILE),
                                       ("max_total_bytes", _MAX_TOTAL)))):
        raise BridgeBankUnavailable(f"installed transport identity or bounds changed: {registry_path}: floati-installed-v0")
    raw = _checked(path, transport["manifest_sha256"], 4 * 1024 * 1024)
    metadata = _json(raw, path)
    if (not isinstance(metadata, dict) or metadata.get("source_sha") != source_sha
            or not isinstance(metadata.get("files"), list)
            or not 1 <= len(metadata["files"]) <= transport["max_files"]):
        raise BridgeBankUnavailable(f"installed runtime manifest shape changed: {path}")
    return raw, transport


def _verify_store(home, *, prior_identity=None, selector_payload=None):
    """Validate closed bank bytes; only receipts may supply a prior identity."""
    home = Path(home)
    _ordinary(home, directory=True)
    store = home / ".local/share/floati-zcode-bridge"
    _ordinary(store, directory=True)
    launcher = _read(store / "launcher.py", 1024 * 1024)
    generation = _provider_generation(_digest(launcher), store / "launcher.py")
    selector_path = store / "active.json"
    selector_raw = _read(selector_path, 4096) if selector_payload is None else selector_payload
    selector = _json(selector_raw, selector_path)
    if (not isinstance(selector, dict)
            or set(selector) != {"schema_version", "bank_sha256", "launcher_sha256"}
            or type(selector["schema_version"]) is not int or selector["schema_version"] != 0
            or selector["launcher_sha256"] != generation[0]
            or not isinstance(selector["bank_sha256"], str)
            or _DIGEST.fullmatch(selector["bank_sha256"]) is None):
        raise BridgeBankUnavailable(f"bridge selector shape or identity changed: {selector_path}")
    banks = store / "banks"
    _ordinary(banks, directory=True)
    bank = banks / selector["bank_sha256"]
    _ordinary(bank, directory=True)
    manifest_path = bank / "bank-manifest.json"
    raw = _checked(manifest_path, bank.name, 4 * 1024 * 1024)
    manifest = _json(raw, manifest_path)
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "source_sha", "registry_sha256", "transport_manifest_sha256", "files"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 0
            or not isinstance(manifest["source_sha"], str) or _SHA.fullmatch(manifest["source_sha"]) is None
            or any(not isinstance(manifest[name], str) or _DIGEST.fullmatch(manifest[name]) is None
                   for name in ("registry_sha256", "transport_manifest_sha256"))
            or not isinstance(manifest["files"], list) or not 1 <= len(manifest["files"]) <= 4099):
        raise BridgeBankUnavailable(f"bridge bank manifest shape changed: {manifest_path}")
    payloads = {}
    total = 0
    for row in manifest["files"]:
        if (not isinstance(row, dict) or set(row) != {"path", "sha256", "mode"}
                or not isinstance(row["path"], str) or not row["path"]
                or not isinstance(row["sha256"], str) or _DIGEST.fullmatch(row["sha256"]) is None
                or type(row["mode"]) is not int or row["mode"] != 0o600):
            raise BridgeBankUnavailable(f"bridge bank entry shape changed: {manifest_path}")
        relative = PurePosixPath(row["path"])
        if (relative.is_absolute() or ".." in relative.parts or str(relative) != row["path"]
                or not relative.parts or row["path"] == "bank-manifest.json" or row["path"] in payloads):
            raise BridgeBankUnavailable(f"unsafe bridge bank entry: {manifest_path}: {row['path']}")
        payloads[row["path"]] = _checked(bank / row["path"], row["sha256"])
        total += len(payloads[row["path"]])
        if total > _MAX_TOTAL:
            raise BridgeBankUnavailable(f"bridge bank exceeds total byte bound: {bank}")
    actual = set()
    for path in bank.rglob("*"):
        item = path.lstat()
        _ordinary(path, directory=stat.S_ISDIR(item.st_mode))
        if not stat.S_ISDIR(item.st_mode):
            actual.add(path.relative_to(bank).as_posix())
    if actual != set(payloads) | {"bank-manifest.json"}:
        raise BridgeBankUnavailable(f"bridge bank closure changed: {bank}")
    metadata_name = "runtime/.floati-install/manifest.v0.json"
    required = {metadata_name, "launcher.py", "hooks/stop-hook-bridge.py", "runtime/floati/records.py", "runtime/floati/__init__.py"}
    if not required.issubset(payloads):
        raise BridgeBankUnavailable(f"bridge bank required runtime files are absent: {bank}")
    for relative, expected in (("launcher.py", generation[1]),
                               ("hooks/stop-hook-bridge.py", generation[2])):
        observed = _digest(payloads[relative])
        if observed != expected:
            raise BridgeBankUnavailable(f"digest changed: {bank / relative} expected={expected} observed={observed}")
    if prior_identity is None:
        trusted, transport = _installed_manifest(home, manifest["source_sha"])
    else:
        if (not isinstance(prior_identity, dict)
                or set(prior_identity) != {"source_sha", "manifest_sha256", "bank_sha256"}
                or prior_identity["source_sha"] != manifest["source_sha"]
                or prior_identity["manifest_sha256"] != manifest["transport_manifest_sha256"]
                or prior_identity["bank_sha256"] != bank.name):
            raise BridgeBankUnavailable(f"receipted prior bank identity changed: {bank}")
        trusted = payloads[metadata_name]
        transport = {"max_file_bytes": _MAX_FILE, "max_total_bytes": _MAX_TOTAL}
    if payloads[metadata_name] != trusted:
        raise BridgeBankUnavailable(f"digest changed: {bank / metadata_name} expected={_digest(trusted)} observed={_digest(payloads[metadata_name])}")
    metadata = _json(payloads[metadata_name], bank / metadata_name)
    if (not isinstance(metadata, dict) or metadata.get("source_sha") != manifest["source_sha"]
            or _digest(payloads[metadata_name]) != manifest["transport_manifest_sha256"]
            or not isinstance(metadata.get("files"), list)):
        raise BridgeBankUnavailable(f"bridge runtime source identity changed: {bank / metadata_name}")
    runtime_paths = set()
    for row in metadata["files"]:
        if (not isinstance(row, dict) or set(row) != {"path", "sha256"}
                or not isinstance(row["path"], str) or not isinstance(row["sha256"], str)
                or _DIGEST.fullmatch(row["sha256"]) is None):
            raise BridgeBankUnavailable(f"bridge runtime inventory changed: {bank / metadata_name}")
        relative = "runtime/" + row["path"]
        if relative in runtime_paths or relative not in payloads or _digest(payloads[relative]) != row["sha256"]:
            raise BridgeBankUnavailable(f"bridge runtime inventory changed: {bank / relative}")
        runtime_paths.add(relative)
        if len(payloads[relative]) > transport["max_file_bytes"]:
            raise BridgeBankUnavailable(f"installed runtime file exceeds transport bound: {bank / relative}")
    if sum(len(payloads[name]) for name in runtime_paths) > transport["max_total_bytes"]:
        raise BridgeBankUnavailable(f"installed runtime exceeds transport total bound: {bank / 'runtime'}")
    if {name for name in payloads if name.startswith("runtime/")} != runtime_paths | {metadata_name}:
        raise BridgeBankUnavailable(f"bridge runtime closure changed: {bank / 'runtime'}")
    reader = _reader_version(payloads["runtime/floati/records.py"], bank / "runtime/floati/records.py")
    return {"bank": bank, "runtime": bank / "runtime", "source_sha": manifest["source_sha"],
            "reader_schema_version": reader, "bridge_sha256": _digest(payloads["hooks/stop-hook-bridge.py"]),
            "files": payloads, "manifest": manifest, "manifest_bytes": raw,
            "selector_bytes": selector_raw, "launcher_bytes": launcher}


def verify_bridge_store(home):
    """Strict current-runtime testimony, always anchored to installed transport."""
    result = _verify_store(home)
    return {name: result[name] for name in (
        "bank", "runtime", "source_sha", "reader_schema_version", "bridge_sha256")}
