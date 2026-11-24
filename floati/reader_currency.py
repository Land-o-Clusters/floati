"""Read-only currency observations for explicitly declared installed readers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .git_process import fixed_git_command, fixed_git_environment, is_shallow_repository

SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_DIR = re.compile(r"[0-9a-f]{64}\Z")
MAX_JSON = 1024 * 1024
_ALIAS_CODES = {
    "escapes": "installed_reader_alias_escapes",
    "chained": "installed_reader_alias_chained",
    "non_digest": "installed_reader_alias_non_digest",
}


def _review_current_alias(root: Path):
    """Admit only `current` → one sibling digest directory. Never follow a chain."""

    if root.name != "current":
        return root, "unavailable"
    try:
        target = os.readlink(root)
    except OSError:
        return root, "unavailable"
    parts = Path(target).parts
    if Path(target).is_absolute() or ".." in parts or len(parts) != 1:
        return root, "escapes"
    destination = root.parent / target
    if destination.is_symlink():
        return root, "chained"
    if DIGEST_DIR.fullmatch(target) is None:
        return root, "non_digest"
    if not destination.is_dir():
        return root, "unavailable"
    return destination, "ok"


def _json(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("coordinate is not an absolute ordinary file")
    if path.stat().st_size > MAX_JSON:
        raise ValueError("coordinate exceeds one MiB")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("coordinate is not an object")
    return value


def _git(source, *args):
    return subprocess.run(fixed_git_command("/usr/bin/git", source, args),
                          env=fixed_git_environment("/usr/bin/git"), capture_output=True,
                          text=True, timeout=15, check=False)


def update_remedy(source, destination):
    return f"floati update --source {shlex.quote(str(source))} --destination {shlex.quote(str(destination))}"


def _source_fetch_remedy(source, *, unshallow=False):
    option = " --unshallow" if unshallow else ""
    return f"git -C {shlex.quote(str(source))} fetch{option} origin"


def _finding(code, subject, detail, remedy, fact=None, severity="warning"):
    from .doctor import _finding as doctor_finding
    row = doctor_finding(code, severity, str(subject), detail, remedy)
    if fact is not None:
        row["installed_reader"] = fact
    return row


def installed_reader_finding(source, ref, root, kind, metadata=None):
    root = Path(root)
    if kind == "waiter":
        from .installed_reader import waiter_repair_remedy
        remedy = waiter_repair_remedy(source)
    else:
        remedy = update_remedy(source, root)
    fact = {"kind": kind, "source_sha": None, "reference_sha": None, "commits_behind": None, "state": "absent"}
    if root.is_symlink():
        measured, status = _review_current_alias(root)
        if status != "ok":
            fact["state"] = "unavailable"
            code = _ALIAS_CODES.get(status, "installed_reader_unavailable")
            return _finding(
                code,
                root,
                f"The declared reader current alias is {status.replace('_', '-')}.",
                remedy,
                fact,
            )
        root = measured
    elif not root.exists():
        return _finding("installed_reader_absent", root, "The declared reader installation is absent.", remedy, fact)
    elif not root.is_dir():
        fact["state"] = "unavailable"
        return _finding("installed_reader_unavailable", root, "The declared reader is not an ordinary directory.", remedy, fact)
    try:
        path = Path(metadata) if metadata else root / ".floati-installed-reader.json"
        if metadata is None and not path.exists():
            path = root / ".floati-install/manifest.v0.json"
        document = _json(path)
        source_sha = document.get("source_sha")
        if not isinstance(source_sha, str) or SHA.fullmatch(source_sha) is None:
            raise ValueError("installed source SHA is not measured")
        fact["source_sha"] = source_sha
        if is_shallow_repository(Path(source), git_executable="/usr/bin/git"):
            fact["state"] = "unavailable"
            return _finding("installed_reader_source_unavailable", root,
                            "source history is shallow; fetch full history before measuring reader age",
                            _source_fetch_remedy(source, unshallow=True), fact)
        reference = _git(source, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if reference.returncode or SHA.fullmatch(reference.stdout.strip()) is None:
            fact["state"] = "unavailable"
            return _finding("installed_reader_source_unavailable", root,
                            "source reference cannot be resolved", _source_fetch_remedy(source), fact)
        reference_sha = reference.stdout.strip()
        fact["reference_sha"] = reference_sha
        present = _git(source, "cat-file", "-e", source_sha + "^{commit}")
        if present.returncode:
            fact["state"] = "unavailable"
            return _finding("installed_reader_source_unavailable", root,
                            f"Installed source commit {source_sha} is unavailable in declared checkout {source}; fetch origin in that clone before measuring reader currency.",
                            _source_fetch_remedy(source), fact)
        ancestry = _git(source, "merge-base", "--is-ancestor", source_sha, reference_sha)
        if ancestry.returncode == 1:
            reverse = _git(source, "merge-base", "--is-ancestor", reference_sha, source_sha)
            if reverse.returncode == 0:
                fact["state"] = "ahead"
                return _finding("installed_reader_ahead", root, "Installed source is newer than the named reference.", None, fact, "ok")
            if reverse.returncode != 1:
                raise ValueError("reverse ancestry could not be measured")
            fact["state"] = "diverged"
            return _finding("installed_reader_diverged", root, "Installed source is not an ancestor of the named reference.", remedy, fact)
        if ancestry.returncode:
            raise ValueError("ancestry could not be measured")
        count = _git(source, "rev-list", "--count", source_sha + ".." + reference_sha)
        if count.returncode or not count.stdout.strip().isdigit():
            raise ValueError("commit distance could not be measured")
        fact["commits_behind"] = int(count.stdout.strip())
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        fact["state"] = "unavailable"
        return _finding("installed_reader_source_unavailable", root, str(exc), remedy, fact)
    if fact["commits_behind"]:
        fact["state"] = "behind"
        return _finding("installed_reader_behind", root,
                        f"Installed source {source_sha} is {fact['commits_behind']} commits behind {reference_sha}.", remedy, fact)
    fact["state"] = "current"
    return _finding("installed_reader_current", root, f"Installed source matches {reference_sha}.", None, fact, "ok")


def _hook_bindings(hooks):
    from .codex_hook_install import parse_waiter_stop_argv

    document = _json(hooks)
    roots = {}
    for group in document.get("hooks", {}).get("Stop", []):
        for handler in group.get("hooks", []):
            command = handler.get("command")
            if not isinstance(command, str):
                continue
            parsed = parse_waiter_stop_argv(shlex.split(command))
            if parsed is None:
                if "floati-codex-wait" in command:
                    raise ValueError("declared Floati Stop command has an unrecognized argument shape")
                continue
            launcher, bus = parsed
            if launcher.is_absolute() and launcher.name == "floati-codex-wait" and launcher.parent.name == "scripts":
                roots.setdefault(launcher.parent.parent, set()).add(str(bus))
    return roots


def _transport_pin_finding(transport_name, transport, metadata, remedy):
    """Compare registry testimony with the declared manifest, independently of age."""
    if not metadata.exists() and not metadata.is_symlink():
        return None  # The ordinary reader finding names the absent installation.
    document = _json(metadata)
    actual = hashlib.sha256(metadata.read_bytes()).hexdigest()
    pinned = transport.get("manifest_sha256")
    source = document.get("source_sha")
    claimed = transport.get("source_sha")
    if pinned == actual and source == claimed:
        return None
    return _finding("installed_reader_transport_pin_drift", metadata,
        f"Transport {transport_name}: manifest SHA-256 pinned {pinned}; installed {actual}; "
        f"source commit pinned {claimed}; installed {source}.", remedy)


def _hook_operator_repair(row, root, hooks, binding, source):
    """Plan the existing native operator act; never acknowledge or install here."""
    from .codex_hook_install import CodexHookInstaller, STOP_HOOK_CHANGE_CONSEQUENCE
    from .errors import ProtocolRefusal

    bus, registry, name, install, profile, transport = binding
    try:
        installer = CodexHookInstaller(source_root=install, bus_home=Path(bus),
            hooks_path=Path(hooks), destination=root.parent)
        plan = installer.plan_stop_definition(hook_timeout_seconds=86400)
    except ProtocolRefusal as exc:
        row["detail"] += f" Native operator repair refused {exc.code}: {exc.detail}"
        row["remediation"] = (exc.remedy if isinstance(exc.remedy, str) else
                              f"Review the malformed Stop hook at {hooks} before native operator installation.")
        return
    argv = [transport["interpreter"], "-B", "-m", "floati.codex_hook_install",
        "--source", str(install), "--root", bus, "--hooks", str(hooks),
        "--destination", str(root.parent), "--workspace", profile["workspace_root"],
        "--node", profile["identity"], "--hook-timeout-seconds", "86400",
        "--wait-deadline-seconds", "86300", "--acknowledge-stop-hook-change",
        plan["preimage"] + ":" + plan["target"]]
    row["remediation"] = (update_remedy(source, install)
                          + f" --profile-registry {shlex.quote(str(registry))} --fleet-profile {shlex.quote(name)}"
                          + f" && cd {shlex.quote(str(install))} && "
                          + " ".join(shlex.quote(part) for part in argv))
    row["detail"] += (" Only the human operator may run the named native repair after reading it. "
                      + STOP_HOOK_CHANGE_CONSEQUENCE + ".")


def governed_install_findings(source, ref, *, profile_registry=None, hooks=None, gateway_receipt=None):
    rows = []
    profile_bindings = []
    gateway_remedy = shlex.quote(str(Path(source) / "scripts/install-codex-gateway.sh"))
    registries = [] if profile_registry is None else [Path(profile_registry)]
    if gateway_receipt is not None and Path(gateway_receipt).exists():
        try:
            evidence = _json(gateway_receipt)["evidence"]
            if evidence.get("profile_registry"):
                registries.append(Path(evidence["profile_registry"]))
            for asset in evidence.get("instruction_assets", []):
                destination = Path(asset["destination"])
                # Compare against current vendored source, not a potentially stale receipt pin.
                source_asset = Path(source) / "tools/codex" / destination.name
                if destination.name not in {"boardbus.md", "floati-agent-instructions.md", "takeover.md"}:
                    raise ValueError("instruction receipt names an unsupported asset")
                if destination.is_symlink() or source_asset.is_symlink():
                    raise ValueError("instruction asset is a symlink")
                expected = hashlib.sha256(source_asset.read_bytes()).hexdigest()
                actual = hashlib.sha256(destination.read_bytes()).hexdigest()
                if actual != expected:
                    rows.append(_finding("host_codex_instruction_vendored_source_drift", destination,
                        f"Installed Codex instruction SHA-256 {actual}; vendored source {expected}.", gateway_remedy))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            rows.append(_finding("host_codex_instruction_unavailable", gateway_receipt, str(exc),
                                 gateway_remedy))
    seen = set()
    for registry in dict.fromkeys(registries):
        if not registry.exists() and not registry.is_symlink():
            rows.append(_finding("installed_reader_registry_absent", registry, "No fleet profile registry is declared at this Codex coordinate.", None, severity="info"))
            continue
        try:
            document = _json(registry)
            for transport_name, transport in document["transports"].items():
                root = Path(transport["install_root"])
                metadata = Path(transport["manifest_path"])
                if not root.is_absolute() or metadata != root / ".floati-install/manifest.v0.json":
                    raise ValueError("transport manifest is outside declared install root")
                profiles = sorted(name for name, profile in document.get("profiles", {}).items() if profile.get("transport") == transport_name)
                suffix = (f" --profile-registry {shlex.quote(str(registry))} --fleet-profile {shlex.quote(profiles[0])}"
                          if profiles else "")
                remedy = update_remedy(source, root) + suffix
                pin = _transport_pin_finding(transport_name, transport, metadata, remedy)
                if pin is not None:
                    rows.append(pin)
                for name in profiles:
                    profile = document["profiles"][name]
                    if "attach" in profile.get("allowed_operations", []):
                        profile_bindings.append((profile.get("bus_root"), registry, name, root, profile, transport))
                # Different transports may pin the same install differently.
                # Compare each pin before deduplicating the history observation.
                if root in seen:
                    continue
                seen.add(root)
                row = installed_reader_finding(source, ref, root, "transport", metadata)
                if row["remediation"] and row["remediation"].startswith("floati update "):
                    row["remediation"] += suffix
                rows.append(row)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            rows.append(_finding("installed_reader_registry_unavailable", registry, str(exc), "Provide the declared fleet-bus-profiles.json registry."))
    if hooks is not None and Path(hooks).exists():
        try:
            for root, buses in sorted(_hook_bindings(hooks).items()):
                if root not in seen:
                    seen.add(root)
                    row = installed_reader_finding(source, ref, root, "waiter")
                    bindings = [binding for binding in profile_bindings if binding[0] in buses]
                    if row["remediation"] and row["remediation"].startswith("floati update ") and bindings:
                        # A shared bus can have several seats. Select a named,
                        # declared coordinate deterministically, never a placeholder.
                        binding = sorted(bindings, key=lambda value: (str(value[1]), value[2]))[0]
                        _hook_operator_repair(row, root, hooks, binding, source)
                    rows.append(row)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            rows.append(_finding("installed_reader_hooks_unavailable", hooks, str(exc), "Provide the exact readable Codex hooks.json file."))
    return rows
