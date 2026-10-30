#!/bin/bash
# Differential runs (Souffle vs FlowLog) of CTADL's taint-front tests and the
# ctadl/taint-front corpus under non-default index/query options. For
# taint-front these options only change the runtime CTADLConfig_Input facts
# (the preprocessed programs are byte-identical), so they reuse the compiled
# analyses but enable rules the default configuration never fires; `allout`
# (--all-outputs, CTADL's ALL_OUTPUTS macro) changes the index program, which
# both backends compile once more.
#   [JOBS=N] run_variants.sh [VARIANT...]
# (default: all variants, with -j 4; results in runs/variants/)
HERE=$(cd "$(dirname "$0")" && pwd)
CTADL=$(dirname "$HERE")
TOP=$(dirname "$CTADL") # the workspace
cd "$TOP" || exit 1
declare -A V=(
  [objtrack]="--index-args=--object-tracking"
  [kctx2]="--index-args=--hybrid-inlining-context-bound=2"
  [nohi]="--index-args=--no-hybrid-inlining"
  [dynap3]="--index-args=--dynamic-access-paths-max-length=3"
  [nointer]="--index-args=--no-interprocedural-data-flow"
  [unbounded]="--index-args=--no-hybrid-inlining-inlining-bound"
  [parthians]="--index-args=--hybrid-inlining-add-parthians-to-vertex"
  [star]="--index-args=--star --match-star-fields"
  [cha]="--index-args=--cha"
  [qall]="--query-args=--compute-slices=all --format=sarif+all"
  [allout]="--index-args=--all-outputs"
)
[ $# -gt 0 ] || set -- "${!V[@]}"
S=(--schema "$HERE/index_schema.json" --schema "$HERE/query_schema.json" --prune -j "${JOBS:-4}")
for v in "$@"; do
  out=runs/variants/$v
  mkdir -p "$out"
  (
    python3 "$HERE/run_taintfront.py" "${S[@]}" "${V[$v]}" --out "$out/tests"
    python3 "$HERE/run_taintfront.py" "${S[@]}" "${V[$v]}" --out "$out/corpus" --testdir "$CTADL/taint-front"
  ) > "$out/summary.txt" 2>&1 &
done
wait
# equal: identical databases (some up to choice-domain witnesses) and SARIF;
# checks-failed: CTADL's expectations, written for the default options, fail
# (on both backends alike when the line is also equal)
for v in "$@"; do
  f=runs/variants/$v/summary.txt
  printf "%-10s equal=%-3s witness=%-3s diff=%-3s checks-failed=%-3s other-failed=%s\n" "$v" \
    "$(grep -c ', 0 different; sarif: equal' "$f")" \
    "$(grep -c 'up to choice-domain witnesses' "$f")" \
    "$(grep 'diff:' "$f" | grep -vc ', 0 different; sarif: equal')" \
    "$(grep -c 'checks FAILED' "$f")" \
    "$(grep 'FAILED' "$f" | grep -vc 'checks FAILED')"
done
