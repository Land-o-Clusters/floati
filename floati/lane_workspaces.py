"""Create and retire only receipt-owned worktrees at explicitly declared coordinates."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess

from . import jsonl as lane_jsonl
from .bus_epoch import shared_epoch_operation
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .fleet_update import _explicit_executable
from .git_process import fixed_git_command, fixed_git_environment
from .ids import uuid7_hex
from .jsonl import _locked_path, append_record, read_records_snapshot
from .lane_workspace_inventory import inventory
from .registry import Registry, utc_now
from .root import FloatiRoot, validate_identifier

KIND = 'lane_workspace_record'
LOCK = Path('state/lane-workspaces.lock')
MAX_DECLARATION_BYTES = 1024 * 1024
MAX_RUNTIME_FILES = 10000
MARKER = 'floati-lane-owner.json'
FENCE_KEYS = ('floati.seatFenceRoot', 'floati.seatFenceNode')


@contextmanager
def lane_workspace_guard(root):
    """Retirement and lane mutations take this guard before registry locks."""
    declaration = root.resolve_relative('state/lanes-root.json')
    if not declaration.exists() and not declaration.is_symlink():
        yield
        return
    with _locked_path(root.resolve_relative(LOCK), exclusive=True, relative=LOCK):
        yield


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _json_file(path, *, optional=False):
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DECLARATION_BYTES:
            raise ValueError('not a bounded ordinary file')
        data = path.read_bytes()
        if len(data) > MAX_DECLARATION_BYTES:
            raise ValueError('declaration too large')
        return json.loads(data, object_pairs_hook=_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except FileNotFoundError:
        if optional:
            return None
        raise ProtocolRefusal('lane_declaration_missing', f'Lane declaration is missing: {path.name}', remedy='write the named explicit lane declaration before operating workspaces')
    except (OSError, ValueError, UnicodeError):
        raise ProtocolRefusal('lane_declaration_invalid', f'Lane declaration is not strict bounded JSON: {path.name}', remedy='repair the named declaration as a regular JSON file with unique keys')


def _canonical(value, label, *, existing=False):
    if not isinstance(value, str) or not value or '://' in value:
        raise ProtocolRefusal('lane_declaration_invalid', f'{label} must name an explicit canonical local path', remedy='write an absolute canonical filesystem path in the named declaration')
    path = Path(value)
    try:
        if not path.is_absolute() or path.resolve(strict=existing) != path:
            raise ValueError('not canonical')
        for part in (path, *path.parents):
            if part.is_symlink():
                raise ValueError('symlink coordinate')
        if existing and not path.is_dir():
            raise ValueError('not a directory')
    except (OSError, ValueError, RuntimeError):
        raise ProtocolRefusal('lane_declaration_invalid', f'{label} is not a canonical directory coordinate', remedy='declare an ordinary canonical directory without symlink components')
    return path


def _identity(value, label):
    try:
        return validate_identifier(value, label)
    except ProtocolRefusal as exc:
        raise ProtocolRefusal(exc.code, exc.detail,
                              remedy=f'pass {label} as the lowercase identifier pattern named in detail') from exc


def _row(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', value) is None:
        raise ProtocolRefusal('lane_row_invalid', 'A row must be one bounded alphanumeric, underscore, or hyphen identifier', remedy='pass --row as a 1 to 96 character identifier beginning with a letter or digit')
    return value


def _mentions(value, workspace):
    if isinstance(value, str):
        return str(workspace) in value
    if isinstance(value, dict):
        return any(_mentions(key, workspace) or _mentions(item, workspace) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_mentions(item, workspace) for item in value)
    return False


def _walk_error(error):
    raise error


class LaneWorkspaces:
    def __init__(self, root, launch_agents=None, lsof_executable=None):
        if not isinstance(root, FloatiRoot):
            raise TypeError('lane workspaces require one validated root')
        self.root = root
        self.launch_agents = Path.home() / 'Library/LaunchAgents' if launch_agents is None else Path(launch_agents)
        self.lsof_executable = lsof_executable

    def _declarations(self):
        declared = _json_file(self.root.resolve_relative('state/lanes-root.json'))
        if not isinstance(declared, dict) or set(declared) != {'schema_version', 'path'} or type(declared['schema_version']) is not int or declared['schema_version'] != 0:
            raise ProtocolRefusal('lane_declaration_invalid', 'lanes-root.json must contain schema_version 0 and path', remedy='write the exact declared lanes-root version-zero object')
        lanes = _canonical(declared['path'], 'lanes root')
        if lanes == self.root.path or self.root.path in lanes.parents:
            raise ProtocolRefusal('lane_declaration_invalid', 'The lanes root cannot be inside the bus root', remedy='declare a lanes root outside the selected bus root')
        document = _json_file(self.root.resolve_relative('state/lane-repositories.json'))
        if not isinstance(document, dict) or set(document) != {'schema_version', 'repositories'} or type(document['schema_version']) is not int or document['schema_version'] != 0 or not isinstance(document['repositories'], dict):
            raise ProtocolRefusal('lane_declaration_invalid', 'lane-repositories.json must contain the version-zero repository map', remedy='declare repository aliases with path and default_base fields')
        repositories = {}
        for name, row in document['repositories'].items():
            _identity(name, 'repository alias')
            if not isinstance(row, dict) or set(row) != {'path', 'default_base'} or not isinstance(row['default_base'], str) or not row['default_base'] or len(row['default_base']) > 1024 or row['default_base'].startswith('-') or any(c.isspace() for c in row['default_base']):
                raise ProtocolRefusal('lane_declaration_invalid', f'Repository {name} has invalid fields', remedy='declare each repository with a canonical checkout path and exact default_base ref')
            repositories[name] = {'path': _canonical(row['path'], f'repository {name}', existing=True),
                                  'default_base': row['default_base']}
        return lanes, repositories

    def _board(self):
        document = _json_file(self.root.resolve_relative('state/lane-board.json'), optional=True)
        if document is None:
            return None
        if not isinstance(document, dict) or set(document) != {'schema_version', 'rows'} or type(document['schema_version']) is not int or document['schema_version'] != 0 or not isinstance(document['rows'], dict):
            raise ProtocolRefusal('lane_declaration_invalid', 'lane-board.json must contain the version-zero rows map', remedy='declare each row as open, landed, or struck')
        for row, state in document['rows'].items():
            _row(row)
            if state not in ('open', 'landed', 'struck'):
                raise ProtocolRefusal('lane_declaration_invalid', f'Board row {row} has an unsupported state', remedy='set the row state to open, landed, or struck')
        return document['rows']

    def _git(self, repository, *arguments):
        try:
            result = subprocess.run(fixed_git_command('/usr/bin/git', repository, arguments),
                                    env=fixed_git_environment('/usr/bin/git'), capture_output=True,
                                    text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', f'Git could not inspect {repository}', remedy='restore the declared repository and fixed Git executable, then retry')
        if len(result.stdout) > 8 * 1024 * 1024 or len(result.stderr) > 1024 * 1024:
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', f'Git testimony exceeds its bound: {repository}', remedy='inspect the declared repository manually before retrying the bounded lane operation')
        return result

    def _required_git(self, repository, *arguments):
        result = self._git(repository, *arguments)
        if result.returncode:
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', f'Git inspection failed for {repository}: {result.stderr.strip()}', remedy='restore the named Git coordinate and retry the lane operation')
        return result.stdout.strip()

    def _common(self, repository):
        top = self._required_git(repository, 'rev-parse', '--show-toplevel')
        if Path(top) != repository:
            raise ProtocolRefusal('lane_workspace_identity_mismatch', f'The declared checkout is not its Git root: {repository}', remedy='restore the exact declared repository checkout; do not substitute a nested directory')
        return Path(self._required_git(repository, 'rev-parse', '--path-format=absolute', '--git-common-dir'))

    def _worktrees(self, repository):
        output = self._required_git(repository, 'worktree', 'list', '--porcelain', '-z')
        rows = []
        current = {}
        for token in output.split('\0'):
            if not token:
                if current:
                    rows.append(current)
                    current = {}
                continue
            key, _, value = token.partition(' ')
            current[key] = value
        if current:
            rows.append(current)
        if not rows or any('worktree' not in row for row in rows):
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', 'Git worktree testimony is malformed', remedy='repair the declared repository worktree registration before retrying')
        return rows

    def _records(self, node):
        return read_records_snapshot(self.root, Path('nodes') / node / 'lanes.jsonl', allowed_kinds={KIND})

    def _open_records(self, node):
        opens = {}
        closed = set()
        for record in self._records(node):
            if record['node_id'] != node or record['path'] != f"{node}/work/{record['row']}":
                raise IntegrityFailure('lane_workspace_history_invalid', f'Lane ledger coordinate differs for node {node}')
            if record['state'] == 'open':
                if record['id'] in opens or any(r['row'] == record['row'] and rid not in closed for rid, r in opens.items()):
                    raise IntegrityFailure('lane_workspace_history_invalid', f'Duplicate active lane opening for {node}')
                opens[record['id']] = record
            else:
                prior = opens.get(record['opened_record_id'])
                if prior is None or prior['id'] in closed or any(record[k] != prior[k] for k in ('node_id', 'row', 'path', 'repo', 'base_sha', 'branch', 'opened_at')):
                    raise IntegrityFailure('lane_workspace_history_invalid', f'Closing does not bind one opening for {node}')
                closed.add(prior['id'])
        return sorted((record for rid, record in opens.items() if rid not in closed), key=lambda r: r['row'])

    def _nodes(self):
        directory = self.root.resolve_relative('nodes')
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', 'Node lane history directory is not ordinary', remedy='restore the declared nodes directory without symlinks')
        names = []
        for count, path in enumerate(directory.iterdir()):
            if count >= 4096:
                raise ProtocolRefusal('lane_workspace_inspection_unavailable', 'Node inventory exceeds its bounded size', remedy='inspect the declared fleet node inventory before retrying lane operations')
            if path.is_symlink():
                raise ProtocolRefusal('lane_workspace_inspection_unavailable', 'A node history directory is a symlink', remedy='restore the named node history directory before reading lane ownership')
            if path.is_dir() and ((path / 'lanes.jsonl').exists() or (path / 'lanes.jsonl').is_symlink()):
                names.append(_identity(path.name, 'node'))
        return sorted(names)

    def _actor(self, actor):
        actor = _identity(actor, 'node')
        registry = Registry(self.root)
        registry.require_active(actor)
        registry.require_protocol_lease(actor, act='lane workspace operation')
        return actor

    def _append(self, record):
        append_record(self.root, Path('nodes') / record['node_id'] / 'lanes.jsonl', record, allowed_kinds={KIND})

    def _preflight_records(self, proposed):
        """Validate all known receipt constraints before the first external mutation."""
        by_node = {}
        for record in proposed:
            try:
                encoded = lane_jsonl._encode_record(record, self.root.tenant_id, frozenset({KIND}))
            except ProtocolRefusal as exc:
                raise ProtocolRefusal(exc.code, exc.detail,
                                      remedy='correct the named lane receipt inputs before retrying; no workspace was removed') from exc
            by_node.setdefault(record['node_id'], []).append((record, encoded))
        for node, batch in by_node.items():
            prior = self._records(node)
            if len(prior) + len(batch) > lane_jsonl.MAX_LEDGER_RECORDS:
                raise ProtocolRefusal('lane_workspace_ledger_record_limit', f'Lane history for {node} cannot fit the proposed receipts', remedy='preserve and archive lane history through governed maintenance before retrying')
            ids = [record['id'] for record, _ in batch]
            if len(set(ids)) != len(ids) or set(ids).intersection(r['id'] for r in prior):
                raise ProtocolRefusal('lane_workspace_record_id_conflict', f'Lane receipt IDs collide for {node}', remedy='retry with fresh generated lane receipt IDs before changing a workspace')
            path = self.root.resolve_relative(Path('nodes') / node / 'lanes.jsonl')
            size = path.stat().st_size if path.exists() else 0
            if size + sum(len(encoded) for _, encoded in batch) > lane_jsonl.MAX_LEDGER_BYTES:
                raise ProtocolRefusal('lane_workspace_ledger_bytes_limit', f'Lane history for {node} cannot fit the proposed receipt bytes', remedy='preserve and archive lane history through governed maintenance before retrying')

    @staticmethod
    def _closing_record(record, actor, why):
        now = utc_now()
        return dict(record, id='lane-workspace-' + uuid7_hex(), timestamp=now,
                    state='closed', closed_at=now, closed_by=actor,
                    opened_record_id=record['id'], why=why)

    @shared_epoch_operation
    def open(self, actor, row, repo, base=None):
        with lane_workspace_guard(self.root):
            actor = self._actor(actor)
            row = _row(row)
            repo = _identity(repo, 'repository alias')
            lanes, repositories = self._declarations()
            if repo not in repositories:
                raise ProtocolRefusal('lane_repo_undeclared', f'Repository alias {repo} is not declared', remedy='add the explicit repository alias to state/lane-repositories.json')
            repository = repositories[repo]['path']
            self._common(repository)
            workspace = lanes / actor / 'work' / row
            _canonical(str(workspace), 'lane workspace')
            if workspace.exists() or workspace.is_symlink() or any(r['row'] == row for r in self._open_records(actor)):
                raise ProtocolRefusal('lane_workspace_exists', f'Lane workspace already exists: {workspace}', remedy='choose a new row or close the recorded lane; never adopt an existing directory')
            branch = f'codex/lane/{actor}/{row}'
            branch_probe = self._git(repository, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch)
            if branch_probe.returncode == 0:
                raise ProtocolRefusal('lane_workspace_exists', f'Lane branch already exists: {branch}', remedy='archive or rename the retained lane branch before reopening this row')
            if branch_probe.returncode != 1:
                raise ProtocolRefusal('lane_workspace_inspection_unavailable', 'The lane branch could not be inspected', remedy='repair the declared repository refs before opening this row')
            selected = repositories[repo]['default_base'] if base is None else base
            if not isinstance(selected, str) or not selected or selected.startswith('-') or len(selected) > 1024 or any(c.isspace() for c in selected):
                raise ProtocolRefusal('lane_base_unreachable', 'Lane base must be one exact local ref', remedy='pass --base naming an available commit or the declared remote main ref')
            shallow = self._required_git(repository, 'rev-parse', '--is-shallow-repository')
            resolved = self._git(repository, 'rev-parse', '--verify', selected + '^{commit}')
            if shallow != 'false' or resolved.returncode or len(resolved.stdout.strip()) != 40:
                raise ProtocolRefusal('lane_base_unreachable', f'Lane base {selected} has unavailable complete-history testimony', remedy='fetch the named base and complete repository history explicitly, then retry')
            base_sha = resolved.stdout.strip()
            fence = False
            for key in FENCE_KEYS:
                probe = self._git(repository, 'config', '--get', key)
                if probe.returncode not in (0, 1):
                    raise ProtocolRefusal('lane_workspace_inspection_unavailable', 'Seat fence configuration is unreadable', remedy='repair the declared repository configuration before opening a lane')
                fence = fence or probe.returncode == 0
            enabled = self._git(repository, 'config', '--bool', '--get', 'extensions.worktreeConfig')
            if fence and (enabled.returncode != 0 or enabled.stdout.strip() != 'true'):
                raise ProtocolRefusal('lane_workspace_fence_isolation_unavailable', 'Seat fences need existing worktree-specific Git configuration', remedy='enable extensions.worktreeConfig in the declared repository explicitly, then retry')
            now = utc_now()
            record = {'schema_version': 0, 'kind': KIND, 'id': 'lane-workspace-' + uuid7_hex(),
                      'tenant_id': self.root.tenant_id, 'timestamp': now, 'node_id': actor, 'row': row,
                      'path': f'{actor}/work/{row}', 'repo': repo, 'base_sha': base_sha, 'branch': branch,
                      'state': 'open', 'opened_at': now, 'closed_at': None, 'closed_by': None,
                      'opened_record_id': None, 'why': None}
            self._preflight_records([record])
            try:
                result = self._git(repository, 'worktree', 'add', '-b', branch, '--', str(workspace), base_sha)
                if result.returncode:
                    raise RuntimeError(result.stderr.strip())
                if fence:
                    for key in FENCE_KEYS:
                        result = self._git(workspace, 'config', '--worktree', key, '')
                        if result.returncode:
                            raise RuntimeError('worktree seat fence override failed')
                admin = _canonical(self._required_git(workspace, 'rev-parse', '--absolute-git-dir'),
                                   'lane Git administration', existing=True)
                if admin == self._common(repository) or admin.is_symlink():
                    raise RuntimeError('worktree administrative identity is not private')
                marker = admin / MARKER
                with marker.open('xb') as stream:
                    stream.write(self._marker_bytes(record['id']))
                    stream.flush()
                    os.fsync(stream.fileno())
                descriptor = os.open(admin, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._append(record)
            except Exception as exc:
                raise DurabilityFailure('lane_workspace_open_incomplete',
                                        f'Opening {record["path"]} may have created branch {branch}; preserve its bytes and inspect opening {record["id"]}: {exc}') from exc
            return {'workspace': str(workspace), 'record': record}

    @staticmethod
    def _marker_bytes(opened_record_id):
        return (json.dumps({'schema_version': 0, 'opened_record_id': opened_record_id}, sort_keys=True, separators=(',', ':')) + '\n').encode()

    def _runtime_references(self, workspace):
        references = []
        directory = self.launch_agents
        if not directory.is_absolute():
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', 'LaunchAgents observer path is not absolute', remedy='supply an absolute LaunchAgents directory for runtime inspection')
        try:
            if directory.is_symlink():
                raise ValueError('LaunchAgents symlink')
            if directory.exists():
                with os.scandir(directory) as entries:
                    files = [Path(entry.path) for entry in entries if entry.name.endswith('.plist')]
                if len(files) > MAX_RUNTIME_FILES:
                    raise ValueError('LaunchAgents inventory bound')
                for path in files:
                    metadata = path.lstat()
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DECLARATION_BYTES:
                        raise ValueError('unreadable plist')
                    value = plistlib.loads(path.read_bytes())
                    if _mentions(value, workspace):
                        references.append(str(path))
            bindings = self.root.resolve_relative('state/wake-daemon/adapters')
            if bindings.is_symlink():
                raise ValueError('binding directory symlink')
            count = 0
            if bindings.exists():
                for parent, dirs, files in os.walk(bindings, followlinks=False, onerror=_walk_error):
                    for name in dirs:
                        if (Path(parent) / name).is_symlink():
                            raise ValueError('binding subtree symlink')
                    for name in files:
                        count += 1
                        path = Path(parent) / name
                        metadata = path.lstat()
                        if count > MAX_RUNTIME_FILES or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DECLARATION_BYTES:
                            raise ValueError('unreadable binding')
                        payload = path.read_bytes()
                        if path.suffix == '.json':
                            value = json.loads(payload, object_pairs_hook=_object)
                            mentioned = _mentions(value, workspace)
                        else:
                            mentioned = str(workspace).encode() in payload
                        if mentioned:
                            references.append(str(path))
        except (OSError, ValueError, UnicodeError, plistlib.InvalidFileException):
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', f'Runtime reference inspection is unavailable for {workspace}', remedy='restore readable ordinary LaunchAgent plists and daemon bindings before removing this lane')
        if references:
            return references
        supplied = self.lsof_executable
        if supplied is None:
            supplied = next((Path(name) for name in ('/usr/sbin/lsof', '/usr/bin/lsof') if Path(name).is_file()), None)
        try:
            executable = _explicit_executable(supplied, 'lane_workspace_inspection_unavailable')
            result = subprocess.run([executable, '-nP', '-F', 'pn', '+D', str(workspace)],
                                    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                                    capture_output=True, text=True, timeout=15, check=False)
        except (ProtocolRefusal, OSError, subprocess.SubprocessError):
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', f'Open-file observation is unavailable for {workspace}', remedy='install or explicitly supply a readable canonical lsof executable before removing this lane')
        # lsof may return 1 with real process records (including on macOS).
        # Positive testimony still blocks removal when the scan was incomplete.
        if re.search(r'^p[0-9]+$', result.stdout, re.MULTILINE):
            return ['lsof: ' + result.stdout.strip()[:8192]]
        if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
            diagnostic = json.dumps(result.stderr.strip()[:8192], ensure_ascii=True)
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', f'lsof could not establish no runtime references for {workspace}; stderr: {diagnostic}', remedy='resolve the lsof diagnostic and retry; force does not bypass unavailable runtime inspection')
        return []

    def _preflight(self, record, lanes, repositories, force=False):
        workspace = lanes / record['path']
        _canonical(str(workspace), 'recorded lane workspace', existing=True)
        if record['repo'] not in repositories:
            raise ProtocolRefusal('lane_repo_undeclared', f'Recorded repository {record["repo"]} is no longer declared', remedy='restore the original explicit repository declaration before closing its lane')
        repository = repositories[record['repo']]['path']
        common = self._common(repository)
        if self._common(workspace) != common:
            raise ProtocolRefusal('lane_workspace_identity_mismatch', f'Lane {workspace} belongs to a different repository', remedy='restore the original recorded worktree; never replace or adopt foreign bytes')
        rows = [row for row in self._worktrees(repository) if row['worktree'] == str(workspace)]
        if len(rows) != 1 or rows[0].get('branch') != 'refs/heads/' + record['branch']:
            raise ProtocolRefusal('lane_workspace_identity_mismatch', f'Lane registration or branch differs: {workspace}', remedy='restore the recorded worktree and branch before closing it')
        admin = _canonical(self._required_git(workspace, 'rev-parse', '--absolute-git-dir'),
                                   'lane Git administration', existing=True)
        marker = admin / MARKER
        try:
            metadata = marker.lstat()
            expected_marker = self._marker_bytes(record['id'])
            if admin == common or admin.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(expected_marker) or marker.read_bytes() != expected_marker:
                raise ValueError('ownership marker mismatch')
        except (OSError, ValueError):
            raise ProtocolRefusal('lane_workspace_identity_mismatch', f'Lane ownership marker differs: {workspace}', remedy='preserve the workspace and restore its original opening marker; do not adopt a replacement')
        dirty = self._required_git(workspace, 'status', '--porcelain=v1', '--untracked-files=all', '--ignored=matching')
        if dirty and not force:
            raise ProtocolRefusal('lane_workspace_dirty', f'Lane has tracked or untracked changes: {workspace}', remedy='commit or preserve the lane changes, or pass --force --why with an explicit discard reason')
        shallow = self._required_git(workspace, 'rev-parse', '--is-shallow-repository')
        if shallow != 'false':
            raise ProtocolRefusal('lane_workspace_inspection_unavailable', f'Remote containment is unavailable for shallow lane {workspace}', remedy='complete repository history explicitly before closing the lane')
        contains = self._required_git(workspace, 'branch', '-r', '--contains', 'HEAD')
        if not contains and not force:
            raise ProtocolRefusal('lane_workspace_unpushed', f'Lane HEAD is on no remote ref: {workspace}', remedy='push the lane commit to a remote ref, or pass --force --why with an explicit discard reason')
        references = self._runtime_references(workspace)
        if references:
            raise ProtocolRefusal('lane_workspace_in_use', f'Lane {workspace} is referenced by: ' + ', '.join(references), remedy='stop or relocate the named runtime dependencies before closing this workspace')
        return workspace, repository

    def _remove(self, record, coordinates, actor, why, *, closed=None):
        workspace, repository = coordinates
        closed = self._closing_record(record, actor, why) if closed is None else closed
        self._preflight_records([closed])
        try:
            arguments = ['worktree', 'remove'] + (['--force'] if why is not None else [])
            result = self._git(repository, *arguments, '--', str(workspace))
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            self._append(closed)
            return {'removed': str(workspace), 'record': closed}
        except Exception as exc:
            raise DurabilityFailure('lane_workspace_close_incomplete',
                                    f'Removal or receipt is incomplete for {record["path"]}; inspect opening {record["id"]} and retain ledger history: {exc}') from exc

    @shared_epoch_operation
    def close(self, actor, row, force=False, why=None):
        with lane_workspace_guard(self.root):
            actor = self._actor(actor)
            row = _row(row)
            if type(force) is not bool or (force and (not isinstance(why, str) or not why.strip() or why != why.strip() or len(why) > 1024 or not why.isprintable())) or (not force and why is not None):
                raise ProtocolRefusal('lane_workspace_force_reason_required', 'Force requires one explicit bounded --why; why requires force', remedy='pass --force --why with a nonempty single-line discard explanation, or omit both')
            lanes, repositories = self._declarations()
            rows = [record for record in self._open_records(actor) if record['row'] == row]
            if len(rows) != 1:
                raise ProtocolRefusal('lane_workspace_unrecorded', f'No open owned lane exists for {actor}/{row}', remedy='select a row created by lane open; unrecorded directories are never adopted')
            coordinates = self._preflight(rows[0], lanes, repositories, force)
            return self._remove(rows[0], coordinates, actor, why)

    def _inventory(self, lanes, repositories, records):
        worktrees = []
        for repository in repositories.values():
            self._common(repository['path'])
            # Git lists the primary checkout first, even when queried from a
            # linked checkout. Only linked worktrees are lane candidates.
            worktrees.extend(Path(row['worktree']) for row in self._worktrees(repository['path'])[1:])
        structural = set()
        for node in self._nodes():
            for record in self._records(node):
                path = lanes / record['path']
                structural.update(parent for parent in path.parents if parent != lanes and lanes in parent.parents)
        return inventory(lanes, [lanes / record['path'] for record in records], worktrees, structural)

    @shared_epoch_operation
    def sweep(self, apply=False):
        with lane_workspace_guard(self.root):
            lanes, repositories = self._declarations()
            board = self._board()
            registry = Registry(self.root)
            latest = {}
            for row in registry.known_records():
                if row['kind'] == 'registry_entry':
                    latest[row['node_id']] = row
            records = [r for node in self._nodes() for r in self._open_records(node)]
            unmanaged = self._inventory(lanes, repositories, records)
            eligible = []
            for record in records:
                node = latest.get(record['node_id'])
                if node is None:
                    raise ProtocolRefusal('lane_workspace_node_unknown', f'Lane history names an undeclared node {record["node_id"]}', remedy='restore the node registry history before sweeping its lane')
                if node['state'] == 'retired' or (board is not None and board.get(record['row']) in ('landed', 'struck')):
                    eligible.append(record)
            closed = []
            if apply:
                for node in {r['node_id'] for r in eligible}:
                    if latest[node]['state'] != 'retired':
                        registry.require_protocol_lease(node, act='lane sweep')
                plans = [(record, self._preflight(record, lanes, repositories), self._closing_record(record, 'operation:sweep', None)) for record in eligible]
                self._preflight_records([closed_record for _, _, closed_record in plans])
                for record, coordinates, closed_record in plans:
                    try:
                        closed.append(self._remove(record, coordinates, 'operation:sweep', None, closed=closed_record))
                    except Exception as exc:
                        raise DurabilityFailure('lane_workspace_sweep_incomplete',
                                                f'Sweep completed {[r["record"]["opened_record_id"] for r in closed]}; inspect opening {record["id"]}: {exc}') from exc
            return {'eligible': eligible, 'closed': closed, 'unmanaged': unmanaged,
                    'status': 'degraded' if unmanaged or board is None else 'ok'}

    @shared_epoch_operation
    def doctor(self):
        lanes, repositories = self._declarations()
        nodes = sorted(set(self._nodes()) | {record['node_id'] for record in Registry(self.root).known_records() if record['kind'] == 'registry_entry'})
        records = [r for node in nodes for r in self._open_records(node)]
        now = datetime.now(timezone.utc)
        rows = []
        for node in nodes:
            selected = [r for r in records if r['node_id'] == node]
            ages = [(now - datetime.fromisoformat(r['opened_at'].replace('Z', '+00:00'))).total_seconds() for r in selected]
            rows.append({'node_id': node, 'open_lanes': len(selected),
                         'oldest_open_age_seconds': max(0.0, max(ages)) if ages else None})
        unmanaged = self._inventory(lanes, repositories, records)
        local = [row for row in unmanaged if Path(row['path']) == lanes or lanes in Path(row['path']).parents]
        total = None if any(row['bytes'] is None for row in local) else sum(row['bytes'] for row in local)
        return {'nodes': rows, 'unmanaged_bytes': total}


def close_node_lanes(root, node, *, lock_already_held=False):
    """Caller validates node/lease before this helper; never reacquire the registry."""
    node = _identity(node, 'node')
    service = LaneWorkspaces(root)
    relative = Path('nodes') / node / 'lanes.jsonl'
    if not root.resolve_relative(relative).exists() and not root.resolve_relative(relative).is_symlink():
        return []
    with (nullcontext() if lock_already_held else lane_workspace_guard(root)):
        records = service._open_records(node)
        if not records:
            return []
        lanes, repositories = service._declarations()
        plans = [(record, service._preflight(record, lanes, repositories), service._closing_record(record, 'operation:retire', None)) for record in records]
        service._preflight_records([closed_record for _, _, closed_record in plans])
        closed = []
        for record, coordinates, closed_record in plans:
            try:
                closed.append(service._remove(record, coordinates, 'operation:retire', None, closed=closed_record))
            except Exception as exc:
                raise DurabilityFailure('lane_workspace_retire_incomplete',
                                        f'Retirement closed {[r["record"]["opened_record_id"] for r in closed]}; inspect opening {record["id"]}: {exc}') from exc
        return closed
