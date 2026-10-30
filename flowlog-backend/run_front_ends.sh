#!/bin/bash
# CTADL's front-end checks with both engines: the steps of tests/bin/check-pcode,
# check-analysis-options and check-ActivityCommunication4, on inputs given here
# (the checks build curl and nginx with nix), each FlowLog result compared with
# Soufflé's.
#   [JOBS=N] run_front_ends.sh OUT CURL NGINX APK
# CURL and NGINX are binaries (check-pcode's are nixpkgs#curl and CTADL's
# nginx-debug), APK DroidBench's ActivityCommunication4.apk. Needs `ctadl` set
# up for FlowLog (README, Setup) and CTADL's Ghidra and JADX plugins
# (GHIDRA_HOME, a JDK); the imports go to OUT/{curl,nginx,ac4}/facts, unless
# those exist. Each engine runs in a copy, OUT/<program>/<engine>[-opt<i>].
# Prints a line per step (exit status, time, largest peak RSS among its
# processes, its check), then the comparisons (compare_pcode.py,
# compare_jadx.py, compare_sarif.py, nginx's summary, and AC4's SARIF and GML
# with the ord-based ids renamed). JOBS defaults to 32. checksarif (SARIF
# validation) isn't run. An analysis' first run includes its compilation, and
# its RSS can be the compiler's.
HERE=$(cd "$(dirname "$0")" && pwd)
CTADL=$(dirname "$HERE")
if [ $# -ne 4 ]; then
  sed -n 2,17p "$0" >&2
  exit 2
fi
mkdir -p "$1" || exit 1
OUT=$(cd "$1" && pwd)
CURL=$(realpath "$2") NGINX=$(realpath "$3") APK=$(realpath "$4")
J=${JOBS:-32}
ENGINES=(souffle flowlog)
OPTS=(--match-star '--cha --no-hybrid-inlining' --star) # check-analysis-options'

step() { # PROG DIR LABEL CHECK CMD...: CMD in DIR, stdout to DIR/LABEL.out
  local prog=$1 d=$2 label=$3 check=$4 st s kb
  shift 4
  (cd "$d" && /usr/bin/time -o "$label.time" -f "%e %M" "$@" > "$label.out" 2> "$label.log")
  st=$?
  read -r s kb < <(tail -n 1 "$d/$label.time")
  printf "%-6s %-14s %-6s exit=%s %7.1f s %6.2f GB  %s\n" "$prog" "$(basename "$d")" "$label" "$st" \
    "$s" "$(awk -v k="$kb" 'BEGIN { print k / 1048576 }')" "$(cd "$d" && eval "$check")"
}
fresh() { # PROG DIR: DIR with a copy of PROG's import
  rm -rf "$2" && mkdir -p "$2" && cp -r "$OUT/$1/facts" "$2/facts"
}

for p in curl:pcode:$CURL nginx:pcode:$NGINX ac4:JADX:$APK; do
  IFS=: read -r name lang input <<< "$p"
  [ -d "$OUT/$name/facts" ] && continue
  mkdir -p "$OUT/$name"
  step "$name" "$OUT/$name" import "test -d facts && echo imported" \
    ctadl import "$lang" "$input" -o facts -f
done

for e in "${ENGINES[@]}"; do
  D=$OUT/curl/$e
  fresh curl "$D"
  step curl "$D" index "" ctadl index -f facts --engine "$e" -j "$J"
  step curl "$D" query "jq -r '\"results: \" + ([.runs[].results[]] | length | tostring)' query.out" \
    ctadl query --engine "$e" -j "$J" --format=sarif
  for i in 1 2 3; do
    fresh curl "$D-opt$i"
    # shellcheck disable=SC2086 # an option list
    step curl "$D-opt$i" index "echo ${OPTS[$((i - 1))]}" ctadl index -f facts --engine "$e" -j "$J" ${OPTS[$((i - 1))]}
  done

  D=$OUT/nginx/$e
  fresh nginx "$D"
  step nginx "$D" index "" ctadl index -f facts --engine "$e" -j "$J"
  step nginx "$D" query "n=\$(grep 'forward source-slice' query.out | awk '{print \$1}'); echo \"forward source-slice: \$n instructions (check: > 10)\"" \
    env XDG_DATA_HOME="$D" ctadl query --engine "$e" -j "$J" --dl "$CTADL/tests/query_nginx.dl" --compute-slice fwd --format summary

  D=$OUT/ac4/$e
  fresh ac4 "$D"
  step ac4 "$D" index "" ctadl index -f facts --engine "$e" -j "$J"
  step ac4 "$D" query "jq -r '\"C0001 results: \" + ([.runs[].results[] | select(.ruleId == \"C0001\")] | length | tostring)' query.out" \
    ctadl query "$CTADL/tests/androidtest-query.json" --engine "$e" -j "$J" --format=sarif
  step ac4 "$D" ctadlir "tail -n 1 ctadlir.out" python3 "$CTADL/tests/test_ctadlir.py" "$CTADL/tests/taint_intent.json"
  step ac4 "$D" export "echo \$(wc -c < export.gml) bytes" ctadl export --format gml -o export.gml
done

compare() { # LABEL CMD...: CMD's lines but the renaming counts, indented
  echo "$1:"
  shift
  "$@" -q 2>&1 | grep -v "^renamed" | sed "s/^/  /"
}
S=${ENGINES[0]} F=${ENGINES[1]}
compare "curl, $S vs $F" python3 "$HERE/compare_pcode.py" "$OUT/curl/$S/ctadlir.db" "$OUT/curl/$F/ctadlir.db"
echo "curl SARIF, $S vs $F: $(python3 "$HERE/compare_sarif.py" "$OUT/curl/$S/query.out" "$OUT/curl/$F/query.out" 2>&1 | tail -n 1)"
for i in 1 2 3; do
  compare "curl ${OPTS[$((i - 1))]}, $S vs $F" python3 "$HERE/compare_pcode.py" "$OUT/curl/$S-opt$i/ctadlir.db" "$OUT/curl/$F-opt$i/ctadlir.db"
done
compare "nginx, $S vs $F" python3 "$HERE/compare_pcode.py" "$OUT/nginx/$S/ctadlir.db" "$OUT/nginx/$F/ctadlir.db"
if cmp -s "$OUT/nginx/$S/query.out" "$OUT/nginx/$F/query.out"; then r=equal; else r=different; fi
echo "nginx summary, $S vs $F: $r"
compare "ac4, $S vs $F" python3 "$HERE/compare_jadx.py" "$OUT/ac4/$S/ctadlir.db" "$OUT/ac4/$F/ctadlir.db"
A=$OUT/ac4
for e in "$S" "$F"; do
  python3 "$HERE/compare_jadx.py" canon "$A/$e/ctadlir.db" "$A/$e/query.out" > "$A/$e.sarif" 2> /dev/null
  python3 "$HERE/compare_jadx.py" canon "$A/$e/ctadlir.db" "$A/$e/export.gml" > "$A/$e.gml" 2> /dev/null
done
echo "ac4 SARIF, $S vs $F: $(python3 "$HERE/compare_sarif.py" "$A/$S.sarif" "$A/$F.sarif" 2>&1 | tail -n 1)"
echo "ac4 GML, $S vs $F: $(python3 "$HERE/compare_gml.py" "$A/$S.gml" "$A/$F.gml" 2>&1 | tail -n 1)"
