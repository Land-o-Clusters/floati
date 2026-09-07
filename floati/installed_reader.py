"""Explicit install provenance for readers facing newer ledger vocabulary."""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, Mapping

from .errors import ProtocolRefusal

PROVENANCE_NAME = '.floati-installed-reader.json'
_SHA = re.compile(r'[0-9a-f]{40}')


def _invalid(detail: str) -> None:
    raise ProtocolRefusal(
        'installed_reader_identity_invalid', detail,
        remedy='reinstall this reader from its governed source and retry',
    )


def _read(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            _invalid('installed reader provenance must be a bounded regular file')
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _invalid('installed reader provenance is unreadable: ' + type(exc).__name__)
    if not isinstance(value, dict):
        _invalid('installed reader provenance must be one object')
    return value


def installed_reader_identity(runtime_root: Path | None = None) -> dict[str, Any] | None:
    """Read only this runtime's provenance; absence identifies a source reader.

    The gateway may stage explicit launch coordinates. Persisted waiter provenance
    contains only commit testimony; its actual runtime directory is the install.
    """
    root = Path(__file__).resolve().parents[1] if runtime_root is None else Path(runtime_root)
    provenance = root / PROVENANCE_NAME
    metadata = root / '.floati-install' / 'manifest.v0.json'
    if provenance.exists() or provenance.is_symlink():
        value = _read(provenance)
        fields = set(value)
        persistent = {'schema_version', 'source_sha', 'source_state'}
        transient = {'schema_version', 'source_sha', 'source_checkout', 'install_root'}
        profile_bound = transient | {'profile_registry', 'fleet_profile'}
        if type(value.get('schema_version')) is not int or value['schema_version'] != 0 or fields not in (persistent, transient, profile_bound):
            _invalid('installed reader provenance shape is unsupported')
        sha = value.get('source_sha')
        if fields == persistent and value.get('source_state') == 'absent' and sha is None:
            pass
        elif not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
            _invalid('installed reader source_sha is invalid')
        elif fields == persistent and value.get('source_state') != 'measured':
            _invalid('installed reader source state is invalid')
        if fields in (transient, profile_bound):
            for key in ('source_checkout', 'install_root'):
                raw = value[key]
                if not isinstance(raw, str) or not Path(raw).is_absolute() or str(Path(raw)) != raw or '..' in Path(raw).parts:
                    _invalid('installed reader launch coordinate is invalid: ' + key)
            if fields == profile_bound:
                registry = value['profile_registry']
                profile = value['fleet_profile']
                if (not isinstance(registry, str) or not Path(registry).is_absolute()
                        or str(Path(registry)) != registry or Path(registry).resolve() != Path(registry)):
                    _invalid('installed reader profile registry is not a canonical absolute path')
                if not isinstance(profile, str) or re.fullmatch(r'[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?', profile) is None:
                    _invalid('installed reader fleet profile is invalid')
            return value
        return dict(value, install_root=str(root), source_checkout=None, reader_kind='waiter')
    if metadata.exists() or metadata.is_symlink():
        value = _read(metadata)
        sha = value.get('source_sha')
        if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
            _invalid('installed metadata source_sha is invalid')
        return {'schema_version': 0, 'source_sha': sha, 'source_state': 'measured',
                'source_checkout': None, 'install_root': str(root)}
    return None


def refuse_older_reader(kind: str, identity: Mapping[str, Any]) -> None:
    source = identity.get('source_checkout')
    checkout = shlex.quote(str(source)) if source else '<checkout>'
    destination = shlex.quote(str(identity['install_root']))
    sha = identity.get('source_sha')
    detail = f'reader source_sha {sha or "unmeasured"} does not know ledger kind {kind}'
    if source is None:
        detail += '; source checkout is unmeasured; replace <checkout> with the explicit source checkout'
    remedy = f'floati update --source {checkout} --destination {destination}'
    if identity.get('profile_registry') is not None:
        remedy += (' --profile-registry ' + shlex.quote(str(identity['profile_registry']))
                   + ' --fleet-profile ' + shlex.quote(str(identity['fleet_profile'])))
    elif identity.get('reader_kind') == 'waiter':
        remedy = waiter_repair_remedy()
    raise ProtocolRefusal(
        'reader_older_than_ledger', detail,
        remedy=remedy,
    )


def waiter_repair_remedy(source: Path | str | None = None) -> str:
    """Rebuild and bind a new waiter generation; never update a digest directory."""
    checkout = '<checkout>' if source is None else shlex.quote(str(source))
    return (f'floati update --source {checkout} --destination <governed-install-root>'
            ' --profile-registry <registry> --fleet-profile <profile>; then run'
            ' codex-fleet-bus <profile> attach to install and bind a new waiter generation;'
            ' if hook trust is pending, trust the exact Stop hook in Codex settings and relaunch')


def reader_failure(failure: Exception) -> ProtocolRefusal | None:
    """Preserve reader testimony through older wrappers that reclassify errors."""
    current: BaseException | None = failure
    for _ in range(16):
        if isinstance(current, ProtocolRefusal) and current.code in {
            'reader_older_than_ledger', 'installed_reader_identity_invalid',
        }:
            return current
        if current is None:
            break
        current = current.__cause__ or current.__context__
    return None


def raise_reader_failure(failure: Exception) -> None:
    refusal = reader_failure(failure)
    if refusal is not None:
        raise refusal
