"""Receipt a vanished Codex target without treating changed bytes as an upgrade."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from .bus_epoch import shared_epoch_operation
from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .registry import Registry, utc_now
from .seat_declaration import require_declared_coordinate
from .wake_daemon_contract import AdapterBindingStore, DAEMON_KINDS
from .wake_daemon_roll import lifecycle_relative
from .wake_hold import wake_coordination_guard

KIND = 'codex_binding_repair_receipt'
_BANNER = re.compile(r'codex-cli [0-9]+\.[0-9]+\.[0-9]+'
    r'(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
    r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?')


def _provider_banner(executable, workspace):
    # Never inherit provider credentials, loader overrides or user config.
    # Keep conventional interpreter directories for npm's env-node launcher.
    with tempfile.TemporaryDirectory(prefix='floati-codex-identity-') as home:
        try:
            completed = subprocess.run([str(executable), '--version'], cwd=workspace,
                env={'HOME': home, 'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin',
                     'LANG': 'C', 'LC_ALL': 'C', 'NO_COLOR': '1'},
                stdin=subprocess.DEVNULL, capture_output=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProtocolRefusal('wake_daemon_codex_identity_invalid',
                'declared executable could not supply a bounded Codex version banner') from exc
    try:
        banner = completed.stdout.decode('utf-8').strip()
    except UnicodeError:
        banner = ''
    if completed.returncode != 0 or len(banner) > 256 or _BANNER.fullmatch(banner) is None:
        raise ProtocolRefusal('wake_daemon_codex_identity_invalid',
            'declared executable did not identify itself with a Codex version banner')
    return banner


@shared_epoch_operation
def repair_codex_binding(coordinate):
    """Operational adapter entry; read-only observers never call this writer."""
    with wake_coordination_guard(coordinate.root, coordinate.node_id):
        return repair_codex_binding_already_guarded(coordinate)


def repair_codex_binding_already_guarded(coordinate, *, acting_session=None):
    from .wake_daemon_adapters import CODEX_EXECUTABLE, adapter_contract_digest

    if coordinate.harness != 'codex':
        return None
    store = AdapterBindingStore(coordinate.root)
    try:
        prior = store._read_metadata(coordinate)
    except ProtocolRefusal as exc:
        if exc.code == 'wake_daemon_binding_absent':
            return None
        raise
    store._validate_metadata_contract(prior)
    recorded = Path(prior['executable'])
    try:
        recorded.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ProtocolRefusal('wake_daemon_executable_invalid',
            'recorded Codex executable cannot be inspected: ' + str(recorded)) from exc
    else:
        # Existing but unreadable, invalid, symlinked or digest-drifted paths
        # are never eligible for automatic trust refresh.
        # Boarding does not launch the provider. An intact recorded target
        # needs no repair, even if the stable declaration has moved or gone.
        # Queue launch independently checks that declaration before execution.
        store.read(coordinate)
        return None
    workspace = Path(prior['workspace'])
    participant = resolve_participant(coordinate.root.tenant_home, workspace)
    if (participant is None or participant.root.path != coordinate.root.path
            or participant.binding.node_id != coordinate.node_id):
        raise ProtocolRefusal('wake_daemon_codex_binding_source_invalid',
            'repair requires the existing declared waiter workspace')
    declaration = require_declared_coordinate(workspace, coordinate.node_id, coordinate.root)
    consent = CodexWaitConsentLedger(coordinate.root).require_armed(participant.binding)
    claims = read_records_snapshot(coordinate.root,
        CodexWaitSessionLedger._relative(coordinate.node_id),
        allowed_kinds=CodexWaitSessionLedger._KINDS)
    matching = [row for row in claims if row['workspace'] == str(workspace)]
    session = prior['session_id'] if acting_session is None else acting_session
    if (declaration['workspace_identity'] != 'declared' or not matching
            or str(Registry(coordinate.root).require_active(coordinate.node_id).get('role', '')).casefold() != 'codex'
            or matching[-1]['acting_session_id'] != session
            or matching[-1]['consent_receipt_id'] != consent['id']
            or matching[-1]['workspace_map_digest'] != participant.binding.map_digest
            or prior['adapter_version'] != '1'
            or prior['adapter_digest'] != adapter_contract_digest('codex')):
        raise ProtocolRefusal('wake_daemon_codex_binding_source_invalid',
            'repair requires the current armed claim, consent and Codex adapter contract')
    try:
        executable = store._binding_executable(coordinate, CODEX_EXECUTABLE)
    except (OSError, RuntimeError, ProtocolRefusal) as exc:
        raise ProtocolRefusal('wake_daemon_executable_invalid',
            'declared Codex executable is missing or invalid: ' + str(CODEX_EXECUTABLE)) from exc
    try:
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProtocolRefusal('wake_daemon_codex_identity_changed',
            'declared executable disappeared before provider identification') from exc
    banner = _provider_banner(executable, workspace)
    try:
        changed = (CODEX_EXECUTABLE.resolve(strict=True) != executable
            or hashlib.sha256(executable.read_bytes()).hexdigest() != digest)
    except (OSError, RuntimeError) as exc:
        raise ProtocolRefusal('wake_daemon_codex_identity_changed',
            'declared executable disappeared during provider identification') from exc
    if changed:
        raise ProtocolRefusal('wake_daemon_codex_identity_changed',
            'declared executable changed during provider identification')
    receipt = {
        'schema_version': 1, 'kind': KIND, 'id': 'codex-binding-repair-' + uuid7_hex(),
        'tenant_id': coordinate.root.tenant_id, 'timestamp': utc_now(),
        'node_id': coordinate.node_id, 'coordinate_digest': coordinate.digest,
        'acting_session_id': session, 'workspace': str(workspace),
        'arm_receipt_id': matching[-1]['id'], 'declared_executable': str(CODEX_EXECUTABLE),
        'old_executable': prior['executable'], 'old_executable_digest': prior['executable_digest'],
        'new_executable': str(executable), 'new_executable_digest': digest,
        'provider_banner': banner, 'reason': 'recorded_path_vanished',
        'idempotency_key': 'codex-repair-' + hashlib.sha256(
            (json.dumps(prior, sort_keys=True) + '\0' + matching[-1]['id']
             + '\0' + str(executable) + '\0' + digest).encode()).hexdigest(),
    }
    semantic = set(receipt) - {'id', 'timestamp'}
    # Native lifecycle rolls retain byte-identical testimony in owned archives.
    # A crash between receipt and replacement must replay across that boundary.
    archive_pattern = re.compile(re.escape(coordinate.node_id) + r'\.\d{8}T\d{6}Z(?:-\d+)?\.jsonl')
    def archived_rows():
        directory = coordinate.root.path / 'receipts/wake-daemon'
        for archive in directory.iterdir():
            if re.fullmatch(r'archive-\d{4}-\d{2}-\d{2}', archive.name) is None:
                continue
            if archive.is_symlink() or not archive.is_dir():
                raise ProtocolRefusal('wake_daemon_receipts_symlink',
                    'repair archive must be an ordinary owned lifecycle directory')
            for path in archive.glob(coordinate.node_id + '.*.jsonl'):
                if archive_pattern.fullmatch(path.name):
                    yield from read_records_snapshot(coordinate.root,
                        path.relative_to(coordinate.root.path), allowed_kinds=DAEMON_KINDS)
    def decide(rows):
        # transact owns the same lifecycle lock as native roll. Enumerating
        # archives here cannot miss testimony moving out of the current file.
        for row in [*rows, *archived_rows()]:
            if row.get('kind') == KIND and row['idempotency_key'] == receipt['idempotency_key']:
                if any(row[field] != receipt[field] for field in semantic):
                    raise ProtocolRefusal('idempotency_conflict', 'repair key has different testimony')
                return row, None
        return receipt, receipt
    result = transact(coordinate.root, lifecycle_relative(coordinate.node_id), decide,
        allowed_kinds=DAEMON_KINDS)
    # Receipt first: an interrupted binding replacement is recoverable with
    # the same claim and key, without losing the predecessor testimony.
    if store._read_metadata(coordinate) != prior:
        raise ProtocolRefusal('wake_daemon_codex_binding_changed', 'binding changed during repair')
    try:
        store.write(coordinate, session_id=session, workspace=workspace, executable=executable,
            adapter_version=prior['adapter_version'], adapter_digest=prior['adapter_digest'],
            binding_epoch=prior['binding_epoch'], resume_state=prior.get('resume_state'),
            expected_executable_digest=digest)
    except (OSError, RuntimeError) as exc:
        raise ProtocolRefusal('wake_daemon_codex_identity_changed',
            'declared executable disappeared during binding replacement') from exc
    return result
