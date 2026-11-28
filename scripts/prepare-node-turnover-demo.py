#!/usr/bin/env python3
"""Prepare one owner-run turnover demonstration, entirely under a named scratch base.

Setup prepares real governed services without starting a harness, model or waiter.
Explicit owner actions save a private Cursor API key or launch the prepared CLI.
Codex planning leaves the requested base absent until its exact Stop-definition
pair is explicitly supplied by the operator on a second invocation.
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import warnings

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1]
CURSOR_AUTH_DOCUMENTATION = 'https://docs.cursor.com/en/cli/reference/authentication'
CURSOR_KEY_FILE = 'cursor-api-key'
MAX_CURSOR_KEY_BYTES = 16 * 1024
sys.path.insert(0, str(SOURCE))


class SetupRefusal(Exception):
    def __init__(self, code, detail, remedy):
        self.code, self.detail, self.remedy = code, detail, remedy


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SetupRefusal("turnover_demo_arguments_invalid", message, self.format_usage().strip())


def paths(base):
    return {"base": base, "root": base / "fleet", "home": base / "seat",
        "workspace": base / "seat/repo", "account_home": base / "account",
        "origin": base / "origin.git", "hooks": base / "account/.codex/hooks.json",
        "destination": base / "account/.local/share/floati/waiter"}


def platform_temp_root():
    """Return the canonical platform root, before any fixture-only override."""
    if sys.platform == "darwin":
        return Path("\x2fprivate/tmp")
    return Path(os.path.realpath(tempfile.gettempdir()))


def validate_base(raw, *, existing=False):
    base = Path(raw)
    temporary_root = platform_temp_root()
    if (not base.is_absolute() or base.parent != temporary_root or
            not base.name.startswith("floati-turnover-owner-") or
            base.name == "floati-turnover-owner-" or base.resolve() != base or
            any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise SetupRefusal("turnover_demo_base_invalid",
            f"base must be a normalized direct child {temporary_root}/floati-turnover-owner-NAME",
            "choose a new absolute scratch base with that prefix")
    if not existing and (base.exists() or base.is_symlink()):
        raise SetupRefusal("turnover_demo_base_exists", str(base) + " already exists; nothing was overwritten",
            "preserve that demonstration and choose a new absent scratch base")
    return base


def cursor_credential_route(base):
    account = paths(base)['account_home']
    return {'file': str(account / CURSOR_KEY_FILE), 'mode': '0600',
        'environment_variable': 'CURSOR_API_KEY', 'documentation': CURSOR_AUTH_DOCUMENTATION,
        'save_command': shlex.join(['/usr/bin/python3', '-B', str(Path(__file__).resolve()),
            '--harness', 'cursor', '--base', str(base), '--save-cursor-key']),
        'launch_command': shlex.join(['/usr/bin/python3', '-B', str(Path(__file__).resolve()),
            '--harness', 'cursor', '--base', str(base), '--launch-cursor', '/ABSOLUTE/PATH/TO/cursor-agent']),
        'remedy': 'Create a Cursor API key yourself using the documented dashboard route, '
            'run save_command once, then launch_command with your installed Cursor CLI path. '
            'The private file supplies CURSOR_API_KEY only to the child; do not use browser login in this scratch HOME.'}


def cursor_key(account):
    """Read only the fixed private account file, pinned through an open directory."""
    directory = descriptor = None
    route = cursor_credential_route(account.parent)
    try:
        directory = os.open(account, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        metadata = os.fstat(directory)
        if (account.resolve(strict=True) != account or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise ValueError('invalid account')
        try:
            descriptor = os.open(CURSOR_KEY_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory)
        except FileNotFoundError:
            raise SetupRefusal('turnover_demo_cursor_credential_required',
                'Cursor needs the owner-written scratch API key before launch',
                route['remedy'] + ' File: ' + route['file'] + '; save_command: ' + route['save_command'])
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or not 0 < before.st_size <= MAX_CURSOR_KEY_BYTES):
            raise ValueError('invalid credential metadata')
        data = bytearray()
        while len(data) <= MAX_CURSOR_KEY_BYTES:
            chunk = os.read(descriptor, MAX_CURSOR_KEY_BYTES + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        current = os.stat(CURSOR_KEY_FILE, dir_fd=directory, follow_symlinks=False)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (identity(before) != identity(after) or identity(after) != identity(current)
                or len(data) != before.st_size
                or identity(os.fstat(directory)) != identity(account.lstat())):
            raise ValueError('credential changed')
        key = data.decode('ascii').rstrip('\r\n')
        if not key or any(not 0x21 <= ord(char) <= 0x7e for char in key):
            raise ValueError('invalid credential contents')
        return key
    except (OSError, ValueError, RuntimeError) as error:
        raise SetupRefusal('turnover_demo_cursor_credential_invalid',
            'Cursor API key must be a bounded nonempty owner-owned single-link 0600 ordinary file '
            'in the canonical private 0700 scratch account',
            route['remedy'] + ' File: ' + route['file']) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def cursor_seat(base):
    coordinates = paths(base)
    workspace, account = coordinates['workspace'], coordinates['account_home']
    try:
        from floati.registry import Registry
        from floati.root import FloatiRoot
        from floati.seat_declaration import SeatDeclaration

        for directory in (base, account, base / 'seat', workspace, coordinates['root']):
            info = directory.lstat()
            if (directory.resolve(strict=True) != directory or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022
                    or directory in (base, account) and stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('invalid scratch coordinate')
        declaration = SeatDeclaration.load(workspace)
        if (declaration is None or declaration.node_id != 'seat'
                or declaration.root != str(coordinates['root'])):
            raise ValueError('invalid seat declaration')
        root = FloatiRoot.open_direct_home(coordinates['root'])
        if Registry(root).require_active('seat')['role'].casefold() != 'cursor':
            raise ValueError('not a Cursor seat')
    except Exception as error:
        raise SetupRefusal('turnover_demo_cursor_seat_invalid',
            'launch requires an unchanged prepared Cursor scratch seat',
            'preserve this base and prepare a new --harness cursor demonstration') from error
    return coordinates


def save_cursor_key(base):
    coordinates = cursor_seat(base)
    account = coordinates['account_home']
    directory = descriptor = None
    try:
        directory = os.open(account, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before = os.fstat(directory)
        def require_account():
            current = account.lstat()
            if (account.resolve(strict=True) != account or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o700
                    or (before.st_dev, before.st_ino, before.st_mode, before.st_uid) !=
                       (current.st_dev, current.st_ino, current.st_mode, current.st_uid)):
                raise ValueError('scratch account changed')
        require_account()
        # A getpass fallback can echo input. Refuse before asking for any value
        # when a hidden terminal is unavailable, rather than accepting fallback.
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            key = getpass.getpass('Cursor API key: ')
        if (not key or len(key) + 1 > MAX_CURSOR_KEY_BYTES
                or any(not 0x21 <= ord(char) <= 0x7e for char in key)):
            raise ValueError('invalid key')
        require_account()
        descriptor = os.open(CURSOR_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory)
        require_account()
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
            descriptor = None
            stream.write(key + '\n')
        return {'status': 'ok', 'credential_file': str(account / CURSOR_KEY_FILE), 'mode': '0600'}
    except (OSError, ValueError, EOFError, getpass.GetPassWarning) as error:
        raise SetupRefusal('turnover_demo_cursor_credential_save_refused',
            'Cursor key was not saved: hidden input, an unchanged private account, a valid key '
            'and an absent key file are required',
            'preserve any existing key file; run save_command in your own terminal for this prepared scratch account') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def launch_cursor(base, executable):
    coordinates = cursor_seat(base)
    workspace, account = coordinates['workspace'], coordinates['account_home']
    if not Path(executable).is_absolute():
        raise SetupRefusal('turnover_demo_cursor_executable_invalid',
            'Cursor executable must be an absolute installed CLI path',
            'supply --launch-cursor with the absolute Cursor CLI executable')
    # Start with a clean environment; ambient auth, endpoint and config overrides
    # must not redirect the scratch child or select a credential store.
    environment = {name: os.environ[name] for name in
        ('LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'TERM', 'TZ', 'TMPDIR') if name in os.environ}
    environment.update({'HOME': str(account), 'XDG_CONFIG_HOME': str(account / '.config'),
        'PYTHONDONTWRITEBYTECODE': '1', 'PATH': str(SOURCE / 'scripts') + os.pathsep + os.defpath,
        'CURSOR_API_KEY': cursor_key(account)})
    prior = Path.cwd()
    try:
        os.chdir(workspace)
        os.execve(executable, [executable], environment)
    except OSError as error:
        # Never expose native errors that could include credential-bearing data.
        raise SetupRefusal('turnover_demo_cursor_launch_failed',
            'the prepared Cursor CLI could not be executed',
            'check the absolute installed executable path; preserve the scratch account') from error
    finally:
        os.chdir(prior)  # Reached only on refusal or an injected test launcher.


@contextlib.contextmanager
def scratch_account(home):
    replacements = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / '.claude'),
        "XDG_CONFIG_HOME": str(home / ".config"), "PYTHONDONTWRITEBYTECODE": "1"}
    prior = {key: os.environ.get(key) for key in replacements}
    os.environ.update(replacements)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def codex_plan(coordinates, acknowledgement):
    # Only the preview's empty preimage is temporary. Its target names the
    # actual future base, so changing the base changes the required pair.
    with tempfile.TemporaryDirectory(prefix="floati-turnover-owner-plan-", dir=platform_temp_root()) as raw:
        temporary = Path(raw)
        hooks = temporary / "hooks.json"
        hooks.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")
        with scratch_account(temporary):
            from floati.codex_hook_install import (
                CodexHookInstaller, require_stop_definition_acknowledgement,
            )

            installer = CodexHookInstaller(source_root=SOURCE, bus_home=coordinates["root"],
                hooks_path=hooks, destination=coordinates["destination"])
            plan = installer.plan_stop_definition(hook_timeout_seconds=1800)
            require_stop_definition_acknowledgement(preimage=plan["preimage"],
                target=plan["target"], acknowledgement=acknowledgement,
                hooks_path=coordinates["hooks"])


def git(cwd, *arguments):
    from floati.git_process import fixed_git_command, fixed_git_environment

    environment = fixed_git_environment("/usr/bin/git")
    environment.update({"HOME": os.environ["HOME"],
        "GIT_AUTHOR_NAME": "Floati scratch demonstration",
        "GIT_AUTHOR_EMAIL": "scratch@example.invalid",
        "GIT_COMMITTER_NAME": "Floati scratch demonstration",
        "GIT_COMMITTER_EMAIL": "scratch@example.invalid"})
    result = subprocess.run(fixed_git_command("/usr/bin/git", cwd, arguments),
        env=environment, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise SetupRefusal("turnover_demo_git_failed", result.stderr.strip()[:4096],
            "preserve the partial scratch base and inspect the named local Git operation")
    return result.stdout.strip()


def prepare(base, harness, acknowledgement, *, zcode_executable=None):
    coordinates = paths(base)
    if zcode_executable is not None:
        if harness != 'zcode':
            raise SetupRefusal('turnover_demo_arguments_invalid',
                '--zcode-executable applies only to a zcode demonstration',
                'use --harness zcode or omit --zcode-executable')
        executable = Path(zcode_executable)
        if (not executable.is_absolute() or not executable.is_file()
                or not os.access(executable, os.X_OK)):
            raise SetupRefusal('turnover_demo_arguments_invalid',
                'the declared ZCode terminal entry must be an absolute executable file',
                'supply the full path to the installed standalone terminal CLI with --zcode-executable')
    elif harness == 'zcode':
        from floati.adapters.zcode import require_zcode_terminal_runtime

        require_zcode_terminal_runtime()
    if harness == 'claude':
        coordinates['hooks'] = coordinates['account_home'] / '.claude/settings.json'
    if harness == "codex":
        codex_plan(coordinates, acknowledgement)
    elif acknowledgement is not None:
        raise SetupRefusal("turnover_demo_acknowledgement_unused",
            "Stop-definition acknowledgement applies only to the Codex setup",
            "omit --acknowledge-stop-hook-change for Cursor, zcode or Claude")
    base.mkdir(mode=0o700)  # Exclusive creation: an existing base is never resumed.
    account = coordinates["account_home"]
    account.mkdir(mode=0o700)
    with scratch_account(account):
        from floati.registry import Registry
        from floati.root import FloatiRoot
        from floati.seat_declaration import FleetGovernance, SeatDeclaration

        root = FloatiRoot.open_direct_home(coordinates["root"], create=True)
        registry = Registry(root)
        registry.register("architect", "architect")
        registry.register("seat", harness)
        governance = FleetGovernance.create(root, topology="star", coordinator="architect",
            coordinator_authority=["dispatch_bounded_work"], owner_tier=["publishing"])
        coordinates["home"].mkdir(mode=0o700)
        git(base, "init", "--bare", "--initial-branch=main", str(coordinates["origin"]))
        # Git otherwise creates this checked launch coordinate using the caller's
        # umask (0775 on stock Ubuntu); clone into our private empty directory.
        coordinates["workspace"].mkdir(mode=0o700)
        git(base, "clone", str(coordinates["origin"]), str(coordinates["workspace"]))
        workspace = coordinates["workspace"]
        SeatDeclaration.publish_declared_workspace(workspace, "seat", root, governance)
        if harness == "cursor":
            from floati.cursor_wait import install_cursor_hook
            installed = install_cursor_hook(workspace=workspace, root=root.path,
                node="seat", runtime=SOURCE)
        elif harness == "zcode":
            from floati.zcode_hook_install import install_zcode_hook
            installed = install_zcode_hook(workspace=workspace, root=root.path,
                node="seat", runtime=SOURCE)
        elif harness == 'claude':
            from floati.claude_hook_install import install_claude_hook
            installed = install_claude_hook(workspace=workspace, root=root.path,
                node='seat', runtime=SOURCE, hooks=coordinates['hooks'])
        else:
            from floati.codex_hook_install import CodexHookInstaller
            hooks = coordinates["hooks"]
            hooks.parent.mkdir(parents=True, exist_ok=True)
            hooks.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")
            installed = CodexHookInstaller(source_root=SOURCE, bus_home=root.path,
                hooks_path=hooks, destination=coordinates["destination"]).install(
                    workspace, "seat", hook_timeout_seconds=1800,
                    wait_deadline_seconds=1500,
                    acknowledge_stop_hook_change=acknowledgement)
        (workspace / "report.md").write_text(
            "# Scratch turnover retention report\n\n"
            "Prepared owner demonstration; no live harness result is claimed.\n"
            "The declared root and seat are in SEAT.json. This local origin banks\n"
            "only scratch evidence; it does not publish product work.\n",
            encoding="utf-8")
        git(workspace, "add", "--all")
        git(workspace, "commit", "-m", "Prepare scratch turnover demonstration")
        git(workspace, "push", "--set-upstream", "origin", "main")
        sha = git(workspace, "rev-parse", "HEAD")
        dirty = git(workspace, "status", "--porcelain")
        if dirty:
            raise SetupRefusal("turnover_demo_workspace_dirty", dirty,
                "preserve the scratch base and inspect its unbanked files before opening a session")
    launcher = shlex.quote(str(SOURCE / 'scripts/floati')) if harness == 'codex' else 'floati'
    return {"status": "ok", "harness": harness, "node": "seat", "coordinator": "architect",
        **({'credential': cursor_credential_route(base)} if harness == 'cursor' else {}),
        **({'launch_argv': [str(executable)]} if zcode_executable is not None else {}),
        **{name: str(path) for name, path in coordinates.items()},
        "runtime": str(SOURCE), "sha": sha, "doc": "report.md",
        "launch_environment": {"HOME": str(account), "CODEX_HOME": str(account / ".codex"),
            "CLAUDE_CONFIG_DIR": str(account / '.claude'),
            "XDG_CONFIG_HOME": str(account / ".config"), "PYTHONDONTWRITEBYTECODE": "1"},
        "installed": installed,
        "next_command": ("Read boot-seat.md and boot this seat with this native session's id"
            if harness == "zcode" else "$boot-seat seat"
            if harness == "codex" else '/boot-seat --settings ' + shlex.quote(str(coordinates['hooks']))
            if harness == 'claude' else "/boot-seat seat"),
        "boot_cli": launcher + " node boot --node seat --session SESSION" +
            (' --settings ' + shlex.quote(str(coordinates['hooks'])) if harness == 'claude' else ''),
        "teardown_cli": launcher + " node teardown --node seat --session SESSION --repo floati --doc report.md",
        "session_remedy": "replace SESSION with the current native harness session id; never invent one"}


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--harness", choices=("cursor", "codex", "zcode", "claude"), required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--acknowledge-stop-hook-change")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--launch-cursor', metavar='EXECUTABLE',
        help='owner-run launch of an existing Cursor demo using its private scratch API key')
    actions.add_argument('--save-cursor-key', action='store_true',
        help='owner-run hidden input saved exclusively in the validated scratch account')
    actions.add_argument('--zcode-executable', metavar='EXECUTABLE',
        help='absolute standalone ZCode terminal executable to record for the owner launch')
    try:
        args = parser.parse_args(argv)
        if args.launch_cursor or args.save_cursor_key:
            if args.harness != 'cursor' or args.acknowledge_stop_hook_change is not None:
                raise SetupRefusal('turnover_demo_arguments_invalid',
                    'Cursor credential actions apply only to an existing Cursor demo without Stop acknowledgement',
                    'use --harness cursor --base BASE --launch-cursor ABSOLUTE_EXECUTABLE')
            base = validate_base(args.base, existing=True)
            if args.launch_cursor:
                launch_cursor(base, args.launch_cursor)
                return 0
            result = save_cursor_key(base)
        else:
            base = validate_base(args.base)
            result = prepare(base, args.harness, args.acknowledge_stop_hook_change,
                zcode_executable=args.zcode_executable)
        code = 0
    except Exception as error:
        if not hasattr(error, "code"):
            result = {"status": "refused", "code": "turnover_demo_setup_failed",
                "detail": str(error), "remedy": "preserve any partial scratch base and inspect this failure"}
        else:
            result = {"status": "refused", "code": error.code, "detail": error.detail,
                "remedy": error.remedy}
            if error.code == "stop_hook_definition_change_unacknowledged":
                detail = error.refusal_detail
                pair = detail["preimage"] + ":" + detail["target"]
                result.update({"base_created": False, "acknowledgement": pair,
                    "next_command": shlex.join(["/usr/bin/python3", "-B", str(Path(__file__).resolve()),
                        "--harness", "codex", "--base", str(base),
                        "--acknowledge-stop-hook-change", pair])})
        code = 20
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
