#!/bin/bash
# Bug B14: flowlog-compiler names the generated crate after the executable,
# and each crate is a cargo workspace of its own. Two programs compiled with
# the same executable name into one target directory therefore share a cargo
# fingerprint, which is checked by mtime. Here b's crate is written while a's
# builds: whichever builds second is found up to date, and flowlog-compiler
# installs the other program's binary for it.
# Expected: a: O={1} and b: O={2}. With the bug, both print the same.
# FlowLog is found as by check.sh. The builds go to $CARGO_TARGET_DIR, or to
# a scratch target directory (which first builds FlowLog's runtime).
#   Usage: repros/same_name.sh
FLC=${FLOWLOG_COMPILER:-flowlog-compiler}
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
TARGET=${CARGO_TARGET_DIR:-$W/target}
for p in a b; do
  mkdir -p "$W/$p"
  v=$([ $p = a ] && echo 1 || echo 2)
  printf '.decl K(x: int32)\nK(0).\n.decl O(x: int32)\n.output O\nO(%s) :- K(_).\n' $v > "$W/$p/prog.dl"
done
"$FLC" "$W/a/prog.dl" -o "$W/a/bin" -T "$TARGET" > "$W/a/log" 2>&1 &
# compile b once a's crate is written, while a builds
until [ -d "$W/a/.bin.build" ]; do sleep 0.1; done
"$FLC" "$W/b/prog.dl" -o "$W/b/bin" -T "$TARGET" > "$W/b/log" 2>&1
wait
for p in a b; do
  (cd "$W/$p" && ./bin -D out > /dev/null 2>&1)
  echo "$p: O={$(sort "$W/$p/out/O.csv" 2>/dev/null | paste -sd ' ')}"
done
