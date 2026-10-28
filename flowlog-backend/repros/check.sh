#!/bin/bash
# Compiles (and runs) each FlowLog repro; prints the compiler's first error,
# or the program's outputs. A repro's UDFs, if any, are in <repro>.rs. With
# -t each repro is first rewritten by
# souffle2flowlog.py and compiled as the CTADL driver does (--str-intern,
# UDFs from ctadl_udf.rs plus an identity functor F; choice-domain relations
# use the minchoice policy), which checks the translator's workaround for
# each gap. With -s each repro runs on Soufflé instead, for reference, with
# FlowLog's types int32 and string declared as Soufflé's number and symbol.
#   Usage: repros/check.sh [-t|-s] [prog.dl...]
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=$(dirname "$HERE")
TOP=$(dirname "$(dirname "$PORT")") # the workspace: the CTADL checkout's parent
FLC=${FLOWLOG_COMPILER:-$TOP/flowlog/target/release/flowlog-compiler}
export FLOWLOG_RUNTIME_PATH=${FLOWLOG_RUNTIME_PATH:-$TOP/flowlog/flowlog-runtime}
TARGET=${FLOWLOG_CARGO_TARGET:-$TOP/fltarget}
# the -B dirs are temporary: skip rustc's incremental cache (GBs in $TARGET)
export CARGO_INCREMENTAL=${CARGO_INCREMENTAL:-0}
MODE=
case $1 in -t | -s) MODE=$1; shift ;; esac
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
if [ "$MODE" = -t ]; then
  echo '{"choice": {"C": "minchoice"}}' > "$W/policy.json"
  { cat "$PORT/ctadl_udf.rs"; echo; echo 'pub fn F(a: String) -> String { a }'; } > "$W/udf.rs"
fi
[ $# -gt 0 ] || set -- "$HERE"/*.dl
for p in "$@"; do
  n=$(basename "$p" .dl)
  d=$W/$n
  mkdir -p "$d/out"
  if [ "$MODE" = -s ]; then
    { echo '.type int32 <: number'; echo '.type string <: symbol'; cat "$p"; } > "$d/prog.dl"
    # 2>/dev/null drops bash's "Aborted (core dumped)" notice
    if ! (cd "$d" && souffle -D out prog.dl > log 2>&1) 2>/dev/null; then
      echo "$n: FAILED: $(grep -m1 -iE 'error|fatal' "$d/log")"
      continue
    fi
  else
    flags=()
    [ -f "${p%.dl}.rs" ] && flags=(--udf-file "${p%.dl}.rs")
    if [ "$MODE" = -t ]; then
      if ! python3 "$PORT/souffle2flowlog.py" "$p" -o "$d/prog.dl" --policy "$W/policy.json" > "$d/log" 2>&1; then
        echo "$n: translate FAILED: $(grep -m1 error "$d/log")"
        continue
      fi
      p=$d/prog.dl
      flags=(--str-intern --udf-file "$W/udf.rs")
    fi
    if ! "$FLC" "$p" "${flags[@]}" -o "$d/bin" -B "$d/build" -T "$TARGET" > "$d/log" 2>&1; then
      # for a failed cargo build, report rustc's first error rather than cargo's summary
      msg=$(sed 's/\x1b\[[0-9;]*m//g' "$d/log" | grep -v -E "^error: (cargo build|could not compile)" |
        grep -m1 -E "^(error|Error|bug)|panicked")
      echo "$n: compile FAILED: ${msg:-$(grep -v "^[[:space:]]*$" "$d/log" | tail -1)}"
      continue
    fi
    # 2>/dev/null drops bash's "Aborted (core dumped)" notice; the panic is reported below
    if ! (cd "$d" && ./bin -D out > run.log 2>&1) 2>/dev/null; then
      msg=$(grep -m1 -A1 'panicked' "$d/run.log" | tail -1)
      echo "$n: run FAILED: ${msg:-$(tail -1 "$d/run.log")}"
      continue
    fi
  fi
  res=""
  for f in "$d"/out/*.csv; do
    [ -e "$f" ] && res+=" $(basename "$f" .csv)={$(sort "$f" | paste -sd ' ')}"
  done
  for db in "$d"/out/*.db; do
    [ -e "$db" ] || continue
    for t in $(sqlite3 "$db" "select name from sqlite_master where type in ('table', 'view') and name not like '\\_%' escape '\\'"); do
      res+=" $t={$(sqlite3 -csv "$db" "select * from \"$t\"" | sort | paste -sd ' ')}"
    done
  done
  echo "$n: ok:${res:- (no output)}"
done
