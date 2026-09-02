#!/usr/bin/env bash
# key-ceremony.sh — the owner's one command for the V2 release-key ceremony.
#
# Runs the owner-act half of docs/runbooks/v2-key-ceremony-2026-08-29.md:
# generation, self-test, backup staging. The secret key and passphrase exist
# ONLY in this terminal — never in an agent session, a repo, or CI. The
# architect takes over at the .pub file this script prints at the end.
#
# Usage:  bash scripts/key-ceremony.sh
# (run it from anywhere; it refuses to write keys inside a git checkout)

set -euo pipefail

say() { printf '\n== %s\n' "$*"; }
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

KEYDIR="${FLOATI_KEYDIR:-$HOME/.floati-release}"
PUB="$KEYDIR/floati-release.pub"
SEC="$KEYDIR/floati-release.key"

# 0 — preconditions
command -v minisign >/dev/null || die "minisign not installed. Run: brew install minisign"
say "minisign found: $(minisign -v 2>&1 | head -1)"

mkdir -p "$KEYDIR"; chmod 700 "$KEYDIR"
( cd "$KEYDIR" && git rev-parse --is-inside-work-tree >/dev/null 2>&1 ) \
  && die "$KEYDIR is inside a git checkout; keys never live in a repo"

[ -e "$SEC" ] && die "$SEC already exists. This script never overwrites a key. If you truly
mean to replace it, that is the ROTATION procedure in the runbook, not this script."

# 1 — generate (minisign prompts for the passphrase itself; use your password manager)
say "Generating the project release key. You will be asked for a passphrase —"
say "create it in your password manager FIRST, then type it here."
minisign -G -p "$PUB" -s "$SEC"

# 2 — self-test: sign a scratch file, verify the signature
say "Self-test: signing and verifying a scratch file (also proves your passphrase)."
SCRATCH="$(mktemp)"; trap 'rm -f "$SCRATCH" "$SCRATCH.minisig"' EXIT
echo "floati key ceremony self-test $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$SCRATCH"
minisign -S -s "$SEC" -x "$SCRATCH.minisig" -t "ceremony self-test" -m "$SCRATCH"
minisign -V -p "$PUB" -x "$SCRATCH.minisig" -m "$SCRATCH" \
  || die "self-test verification FAILED — do not proceed; tell the architect"
say "Self-test PASSED."
if command -v floati >/dev/null 2>&1; then
  say "floati is on PATH — the architect's gate will also run the shipped verifier."
fi

# 3 — backup staging (manual by design; a script cannot verify your drive is offline)
say "BACKUP — do this now, before anything is published:"
echo "   1. Copy $SEC to TWO offline media (it is already passphrase-encrypted)."
echo "   2. Store the passphrase separately from both (password manager + one other place)."
echo "   3. Restore-verify ONE copy: from the backup, run:"
echo "        t=\"\$(mktemp)\"; echo restore-test >\"\$t\""
echo "        minisign -S -s <backup-copy> -x \"\$t.minisig\" -t restore-test -m \"\$t\""
echo "        rm -f \"\$t\" \"\$t.minisig\""
echo "      (a backup that never restored is not a backup)"

# 4 — the handoff
say "DONE ON YOUR SIDE. Send the architect this public key (safe to paste anywhere):"
echo "----------------------------------------------------------------------"
cat "$PUB"
echo "----------------------------------------------------------------------"
say "The architect lands trust/floati-release.pub, keys.json, and the README pin, then gates it."
