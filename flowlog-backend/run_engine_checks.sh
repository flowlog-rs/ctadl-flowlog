#!/bin/bash
# Checks of `ctadl --engine` that run_taintfront.py does not make. The results
# are Soufflé's when every line says `0 different`, `sarif: equal`, `ok` and
# `same files as Soufflé`, and the analysis compiles once; the exit status is
# 1 otherwise.
#  - Engines mixed: 4 taint-front tests whose query runs on the other engine's
#    index, both ways round, compared with Soufflé for both phases (database
#    and SARIF), and CTADL's checks of the test.
#  - A --dl query fragment with file inputs and outputs (plain, records,
#    nullary, a component instance, a subdirectory, further outputs of a
#    relation, one the query writes to its database) on alias1, run with
#    Soufflé, and twice at the same time with FlowLog, which compile its
#    analysis once. That analysis is then deleted from CTADL's cache, so that
#    each run of this script compiles it again.
#   [JOBS=N] run_engine_checks.sh
# (default: -j 4; runs in runs/engine/; prints a line per check, then the
# fragment's files)
HERE=$(cd "$(dirname "$0")" && pwd)
CTADL=$(dirname "$HERE")
TOP=$(dirname "$CTADL") # the workspace
cd "$TOP" || exit 1
T=$CTADL/tests/taint-front
O=$TOP/runs/engine
J=${JOBS:-4}
S=(--schema "$HERE/index_schema.json" --schema "$HERE/query_schema.json")
rm -rf "$O"
mkdir -p "$O"

prepare() { # DIR TEST INDEX_ENGINE
  local opts=()
  [ -f "$T/$2.options" ] && read -ra opts < "$T/$2.options"
  ctadl import taint-front "$T/$2.tnt" -o "$1" -f &&
    ctadl --directory "$1" index --engine "$3" -f -j "$J" "${opts[@]}"
}
compare_db() { # DB1 DB2
  python3 "$HERE/compare_db.py" "$1" "$2" -q --keys VirtualAssign=0,1,2,3,4,5,6 "${S[@]}" 2>&1 | tail -1
}
compare_sarif() { # SARIF1 SARIF2
  python3 "$HERE/compare_sarif.py" "$1" "$2" 2>&1 | tail -1
}
checks() { # DIR TEST: CTADL's checks of the test, as check-taintfront makes them
  local sarif=$1/$2.sarif
  if [ -f "$T/$2.json" ]; then
    (cd "$1" && python3 "$CTADL/tests/test_ctadlir.py" "$T/$2.json") >> "$1/run.log" 2>&1 &&
      echo "ctadlir ok" || echo "ctadlir FAILED"
  fi
  if [ -f "$T/$2.codeflows" ]; then
    python3 "$CTADL/tests/bin/sarif_has_code_flows.py" "$sarif" >> "$1/run.log" 2>&1 &&
      echo "codeflows ok" || echo "codeflows FAILED"
  fi
  if [ -f "$T/$2.nocodeflows" ]; then
    python3 "$CTADL/tests/bin/sarif_has_code_flows.py" "$sarif" >> "$1/run.log" 2>&1 &&
      echo "nocodeflows FAILED" || echo "nocodeflows ok"
  fi
}

