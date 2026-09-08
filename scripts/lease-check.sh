#!/bin/bash
# THE LEASE CHECK. Two earlier versions of this counted THEMSELVES: the first because
# `ps aux | grep -c` saw the calling shell's own argv, the second because excluding $$
# does not exclude the parent shell whose argv carries this script's heredoc. The fix is
# not a better pattern, it is a better SUBJECT: a suite is a PYTHON process, so filter on
# argv[0] being a python binary first and only then look at the arguments. A shell wrapper
# can never satisfy that, whatever its command line happens to quote.
n=0
while IFS= read -r line; do
  pid=${line%% *}; cmd=${line#* }
  exe=${cmd%% *}
  case "$exe" in *python*|*Python*) ;; *) continue ;; esac
  case "$cmd" in *unittest*) n=$((n+1)); echo "  BUSY pid $pid  ${cmd:0:110}" ;; esac
done < <(ps -Ao pid=,command=)
echo "  suite processes: $n"
uptime
exit $n
