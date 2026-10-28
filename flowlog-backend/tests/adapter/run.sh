#!/bin/bash
# fl_db_adapter.py on record columns, with a DB that Soufflé itself writes.
# Soufflé stores a record column as a record id that only the writing
# process can decode, so:
#   - an sqlite input that no rule reads is marked "read": false by the
#     translator and created empty, even if its record ids are undecodable;
#   - an input that a rule reads fails with an explanation;
#   - record columns the port writes (to-souffle) still round-trip, and a
#     nil (or dangling) record id fails;
#   - an empty record relation needs no record table, both ways;
#   - without FlowLog's DB, to-souffle creates every output empty.
# Needs `souffle` on PATH.   tests/adapter/run.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=$(dirname "$(dirname "$HERE")")
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

python3 - "$PORT" "$W" <<'EOF'
import sqlite3, sys
sys.path.insert(0, sys.argv[1])
import fl_db_adapter as A
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
except SystemExit as e:
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
except SystemExit as e:
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
sys.exit(1 if fails else 0)
EOF