report() {
  for t in alias1 global1 ex3 sanitizer1; do
    for c in souffle-souffle souffle-flowlog flowlog-souffle; do
      d=$O/mixed/$t/$c
      mkdir -p "$d"
      { prepare "$d" "$t" "${c%-*}" &&
        ctadl --directory "$d" query --engine "${c#*-}" -j "$J" --format sarif -o "$d/$t.sarif"; } > "$d/run.log" 2>&1 ||
        echo "$t, index-query $c: FAILED (log: $d/run.log)"
    done
    for c in souffle-flowlog flowlog-souffle; do
      d=$O/mixed/$t/$c
      r=$O/mixed/$t/souffle-souffle
      echo "$t, index-query $c: $(compare_db "$r/ctadlir.db" "$d/ctadlir.db"); $(compare_sarif "$r/$t.sarif" \
        "$d/$t.sarif"); $(checks "$d" "$t" | paste -sd ' ')"
    done
  done

  F=$O/frag
  mkdir -p "$F"
  cat > "$F/frag.dl" << 'END'
.type FragPair = [a: symbol, b: symbol]
.decl FragName(f: symbol, n: symbol)
.output FragName
FragName(f, n) :- CFunction_Name(f, n).
.decl FragRec(p: FragPair, n: number)
.output FragRec(filename="frag_rec.tsv")
.output FragRec(filename="frag/rec.tsv")
FragRec([f, n], strlen(n)) :- CFunction_Name(f, n).
.decl FragVar(v: symbol, n: symbol)
.output FragVar(filename="frag/vars.csv")
FragVar(v, n) :- CVar_Name(v, n).
.comp FragC {
  .decl In(x: symbol)
  .input In
  .decl Out(x: symbol, n: symbol)
  .output Out
  Out(x, n) :- In(x), CFunction_Name(_, n).
}
.init frag = FragC
.decl FragRecIn(p: FragPair)
.input FragRecIn
.decl FragRecOut(a: symbol, b: symbol)
.output FragRecOut
FragRecOut(a, b) :- FragRecIn([a, b]).
.decl FragAny()
.output FragAny
.output FragAny(filename="frag/any.csv")
FragAny() :- CFunction_Name(_, _).
.decl FragNone()
.output FragNone
FragNone() :- CFunction_Name(_, "no such function").
.output IntCInsn_InFunction
END
  for e in souffle flowlog1 flowlog2; do
    d=$F/$e
    mkdir -p "$d"
    prepare "$d" alias1 "${e%[12]}" > "$d/run.log" 2>&1 || echo "--dl fragment, $e: index FAILED (log: $d/run.log)"
    # Soufflé writes nothing into a missing directory
    mkdir -p "$d/frag"
    printf '%s\n' x 'y z' > "$d/frag.In.facts"
    printf '%s\n' '[a, "b c"]' '[ d ,  e ]' '["f\"g", h]' > "$d/FragRecIn.facts"
  done
  query() { # ENGINE DIR
    ctadl --directory "$2" query --engine "$1" -j "$J" --dl "$F/frag.dl" --format sarif -o "$2/q.sarif" \
      >> "$2/run.log" 2>&1 || echo "--dl fragment, $(basename "$2"): query FAILED (log: $2/run.log)"
  }
  query souffle "$F/souffle"
  query flowlog "$F/flowlog1" &
  query flowlog "$F/flowlog2" &
  wait
  files() { # DIR
    (cd "$1" && for f in FragName.csv frag_rec.tsv frag/rec.tsv frag/vars.csv frag.Out.csv FragRecOut.csv \
      FragAny.csv frag/any.csv FragNone.csv IntCInsn_InFunction.csv; do
      if [ -f "$f" ]; then
        echo "== $f"
        sort "$f"
      else
        echo "== $f: MISSING"
      fi
    done)
  }
  files "$F/souffle" > "$F/souffle.txt"
  for e in flowlog1 flowlog2; do
    files "$F/$e" > "$F/$e.txt"
    if cmp -s "$F/souffle.txt" "$F/$e.txt" && ! grep -q MISSING "$F/$e.txt"; then
      same="same files as Soufflé ($(grep -cv '^==' "$F/$e.txt") rows)"
    else
      same="files DIFFER"
    fi
    echo "--dl fragment, $e: $same; $(compare_db "$F/souffle/ctadlir.db" "$F/$e/ctadlir.db"); $(compare_sarif \
      "$F/souffle/q.sarif" "$F/$e/q.sarif")"
  done
  # FlowLog's status line names the analysis in the cache
  keys=$(sed -n "s/^.*compiling FlowLog analysis to '\(.*\)\/analysis'.*$/\1/p" "$F"/flowlog[12]/run.log)
  echo "--dl fragment, FlowLog compilations: $(printf '%s' "$keys" | grep -c .)"
  for k in $keys; do
    target=${CARGO_TARGET_DIR:-$(dirname "$k")/target}
    rm -rf "$k" "$target/release/ctadl_$(basename "$k")"{,.d} \
      "$target"/release/{deps,.fingerprint}/"ctadl_$(basename "$k")"-*
  done
  cat "$F/souffle.txt"
}

report | tee "$O/summary.txt"
! grep -q -e FAILED -e DIFFER -e MISSING "$O/summary.txt" &&
  [ "$(grep -c ', 0 different; sarif: equal' "$O/summary.txt")" = 10 ] &&
  grep -q '^--dl fragment, FlowLog compilations: 1$' "$O/summary.txt"
