#!/bin/bash
# Soufflé oracle for `ord` on P-code. The engines number symbols differently,
# and typeprop.dl orders its edges and picks each class's representative
# varnode by `ord`, so CVar_Type and VarnodePointerToAggregate, and on larger
# programs the data-flow IR, depend on the numbering. This runs CTADL's P-code
# index with Soufflé, each `ord(x)` replaced by FlowLog's number for x
# (`@oord(x)`, ord_oracle.cpp), on the facts that FlowLog's index read, and
# compares the database with FlowLog's. Equal relations mean that FlowLog
# computes what Soufflé computes with FlowLog's numbering.
#  - FlowLog's numbering is read from FLOWLOG_DB: pcode_lang.dl records the
#    id it computes for a symbol x as CSourceInfo_Location(x, 1, ord(x)).
#    Varnodes without an address (VNODE_PC_ADDRESS) have no such row; they
#    get numbers past the others (run.log counts them), and typeprop.dl's
#    results depend on those numbers only for varnodes in TypeConstraint_Edge.
#  - ReachableVertex's `ord(cat(...))` stay Soufflé's: nothing reads those ids.
#   [JOBS=N] ord_oracle.sh IMPORT FLOWLOG_DB SCHEMA OUT
# IMPORT is the import directory that `ctadl index --engine flowlog` ran on
# (the index adds model facts to it), FLOWLOG_DB that run's database, before
# any query (whose relations would count as extra), SCHEMA the schema.json of
# the index's FlowLog cache entry, and OUT a directory for the oracle, its
# database and the comparison (compare.txt). OUT/oracle is reused when it
# exists; JOBS defaults to 32. The exit status is compare_db.py's. Expect one
# different relation, CTADLConfig: the oracle's lacks the CTADL_Index_Models
# row, which CTADL's driver adds after the analysis.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
LOGIC=$(dirname "$HERE")/src/ctadl/souffle-logic
if [ $# -ne 4 ]; then
  sed -n 2,25p "$0" >&2
  exit 2
fi
[ -d "$1/facts" ] || { echo "ord_oracle: no fact directory $1/facts" >&2; exit 2; }
for f in "$2" "$3"; do
  [ -f "$f" ] || { echo "ord_oracle: no file $f" >&2; exit 2; }
done
IMPORT=$(cd "$1" && pwd)
DB=$(realpath "$2")
SCHEMA=$(realpath "$3")
mkdir -p "$4"
OUT=$(cd "$4" && pwd)
J=${JOBS:-32}

if [ ! -x "$OUT/oracle" ]; then
  ctadl dump-analysis index --language pcode -o "$OUT/index.dl"
  python3 - "$OUT/index.dl" "$OUT/oracle.dl" <<'EOF'
import re, sys
src = open(sys.argv[1]).read()
ords = re.compile(r"\bord\((?!cat\()")
print(f"ord_oracle: {len(ords.findall(src))} ord calls replaced by @oord", file=sys.stderr)
open(sys.argv[2], "w").write(".functor oord(x: symbol): number\n" + ords.sub("@oord(", src))
EOF
  INC=$(dirname "$(dirname "$(command -v souffle)")")/include
  c++ -O2 --std=c++17 -shared -fPIC -I "$INC" -I "$INC/souffle" \
    "$LOGIC/functors.cpp" "$HERE/ord_oracle.cpp" -o "$OUT/libfunctors.so"
  souffle -j "$J" -L "$OUT" -o "$OUT/oracle" "$OUT/oracle.dl" > "$OUT/compile.log" 2>&1
fi

sqlite3 -separator $'\t' "$DB" \
  "SELECT element, region_id FROM CSourceInfo_Location WHERE file_id = 1" > "$OUT/ords.tsv"
for col in 1 2; do # one number per symbol and one symbol per number, as for ord
  if [ -n "$(cut -f$col "$OUT/ords.tsv" | sort | uniq -d | head -1)" ]; then
    echo "ord_oracle: $DB does not give one number per symbol" >&2
    exit 1
  fi
done
rm -f "$OUT/ctadlir.db"
(cd "$OUT" && OORD_MAP="$OUT/ords.tsv" LD_LIBRARY_PATH="$OUT" ./oracle -j "$J" -F "$IMPORT/facts" 2> run.log)
cat "$OUT/run.log"
python3 "$HERE/compare_db.py" "$OUT/ctadlir.db" "$DB" --schema "$SCHEMA" -q | tee "$OUT/compare.txt"
