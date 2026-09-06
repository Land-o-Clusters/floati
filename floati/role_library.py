"""Validated root-local role files with recoverable digest-bound write receipts."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Mapping, Optional

from .bus_epoch import shared_epoch_operation
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import _locked_path, read_records_snapshot, transact
from .registry import utc_now
from .role_templates import (
    SHIPPED_ROLE_NAMES, RoleTemplate, _MAX_FILE_BYTES, _parse_role_template_bytes,
    load_role_template_payload, load_shipped_role_templates, parse_role_template,
)
from .root import FloatiRoot, validate_identifier
from .wake_hold import _validate_wake_key

KIND = 'role_template_write_receipt'
LEDGER = Path('receipts/role-templates.jsonl')

# Operator acts shared by several sites; each names a thing to do, not a state.
CUSTOM_FILE_REMEDY = 'replace the named file under roles/custom with a regular JSON file under 256 KiB'
CUSTOM_DIRECTORY_REMEDY = 'replace roles/custom under the declared --root with a real directory, then repeat the command'
QUIESCE_REMEDY = 'stop concurrent writes to roles/custom, then repeat the exact request'
INSPECT_THEN_REPEAT_REMEDY = 'preserve the file under roles/custom, inspect its bytes, then repeat the original request'
REPEAT_EXACT_REMEDY = 'repeat the exact write request with its original --idempotency-key'


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal('role_template_invalid', 'role fields must be strict JSON values',
            remedy='pass each --set value as strict JSON with finite numbers and no duplicate keys') from exc


def _digest(payload):
    return None if payload is None else hashlib.sha256(payload).hexdigest()


class RoleTemplateLibrary:
    def __init__(self, root: FloatiRoot, shipped_directory=None):
        if not isinstance(root, FloatiRoot):
            raise TypeError('role library requires one validated root')
        self.root = root
        self.shipped_directory = (Path(__file__).resolve().parents[1] / 'roles/shipped'
                                  if shipped_directory is None else Path(shipped_directory))

    @staticmethod
    def _name(name):
        try:
            name = validate_identifier(name, 'role')
        except ProtocolRefusal as exc:
            # Shared validator: keep its code and detail, add this verb's act.
            raise ProtocolRefusal(exc.code, exc.detail,
                remedy='pass --name as a lowercase identifier matching the pattern named in detail') from exc
        if name in SHIPPED_ROLE_NAMES:
            raise ProtocolRefusal('role_template_reserved', 'shipped role names are read-only; choose a custom role name',
                remedy='choose a --name that role list does not already show as a shipped role')
        return name

    @contextmanager
    def _directory(self, *, create=False, durable=False):
        """Anchor traversal without misclassifying failures from the yielded operation."""
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
        descriptors = []
        try:
            missing = False
            try:
                descriptors.append(os.open(self.root.tenant_home, flags))
                for name in ('roles', 'custom'):
                    parent = descriptors[-1]
                    if create:
                        try:
                            os.mkdir(name, mode=0o700, dir_fd=parent)
                        except FileExistsError:
                            pass
                    try:
                        descriptors.append(os.open(name, flags, dir_fd=parent))
                    except FileNotFoundError:
                        if create:
                            raise
                        missing = True
                        break
                    # Retry may find a directory whose creation was never synced.
                    if create or durable:
                        os.fsync(parent)
            except OSError as exc:
                if create or durable:
                    raise DurabilityFailure('role_template_publication_unknown',
                        'roles/custom directory durability is unproved; repeat the exact write request to recover') from exc
                raise ProtocolRefusal('role_template_path_invalid', 'roles/custom must be a real contained directory',
                    remedy=CUSTOM_DIRECTORY_REMEDY) from exc
            yield None if missing else descriptors[-1]
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _read_at(directory, name):
        if directory is None:
            return None
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProtocolRefusal('role_template_path_invalid', 'custom role must be a bounded regular file',
                remedy=CUSTOM_FILE_REMEDY) from exc
        try:
            identity = os.fstat(descriptor)
            if not stat.S_ISREG(identity.st_mode) or identity.st_size > _MAX_FILE_BYTES:
                raise ProtocolRefusal('role_template_path_invalid', 'custom role must be a bounded regular file',
                    remedy=CUSTOM_FILE_REMEDY)
            with os.fdopen(descriptor, 'rb') as source:
                descriptor = -1
                payload = source.read(_MAX_FILE_BYTES + 1)
            if len(payload) > _MAX_FILE_BYTES:
                raise ProtocolRefusal('role_template_path_invalid', 'custom role exceeds the file size limit',
                    remedy=CUSTOM_FILE_REMEDY)
            return payload
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @shared_epoch_operation
    def templates(self):
        library = load_shipped_role_templates(self.shipped_directory)
        with self._directory() as directory:
            if directory is None:
                return library
            for filename in sorted(os.listdir(directory)):
                if not filename.endswith('.json'):
                    continue
                name = self._name(filename[:-5])
                payload = self._read_at(directory, filename)
                if payload is None:
                    raise ProtocolRefusal('role_template_path_invalid', 'custom role changed during library read',
                        remedy=QUIESCE_REMEDY)
                template = _parse_role_template_bytes(payload)
                if template.role != name:
                    raise ProtocolRefusal('role_template_name_mismatch', 'custom role field must match its filename',
                        remedy='rename the file under roles/custom, or set its role field to the filename')
                library[name] = template
        return library

    @staticmethod
    def _source(path):
        if '://' in str(path):
            raise ProtocolRefusal('role_template_path_invalid', 'role source must be a local file path, never a URL',
                remedy='pass --from a local file path; fetch the file yourself before importing it')
        return load_role_template_payload(path)

    def validate_file(self, path):
        return self._source(path)[0]

    def new(self, name, *, from_role, idempotency_key):
        name = self._name(name)
        template = self.templates().get(from_role)
        if template is None:
            raise ProtocolRefusal('role_template_unknown', 'source role is absent from the selected library',
                remedy='pass --from a role that role list shows for this --root')
        candidate = parse_role_template(dict(template.record, role=name))
        request = {'operation': 'new', 'role': name, 'from_role': from_role,
                   'source_sha256': template.digest}
        return self._write(name, 'new', request, lambda before: candidate, idempotency_key)

    def import_file(self, path, *, idempotency_key):
        template, payload = self._source(path)
        name = self._name(template.role)
        request = {'operation': 'import', 'role': name, 'source_sha256': _digest(payload)}
        return self._write(name, 'import', request, lambda before: template, idempotency_key)

    def edit(self, name, *, changes=None, from_file=None, idempotency_key):
        name = self._name(name)
        if (changes is None) == (from_file is None):
            raise ProtocolRefusal('role_template_edit_invalid', 'edit requires exactly one of changes or a local source file',
                remedy='pass exactly one of --set or --from')
        if from_file is not None:
            template, payload = self._source(from_file)
            if template.role != name:
                raise ProtocolRefusal('role_template_name_mismatch', 'role field cannot rename an existing template',
                    remedy="set the source file's role field to the --name you passed, or import it as a new role")
            request = {'operation': 'edit', 'role': name, 'source_sha256': _digest(payload)}
            candidate = lambda before: template
        else:
            if not isinstance(changes, Mapping) or not changes:
                raise ProtocolRefusal('role_template_edit_invalid', 'changes must contain at least one declared field',
                    remedy='pass at least one --set field=value')
            # Copy caller input before hashing and before reading mutable current bytes.
            values = json.loads(_encoded(dict(changes)))
            if 'role' in values and values['role'] != name:
                raise ProtocolRefusal('role_template_name_mismatch', 'role field cannot rename an existing template',
                    remedy='drop role from --set; role edit cannot rename a template')
            request = {'operation': 'edit', 'role': name, 'changes': values}
            candidate = lambda before: parse_role_template(dict(_parse_role_template_bytes(before).record, **values))
        return self._write(name, 'edit', request, candidate, idempotency_key)

    def _append(self, row):
        def decide(prior):
            matching = [r for r in prior if r['idempotency_key'] == row['idempotency_key']]
            same = [r for r in matching if r['state'] == row['state']]
            if same:
                fields = set(row) - {'id', 'timestamp'}
                if len(same) == 1 and all(same[0][k] == row[k] for k in fields):
                    return same[0], None
                raise IntegrityFailure('role_template_receipt_invalid', 'write receipt replay has inconsistent fields')
            return row, row
        return transact(self.root, LEDGER, decide, allowed_kinds={KIND})

    @staticmethod
    def _publish(directory, filename, payload):
        temporary = '.role-' + uuid7_hex() + '.tmp'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    @shared_epoch_operation
    def _write(self, name, operation, request, candidate, key):
        key = _validate_wake_key(key)
        request_digest = _digest(_encoded(request))
        relative = Path('state/role-templates.lock')
        with _locked_path(self.root.resolve_relative(relative), exclusive=True, relative=relative):
            history = read_records_snapshot(self.root, LEDGER, allowed_kinds={KIND})
            rows = [r for r in history if r['idempotency_key'] == key]
            if rows:
                first = rows[0]
                if first['request_sha256'] != request_digest or first['role'] != name or first['operation'] != operation:
                    raise ProtocolRefusal('role_template_idempotency_conflict', 'write key names different explicit inputs',
                        remedy='pass a fresh --idempotency-key, or repeat this key with its original inputs')
                if [r['state'] for r in rows] not in (['prepared'], ['prepared', 'applied']):
                    raise IntegrityFailure('role_template_receipt_invalid', 'write receipt progress is inconsistent')
                if len(rows) == 2:
                    if rows[1]['predecessor_receipt_id'] != first['id'] or any(
                        rows[1][field] != first[field] for field in
                        ('operation', 'role', 'path', 'request_sha256', 'before_sha256', 'after_sha256', 'template_sha256')
                    ):
                        raise IntegrityFailure('role_template_receipt_invalid', 'applied receipt differs from prepared intent')
                    return rows[1]
            latest = {r['idempotency_key']: r for r in history if r['role'] == name}
            if any(r['state'] == 'prepared' and r['idempotency_key'] != key for r in latest.values()):
                raise ProtocolRefusal('role_template_write_pending', 'this role has an unfinished prepared write; repeat its original request first',
                    remedy='repeat the unfinished write with its original --idempotency-key before starting another')
            filename = name + '.json'
            with self._directory() as directory:
                before = self._read_at(directory, filename)
            current_digest = _digest(before)
            if rows:
                intent = rows[0]
                if current_digest == intent['after_sha256']:
                    with self._directory(durable=True) as directory:
                        self._sync_published(directory, filename, intent['after_sha256'])
                    return self._complete(intent)
                if current_digest != intent['before_sha256']:
                    raise ProtocolRefusal('role_template_write_conflict', 'custom role differs from prepared write; preserve and inspect its bytes',
                        remedy=INSPECT_THEN_REPEAT_REMEDY)
            else:
                if operation == 'edit' and before is None:
                    raise ProtocolRefusal('role_template_unknown', 'custom role does not exist; create it first',
                        remedy='run role new --name for this role before editing it')
                if operation != 'edit' and before is not None:
                    raise ProtocolRefusal('role_template_exists', 'custom role already exists; use explicit edit',
                        remedy='run role edit --name for this role, or choose a --name that is free')
            template = candidate(before)
            if template.role != name:
                raise ProtocolRefusal('role_template_name_mismatch', 'role field must match the selected name',
                    remedy="set the template's role field to the --name you passed")
            payload = _encoded(template.record) + b'\n'
            if len(payload) > _MAX_FILE_BYTES:
                raise ProtocolRefusal('role_template_path_invalid', 'custom role exceeds the file size limit',
                    remedy=CUSTOM_FILE_REMEDY)
            if rows:
                if _digest(payload) != intent['after_sha256'] or template.digest != intent['template_sha256']:
                    raise ProtocolRefusal('role_template_write_conflict', 'prepared output differs from the repeated request',
                        remedy='pass a fresh --idempotency-key, or repeat this key with its original inputs')
            else:
                intent = self._append({
                    'schema_version': 1, 'kind': KIND, 'id': 'role-template-write-' + uuid7_hex(),
                    'tenant_id': self.root.tenant_id, 'timestamp': utc_now(), 'operation': operation,
                    'role': name, 'path': 'roles/custom/' + filename, 'state': 'prepared',
                    'request_sha256': request_digest, 'before_sha256': current_digest,
                    'after_sha256': _digest(payload), 'template_sha256': template.digest,
                    'idempotency_key': key, 'predecessor_receipt_id': None,
                })
            with self._directory(create=True) as directory:
                if _digest(self._read_at(directory, filename)) != current_digest:
                    raise ProtocolRefusal('role_template_write_conflict', 'custom role changed before publication',
                        remedy=QUIESCE_REMEDY)
                try:
                    self._publish(directory, filename, payload)
                except OSError as exc:
                    raise DurabilityFailure('role_template_publication_unknown',
                        'roles/custom publication durability is unproved; repeat the exact write request to recover') from exc
                if _digest(self._read_at(directory, filename)) != intent['after_sha256']:
                    raise IntegrityFailure('role_template_write_unknown', 'published role did not match its prepared digest')
            return self._complete(intent)

    def _sync_published(self, directory, filename, expected_digest):
        """Recover publication durability before claiming the prepared write applied."""
        if directory is None:
            raise ProtocolRefusal('role_template_write_conflict', 'prepared output is absent during recovery',
                remedy=REPEAT_EXACT_REMEDY)
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            descriptor = os.open(filename, flags, dir_fd=directory)
            identity = os.fstat(descriptor)
            if not stat.S_ISREG(identity.st_mode) or identity.st_size > _MAX_FILE_BYTES:
                raise ProtocolRefusal('role_template_write_conflict', 'prepared output is not a bounded regular file',
                    remedy=CUSTOM_FILE_REMEDY)
            with os.fdopen(descriptor, 'rb') as source:
                descriptor = -1
                payload = source.read(_MAX_FILE_BYTES + 1)
                if _digest(payload) != expected_digest:
                    raise ProtocolRefusal('role_template_write_conflict', 'prepared output changed during recovery',
                        remedy=QUIESCE_REMEDY)
                os.fsync(source.fileno())
            os.fsync(directory)
            if _digest(self._read_at(directory, filename)) != expected_digest:
                raise ProtocolRefusal('role_template_write_conflict', 'prepared output changed before receipt completion',
                    remedy=QUIESCE_REMEDY)
        except OSError as exc:
            raise DurabilityFailure('role_template_publication_unknown',
                'roles/custom recovery durability is unproved; repeat the exact write request to recover') from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _complete(self, intent):
        return self._append(dict(intent, id='role-template-write-' + uuid7_hex(), timestamp=utc_now(),
                                 state='applied', predecessor_receipt_id=intent['id']))
