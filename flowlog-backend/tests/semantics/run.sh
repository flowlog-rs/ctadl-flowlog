#!/bin/bash
# Differential semantics suite for souffle2flowlog.py and ctadl_udf.rs.
#   cases/*.dl    Soufflé and the port (FlowLog with 1 and 4 workers) must
#                 write the same rows to every output relation.
#   rejects/*.dl  the translator must refuse them with an error message.
#   invalid/*.dl  Soufflé must reject them as erroneous, and so must the
#                 translator (with an error message).
#   aborts/*.dl   Soufflé must stop with an error, and so must the FlowLog
#                 program (with 1 and 4 workers): the check rules for
#                 to_number, / and % raise Soufflé's complaint.
#   warns/*.dl    Soufflé must stop with an error on a partial call the
#                 check rules cannot cover, and the translator must list
#                 every line marked `// unchecked` in its warning.
# A `// expect: <text>` line in a reject or an invalid program names text
# the translator's error message must contain; a `// expect-fl: <text>` line
# in a case names text its translation must contain.
# Needs a 64-bit-domain `souffle` on PATH (CTADL requires one).
#   [WORKERS="1 4 32"] tests/semantics/run.sh [name...]
# (default: every case, with 1 and 4 workers)
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=$(dirname "$(dirname "$HERE")")
TOP=$(dirname "$(dirname "$PORT")") # the workspace: the CTADL checkout's parent
FLC=${FLOWLOG_COMPILER:-$TOP/flowlog/target/release/flowlog-compiler}
export FLOWLOG_RUNTIME_PATH=${FLOWLOG_RUNTIME_PATH:-$TOP/flowlog/flowlog-runtime}
export CARGO_INCREMENTAL=${CARGO_INCREMENTAL:-0}
TARGET=${FLOWLOG_CARGO_TARGET:-$TOP/fltarget}
WORKERS=${WORKERS:-1 4}
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
status=0

selected() {
  [ $# -eq 1 ] && return 0
  local n=$1; shift
  for s in "$@"; do [ "$s" = "$n" ] && return 0; done
  return 1
}

# translate and compile $1 into $2/bin; a case's choice-domain policy is
# read from <case>.policy.json next to it
build() {
  local pol=()
  [ -f "${1%.dl}.policy.json" ] && pol=(--policy "${1%.dl}.policy.json")
  python3 "$PORT/souffle2flowlog.py" "$1" -o "$2/p.fl" "${pol[@]}" > "$2/translate.log" 2>&1 \
    || { echo "  translation failed:"; sed 's/^/    /' "$2/translate.log"; return 1; }
  "$FLC" "$2/p.fl" --str-intern --udf-file "$PORT/ctadl_udf.rs" -o "$2/bin" -B "$2/build" \
    -T "$TARGET" > "$2/compile.log" 2>&1 \
    || { echo "  compilation failed:"; grep -m5 -A3 'error' "$2/compile.log" | sed 's/^/    /'; return 1; }
}

describe_exit() {
  case $1 in
    0) echo "exit 0" ;;
    124) echo "TIMEOUT" ;;
    13[0-9]|14[0-9]|15[0-9]) echo "signal $(kill -l $(($1 - 128)))" ;;
    *) echo "exit $1" ;;
  esac
}

# the text a `// expect:` line asks of the error message (empty: any)
expected() {
  sed -n 's|^// expect: ||p' "$1" | head -1
}

