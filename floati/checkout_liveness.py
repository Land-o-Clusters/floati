"""Read-only, point-in-time checkout evidence for cleanup callers."""

from __future__ import annotations

import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .codex_wait_contract import armed_workspace_claims
from .consumption import ConsumptionLedger
from .git_process import fixed_git_command, fixed_git_environment
from .reclaim import host_process_cwds
from .registry import Registry, read_registry_compatible
from .root import FloatiRoot
from .territory import TerritoryLedger
from .territory_records import TERRITORY_RELATIVE
from .wake_control import WakeController
from .wake_daemon import claimant_runtime_missing


_READ_ERRORS = (OSError, RuntimeError, ValueError, subprocess.SubprocessError)


def _read_git(path, *arguments, allowed=(0,)):
    env = fixed_git_environment('/usr/bin/git')
    env.update(GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='')
    result = subprocess.run(
        fixed_git_command('/usr/bin/git', path, arguments), env=env,
        capture_output=True, timeout=10, check=False,
    )
    if result.returncode not in allowed or result.stderr:
        raise OSError(f'Git {arguments[0]} did not return complete evidence (exit {result.returncode})')
    return result.stdout


def _git_registration(path):
    if _read_git(path, 'rev-parse', '--show-prefix') not in (b'', b'\n'):
        raise OSError('The path is inside a checkout and is not its root')
    listing = _read_git(path, 'worktree', 'list', '--porcelain', '-z')
    for index, row in enumerate(listing.split(b'\0\0')):
        fields = dict(field.partition(b' ')[::2] for field in row.split(b'\0') if field)
        if b'worktree' not in fields or Path(os.fsdecode(fields[b'worktree'])).resolve() != path:
            continue
        if b'bare' in fields or b'prunable' in fields:
            raise OSError('Git lists the path as bare or prunable')
        return {
            'registration': 'primary' if index == 0 else 'linked',
            'branch': os.fsdecode(fields[b'branch']).removeprefix('refs/heads/') if b'branch' in fields else None,
            'head': fields.get(b'HEAD', b'').decode('ascii'),
            'locked': b'locked' in fields,
        }
    raise OSError('Git does not list the path as a registered worktree')


def _git_changes(path):
    # Status may invoke a configured clean or process filter. A checkout
    # query must never run repository-configured commands.
    filters = _read_git(path, 'config', '--get-regexp', r'^filter\..*\.(clean|process)$', allowed=(0, 1))
    if filters:
        raise OSError('Git status may require an external filter')
    index = [entry for entry in _read_git(path, 'ls-files', '-v', '--stage', '-z').split(b'\0') if entry]
    submodules = any(entry[2:].startswith(b'160000 ') for entry in index)
    masked = any(entry[:1].islower() or entry.startswith(b'S ') for entry in index)
    status = _read_git(path, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                       '--ignored=matching', '--no-renames', '--ignore-submodules=dirty')
    rows = [entry[:2] for entry in status.split(b'\0') if entry]
    return {
        'tracked_changes': any(row not in (b'??', b'!!') for row in rows),
        'untracked_files': any(row in (b'??', b'!!') for row in rows),
        'submodules': submodules,
        'masked': masked,
        'dirty_count': len(rows),
    }


def _git_unpushed_count(path, head):
    if _read_git(path, 'rev-parse', '--is-shallow-repository') != b'false\n':
        raise OSError('Remote reachability cannot be read from shallow history')
    if len(head) not in (40, 64) or any(character not in '0123456789abcdef' for character in head):
        raise OSError('The registered worktree HEAD is unavailable')
    count = _read_git(path, 'rev-list', '--count', head, '--not', '--remotes').strip()
    if not count.isdigit():
        raise OSError('Git did not return an unpushed commit count')
    return int(count)


def _ordinary_ledger(root, relative):
    """An absent ledger is empty. Aliases and other file types are unreadable."""
    path = root.path
    for part in Path(relative).parts:
        path /= part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        expected = stat.S_ISREG if path == root.path / relative else stat.S_ISDIR
        if not expected(mode):
            raise OSError(f'Ledger coordinate {relative} is not an ordinary file path')


