import json
import select
import subprocess
import sys
from pathlib import Path

BASE = Path('\x2fprivate/tmp/floati-m4-codex-20260905')
node, batch = sys.argv[1:]
requests = json.loads((BASE / (batch + '.requests.json')).read_text())
argv = ['/usr/bin/python3', '-B', '-m', 'floati', 'mcp', 'serve', '--root', str(BASE / 'fleet'), '--as', node, '--session', 'codex-m4-' + node]
env = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
with (BASE / (batch + '.wire.jsonl')).open('w') as log, (BASE / (batch + '.stderr')).open('w') as err:
    p = subprocess.Popen(argv, cwd=BASE / 'installed', env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True, bufsize=1)
    log.write(json.dumps({'argv': argv, 'pid': p.pid}) + '\n')
    try:
        for req in requests:
            raw = req if isinstance(req, str) else json.dumps(req, separators=(',', ':'))
            log.write(json.dumps({'direction': 'request', 'raw': raw}) + '\n')
            p.stdin.write(raw + '\n')
            p.stdin.flush()
            if not isinstance(req, str) and 'id' not in req:
                continue
            if not select.select([p.stdout], [], [], 45)[0]:
                raise RuntimeError('response deadline')
            answer = p.stdout.readline()
            log.write(json.dumps({'direction': 'response', 'raw': answer}) + '\n')
            log.flush()
            parsed = json.loads(answer)
            artifact = parsed.get('result', {}).get('structuredContent', {})
            print(json.dumps({'id': parsed.get('id'), 'status': artifact.get('status'), 'evidence': artifact.get('evidence') if artifact else parsed.get('error')}), flush=True)
        sockets = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(p.pid), '-i'], capture_output=True, text=True)
        log.write(json.dumps({'socket_sample': {'pid': p.pid, 'exit': sockets.returncode, 'stdout': sockets.stdout, 'stderr': sockets.stderr}}) + '\n')
    finally:
        p.stdin.close()
        tail = p.stdout.read()
        status = p.wait(timeout=10)
        log.write(json.dumps({'exit': status, 'tail': tail}) + '\n')
