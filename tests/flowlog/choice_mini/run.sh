#!/bin/bash
# Regression test for the recursive choice-domain emulation (min-lattice of
# choice keys + choice_claim UDFs): translates choice_mini.dl with
# ctadl.flowlog's souffle2flowlog.py, compiles it, runs it with 1 and 4 workers (or $WORKERS)
# and compares the outputs with expected/ (FlowLog's choices, which must not
# depend on the worker count) and with souffle/ (`souffle -F facts -D souffle
# choice_mini.dl`).
# Soufflé differs only where two candidates for a key are derived in the same
# round by the same clause (Reach `c`, round 2, and `t`, round 3): both are
# valid choice models, Soufflé breaks such ties by B-tree order, FlowLog by hash.
# FlowLog is found as by `ctadl --engine flowlog` (see ctadl.flowlog):
# $FLOWLOG_COMPILER or flowlog-compiler on PATH, and $FLOWLOG_RUNTIME_PATH
# if set; Rust builds go to $CARGO_TARGET_DIR (default:
# ctadl-test-output/flowlog-target).
#   [WORKERS="1 4 32"] tests/flowlog/choice_mini/run.sh
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd) # the CTADL checkout
PORT=$ROOT/src/ctadl/flowlog
FLC=${FLOWLOG_COMPILER:-flowlog-compiler}
TARGET=${CARGO_TARGET_DIR:-$ROOT/ctadl-test-output/flowlog-target}
# flowlog-compiler names the crate after the executable, and cargo can take
# a build of another crate of that name in $TARGET for this one's: each run
# compiles under a name of its own (FlowLog finding B14), and removes that
# crate's build products from $TARGET when it exits (named after $BIN, which
# FlowLog's fix, flowlog-rs/flowlog#507, suffixes with a hash)
BIN=bin_$$
WORKERS=${WORKERS:-1 4}
W=$(mktemp -d)
trap 'rm -rf "$W" "$TARGET"/release/{,deps/,.fingerprint/}"$BIN"{,.d,-*,_*}' EXIT

python3 "$PORT/souffle2flowlog.py" "$HERE/choice_mini.dl" -o "$W/choice_mini.fl" \
  --policy "$HERE/policy.json" > "$W/translate.log" 2>&1 || { cat "$W/translate.log"; exit 1; }
"$FLC" "$W/choice_mini.fl" --str-intern --udf-file "$PORT/souffle2flowlog.rs" \
  -o "$W/$BIN" -T "$TARGET" > "$W/compile.log" 2>&1 || { tail -30 "$W/compile.log"; exit 1; }
mv "$W/$BIN" "$W/bin"
for w in $WORKERS; do
  mkdir -p "$W/out$w"
  "$W/bin" -F "$HERE/facts" -D "$W/out$w" -w $w > "$W/run$w.log" 2>&1 || { cat "$W/run$w.log"; exit 1; }
done

status=0
for r in Reach NR FwOut BwOut; do
  line="$r:"
  for w in $WORKERS; do
    if [ "$(sort "$W/out$w/$r.csv")" = "$(cat "$HERE/expected/$r.csv")" ]; then
      line+=" -w$w ok"
    else
      line+=" -w$w DIFFERENT"
      status=1
    fi
  done
  first=${WORKERS%% *}
  if [ "$(sort "$W/out$first/$r.csv")" = "$(cat "$HERE/souffle/$r.csv")" ]; then
    line+=" (= Souffle)"
  else
    line+=" (Souffle: same-round tie broken differently)"
  fi
  echo "$line"
done
exit $status