def _registry_leases(root, now):
    _ordinary_ledger(root, 'registry/entries.jsonl')
    rows, unread, _versions = read_registry_compatible(root)
    registrations = {row['node_id']: row['state'] for row in rows if row['kind'] == 'registry_entry'}
    leases = {row['node_id']: row for row in rows if row['kind'] == 'node_lease'}
    active = [
        row for node, row in leases.items() if registrations.get(node) == 'active'
        and Registry._node_lease_state_from_records(node, rows, now=now)['state'] == 'active'
    ]
    unidentified = [row for node, row in leases.items()
                    if node not in registrations and row['state'] == 'active']
    return registrations, active, unidentified, unread


def _live_work_leases(root):
    _ordinary_ledger(root, ConsumptionLedger.relative_path)
    return [item for item in ConsumptionLedger(root).project().values()
            if item['state'] == 'claimed' and item['workspace']]


def _territory_leases(root, now):
    _ordinary_ledger(root, TERRITORY_RELATIVE)
    return TerritoryLedger(root).show(now=now)['territories']


def _matching_workspace(candidate, checkout):
    workspace = Path(candidate).resolve()
    return workspace if workspace.is_relative_to(checkout) else None


def _covering_path(candidate, checkout):
    """A leased path inside the checkout, or one the checkout lies inside."""
    leased = Path(candidate).resolve()
    return leased if leased.is_relative_to(checkout) or checkout.is_relative_to(leased) else None


def _session_inactive_cause(root, node, session):
    if WakeController(root).status(node, session)['state'] == 'paused':
        return 'paused'
    if claimant_runtime_missing(root, node, session):
        return 'runtime_missing'
    return None


