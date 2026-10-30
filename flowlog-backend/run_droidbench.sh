#!/bin/bash
# Android apps with both engines, compared: each app is imported with JADX,
# indexed, queried with CTADL's tests/androidtest-query.json (DroidBench's
# leak: TelephonyManager.getDeviceId to Log.i) and exported to GML, as
# tests/bin/check-ActivityCommunication4 does, by each engine in a copy.
#   [JOBS=N] [P=N] run_droidbench.sh OUT APK...
# The first app runs alone (it compiles the analyses the first time), the
# others P at a time (default 8), each engine with JOBS workers (default 4).
# OUT/<app> holds the import, each engine's run and cmp.txt; prints a line per
# app: the SARIF results of each engine, then compare_jadx.py's summary and
# the comparisons of the SARIF reports and GML exports (compare_gml.py), with
# the ord-based ids renamed, or the step at which each engine failed and the
# relation it could not load; then a tally. Needs `ctadl` set up for FlowLog
# (README, Setup) and CTADL's JADX plugin.
HERE=$(cd "$(dirname "$0")" && pwd)
CTADL=$(dirname "$HERE")
if [ $# -lt 2 ]; then
  sed -n 2,14p "$0" >&2
  exit 2
fi
mkdir -p "$1" || exit 1
OUT=$(cd "$1" && pwd)
shift
export OUT HERE CTADL J=${JOBS:-4}

why() { # LOG: " (cannot load R)" if the engine's log says it cannot load relation R
  local rel
  # shellcheck disable=SC2016 # the backquotes are the log's
  rel=$(sed -n 's/.*Error loading \([^ ]*\) data.*/\1/p; s/.*relation `\([^`]*\)` from file.*/\1/p' "$1" | head -n 1)
  [ -z "$rel" ] || echo " (cannot load $rel)"
}

app() { # APK: the app's runs and comparisons, and its line
  local apk=$1 name d e r s f
  name=$(basename "$apk" .apk)
  d=$OUT/$name
  rm -rf "$d" && mkdir -p "$d" && cd "$d" || return
  if ! ctadl import JADX "$apk" -o facts -f > import.log 2>&1; then
    echo "$name: import failed"
    return
  fi
  for e in souffle flowlog; do
    mkdir "$e" && cp -r facts "$e/facts"
    r+=$(
      cd "$e" || exit
      if ! ctadl index -f facts --engine "$e" -j "$J" > index.log 2>&1; then
        echo " $e: index failed$(why index.log);"
      elif ! ctadl query "$CTADL/tests/androidtest-query.json" --engine "$e" -j "$J" --format=sarif > query.sarif 2> query.log; then
        echo " $e: query failed$(why query.log);"
      elif ! ctadl export --format gml -o export.gml > export.log 2>&1; then
        echo " $e: export failed;"
      fi
    )
  done
  if [ -n "$r" ]; then
    echo "$name:$r"
    return
  fi
  python3 "$HERE/compare_jadx.py" souffle/ctadlir.db flowlog/ctadlir.db -q > cmp.txt 2>&1
  for e in souffle flowlog; do
    python3 "$HERE/compare_jadx.py" canon "$e/ctadlir.db" "$e/query.sarif" > "$e.sarif" 2>> cmp.txt
    python3 "$HERE/compare_jadx.py" canon "$e/ctadlir.db" "$e/export.gml" > "$e.gml" 2>> cmp.txt
  done
  s=$(python3 "$HERE/compare_sarif.py" souffle.sarif flowlog.sarif 2>&1 | tail -n 1)
  f=$(python3 "$HERE/compare_gml.py" souffle.gml flowlog.gml 2>&1 | tail -n 1)
  [[ $f == "gml: equal ("* ]] && f="gml: equal"
  echo "$name: results $(jq '[.runs[].results[]] | length' souffle/query.sarif)/$(jq '[.runs[].results[]] | length' flowlog/query.sarif)," \
    "$(grep '^summary' cmp.txt), $s, $f"
}
export -f why app

APKS=()
for a in "$@"; do APKS+=("$(realpath "$a")"); done
{
  (app "${APKS[0]}")
  if [ ${#APKS[@]} -gt 1 ]; then
    # shellcheck disable=SC2016 # the inner shell expands $1
    printf "%s\n" "${APKS[@]:1}" | xargs -d '\n' -P "${P:-8}" -I {} bash -c 'app "$1"' _ {}
  fi
} | tee "$OUT/summary.txt"
echo "apps: $(grep -c "" "$OUT/summary.txt");" \
  "identical (DB, SARIF, GML): $(grep -c "0 different, sarif: equal, gml: equal$" "$OUT/summary.txt");" \
  "identical but for the GML's row order: $(grep -c "0 different, sarif: equal, gml: equal up to row order" "$OUT/summary.txt");" \
  "failed alike with both engines: $(grep -cE ": souffle: ([a-z]+ failed[^;]*); flowlog: \1;$" "$OUT/summary.txt")"
