#!/bin/bash
# Native shared SQLite contract, plus the existing text facts adapter checks.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=$(cd "$HERE/../../../src/ctadl/flowlog" && pwd)
cd "$HERE/../../.."
python3 -m unittest tests.test_flowlog_runtime_cache
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT

cat > "$W/comp.dl" <<'EOF'
.type R = [a: number, b: symbol]
.comp Inner {
  .decl A(x: number)
  .input A
  .decl O(x: number)
  .output O
  O(x) :- A(x).
  .decl Z(x: number)
  .output Z
  .decl F(x: number)
  .output F(filename="sub/f.csv")
  F(x) :- A(x).
  .decl Rec(r: R)
  .input Rec
  .decl RO(x: number, s: symbol)
  .output RO
  RO(a, b) :- Rec([a, b]).
  .decl RR(r: R, s: symbol)
  .output RR
  RR(r, b) :- Rec(r), r = [_, b].
}
.comp Outer {
  .init in = Inner
}
.init i = Inner
.init o = Outer
.type One = [v: number]
.decl Single(r: One)
.input Single
.decl SO(v: number)
.output SO
SO(v) :- Single([v]).
.decl U(x: number)
.input U
.type N = [r: R, c: number]
.decl Nest(n: N)
.input Nest
.decl NO(a: number, b: symbol, c: number)
.output NO
NO(a, b, c) :- Nest([[a, b], c]).
.decl NR(n: N)
.output NR(filename="sub/nr.tsv")
NR(n) :- Nest(n).
.decl T()
.output T
T() :- Single([5]).
.decl TF()
.output TF(filename="sub/tf.csv")
TF() :- Single([6]).
EOF
mkdir -p "$W/facts" "$W/sout/sub"
echo 1 > "$W/facts/i.A.facts"
echo 2 > "$W/facts/o.in.A.facts"
# record literals as Soufflé reads them: quoted symbols with escapes,
# unquoted ones up to `,` or `]` (spaces kept), spaces around the rest; a
# line ends at a newline only (the carriage return in "c<CR>d" is part of
# the symbol), and the carriage return of a CRLF line end is dropped
printf '%s\n' '[1, "x"]' '[ 3 ,  s ]' '[4, "q\"r"]' '[6, ]' '[7,"a,b" ]' > "$W/facts/i.Rec.facts"
printf '[8, "c\rd"]\r\n' >> "$W/facts/i.Rec.facts"
echo '[2, "y"]' > "$W/facts/o.in.Rec.facts"
echo '[5]' > "$W/facts/Single.facts"
echo 9 > "$W/facts/U.facts"
printf '%s\n' '[[4, q], 5]' '[ [ 8 , "t u" ] , 9 ]' > "$W/facts/Nest.facts"
python3 "$PORT/souffle2flowlog.py" "$W/comp.dl" -o "$W/comp.fl" --schema "$W/comp.json" > /dev/null
souffle --wno=no-rules-nor-facts -F "$W/facts" -D "$W/sout" "$W/comp.dl"

python3 - "$PORT" "$W" <<'EOF'
import json, sys
sys.path.insert(0, sys.argv[1])
W = sys.argv[2]
fails = 0
def check(what, ok):
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    fails += not ok
# facts.py on comp.dl
import os, shutil
from pathlib import Path
import facts as F
W = Path(W)
files = json.loads((W / "comp.json").read_text())["files"]
check("the manifest lists the file inputs of each component instance",
      sorted(e["name"] for e in files["inputs"]) == ["Nest", "Single", "U", "i.A", "i.Rec", "o.in.A", "o.in.Rec"])
F.convert(files["inputs"], W / "facts", W / "in")

# a file's lines, as Soufflé and FlowLog read them: ending at a newline only
def lines(p):
    with p.open(newline="\n") as f:
        return sorted(f)

check("inputs are linked, or converted where they have record columns",
      (W / "in/i.A.facts").is_symlink() and (W / "in/o.in.Rec.facts").read_text() == "2\ty\n"
      and (W / "in/Single.facts").read_text() == "5\n")
# RO and NO project the records on their fields
check("records are read as Soufflé reads them, also nested ones",
      lines(W / "in/i.Rec.facts") == lines(W / "sout/i.RO.csv") and len(lines(W / "sout/i.RO.csv")) == 6
      and "8\tc\rd\n" in lines(W / "sout/i.RO.csv")
      and lines(W / "in/Nest.facts") == lines(W / "sout/NO.csv") and len(lines(W / "sout/NO.csv")) == 2)
try:
    F.convert([{"name": "R", "file": "i.Rec.facts", "columns": [
        {"attr": "r", "kind": "record", "fl": ["r_a"], "fl_kinds": ["number"], "format": "[{}]"}]}],
        W / "facts", W / "in3")
    check("a malformed record fails", False)
except ValueError as e:
    check(f"a malformed record fails: {e}", "i.Rec.facts:1: expected ']'" in str(e))
check("an input no rule reads is created empty", (W / "in/U.facts").read_text() == "")
(W / "facts/o.in.A.facts").unlink()
F.convert(files["inputs"], W / "facts", W / "in2")
check("a missing fact file is left out",
      not (W / "in2/o.in.A.facts").exists() and (W / "in2/i.A.facts").exists())
# FlowLog writes the files Souffle wrote, with record columns flattened, and
# a nullary fact as `True`, but none for the outputs nothing derives (i.Z,
# o.in.Z), which it prunes
sout, out = W / "sout", W / "out"
for p in sout.rglob("*"):
    if p.is_file() and p.name not in ("i.Z.csv", "o.in.Z.csv"):
        (out / p.relative_to(sout)).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(p, out / p.relative_to(sout))
for i in ("i", "o.in"):
    rows = [r.rstrip("\n") for r in lines(W / f"in/{i}.Rec.facts")]
    (out / f"{i}.RR.csv").write_text("".join(f"{r}\t{r.split(chr(9))[1]}\n" for r in rows))
shutil.copy(W / "in/Nest.facts", out / "sub/nr.tsv")
(out / "T.csv").write_text("True\n")
(W / "cwd").mkdir()
os.chdir(W / "cwd")
F.place_outputs(files["outputs"], out)
# FlowLog and Soufflé order rows differently
tree = lambda d: {str(p.relative_to(d)): lines(p) for p in d.rglob("*") if p.is_file()}
check("output files are placed as Souffle writes them, also the pruned and nullary ones",
      tree(W / "cwd") == tree(sout) and len(tree(sout)) == 14
      and tree(sout)["i.Z.csv"] == [] and tree(sout)["sub/f.csv"] == ["2\n"]
      and tree(sout)["T.csv"] == ["()\n"] and tree(sout)["sub/tf.csv"] == []
      and tree(sout)["sub/nr.tsv"] == ["[[4, q], 5]\n", "[[8, t u], 9]\n"]
      and '[4, q"r]\tq"r\n' in tree(sout)["i.RR.csv"] and '[8, c\rd]\tc\rd\n' in tree(sout)["i.RR.csv"])
sys.exit(1 if fails else 0)
EOF