def checkout_liveness(root_path, path):
    """Inspect one explicit direct home and one absolute checkout path."""
    evidence = {'path': os.fspath(path), 'git': {}, 'reasons': []}
    reasons = evidence['reasons']

    def reason(code, **coordinates):
        reasons.append({'code': code, **coordinates})

    def unidentified(source, holder, workspace):
        reason('unidentified_holder', source=source, holder=holder,
               workspace=str(workspace), cause='holder_not_registered')

    def observe(source, read):
        try:
            return read()
        except _READ_ERRORS as failure:
            detail = getattr(failure, 'code', None) or str(failure)
            reason('input_unreadable', source=source, detail=detail)
            return None

    def resolve_checkout():
        checkout = Path(path)
        if not checkout.is_absolute():
            raise ValueError('The checkout path must be absolute')
        return checkout.resolve(strict=True)

    checkout = observe('path', resolve_checkout)
    if checkout is None:
        return dict(evidence, verdict='unknown')
    evidence['path'] = str(checkout)
    registration = observe('git_registration', lambda: _git_registration(checkout))
    if registration is not None:
        evidence['git'].update(registration)
        if registration['locked']:
            reason('worktree_locked')
        changes = observe('git_status', lambda: _git_changes(checkout))
        if changes is not None:
            for code in ('tracked_changes', 'untracked_files'):
                if changes[code]:
                    reason(code)
            if changes['submodules']:
                reason('input_unreadable', source='git_status', detail='Submodule contents were not inspected')
            if changes['masked']:
                reason('input_unreadable', source='git_status', detail='The Git index can hide changes to tracked paths')
            if not (changes['submodules'] or changes['masked']):
                evidence['git']['dirty_count'] = changes['dirty_count']
        unpushed = observe('git_remote_refs', lambda: _git_unpushed_count(checkout, registration['head']))
        if unpushed:
            reason('unpushed_commits', head=registration['head'])
        if unpushed is not None:
            evidence['git'].update(on_remote_ref=unpushed == 0, unpushed_count=unpushed)

    inactive_sessions = []
    root = observe('fleet_root', lambda: FloatiRoot.open_direct_home(root_path, create=False))
    if root is not None:
        now = datetime.now(timezone.utc)

        def read_sessions():
            for row, state in armed_workspace_claims(root, strict=True):
                workspace = observe('sessions', lambda: _matching_workspace(row['workspace'], checkout))
                if workspace is None:
                    continue
                holder = row['node_id']
                if state is None:
                    unidentified('sessions', holder, workspace)
                    continue
                cause = ('node_retired' if state == 'retired' else
                         _session_inactive_cause(root, holder, row['acting_session_id']))
                if cause:
                    inactive_sessions.append({'workspace': str(workspace), 'holder': holder, 'cause': cause})
                else:
                    reason('live_session', workspace=str(workspace), holder=holder)

        observe('sessions', read_sessions)
        nodes = observe('node_leases', lambda: _registry_leases(root, now))
        registrations = None
        if nodes is not None:
            registrations, rows, unknown_holders, unread = nodes
            if unread:
                reason('input_unreadable', source='node_leases', detail='Registry contains unrecognized records')
            for row in rows:
                workspace = observe('node_leases', lambda: _matching_workspace(row['workspace'], checkout))
                if workspace is not None:
                    reason('live_node_lease', workspace=str(workspace),
                           node=row['node_id'], lease_id=row['id'])
            for row in unknown_holders:
                workspace = observe('node_leases', lambda: _matching_workspace(row['workspace'], checkout))
                if workspace is not None:
                    unidentified('node_leases', row['node_id'], workspace)

        # NET-TERRITORY-1's path-bearing leases are an independent observation,
        # so a failed ledger cannot hide the other lease sources or turn their
        # absence into a safe verdict. A retired holder's lease is not live; an
        # unregistered holder's unexpired lease cannot be judged.
        for row in observe('territory_leases', lambda: _territory_leases(root, now)) or ():
            if row['state'] == 'live':
                holder_known = True
            elif row['state'] == 'holder_inactive' and registrations is not None:
                holder_known = row['node_id'] in registrations
                if holder_known:
                    continue
            else:
                continue
            for entry in row['paths']:
                workspace = observe('territory_leases', lambda: _covering_path(entry['path'], checkout))
                if workspace is None:
                    continue
                if holder_known:
                    reason('live_territory_lease', workspace=str(workspace),
                           node=row['node_id'], territory_id=row['id'])
                else:
                    unidentified('territory_leases', row['node_id'], workspace)

        for row in observe('work_leases', lambda: _live_work_leases(root)) or ():
            workspace = observe('work_leases', lambda: _matching_workspace(row['workspace'], checkout))
            if workspace is not None:
                if registrations is not None and row['holder'] not in registrations:
                    unidentified('work_leases', row['holder'], workspace)
                else:
                    reason('live_work_lease', workspace=str(workspace), work_id=row['id'], holder=row['holder'])

    process_errors = set()
    cwds = observe('process_cwds', lambda: host_process_cwds(on_unreadable=process_errors.add))
    for detail in sorted(process_errors):
        reason('input_unreadable', source='process_cwds', detail=detail)
    for cwd in sorted(set(cwds or ())):
        workspace = observe('process_cwds', lambda: _matching_workspace(cwd, checkout))
        if workspace is not None:
            reason('process_cwd', cwd=str(workspace))

    git = evidence['git']
    sessions_uncertain = any(row['code'] == 'live_session' or row.get('source') == 'sessions'
                             for row in reasons)
    if inactive_sessions and not sessions_uncertain and {'unpushed_count', 'dirty_count'} <= git.keys():
        branch = git['branch'] or 'detached HEAD'
        head, unpushed, dirty = git['head'], git['unpushed_count'], git['dirty_count']
        evidence['left_behind'] = {
            'branch': git['branch'], 'head': head, 'unpushed_count': unpushed, 'dirty_count': dirty,
            'sessions': inactive_sessions,
            'summary': f'left behind: {branch} @ {head}, {unpushed} unpushed, {dirty} dirty',
        }

    if any(row['code'] in ('input_unreadable', 'unidentified_holder') for row in reasons):
        verdict = 'unknown'
    else:
        verdict = 'in_use' if reasons else 'safe'
    return dict(evidence, verdict=verdict)
