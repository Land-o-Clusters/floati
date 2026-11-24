"""Perform the declared seat's lifecycle using its native governed services.

The native boarding, consent, teardown and hold receipts are the journal. This
composition creates no parallel authority ledger and never retires a node.
"""
from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

from .codex_wait_contract import CodexWaitConsentLedger, resolve_participant
from .errors import ProtocolRefusal
from .registry import Registry
from .root import FloatiRoot
from .seat_declaration import SeatDeclaration, require_declared_coordinate
from .wake_control import validate_session_id


class NodeTurnover:
    def __init__(self, root, node, workspace):
        self.root, self.node, self.workspace = root, node, workspace

    @classmethod
    def from_workspace(cls, workspace, *, node=None, root=None):
        workspace = Path(workspace)
        if not workspace.is_absolute() or workspace.is_symlink() or workspace.resolve() != workspace:
            raise ProtocolRefusal('node_boot_workspace_unrooted',
                'the session must be rooted in its exact declared seat workspace',
                remedy='open the declared seat workspace containing SEAT.json and retry the same command')
        declaration = SeatDeclaration.load(workspace)
        if declaration is None:
            raise ProtocolRefusal('node_boot_workspace_unrooted',
                'this folder has no seat declaration; no fleet was discovered or changed',
                remedy='open the declared seat workspace containing SEAT.json and retry the same command')
        fleet = FloatiRoot.open_direct_home(Path(declaration.root))
        if (node is not None and node != declaration.node_id) or (
            root is not None and str(root) != declaration.root
        ):
            raise ProtocolRefusal('node_boot_workspace_mismatch',
                'the requested node/root differs from this folder\'s SEAT.json',
                remedy='open the requested node\'s declared seat workspace and retry there')
        require_declared_coordinate(workspace, declaration.node_id, fleet)
        return cls(fleet, declaration.node_id, workspace)

    def _harness(self, operation):
        harness = str(Registry(self.root).require_active(self.node)['role']).casefold()
        if harness not in {'cursor', 'codex', 'zcode', 'claude'}:
            raise ProtocolRefusal('node_' + operation + '_harness_unsupported',
                f'no governed turnover installer exists for {harness!r}',
                remedy='use a registered Cursor, Codex, zcode or Claude seat')
        return harness

    def _key(self, operation, session, *extra):
        raw = '\0'.join((str(self.root.path), self.node, str(self.workspace), session, *extra))
        return 'node-' + operation + '-' + hashlib.sha256(raw.encode()).hexdigest()

    def boot(self, session, *, runtime=None, hooks=None, destination=None,
             acknowledge_stop_hook_change=None, monotonic=time.monotonic,
             sleep=time.sleep, wait_readable=None):
        from .seat_board import SeatBoard
        from .stop_hold import StopHoldLedger
        from .waiter_consent import arm_waiter_consent

        session = validate_session_id(session)
        harness = self._harness('boot')
        from .seat_teardown import completed_teardown_for_session
        if completed_teardown_for_session(self.root, self.node, session) is not None:
            raise ProtocolRefusal('node_boot_session_ended',
                'this exact session already completed teardown; its activation stays ended',
                remedy='open a fresh native session in the declared seat workspace and boot that session')
        # Refuse moved sessions before any hook or mapping write.
        if Path.cwd() != self.workspace:
            raise ProtocolRefusal('node_boot_workspace_unrooted',
                'the current session folder is not the declared seat workspace',
                remedy='open ' + str(self.workspace) + ' and retry the same command')
        runtime = Path(runtime) if runtime is not None else Path(__file__).resolve().parents[1]
        if harness == 'cursor':
            from .cursor_wait import install_cursor_hook
            installed = install_cursor_hook(workspace=self.workspace, root=self.root.path,
                node=self.node, runtime=runtime)
            timeout, deadline = installed['timeout'], 1500
        elif harness == 'zcode':
            from .zcode_hook_install import install_zcode_hook, ZCODE_HOLD_SECONDS
            installed = install_zcode_hook(workspace=self.workspace, root=self.root.path,
                node=self.node, runtime=runtime)
            timeout, deadline = ZCODE_HOLD_SECONDS
        elif harness == 'claude':
            from .claude_hook_install import install_claude_hook
            installed = install_claude_hook(workspace=self.workspace, root=self.root.path,
                node=self.node, runtime=runtime, hooks=hooks, verify_existing=True)
            timeout = installed['consent']['hook_timeout_seconds']
            deadline = installed['consent']['wait_deadline_seconds']
        else:
            from .codex_hook_install import CodexHookInstaller
            codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
            hooks = Path(hooks) if hooks is not None else codex_home / 'hooks.json'
            destination = (Path(destination) if destination is not None else
                Path.home() / '.local/share/floati/waiter')
            installer = CodexHookInstaller(source_root=runtime, bus_home=self.root.path,
                hooks_path=hooks, destination=destination)
            from .codex_hook_trust import board_guard_observation
            trust = board_guard_observation(hooks) if hooks.exists() else {'refused': False, 'measured_waiters': 0}
            if not trust['refused'] and trust['measured_waiters'] == 0:
                raise ProtocolRefusal('node_boot_hook_setup_required',
                    'prepare the exact global Stop definition after reviewing its acknowledgment pair; '
                    'host hook trust and relaunch then remain operator actions',
                    remedy=installer.preparation_command(hook_timeout_seconds=1800))
            if trust['refused']:
                raise ProtocolRefusal(trust['code'],
                    'the current Codex Stop hook is not enabled and trusted',
                    remedy=trust['banner'])
            timeout, deadline = 1800, 1500
            installed = installer.install(self.workspace, self.node,
                    hook_timeout_seconds=timeout, wait_deadline_seconds=deadline,
                    acknowledge_stop_hook_change=acknowledge_stop_hook_change)
            # A changed definition invalidates native trust even when the caller
            # explicitly acknowledged the definition write. Never board it.
            trust = board_guard_observation(hooks)
            if trust['refused']:
                raise ProtocolRefusal(trust['code'],
                    'the installed Codex Stop hook requires native review and relaunch',
                    remedy=trust['banner'])
        consent_result = arm_waiter_consent(self.root.path, self.workspace, self.node,
            harness, timeout, deadline)
        board = SeatBoard(self.root).board(self.node, self.workspace, session,
            idempotency_key=self._key('boot', session))
        # Installer readback verifies the actual definition; invoking the native
        # waiter below proves the hold. Neither proves a harness loaded the hook.
        hook_path = Path(installed['hooks_path']) if 'hooks_path' in installed else hooks
        hook = {'state': 'verified', 'path': str(hook_path),
            'sha256': hashlib.sha256(hook_path.read_bytes()).hexdigest(),
            'native_dispatch': 'unmeasured'}
        if harness == 'codex':
            hook.update({k:v for k,v in installed.items() if k.startswith('hook_')})
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        ledger = StopHoldLedger(self.root)
        starts = [r for r in ledger.read(self.node) if r['phase'] == 'start' and
            r['workspace'] == str(self.workspace) and r['acting_session_id'] == session and
            r['consent_receipt_id'] == consent['id']]
        if not starts:
            output, errors = io.StringIO(), io.StringIO()
            options = {} if wait_readable is None else {'wait_readable': wait_readable}
            if harness == 'cursor':
                from .cursor_wait import run_cursor_stop_wait
                run_cursor_stop_wait(root=self.root.path, node=self.node, runtime=runtime,
                    deadline_seconds=deadline, poll_seconds=3, hook_timeout_seconds=timeout,
                    payload={'status':'completed', 'conversation_id':session, 'loop_count':0},
                    stdout=output, stderr=errors, clock=monotonic, sleep=sleep, **options)
            else:
                from .codex_wait import run_stop_waiter
                run_stop_waiter(bus_home=self.root.path,
                    hook_payload={'cwd':str(self.workspace), 'session_id':session},
                    stdout=output, stderr=errors, monotonic=monotonic, sleep=sleep, **options)
            starts = [r for r in ledger.read(self.node) if r['phase'] == 'start' and
                r['workspace'] == str(self.workspace) and r['acting_session_id'] == session and
                r['consent_receipt_id'] == consent['id']]
            if not starts:
                raise ProtocolRefusal('node_boot_first_hold_unproved',
                    'the native waiter returned without a hold-start receipt',
                    remedy='inspect this node\'s waiter exit and consent receipts before retrying boot')
        if not any(r['phase'] == 'exit' and r['invocation_id'] == starts[0]['invocation_id']
                   for r in ledger.read(self.node)):
            raise ProtocolRefusal('node_boot_first_hold_incomplete',
                'the retained first hold has no verified exit receipt',
                remedy='inspect this exact hold invocation before retrying; a recorded start is not a completed hold')
        return {'kind':'node_boot_result', 'node_id':self.node, 'acting_session_id':session, 'harness':harness,
            'board':board, 'consent_receipt_id':consent_result['consent_receipt_id'],
            'hook':hook, 'first_hold':starts[0]}

    def teardown(self, session, *, repo, doc):
        from .seat_teardown import SeatTeardown
        from .seat_waiter_lifecycle import retire_waiter
        session = validate_session_id(session)
        harness = self._harness('teardown')
        key = self._key('teardown', session, repo, doc)
        native = SeatTeardown(self.root).teardown(self.node, self.workspace.parent,
            session, repo=repo, doc=doc, idempotency_key=key)
        from .bus_epoch import epoch_guard
        from .wake_hold import wake_coordination_guard
        with epoch_guard(self.root), wake_coordination_guard(self.root, self.node):
            return self._retire_activation(session, harness, native, key)

    def _retire_activation(self, session, harness, native, key):
        from .codex_wait_contract import CodexWaitSessionLedger
        from .jsonl import read_records_snapshot
        from .seat_waiter_lifecycle import retire_waiter
        claims = read_records_snapshot(self.root, CodexWaitSessionLedger._relative(self.node),
            allowed_kinds={'codex_wait_session_receipt'})
        matching = [r for r in claims if r['workspace'] == str(self.workspace)]
        if not matching or matching[-1]['id'] != native['claim_receipt_id']:
            raise ProtocolRefusal('node_teardown_session_superseded',
                'a newer session owns this workspace; the old teardown cannot remove its activation',
                remedy='preserve the current activation and use the current session\'s teardown command')
        if harness == 'cursor':
            from .cursor_user_hook import uninstall_cursor_seat_marker
            # Read the exact marker runtime, preserving the shared user hook.
            from .cursor_user_hook import _read_marker
            kind, _path, seat = _read_marker(str(self.workspace))
            if kind != 'absent':
                if kind != 'resolved' or seat['node'] != self.node or seat['root'] != str(self.root.path):
                    raise ProtocolRefusal('node_teardown_marker_invalid',
                        'the Cursor marker no longer names this exact seat',
                        remedy='preserve the marker and inspect the changed seat coordinate')
                uninstall_cursor_seat_marker(workspace=self.workspace, root=self.root.path,
                    node=self.node, runtime=Path(seat['runtime']))
        retired = retire_waiter(self.root, self.node, self.workspace, key + '-consent')
        return {'kind':'node_teardown_result', 'node_id':self.node, 'acting_session_id':session, 'harness':harness,
            'teardown':native, 'activation':{'state':retired['state'],
                'consent_receipt_id':retired['consent_receipt_id']}}
