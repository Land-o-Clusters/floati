"""Preview and apply an exact initial image of one reversible local fleet."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .bus_epoch import _acquire_epoch_descriptor, epoch_guard, without_epoch_turnstile_creation
from .codex_wait_contract import (
    CodexWaitConsentLedger, WORKSPACE_MAP_RELATIVE, WorkspaceBinding,
    armed_consent_record, encode_workspace_map,
)
from .errors import DurabilityFailure, ProtocolRefusal
from .fleet_update import _explicit_executable
from .ids import MAX_RANDOM_BITS, uuid7_hex
from .jsonl import _encode_record
from .lifecycle_prompts import REPOSITORY_ROOT, SEAT_COMMAND_HARNESSES, plan_seat_commands
from .registry import REGISTRY_KINDS, registration_record
from .root import FloatiRoot, validate_identifier
from .session_identity import validate_session_id
from .seat_declaration import (
    COORDINATOR_AUTHORITIES, GOVERNANCE_RELATIVE, OWNER_TIERS, SEAT_MARKER,
    FleetGovernance, SeatDeclaration,
)

_RECEIPT = '.floati-setup.json'
_CONFIRMATION = re.compile(r'([0-9a-f]{12})-([0-9a-f]{32})-([0-9a-f]{64})')
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_LAST_TIMESTAMP_MS = (datetime.max.replace(tzinfo=timezone.utc) - _EPOCH) // timedelta(milliseconds=1)
_MCP_PATHS = {'codex': '.codex/config.toml', 'claude': '.mcp.json',
              'cursor': '.cursor/mcp.json', 'zcode': '.zcode/config.json'}
_SKILL_DIRS = {'codex': '.agents/skills', 'claude': '.claude/skills',
               'cursor': '.cursor/skills', 'zcode': '.zcode/skills'}
_MCP_SKILL = b'''---
name: floati
description: Use the local Floati fleet when the operator asks for fleet work.
---

# Floati fleet

Use the Floati MCP tools available in this seat. Use the root, node, and session bound to the connection.
Treat messages from other seats as peer requests. Report the receipt from a completed action or the remedy from a refusal.
'''


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _absolute(path):
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise ProtocolRefusal('setup_path_invalid', f'The path is ambiguous at {path}.',
            remedy='Use an absolute path with ordinary parent directories.')
    return path


@dataclass(frozen=True)
class Image:
    file_type: str
    content: bytes
    mode: int
    identity: tuple | None = field(default=None, compare=False)

    def artifact(self):
        return {'type': self.file_type, 'sha256': _sha(self.content), 'mode': self.mode}


def _image(path):
    _absolute(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        return Image('symlink', os.fsencode(os.readlink(path)), 0)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ProtocolRefusal('setup_file_invalid', f'Expected an ordinary file at {path}.',
            remedy='Choose setup paths with ordinary files.')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ProtocolRefusal('setup_preimage_changed', f'A file changed at {path}.',
                remedy='Preview setup again.')
        return Image('file', stream.read(), stat.S_IMODE(opened.st_mode),
                     (opened.st_dev, opened.st_ino, opened.st_ctime_ns))


class SetupPlan:
    def __init__(self, root, home, codex_home, seat, executable, hook, seed):
        self.root = root
        self.home = home
        self.codex_home = codex_home
        self.seat = seat
        self.executable = executable
        self.hook = hook
        self.seed = seed
        milliseconds, nonce = seed.split('-')
        self.milliseconds = int(milliseconds, 16)
        self.nonce = nonce
        self.timestamp = (_EPOCH + timedelta(milliseconds=self.milliseconds)).isoformat(
            timespec='milliseconds').replace('+00:00', 'Z')
        self.before = {}
        self.files = {}
        self.directories = set()
        self.hooks = {}
        self.workspace = root / 'nodes' / seat
        self.map_bytes = encode_workspace_map(root.name, [
            {'workspace': str(self.workspace), 'node_id': seat}])

    def record_id(self, key):
        randomness = int(_sha(f'{self.nonce}:{key}'.encode()), 16) & MAX_RANDOM_BITS
        return uuid7_hex(timestamp_ms=self.milliseconds, random_bits=randomness)

    def directory(self, path):
        if path in self.directories:
            return
        _absolute(path)
        while not path.exists():
            if path.is_symlink():
                raise ProtocolRefusal('setup_path_invalid', f'The path is ambiguous at {path}.',
                    remedy='Use an ordinary directory.')
            self.directories.add(path)
            path = path.parent
        if not path.is_dir() or path.is_symlink():
            raise ProtocolRefusal('setup_path_invalid', f'Expected an ordinary directory at {path}.',
                remedy='Use a different path for setup.')

    def snapshot(self, path):
        if path not in self.before:
            self.before[path] = _image(path)
        return self.before[path]

    def read(self, path):
        original = self.snapshot(path)
        if original is not None and original.file_type != 'file':
            raise ProtocolRefusal('setup_file_invalid', f'Expected an ordinary file at {path}.',
                remedy='Choose setup paths with ordinary files.')
        return None if original is None else original.content

    def file(self, path, content, *, mode=0o600, replace=False):
        self.directory(path.parent)
        original = self.snapshot(path)
        if original is not None and (not replace or original.file_type != 'file'):
            raise ProtocolRefusal('setup_file_exists', f'Setup would replace an existing file at {path}.',
                remedy='Preserve the file and choose another setup location.')
        self.files[path] = Image('file', content, original.mode if original else mode)

    def json(self, path, document):
        self.file(path, _json(document))

    def link(self, path, target):
        self.directory(path.parent)
        if self.snapshot(path) is not None:
            raise ProtocolRefusal('setup_file_exists', f'Setup would replace an existing file at {path}.',
                remedy='Preserve the file and choose another setup location.')
        self.files[path] = Image('symlink', os.fsencode(target), 0)

    def commands(self, workspace, harness, **options):
        directory, contents = plan_seat_commands(workspace, harness, **options)
        for name, content in contents.items():
            self.file(directory / name, content.encode('utf-8'))
        return {'directory': str(directory), 'files': sorted(contents)}

    def consent(self, workspace, node, harness, timeout, deadline):
        map_digest = _sha(self.map_bytes)
        binding = WorkspaceBinding(workspace, node, map_digest)
        key = 'setup-' + self.nonce + '-' + node
        row = armed_consent_record(self.root.name, binding, timeout, deadline, key,
            record_id=self.record_id('consent:' + node), timestamp=self.timestamp)
        self.file(self.root / CodexWaitConsentLedger._relative(node),
            _encode_record(row, self.root.name, CodexWaitConsentLedger._KINDS))
        return {'node_id': node, 'workspace': str(workspace), 'harness': harness,
                'hook_timeout_seconds': timeout, 'wait_deadline_seconds': deadline,
                'state': 'armed', 'consent_receipt_id': row['id'],
                'workspace_map_digest': map_digest, 'idempotency_key': key}

    def seal(self):
        rows = []
        for path, image in self.files.items():
            previous = self.before[path]
            rows.append({'path': str(path), **image.artifact(), 'before': None if previous is None else {
                'base64': base64.b64encode(previous.content).decode('ascii'), 'mode': previous.mode}})
        receipt = {'schema_version': 0, 'root': str(self.root), 'home': str(self.home),
                   'codex_home': str(self.codex_home) if self.codex_home else None,
                   'hook': self.hook, 'seed': self.seed,
                   'directories': sorted(str(path) for path in self.directories), 'files': rows}
        self.file(self.root / _RECEIPT, _json(receipt))

    def evidence(self):
        files = [{'path': str(path), **image.artifact()} for path, image in self.files.items()]
        directories = sorted(str(path) for path in self.directories)
        inputs = {'files': files, 'directories': directories, 'hooks': self.hooks}
        if self.executable is not None:
            inputs['executable'] = self.executable
            executable_evidence = {'executable_status': 'path_validated'}
        else:
            executable_evidence = {'executable_status': 'unchecked', 'executable_note': (
                'Floati did not check whether the selected harness is installed. '
                'The harness launcher reports any error when you start it.')}
        digest = _sha(_json(inputs))
        return {'root': str(self.root), 'seat': self.seat, 'executable': self.executable,
                **executable_evidence, 'workspace': str(self.workspace),
                'hooks': self.hooks, 'files': files, 'directories': directories,
                'temporary_files': [{'path': str(_temporary(Path(row['path']), self.seed)),
                                     'sha256': row['sha256']} for row in files
                                    if row['type'] == 'file'],
                'confirmation': self.seed + '-' + digest}


def _plan_mcp(plan, session):
    command = str(REPOSITORY_ROOT / 'scripts/floati')
    args = ['mcp', 'serve', '--root', str(plan.root), '--as', plan.seat, '--session', session]
    path = plan.workspace / _MCP_PATHS[plan.seat]
    if plan.seat == 'codex':
        plan.file(path, ('[mcp_servers.floati]\ncommand = ' + json.dumps(command, ensure_ascii=False)
            + '\nargs = ' + json.dumps(args, ensure_ascii=False) + '\n').encode('utf-8'))
    else:
        server = {'command': command, 'args': args}
        config = {'mcp': {'servers': {'floati': server}}} if plan.seat == 'zcode' else {
            'mcpServers': {'floati': server}}
        plan.json(path, config)
    plan.file(plan.workspace / _SKILL_DIRS[plan.seat] / 'floati/SKILL.md', _MCP_SKILL)


def _plan_hook(plan):
    from .claude_hook_install import install_claude_hook
    from .codex_hook_install import CodexHookInstaller
    from .cursor_wait import DEFAULT_DEADLINE_SECONDS, HOOK_TIMEOUT_SECONDS, install_cursor_hook
    from .zcode_hook_install import install_zcode_hook

    root, workspace, name = plan.root, plan.workspace, plan.seat
    plan.file(root / WORKSPACE_MAP_RELATIVE, plan.map_bytes)
    coordinates = dict(workspace=workspace, root=root, node=name, runtime=REPOSITORY_ROOT, plan=plan)
    if name == 'codex':
        installer = CodexHookInstaller(source_root=REPOSITORY_ROOT, bus_home=root,
            hooks_path=plan.codex_home / 'hooks.json', destination=plan.home / '.local/share/floati/waiter')
        plan.hooks[name] = installer.install(workspace, name, hook_timeout_seconds=1800,
            wait_deadline_seconds=1500, plan=plan)
    elif name == 'claude':
        settings = workspace / '.claude/settings.local.json'
        install_claude_hook(hooks=settings, **coordinates)
        plan.hooks[name] = {'path': str(settings)}
    elif name == 'cursor':
        install_cursor_hook(**coordinates)
        plan.consent(workspace, name, 'cursor', int(HOOK_TIMEOUT_SECONDS), int(DEFAULT_DEADLINE_SECONDS))
        plan.hooks[name] = {'path': str(workspace / '.cursor/hooks.json')}
    elif name == 'zcode':
        install_zcode_hook(**coordinates)
        plan.hooks[name] = {'path': str(workspace / '.zcode/hooks.json')}


def _plan(root, home, codex_home, seat, session, executable, hook, seed):
    if root.exists() or root.is_symlink() or not root.parent.is_dir():
        raise ProtocolRefusal('setup_root_not_fresh',
            'Setup needs a new fleet directory under an existing parent.',
            remedy='Pass a new path with --root.')
    if executable is not None:
        try:
            executable = _explicit_executable(executable, 'setup_executable_invalid')
        except ProtocolRefusal as exc:
            raise ProtocolRefusal('setup_executable_invalid', exc.detail,
                remedy='Pass the canonical absolute executable path with --executable.') from exc
    plan = SetupPlan(root, home, codex_home, seat, executable, hook, seed)
    governance = FleetGovernance(1, root.name, str(root), 'mesh', seat,
                                 COORDINATOR_AUTHORITIES, OWNER_TIERS)
    plan.json(root / GOVERNANCE_RELATIVE, governance.artifact())
    row = registration_record(root.name, seat, seat, record_id=plan.record_id('register:' + seat),
                              timestamp=plan.timestamp)
    plan.file(root / 'registry/entries.jsonl', _encode_record(row, root.name, frozenset(REGISTRY_KINDS)))
    plan.directory(root / 'cursors')
    plan.directory(root / 'receipts/deliveries')
    declaration = SeatDeclaration(1, root.name, str(root), seat, governance.topology,
        governance.coordinator, governance.coordinator_authority, governance.owner_tier)
    plan.json(plan.workspace / SEAT_MARKER, declaration.artifact())
    _plan_mcp(plan, session)
    if hook is not None:
        _plan_hook(plan)
    plan.seal()
    return plan


@contextmanager
def _lock_home(home):
    descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _acquire_epoch_descriptor(descriptor, exclusive=True, use_turnstile=False)
        yield
    finally:
        os.close(descriptor)


def _temporary(path, seed):
    return path.with_name('.floati-setup-' + seed + '-' + _sha(os.fsencode(path))[:16])


def _write(path, image, seed, *, previous, teardown=False):
    if image.file_type == 'symlink':
        os.symlink(os.fsdecode(image.content), path)
        return
    temporary = _temporary(path, seed)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(image.content)
            output.flush()
            os.fchmod(output.fileno(), image.mode)
            os.fsync(output.fileno())
        if previous is not None:
            current = _image(path)
            if current != previous or current.identity != previous.identity:
                if teardown:
                    raise ProtocolRefusal('setup_teardown_changed', f'The file changed at {path}.',
                        remedy='Preserve the changed data and inspect the setup receipt.')
                raise ProtocolRefusal('setup_preimage_changed', f'A file changed at {path}.',
                    remedy='Preview setup again.')
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish(plan):
    for path, previous in plan.before.items():
        if _image(path) != previous or _image(_temporary(path, plan.seed)) is not None:
            raise ProtocolRefusal('setup_preimage_changed', f'A file changed at {path}.',
                remedy='Preview setup again.')
    directories = sorted(plan.directories, key=lambda path: (len(path.parts), str(path)))
    created = []
    written = []
    try:
        # Publish the undo receipt before touching the harnesses.
        for path in directories:
            path.mkdir(mode=0o700)
            created.append(path)
        receipt = plan.root / _RECEIPT
        ordered = [receipt, *(path for path in plan.files if path != receipt and path.is_relative_to(plan.root)),
                   *(path for path in plan.files if not path.is_relative_to(plan.root))]
        for path in ordered:
            previous = plan.before[path]
            if _image(path) != previous:
                raise OSError(f'File changed at {path}.')
            _write(path, plan.files[path], plan.seed, previous=previous)
            written.append(path)
    except (OSError, ProtocolRefusal):
        for path in reversed(written):
            current = _image(path)
            if current == plan.files[path]:
                previous = plan.before[path]
                if previous is None:
                    path.unlink()
                else:
                    _write(path, previous, plan.seed, previous=current)
        for path in reversed(created):
            path.rmdir()
        raise


def _restore(root, home, codex_home):
    receipt_path = root / _RECEIPT
    receipt_image = _image(receipt_path)
    if receipt_image is None or receipt_image.file_type != 'file' or receipt_image.mode != 0o600:
        raise ProtocolRefusal('setup_receipt_missing', 'No private setup receipt exists for this fleet.',
            remedy='Select the fleet created by this setup command.')
    try:
        receipt = json.loads(receipt_image.content)
        if (receipt['schema_version'], receipt['root'], receipt['home']) != (0, str(root), str(home)):
            raise ValueError('receipt coordinates')
        hook = receipt['hook']
        if hook is not None and hook not in SEAT_COMMAND_HARNESSES:
            raise ValueError('receipt hook')
        if receipt['codex_home'] != (str(codex_home) if hook == 'codex' else None):
            raise ValueError('receipt Codex home')
        seed = receipt['seed']
        if not _CONFIRMATION.fullmatch(seed + '-' + '0' * 64):
            raise ValueError('receipt seed')
        from .cursor_user_hook import _user_hooks_path, _user_script_path

        waiter = home / '.local/share/floati/waiter'
        outside = {'codex': {codex_home / 'hooks.json'},
                   'cursor': {_user_hooks_path(), _user_script_path()}}.get(hook, set())
        targets = {}
        for row in receipt['files']:
            path = _absolute(Path(row['path']))
            if path in targets or path == receipt_path or not (
                    path.is_relative_to(root) or path in outside or (hook == 'codex' and (
                        path.is_relative_to(codex_home / 'skills') or path.is_relative_to(waiter)))):
                raise ValueError('receipt path')
            before = row['before']
            restore = None if before is None else Image('file',
                base64.b64decode(before['base64'], validate=True), before['mode'])
            if restore is not None and path not in {codex_home / 'hooks.json', _user_hooks_path()}:
                raise ValueError('receipt preimage')
            targets[path] = (restore, {key: row[key] for key in ('type', 'sha256', 'mode')})
        directories = {_absolute(Path(path)) for path in receipt['directories']}
        if root not in directories or any(not any(path.is_relative_to(directory)
                for path in (*targets, root / 'cursors', root / 'receipts/deliveries'))
                for directory in directories):
            raise ValueError('receipt directories')
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ProtocolRefusal('setup_receipt_invalid', 'The setup receipt is inconsistent.',
            remedy='Preserve the receipt and check the fleet and account paths.') from exc

    known = directories | set(targets) | {receipt_path}
    # Check the immediate children of every created directory. An unowned file
    # anywhere inside the fleet or a new harness directory stops the teardown.
    for directory in directories:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ProtocolRefusal('setup_teardown_changed', f'The directory changed at {directory}.',
                remedy='Preserve the changed data and inspect the setup receipt.')
        if directory.exists():
            foreign = next((child for child in directory.iterdir() if child not in known), None)
            if foreign is not None:
                raise ProtocolRefusal('setup_teardown_changed', f'Setup did not create {foreign}.',
                    remedy='Preserve the changed data and inspect the setup receipt.')
    for path, (before, after) in targets.items():
        current = _image(path)
        if current != before and (current is None or current.artifact() != after):
            raise ProtocolRefusal('setup_teardown_changed', f'The file changed at {path}.',
                remedy='Preserve the changed data and inspect the setup receipt.')
    for path, (before, after) in reversed(list(targets.items())):
        current = _image(path)
        if current == before:
            continue
        if current is None or current.artifact() != after:
            raise ProtocolRefusal('setup_teardown_changed', f'The file changed at {path}.',
                remedy='Preserve the changed data and inspect the setup receipt.')
        if before is None:
            path.unlink()
        else:
            _write(path, before, seed, previous=current, teardown=True)
    for directory in sorted(directories - {root}, key=lambda path: -len(path.parts)):
        if directory.exists():
            directory.rmdir()
    receipt_path.unlink()
    root.rmdir()
    return {'state': 'removed', 'root': str(root), 'files': len(targets) + 1}


def setup(root_path, *, seat=None, session=None, executable=None, hook=None,
          confirmation=None, teardown=False):
    root = _absolute(Path(root_path).expanduser())
    validate_identifier(root.name, 'direct_home_tenant')
    home = Path.home().resolve(strict=True)
    codex_home = Path(os.environ.get('CODEX_HOME', home / '.codex')).expanduser()
    supplied = _CONFIRMATION.fullmatch(confirmation) if confirmation is not None else None
    if confirmation is not None and (supplied is None or int(supplied[1], 16) > _LAST_TIMESTAMP_MS):
        raise ProtocolRefusal('setup_confirmation_invalid', 'The setup confirmation token is invalid.',
            remedy='Use the confirmation from a setup preview.')
    try:
        if teardown:
            if any(value is not None for value in (seat, session, executable, hook)):
                raise ProtocolRefusal('setup_teardown_arguments', 'Teardown takes its seat from the setup receipt.',
                    remedy='Pass only --root and --teardown.')
            with _lock_home(home), without_epoch_turnstile_creation(), epoch_guard(
                    FloatiRoot.open_direct_home(root), exclusive=True):
                return _restore(root, home, codex_home)
        if seat not in SEAT_COMMAND_HARNESSES or session is None:
            raise ProtocolRefusal('setup_seat_required', 'The setup seat or session is missing.',
                remedy='Choose one harness with --seat and supply its --session.')
        validate_session_id(session)
        if hook is not None and hook != seat:
            raise ProtocolRefusal('setup_hook_seat_mismatch', 'The requested hook belongs to a different harness.',
                remedy='Use the same harness for --hook and --seat.')
        codex_home = _absolute(codex_home) if hook == 'codex' else None
        if supplied is None:
            seed = f'{time.time_ns() // 1_000_000:012x}-{secrets.token_hex(16)}'
            return {'state': 'preview', **_plan(root, home, codex_home, seat, session,
                                               executable, hook, seed).evidence()}
        with _lock_home(home):
            plan = _plan(root, home, codex_home, seat, session, executable, hook,
                         '-'.join(supplied.groups()[:2]))
            preview = plan.evidence()
            if not secrets.compare_digest(confirmation, preview['confirmation']):
                raise ProtocolRefusal('setup_confirmation_changed', 'The setup inputs or files changed.',
                    remedy='Preview setup again.')
            _publish(plan)
            return {'state': 'installed', **preview}
    except OSError as exc:
        raise DurabilityFailure('setup_io_failed', str(exc)) from exc
