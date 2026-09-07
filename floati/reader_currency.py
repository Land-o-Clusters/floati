"""Read-only currency observations for explicitly declared installed readers."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

from .git_process import fixed_git_command, fixed_git_environment, is_shallow_repository

SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_JSON = 1024 * 1024


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
    if not root.exists():
        return _finding("installed_reader_absent", root, "The declared reader installation is absent.", remedy, fact)
    if root.is_symlink() or not root.is_dir():
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
            raise ValueError("source history is shallow; fetch full history before measuring reader age")
        reference = _git(source, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if reference.returncode or SHA.fullmatch(reference.stdout.strip()) is None:
            raise ValueError("source reference cannot be resolved")
        reference_sha = reference.stdout.strip()
        fact["reference_sha"] = reference_sha
        present = _git(source, "cat-file", "-e", source_sha + "^{commit}")
        if present.returncode:
            raise ValueError("installed source commit is unavailable in this checkout")
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


def _hook_roots(hooks):
    document = _json(hooks)
    roots = set()
    for group in document.get("hooks", {}).get("Stop", []):
        for handler in group.get("hooks", []):
            command = handler.get("command")
            if not isinstance(command, str):
                continue
            for word in shlex.split(command):
                launcher = Path(word)
                if launcher.is_absolute() and launcher.name == "floati-codex-wait" and launcher.parent.name == "scripts":
                    roots.add(launcher.parent.parent)
    return sorted(roots)


def governed_install_findings(source, ref, *, profile_registry=None, hooks=None, gateway_receipt=None):
    rows = []
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
                if destination.name not in {"boardbus.md", "floati-agent-instructions.md"}:
                    raise ValueError("instruction receipt names an unsupported asset")
                if destination.is_symlink() or source_asset.is_symlink():
                    raise ValueError("instruction asset is a symlink")
                expected = hashlib.sha256(source_asset.read_bytes()).hexdigest()
                actual = hashlib.sha256(destination.read_bytes()).hexdigest()
                if actual != expected:
                    rows.append(_finding("host_codex_instruction_vendored_source_drift", destination,
                        "Installed Codex instructions differ from the vendored source.", "Run scripts/install-codex-gateway.sh from the current source checkout."))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            rows.append(_finding("host_codex_instruction_unavailable", gateway_receipt, str(exc),
                                 "Run scripts/install-codex-gateway.sh from the current source checkout."))
    seen = set()
    for registry in dict.fromkeys(registries):
        if not registry.exists() and not registry.is_symlink():
            rows.append(_finding("installed_reader_registry_absent", registry, "No fleet profile registry is declared at this Codex coordinate.", None, severity="info"))
            continue
        try:
            document = _json(registry)
            for transport_name, transport in document["transports"].items():
                root = Path(transport["install_root"])
                if root in seen:
                    continue
                seen.add(root)
                metadata = Path(transport["manifest_path"])
                if not root.is_absolute() or metadata != root / ".floati-install/manifest.v0.json":
                    raise ValueError("transport manifest is outside declared install root")
                row = installed_reader_finding(source, ref, root, "transport", metadata)
                profiles = sorted(name for name, profile in document.get("profiles", {}).items() if profile.get("transport") == transport_name)
                if row["remediation"] and profiles:
                    row["remediation"] += f" --profile-registry {shlex.quote(str(registry))} --fleet-profile {shlex.quote(profiles[0])}"
                rows.append(row)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            rows.append(_finding("installed_reader_registry_unavailable", registry, str(exc), "Provide the declared fleet-bus-profiles.json registry."))
    if hooks is not None and Path(hooks).exists():
        try:
            for root in _hook_roots(hooks):
                if root not in seen:
                    seen.add(root)
                    rows.append(installed_reader_finding(source, ref, root, "waiter"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            rows.append(_finding("installed_reader_hooks_unavailable", hooks, str(exc), "Provide the exact readable Codex hooks.json file."))
    return rows
