#!/bin/sh
# F2-HOOK-CEILING-1 probe: one zcode headless turn with a Stop hook that sleeps S s
# under declared timeout T s. Records whether the hook ran to completion, when,
# and what the harness did. Usage: probe.sh T S TAG
set -u
T="$1"; S="$2"; TAG="$3"
BASE=~/Projects/lanes/puddle/z1/scratch/ceiling-probe
WS="$BASE/ws"
MARK="$WS/markers"

rm -rf "$MARK"; mkdir -p "$MARK" "$WS/.zcode"
cat > "$WS/.zcode/hooks.json" <<EOF
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/bin/sh -c '/bin/date +%s > $MARK/hook_start; /bin/sleep $S; /bin/date +%s > $MARK/hook_end; echo done >> $MARK/done'",
            "timeout": $T
          }
        ]
      }
    ]
  }
}
EOF

START=$(date +%s)
echo "$$" > "$BASE/last-probe.pid"
ELECTRON_RUN_AS_NODE=1 /Applications/ZCode.app/Contents/MacOS/ZCode \
  /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs \
  -p "reply with the single word ok" --cwd "$WS" --no-color \
  > "$BASE/out-$TAG.txt" 2> "$BASE/err-$TAG.txt"
RC=$?
END=$(date +%s)
kill -0 "$(cat "$BASE/last-probe.pid")" 2>/dev/null || true

# post-observation window: give a survived-but-abandoned hook 15 s past the
# declared timeout to prove itself, then read the markers once; capped so
# acceptance probes with huge T never sleep for hours
WAIT=$((T + 15 - (END - START)))
[ "$WAIT" -gt 20 ] && WAIT=20
[ "$WAIT" -gt 0 ] && sleep "$WAIT"

echo "== probe $TAG: timeout=$T sleep=$S =="
echo "zcode_rc=$RC wall=$((END - START))s"
for f in hook_start hook_end done; do
  if [ -f "$MARK/$f" ]; then echo "marker $f: $(cat "$MARK/$f" | tr '\n' ' ')"; else echo "marker $f: ABSENT"; fi
done
if [ -f "$MARK/hook_start" ] && [ -f "$MARK/hook_end" ]; then
  echo "hook_runtime=$(( $(cat "$MARK/hook_end") - $(cat "$MARK/hook_start") ))s"
fi
echo "--- stderr (first 5 lines) ---"
head -5 "$BASE/err-$TAG.txt"
echo "--- stdout tail (first 5 lines) ---"
head -5 "$BASE/out-$TAG.txt"
