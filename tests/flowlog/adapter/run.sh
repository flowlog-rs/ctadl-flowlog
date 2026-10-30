#!/bin/bash
# ctadl.flowlog's sqlite_layout.py on record columns, with a DB that Soufflé
# itself writes.
# Soufflé stores a record column as a record id that only the writing
# process can decode, so:
#   - an sqlite input that no rule reads is marked "read": false by the
#     translator and created empty, even if its record ids are undecodable;
#   - an input that a rule reads fails with an explanation;
#   - record columns the port writes (to-souffle) still round-trip, and a
#     nil (or dangling) record id fails;
#   - an empty record relation needs no record table, both ways;
#   - without FlowLog's DB, to-souffle creates every output empty;
#   - an input missing from the DB is not created (FlowLog, like Soufflé,
#     fails if it reads it);
#   - an append (to-souffle --append, as to CTADL's index) interrupted by
#     Ctrl-C, or killed, leaves the DB as it was.
# And facts.py, on a program with component instances (Soufflé names their
# relations, and files, `i.A`, `o.in.A`): the inputs get their files in
# FlowLog's layout (record columns flattened, as Soufflé reads them; an
# input no rule reads empty; a missing file left out), and the output files
# of a run are placed where Soufflé writes them, also for the outputs
# FlowLog prunes, with record columns and nullary facts as Soufflé writes
# them.
# Needs `souffle` on PATH.   tests/flowlog/adapter/run.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=$(cd "$HERE/../../../src/ctadl/flowlog" && pwd)
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT

decls='.type R = [id: number, m: symbol]
.decl Call(n: number, info: R)
.decl Name(s: symbol)'
cat > "$W/index.dl" <<EOF
$decls
Call(1, [7, "f"]). Call(2, [8, "g"]).
Name("a"). Name("b").
.output Call(IO=sqlite, filename="db.sqlite")
.output Name(IO=sqlite, filename="db.sqlite")
EOF
for q in unread read; do
  body='Out(s) :- Name(s).'
  [ $q = read ] && body='Out(to_string(n)) :- Call(n, _).'
  cat > "$W/$q.dl" <<EOF
$decls
.input Call(IO=sqlite, filename="db.sqlite")
.input Name(IO=sqlite, filename="db.sqlite")
.decl Out(s: symbol)
$body
.output Out
EOF
  python3 "$PORT/souffle2flowlog.py" "$W/$q.dl" -o "$W/$q.fl" --schema "$W/$q.json" > /dev/null
done
python3 "$PORT/souffle2flowlog.py" "$W/index.dl" -o "$W/index.fl" --schema "$W/index.json" > /dev/null
souffle -D "$W" "$W/index.dl"

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
import json, sqlite3, sys
sys.path.insert(0, sys.argv[1])
import sqlite_layout as A
W = sys.argv[2]
fails = 0

def check(what, ok):
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    fails += not ok

def rows(db, rel):
    return sorted(sqlite3.connect(db).execute(f'SELECT * FROM "{rel}"'))

unread, read = (A.load_schema([f"{W}/{q}.json"]) for q in ("unread", "read"))
flag = {q: {e["name"]: e.get("read", True) for e in s["inputs"]} for q, s in (("unread", unread), ("read", read))}
check("the translator marks Call unread when no rule reads it", flag["unread"] == {"Call": False, "Name": True})
check("and read when a rule does", flag["read"] == {"Call": True, "Name": False})

counts = A.to_flowlog(f"{W}/db.sqlite", f"{W}/fl1.db", unread)
check("Souffle's DB: the unread Call is created empty", counts["Call"] == 0 and rows(f"{W}/fl1.db", "Call") == [])
check("and Name is converted", rows(f"{W}/fl1.db", "Name") == [("a",), ("b",)])
try:
    A.to_flowlog(f"{W}/db.sqlite", f"{W}/fl2.db", read)
    check("Souffle's DB: reading Call's records fails", False)
except ValueError as e:
    check(f"Souffle's DB: reading Call's records fails: {e}", "does not store record contents" in str(e))