for f in "$HERE"/cases/*.dl; do
  n=$(basename "$f" .dl)
  selected "$n" "$@" || continue
  d="$W/$n"; mkdir -p "$d/souffle"
  echo "case $n"
  souffle -D "$d/souffle" "$f" > "$d/souffle.log" 2>&1 \
    || { echo "  Soufflé failed:"; sed 's/^/    /' "$d/souffle.log"; status=1; continue; }
  build "$f" "$d" || { status=1; continue; }
  while IFS= read -r want; do
    grep -qF -- "$want" "$d/p.fl" || { echo "  translation lacks: $want"; status=1; }
  done < <(sed -n 's|^// expect-fl: ||p' "$f")
  for w in $WORKERS; do
    mkdir -p "$d/out$w"
    timeout 300 "$d/bin" -D "$d/out$w" -w $w > "$d/run$w.log" 2>&1 \
      || { echo "  -w $w: $(describe_exit $?)"; tail -5 "$d/run$w.log" | sed 's/^/    /'; status=1; continue; }
    bad=()
    rows=0
    for s in "$d"/souffle/*.csv; do
      r=$(basename "$s")
      o="$d/out$w/$r"
      [ -f "$o" ] || o=/dev/null
      if [ "$(LC_ALL=C sort "$s")" != "$(LC_ALL=C sort "$o")" ]; then
        bad+=("${r%.csv}")
        diff <(LC_ALL=C sort "$s") <(LC_ALL=C sort "$o") | head -10 | sed "s/^/    ${r%.csv} /" > "$d/diff$w.${r%.csv}"
      fi
      rows=$((rows + $(wc -l < "$s")))
    done
    if [ ${#bad[@]} -eq 0 ]; then
      echo "  -w $w: same as Soufflé ($(ls "$d"/souffle/*.csv | wc -l) relations, $rows rows)"
    else
      echo "  -w $w: DIFFERENT in ${bad[*]} (< Soufflé, > FlowLog)"
      cat "$d"/diff$w.* 
      status=1
    fi
  done
done

for f in "$HERE"/rejects/*.dl; do
  n=$(basename "$f" .dl)
  selected "$n" "$@" || continue
  python3 "$PORT/souffle2flowlog.py" "$f" -o "$W/reject.fl" > "$W/reject.log" 2>&1
  rc=$?
  msg=$(grep -v '^$' "$W/reject.log" | tail -1)
  want=$(expected "$f")
  if [ $rc -eq 1 ] && [[ $msg == error:* ]] && [[ $msg == *"$want"* ]]; then
    echo "reject $n: ok, $msg"
  else
    echo "reject $n: NOT REJECTED CLEANLY (exit $rc): $msg${want:+ (expected: $want)}"
    status=1
  fi
done

for f in "$HERE"/invalid/*.dl; do
  n=$(basename "$f" .dl)
  selected "$n" "$@" || continue
  d="$W/$n"; mkdir -p "$d/souffle"
  timeout 60 souffle -D "$d/souffle" "$f" > "$d/souffle.log" 2>&1
  rc=$?
  smsg=$(grep -m1 '^Error' "$d/souffle.log")
  python3 "$PORT/souffle2flowlog.py" "$f" -o "$d/p.fl" > "$d/translate.log" 2>&1
  trc=$?
  msg=$(grep -v '^$' "$d/translate.log" | tail -1)
  want=$(expected "$f")
  if [ $rc -eq 0 ] || [ $rc -eq 124 ] || [ -z "$smsg" ]; then
    echo "invalid $n: Soufflé DID NOT REJECT IT ($(describe_exit $rc))"
    status=1
  elif [ $trc -eq 1 ] && [[ $msg == error:* ]] && [[ $msg == *"$want"* ]]; then
    echo "invalid $n: ok, $msg; Soufflé: ${smsg/ in file $f/}"
  else
    echo "invalid $n: NOT REJECTED CLEANLY (exit $trc): $msg${want:+ (expected: $want)}"
    status=1
  fi
done

for f in "$HERE"/aborts/*.dl; do
  n=$(basename "$f" .dl)
  selected "$n" "$@" || continue
  d="$W/$n"; mkdir -p "$d/souffle"
  echo "abort $n"
  timeout 60 souffle -D "$d/souffle" "$f" > "$d/souffle.log" 2>&1
  rc=$?
  echo "  Soufflé: $(describe_exit $rc), $(grep -m1 -v '^$' "$d/souffle.log" || echo 'no message'); O = {$(cat "$d/souffle/O.csv" 2>/dev/null | tr '\n' ' ')}"
  if [ $rc -eq 0 ] || [ $rc -eq 124 ]; then
    echo "  Soufflé DID NOT STOP"
    status=1
  fi
  build "$f" "$d" || { status=1; continue; }
  for w in $WORKERS; do
    mkdir -p "$d/out$w"
    timeout 60 "$d/bin" -D "$d/out$w" -w $w > "$d/run$w.log" 2>&1
    rc=$?
    msg=$(grep -m1 -E 'error|panicked' "$d/run$w.log" || echo 'no message')
    if [ $rc -ne 0 ] && [ $rc -ne 124 ]; then
      echo "  -w $w: ok, $(describe_exit $rc), $msg"
    else
      echo "  -w $w: DID NOT ABORT ($(describe_exit $rc)), $msg"
      status=1
    fi
  done
done

for f in "$HERE"/warns/*.dl; do
  n=$(basename "$f" .dl)
  selected "$n" "$@" || continue
  d="$W/$n"; mkdir -p "$d/souffle"
  timeout 60 souffle -D "$d/souffle" "$f" > "$d/souffle.log" 2>&1
  rc=$?
  python3 "$PORT/souffle2flowlog.py" "$f" -o "$d/p.fl" > "$d/translate.log" 2>&1
  trc=$?
  msg=$(grep -m1 'partial calls are not fully checked' "$d/translate.log")
  miss=()
  for l in $(grep -n '// unchecked' "$f" | cut -d: -f1); do
    [[ $msg == *"line $l: "* ]] || miss+=("$l")
  done
  if [ $rc -eq 0 ] || [ $rc -eq 124 ]; then
    echo "warn $n: Soufflé DID NOT STOP ($(describe_exit $rc))"
    status=1
  elif [ $trc -ne 0 ]; then
    echo "warn $n: translation failed:"; sed 's/^/    /' "$d/translate.log"
    status=1
  elif [ ${#miss[@]} -gt 0 ]; then
    echo "warn $n: NOT WARNED about line(s) ${miss[*]}: ${msg:-no warning}"
    status=1
  else
    echo "warn $n: ok, Soufflé $(describe_exit $rc); ${msg#warning: }"
  fi
done

[ $status -eq 0 ] && echo "all passed" || echo "FAILURES"
exit $status
