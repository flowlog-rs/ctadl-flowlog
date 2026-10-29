#!/bin/bash
# Synthetic differential sweep. gen_tnt.py programs, larger than CTADL's tests,
# on which choice-domain ties occur. Per program, the index phase runs with
# Souffle -j 1 (deterministic: the reference), and with Souffle and FlowLog on
# JOBS workers (default 8). The FlowLog and Souffle -j JOBS databases are
# compared with the reference on full rows (compare_db.py), and FlowLog's
# --all-outputs database is checked to be a choice model of the program
# (check_choice_model.py).
#   [JOBS=N] run_sweep.sh [-P PARALLEL] [OUT]   (default OUT: runs/sweep)
# FUNCS and SEEDS select the programs (default "6 8 10 12 15" x "1 2 3 4 5 6").
# One line per program in OUT/results.txt:
#   fN sS | flowlog: D: <relations that differ> W: <relation=keys with another
#   witness> | souffle-jJOBS: D: .. W: .. | model: yes|NO
# KEEP=1 keeps the databases.
HERE=$(cd "$(dirname "$0")" && pwd)
J=${JOBS:-8}
TOP=$(dirname "$(dirname "$HERE")") # the workspace: the CTADL checkout's parent
cd "$TOP" || exit 1

one() {
  local n=$1 s=$2 D=$OUT/f$1_s$2 v m
  local SC=(--schema "$HERE/index_schema.json")
  local CMP=(python3 "$HERE/compare_db.py" "${SC[@]}" --keys VirtualAssign=0,1,2,3,4,5,6 -q)
  rm -rf "$D"
  mkdir -p "$D"
  python3 "$HERE/gen_tnt.py" --funcs "$n" --stmts 12 --seed "$s" --back 0.05 -o "$D/p.tnt" > /dev/null
  for v in s1 sj fl fa; do
    ctadl import taint-front "$D/p.tnt" -o "$D/$v" -f > /dev/null 2>&1 || { echo "f$n s$s | import FAILED"; return; }
  done
  ctadl --directory "$D/s1" index --engine souffle -j 1 > "$D/s1.log" 2>&1 || { echo "f$n s$s | souffle -j1 FAILED"; return; }
  ctadl --directory "$D/sj" index --engine souffle -j "$J" > "$D/sj.log" 2>&1 || { echo "f$n s$s | souffle -j$J FAILED"; return; }
  ctadl --directory "$D/fl" index --engine flowlog -j "$J" > "$D/fl.log" 2>&1 ||
    { echo "f$n s$s | flowlog FAILED"; return; }
  ctadl --directory "$D/fa" index --engine flowlog --all-outputs -j "$J" > "$D/fa.log" 2>&1 ||
    { echo "f$n s$s | flowlog --all-outputs FAILED"; return; }
  "${CMP[@]}" "$D/s1/ctadlir.db" "$D/fl/ctadlir.db" > "$D/flowlog.cmp" 2>&1
  "${CMP[@]}" "$D/s1/ctadlir.db" "$D/sj/ctadlir.db" > "$D/souffle-j$J.cmp" 2>&1
  python3 "$HERE/check_choice_model.py" "$D/fa/index.dl" "$D/fa/facts" "$D/fa/ctadlir.db" "$D/model" \
    -j "$J" "${SC[@]}" > "$D/model.txt" 2>&1
  m=$(grep -q "^choice model: yes" "$D/model.txt" && echo yes || echo NO)
  echo "f$n s$s | flowlog: $(summary "$D/flowlog.cmp") | souffle-j$J: $(summary "$D/souffle-j$J.cmp") | model: $m"
  [ -n "$KEEP" ] || rm -rf "${D:?}"/{s1,sj,fl,fa,model}
}

summary() {
  local d w
  d=$(grep "^DIFF" "$1" | awk '{print $2}' | tr -d ':' | sed 's/^.*\.//' | paste -sd ' ')
  w=$(grep "^WITNESS" "$1" | sed -E 's/^WITNESS +([^:]+):.* ([0-9]+) with another witness/\1=\2/' |
    sed 's/^.*\.//' | paste -sd ' ')
  echo "D: ${d:--} W: ${w:--}"
}

if [ "$1" = --one ]; then
  one "$2" "$3"
  exit
fi
P=6
if [ "$1" = -P ]; then
  P=$2
  shift 2
fi
OUT=$(realpath -m "${1:-runs/sweep}")
export OUT KEEP
mkdir -p "$OUT"
for n in ${FUNCS:-6 8 10 12 15}; do
  for s in ${SEEDS:-1 2 3 4 5 6}; do
    echo "$n $s"
  done
done | xargs -P "$P" -n 2 "$HERE/run_sweep.sh" --one | sort -V > "$OUT/results.txt"
cat "$OUT/results.txt"
r=$OUT/results.txt
echo "programs=$(wc -l < "$r") choice-models=$(grep -c 'model: yes' "$r")" \
  "flowlog-no-diff=$(grep -c 'flowlog: D: - ' "$r") souffle-j$J-no-diff=$(grep -c "souffle-j$J: D: - " "$r")"
