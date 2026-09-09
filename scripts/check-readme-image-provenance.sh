#!/bin/bash
# THE README IMAGE PROVENANCE FENCE (R-9 as amended by Am.1 and Am.2,
# docs/rulings/2026-09-08-readme-provenance-am1.md,
# docs/rulings/2026-09-08-readme-provenance-am2.md). The README's provenance
# sentence is enforced here, not asserted. Three arms:
#   (a) no image referenced by README.md may have a sidecar .txt whose FIRST
#       LINE CARRIES `FLOATI // ` ANYWHERE — decoration in front of a slide
#       title is arbitrary and unbounded; the separator is the signature
#       (un-anchored: `⊙ FLOATI // HARBOR BOARD` evaded the R-9 anchor).
#   (c) a referenced image that does not exist refuses: a reference nothing
#       can check is not provenance.
#   (d) THE CENSUS: every referenced image NOT under docs/assets/ must appear
#       in a manifest with a matching SHA-256. This is exactly the predicate
#       of the README's provenance sentence, so the sentence is true by
#       construction. docs/assets/ is exempt: it is the hand-drawn brand art
#       the sentence names as illustrating rather than measuring.
# The placeholder arm (`<scratch>`/`<root>`/`<node>`) was DELETED by Am.1 §3(b):
# our own generator inserts those tokens when it redacts a declared-fixture
# path, so the arm refused exactly what the sentence permits and would have
# red the FQ-1b recapture pass.
# Every refusal names the file. The passing run names each sidecar and census
# entry it checked, so a pass is never vacuous.
# Am.2 §3(1)-(2): the census verdict is composed from the VALUE N (refuse when
# N > 0, never by matching a rendered verdict string — Z2 proved two failures
# exited 0 under a `census-failures=1` literal grep), and an unreadable or
# non-JSON manifest is a loud refusal naming the file, never a silent
# "not listed".
#
# Written for /bin/bash 3.2 (macOS): no case pattern inside command
# substitution (bash 3.2 misparses `''|*://*` there).
set -u

root=$(git rev-parse --show-toplevel) || exit 1
readme="$root/README.md"
if [ ! -f "$readme" ]; then
  echo "REFUSED: no README.md at repo root $root" >&2
  exit 1
fi

# Every local image path README.md references: <source srcset> and <img src>
# candidates (srcset lists split on commas, size descriptors dropped,
# #fragment stripped, remote URLs skipped) plus markdown ![alt](path) images.
refs=$(
  {
    grep -o 'srcset="[^"]*"\| src="[^"]*"' "$readme" | sed 's/^ *//;s/[a-z]*="//;s/"$//'
    grep -o '!\[[^]]*\]([^)]*)' "$readme" | sed 's/.*(\([^ )]*\)).*/\1/'
  } | tr ',' '\n' \
    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/#.*$//' \
    | awk 'NF { print $1 }' \
    | grep -Ev '^[A-Za-z][A-Za-z0-9+.-]*:' \
    | sort -u
)

if [ -z "$refs" ]; then
  echo "REFUSED: README.md references no images; the fence checks nothing" >&2
  exit 1
fi

failures=0
checked=0
images=0
for rel in $refs; do
  path="$root/$rel"
  images=$((images + 1))
  if [ ! -f "$path" ]; then
    echo "REFUSED: README.md references a missing image: $rel" >&2
    failures=$((failures + 1))
    continue
  fi
  sidecar="${path%.*}.txt"
  [ -f "$sidecar" ] || continue
  checked=$((checked + 1))
  firstline=$(head -n 1 "$sidecar")
  case "$firstline" in
    *"FLOATI // "*)
      echo "REFUSED: $sidecar first line carries a slide header: $firstline" >&2
      failures=$((failures + 1))
      continue
      ;;
  esac
  echo "  ok  sidecar  $sidecar"
done

# Census arm (d): run in python3 — manifest rows are JSON and the comparison
# is SHA-256. Keyed by (manifest directory, entry basename), which is how every
# capture manifest in this repository names its rows.
census_input=$(printf '%s\n' $refs)
census_result=$(printf '%s\n' "$census_input" | python3 -c '
import sys, os, json, hashlib, subprocess

root = sys.argv[1]
def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()

refs = [line.strip() for line in sys.stdin if line.strip()]
rows = {}
unreadable = 0
tracked = subprocess.run(
    ["git", "-C", root, "ls-files", "*manifest.json"],
    capture_output=True, text=True, check=True,
).stdout.splitlines()
for rel in tracked:
    mj = os.path.join(root, rel)
    try:
        with open(mj, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        print("REFUSED: manifest %s is unreadable or not valid JSON; the census cannot see what it lists" % rel,
              file=sys.stderr)
        unreadable += 1
        continue
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "sha256" in item and ("path" in item or "name" in item):
                name = item.get("path") or item.get("name")
                rows[(os.path.dirname(mj), os.path.basename(name))] = (mj, item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)

failures = unreadable
for rel in refs:
    if rel.startswith("docs/assets/"):
        continue
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        continue  # the missing-image arm already refused and named it
    hit = rows.get((os.path.dirname(path), os.path.basename(path)))
    if hit is None:
        print("REFUSED: %s is in no manifest; the provenance sentence has nothing that says which" % rel,
              file=sys.stderr)
        failures += 1
        continue
    mj, row = hit
    if sha256(path) != row["sha256"]:
        print("REFUSED: %s sha256 does not match its manifest row in %s" % (rel, mj),
              file=sys.stderr)
        failures += 1
        continue
    print("  ok  census   %s (%s)" % (rel, os.path.relpath(mj, root)))
print("census-failures=%d" % failures)
' "$root" 2>&1)
census_code=$?
printf '%s\n' "$census_result" | grep '^  ok' || true
printf '%s\n' "$census_result" | grep '^REFUSED' >&2 || true
# Am.2 §3(1): the verdict comes from the VALUE N, never from matching a
# rendered verdict string — two failures must fail exactly like one. The ok and
# REFUSED lines above are rendered output only and are never read back.
census_tail=${census_result##*$'\n'}
N=
case "$census_tail" in
  census-failures=[0-9]*) N=${census_tail#census-failures=} ;;
esac
if [ "$census_code" -ne 0 ] || [ -z "$N" ]; then
  echo "REFUSED: census arm could not produce a verdict" >&2
  failures=$((failures + 1))
elif [ "$N" -gt 0 ]; then
  failures=$((failures + 1))
fi

echo "  README.md images referenced: $images; sidecars checked: $checked; census verified: $(printf '%s\n' "$census_result" | grep -c '^  ok  census' || true)"
if [ "$failures" -gt 0 ]; then
  exit 1
fi
exit 0
