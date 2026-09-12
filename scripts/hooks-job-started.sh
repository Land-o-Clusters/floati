#!/bin/bash
# floati-mac pre-job hook (ACTIONS_RUNNER_HOOK_JOB_STARTED). Queue behind the
# runner-host shared lock, ONE DIRECTION: we PROBE that lock and never hold it;
# our own gates hold <temp>/floati-harbor-gate.lock, never this one. The shared
# path is host configuration (FLOATI_HOOK_LOCK_PATH); the shipped default is
# a floati-owned placeholder so the hook is not a product-name carrier.
# flock has no read-only probe: acquire LOCK_EX|LOCK_NB and, on success, RELEASE AND CLOSE AT ONCE. A descriptor kept open
# for the job's duration would be a shared mutex by accident. The FIRST LOG LINE shows acquire-nonblocking, release and
# verdict - three states - because a verdict-only line cannot tell "probed and released" from "probed and still holding".
# An ABSENT or UNREADABLE lock file counts as HELD (fail closed). This is deliberate. Do not "fix" it.
#
# FQ-12 / BL-7: the lock and load gates are not loosened. A hook-side deadline
# (FLOATI_HOOK_MAX_WAIT, default 1500s = 25 min) fails with a typed reason
# before GitHub timeout-minutes: 40 spends the whole job budget.
LOCK="${FLOATI_HOOK_LOCK_PATH:-/tmp/floati-hook.lock}"
OURS="${FLOATI_HARBOR_GATE_LOCK_PATH:-/tmp/floati-harbor-gate.lock}"
MAX_WAIT="${FLOATI_HOOK_MAX_WAIT:-1500}"
case "$MAX_WAIT" in
  ''|*[!0-9]*) MAX_WAIT=1500 ;;
esac
# One-minute load: /proc/loadavg when that file is readable (Linux CI),
# otherwise sysctl -n vm.loadavg (Darwin). Never leak a reader error to
# stderr. Unreadable or unparsable is a typed absence (loadavg=unavailable)
# and is not quiet: the hook waits or hits its deadline, never a substituted
# number.
_read_load1() {
  proc="${FLOATI_HOOK_PROC_LOADAVG:-/proc/loadavg}"
  if [ -r "$proc" ]; then
    awk '{print $1; exit}' "$proc"
    return
  fi
  sysctl -n vm.loadavg 2>/dev/null | awk '{print $2; exit}'
}
t0=$(date -u +%s)
while :; do
  lock_held=false
  if [ ! -r "$LOCK" ]; then
    line="acquire-nonblocking=skipped(lock absent or unreadable) release=n/a verdict=held(fail-closed)"; state=held
    lock_held=true
  else
    line=$(/usr/bin/python3 - "$LOCK" "$OURS" <<'PY'
import fcntl, sys
def probe(path):
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN); f.close()
        return "acquire-nonblocking=ok release=done-at-once verdict=free"
    except OSError as e:
        f.close()
        who = ""
        try:
            import subprocess
            out = subprocess.run(["/usr/sbin/lsof", "-Fpc", path], capture_output=True, text=True, timeout=5).stdout
            pids = [l[1:] for l in out.splitlines() if l.startswith("p")]
            cmds = [l[1:] for l in out.splitlines() if l.startswith("c")]
            who = " holder=" + ",".join(f"{a}({b})" for a, b in zip(pids, cmds)) if pids else " holder=unknown(lsof empty)"
        except Exception as exc:
            who = f" holder=unknown({type(exc).__name__})"
        return f"acquire-nonblocking=refused(errno {e.errno}) release=nothing-held verdict=held{who}"
theirs = probe(sys.argv[1])
ours = probe(sys.argv[2]) if __import__("os").path.exists(sys.argv[2]) else "ours=absent(free)"
print(f"hook-lock: {theirs} | floati-gate: {ours}")
PY
)
    case "$line" in *"verdict=held"*) state=held; lock_held=true;; *) state=free; lock_held=false;; esac
  fi
  load1=$(_read_load1)
  case "$load1" in
    ''|*[!0-9.]*) loadavg_token=unavailable ;;
    *) loadavg_token=$load1 ;;
  esac
  echo "$line loadavg=${loadavg_token} at $(date -u +%H:%M:%SZ) waited=$(( $(date -u +%s) - t0 ))s"
  # LOAD GATE (2026-09-02): two independent gates on this host died at load ~13 from suites neither could see. A job is
  # queued, typed, while the 1-minute load average is above the threshold; the line prints the number it read.
  # An unavailable reading is not quiet: it must not start a job.
  max="${FLOATI_HOOK_MAX_LOAD:-8}"
  if [ "$loadavg_token" = "unavailable" ]; then
    if [ "$state" = "free" ]; then
      echo "load-gate=held load1=unavailable max=$max verdict=queued-behind-unread-load at $(date -u +%H:%M:%SZ)"
      state=held
    fi
  elif [ "$state" = "free" ] && python3 -c "import sys; sys.exit(0 if float('$load1') > float('$max') else 1)"; then
    echo "load-gate=held load1=$load1 max=$max verdict=queued-behind-load at $(date -u +%H:%M:%SZ)"; state=held
  fi
  # A permitted start is a SAMPLE at one moment; it says nothing about load arriving mid-run. The job-completed hook prints
  # the load again at the end so a red carries both numbers; neither line means "the run was unloaded".
  [ "$state" = "free" ] && { echo "load-gate=start-permitted load1=$load1 max=$max at $(date -u +%H:%M:%SZ)"; exit 0; }
  elapsed=$(( $(date -u +%s) - t0 ))
  if [ "$elapsed" -ge "$MAX_WAIT" ]; then
    echo "hook_wait_deadline lock_held=${lock_held} loadavg=${loadavg_token}"
    exit 1
  fi
  remaining=$(( MAX_WAIT - elapsed ))
  sleep_for=15
  if [ "$remaining" -lt "$sleep_for" ]; then
    sleep_for=$remaining
  fi
  [ "$sleep_for" -le 0 ] && { echo "hook_wait_deadline lock_held=${lock_held} loadavg=${loadavg_token}"; exit 1; }
  sleep "$sleep_for"
done