# records written by the port (FlowLog layout -> Souffle layout -> back)
out = A.load_schema([f"{W}/index.json"])
fl = sqlite3.connect(f"{W}/fl_out.db")
cols = [c for col in next(e for e in out["outputs"] if e["name"] == "Call")["columns"] for c in col["fl"]]
fl.execute(f'CREATE TABLE Call ({", ".join(cols)})')
fl.executemany("INSERT INTO Call VALUES (?, ?, ?)", [(1, 7, "f"), (2, 8, "g")])
fl.execute("CREATE TABLE Name (s)")
fl.commit()
A.to_souffle(f"{W}/fl_out.db", f"{W}/port.sqlite", out, False)
A.to_flowlog(f"{W}/port.sqlite", f"{W}/fl3.db", read)
check("the port's DB: Call's records round-trip", rows(f"{W}/fl3.db", "Call") == [(1, 7, "f"), (2, 8, "g")])
db = sqlite3.connect(f"{W}/port.sqlite")
db.execute('UPDATE "_Call" SET "1" = 0 WHERE "0" = 2')
db.commit()
try:
    A.to_flowlog(f"{W}/port.sqlite", f"{W}/fl4.db", read)
    check("a nil record id fails", False)
except ValueError as e:
    check(f"a nil record id fails: {e}", "record id 0 is nil or unknown" in str(e))
counts = A.to_souffle(f"{W}/missing.db", f"{W}/empty.sqlite", out, False)
check("without FlowLog's DB, every output is created empty",
      counts == {"Call": 0, "Name": 0} and rows(f"{W}/empty.sqlite", "Call") == [])
fl = sqlite3.connect(f"{W}/fl_norec.db")
fl.execute(f'CREATE TABLE Call ({", ".join(cols)})')
fl.execute("CREATE TABLE Name (s)")
fl.execute("INSERT INTO Name VALUES ('a')")
fl.commit()
counts = A.to_souffle(f"{W}/fl_norec.db", f"{W}/norec.sqlite", out, False)
check("an empty record relation is converted (with no record table)", counts == {"Call": 0, "Name": 1})
counts = A.to_flowlog(f"{W}/norec.sqlite", f"{W}/fl5.db", read)
check("and read back empty", counts["Call"] == 0 and rows(f"{W}/fl5.db", "Call") == [])
sqlite3.connect(f"{W}/none.sqlite").execute("CREATE TABLE T (x)")
counts = A.to_flowlog(f"{W}/none.sqlite", f"{W}/fl6.db", read)
tables = {r[0] for r in sqlite3.connect(f"{W}/fl6.db").execute("SELECT name FROM sqlite_master")}
check("an input missing from the DB is not created (the unread Name still is)",
      counts == {"Call": -1, "Name": 0} and tables == {"Name"})

# an append interrupted after writing (part of) the DB: Ctrl-C rolls it back
# when the connection closes, and after a kill the next connection rolls it
# back from the journal (Soufflé's in-memory one would be lost)
import os, signal
fl = sqlite3.connect(f"{W}/fl_big.db")
fl.execute(f'CREATE TABLE Call ({", ".join(cols)})')
fl.execute("CREATE TABLE Name (s)")
fl.executemany("INSERT INTO Name VALUES (?)", ((f"symbol {i}",) for i in range(20000)))
fl.commit()
fl.close()
A.to_souffle(f"{W}/fl_out.db", f"{W}/idx.sqlite", out, False)
before = list(sqlite3.connect(f"{W}/idx.sqlite").iterdump())

def append_and(stop):
    """Appends fl_big.db's outputs to idx.sqlite, and calls STOP after Name's rows."""
    copy, cache = A.copy_to_souffle, A.CACHE_KIB
    def copy_and_stop(conn, name, cols):
        n = copy(conn, name, cols)
        if name == "Name":
            stop()
        return n
    A.copy_to_souffle = copy_and_stop
    A.CACHE_KIB = 1  # so that SQLite writes the DB before the commit
    try:
        A.to_souffle(f"{W}/fl_big.db", f"{W}/idx.sqlite", out, True)
    finally:
        A.copy_to_souffle, A.CACHE_KIB = copy, cache

def intact(db):
    try:
        c = sqlite3.connect(db, timeout=0)
        return c.execute("PRAGMA integrity_check").fetchone() == ("ok",) and list(c.iterdump()) == before
    except sqlite3.Error as e:  # e.g. still locked
        print(f"  {db}: {e}")
        return False

def interrupt():
    raise KeyboardInterrupt
try:
    append_and(interrupt)
    check("an append is interrupted", False)
except KeyboardInterrupt:
    check("an append interrupted by Ctrl-C leaves the DB as it was", intact(f"{W}/idx.sqlite"))
sys.stdout.flush()
pid = os.fork()
if pid == 0:
    append_and(lambda: os.kill(os.getpid(), signal.SIGKILL))
    os._exit(0)
status = os.waitpid(pid, 0)[1]
check("as does one killed, from the journal it leaves",
      os.WIFSIGNALED(status) and os.path.exists(f"{W}/idx.sqlite-journal") and intact(f"{W}/idx.sqlite"))

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
