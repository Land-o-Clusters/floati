#!/bin/bash
# THE LEASE CHECK. Two earlier versions of this counted THEMSELVES: the first because
# `ps aux | grep -c` saw the calling shell's own argv, the second because excluding $$
# does not exclude the parent shell whose argv carries this script's heredoc. The fix is
# not a better pattern, it is a better SUBJECT: a suite is a PYTHON process, so filter on
# argv[0] being a python binary first and only then look at the arguments. A shell wrapper
# can never satisfy that, whatever its command line happens to quote.
# LC-PID-1 (2026-09-11): `ps -Ao pid=` RIGHT-ALIGNS the pid column, so a pid narrower
# than the widest one on the box arrives with leading spaces; `${line%% *}` then reads
# an empty pid and the wrong first word, and the process is silently not counted. Both
# hosted legs and this Mac (after its pid counter wrapped) said "suite processes: 0"
# with a live pytest sleeper on the box. `read -r pid cmd` splits on IFS, which strips
# the padding first.
n=0
while read -r pid cmd; do
  [ -n "$pid" ] || continue
  exe=${cmd%% *}
  case "$exe" in *python*|*Python*) ;; *) continue ;; esac
  case "$cmd" in
    *unittest*|*pytest*|*xctest*|*swift\ test*|*swift-build*)
      n=$((n+1)); echo "  BUSY pid $pid  ${cmd:0:110}" ;;
  esac
done < <(ps -Ao pid=,command=)
echo "  suite processes: $n"
uptime
exit $n
