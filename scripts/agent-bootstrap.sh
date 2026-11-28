#!/bin/sh
set -eu

cd -P -- "$(dirname -- "$0")/.."
python=python3
# Use the managed job's interpreter on macOS so a newer PATH Python cannot
# select a wheel ABI absent from the test lock.
if [ "$(uname -s)" = Darwin ] && [ -x /usr/bin/python3 ]; then
    python=/usr/bin/python3
fi
if ! command -v "$python" >/dev/null 2>&1; then
    printf '%s\n' 'Python 3 is missing. On this Mac run: xcode-select --install' >&2
    exit 1
fi
exec "$python" -I -B scripts/agent_bootstrap.py "$@"
