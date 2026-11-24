#!/usr/bin/env python3
"""Observe native command discovery without credentials, threads or model turns.

This opt-in probe installs through the existing command generator into an
explicit empty bank, then asks the installed Codex app-server for skills/list.
It owns its native child and retains the bank for operator inspection.
"""
import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bank', type=Path, required=True)
    parser.add_argument('--codex', type=Path, required=True)
    args = parser.parse_args()
    bank, executable = args.bank, args.codex
    if (not bank.is_absolute() or bank.resolve() != bank or not bank.is_dir()
            or any(bank.iterdir()) or not executable.is_absolute()):
        parser.error('supply an existing empty canonical bank and an absolute Codex executable')
    home, workspace = bank / 'home', bank / 'workspace'
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    codex_home = home / '.codex'
    environment = {'HOME': str(home), 'CODEX_HOME': str(codex_home),
        'XDG_CONFIG_HOME': str(home / '.config'), 'PATH': os.defpath,
        'TMPDIR': str(bank), 'PYTHONDONTWRITEBYTECODE': '1'}
    # The installer and every native child see only this declared account.
    os.environ.clear()
    os.environ.update(environment)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from floati.lifecycle_prompts import install_seat_commands

    installed = install_seat_commands(workspace, 'codex', codex_home=codex_home)
    version = subprocess.run([str(executable), '--version'], cwd=workspace,
        env=environment, capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    started = time.monotonic()
    with (bank / 'app-server.stderr').open('wb') as errors:
        child = subprocess.Popen([str(executable), 'app-server'], cwd=workspace,
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        buffer = bytearray()

        def send(message):
            child.stdin.write((json.dumps(message) + '\n').encode())
            child.stdin.flush()

        def response(identifier):
            deadline = time.monotonic() + 30
            while True:
                if b'\n' in buffer:
                    line, _, rest = buffer.partition(b'\n')
                    buffer[:] = rest
                    message = json.loads(line)
                    if message.get('id') == identifier:
                        if 'error' in message:
                            raise RuntimeError(message['error'])
                        return message['result']
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                    raise TimeoutError('native skills discovery did not answer')
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError('native app-server ended before answering')
                buffer.extend(chunk)
                if len(buffer) > 4 * 1024 * 1024:
                    raise RuntimeError('native response exceeded probe bound')

        try:
            send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'floati-seat-command-probe', 'version': '1'},
                'capabilities': {'experimentalApi': True}}})
            response(1)
            send({'method': 'initialized'})
            send({'id': 2, 'method': 'skills/list', 'params': {
                'cwds': [str(workspace)], 'forceReload': True}})
            result = response(2)
        finally:
            try:
                try:
                    child.stdin.close()
                except BrokenPipeError:
                    pass  # The native child may already have closed its input.
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            finally:
                child.stdout.close()
    skills = [skill for entry in result['data'] for skill in entry['skills']
        if skill['name'] in ('boot-seat', 'teardown-seat')]
    expected = {str(codex_home / 'skills' / name / 'SKILL.md')
        for name in ('boot-seat', 'teardown-seat')}
    loaded = {skill['path'] for skill in skills if skill.get('enabled', True)}
    summary = {'version': version, 'installed': installed, 'skills': skills,
        'surface_loaded': loaded == expected, 'child_exit': child.returncode,
        'elapsed_seconds': time.monotonic() - started, 'load': os.getloadavg()}
    rendered = json.dumps(summary, sort_keys=True) + '\n'
    (bank / 'native-surface.json').write_text(rendered)
    print(rendered, end='')
    return 0 if summary['surface_loaded'] and child.returncode == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
