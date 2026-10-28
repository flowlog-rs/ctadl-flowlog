#!/usr/bin/env python3
"""Convert relations between FlowLog's sqlite layout and Souffle's.

FlowLog writes each relation as a plain table, one column per (flattened)
attribute, named after the FlowLog attribute (reserved words get a `_k`
suffix, record attributes are flattened to `attr_field`). Souffle -- and
CTADL's Python tooling, which reads and writes the index database -- uses a
symbol-table layout: `__SymbolTable(id, symbol)`, a shadow table
`_R('0' INTEGER, ...)` holding symbol ids and numbers, and a view `R` that
joins them back under the original attribute names.

Both directions are driven by the schema manifest that souffle2flowlog.py
writes with --schema (its "outputs"/"inputs" entries list the sqlite
relations with their original and FlowLog column names and types).

  to-souffle FLDB OUTDB --schema S.json [--append]
      Copy the manifest's outputs from the FlowLog DB into OUTDB in Souffle's
      layout. Outputs FlowLog did not write (it prunes relations without
      rules) are created empty, as Souffle would. Without --append OUTDB is
      recreated; with it, OUTDB's symbol table is extended and only the
      listed relations are replaced.
  to-flowlog INDB FLDB --schema S.json
      Copy the manifest's sqlite inputs from a Souffle-layout DB (read by
      position through the views, as Souffle does) into a fresh FlowLog DB.
      Inputs the manifest marks `"read": false` (no rule reads them) are
      created empty.

Souffle does not persist record contents; record columns are stored as
synthetic record ids, with the flattened fields kept in `__FlowLogRecord` so
that to-flowlog can restore them. A DB written by Souffle has no such table,
so it can supply record columns only to inputs nothing reads.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Iterable

SQL_TYPE = {"symbol": "TEXT", "number": "INTEGER", "unsigned": "INTEGER", "float": "REAL"}
RECORD_TABLE = "__FlowLogRecord"


def q(name: str) -> str:
    """Quote an identifier the way Souffle's sqlite writer does."""
    return "'" + name.replace("'", "''") + "'"


def dq(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def load_schema(paths: Iterable[str]) -> dict:
    merged: dict = {"outputs": [], "inputs": []}
    for p in paths:
        with open(p) as fh:
            s = json.load(fh)
        for key in ("outputs", "inputs"):
            seen = {e["name"] for e in merged[key]}
            merged[key].extend(e for e in s.get(key, []) if e["name"] not in seen)
    return merged


def existing_relations(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


class SymbolTable:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.execute(f"CREATE TABLE IF NOT EXISTS {q('__SymbolTable')} (id INTEGER PRIMARY KEY, symbol TEXT UNIQUE)")
        self.ids = {s: i for i, s in conn.execute("SELECT id, symbol FROM __SymbolTable")}
        self.next = max(self.ids.values(), default=0) + 1
        self.pending: list = []
        self.records: dict | None = None
        self.next_rec = 1
        self.pending_rec: list = []

    def sym(self, s: str) -> int:
        i = self.ids.get(s)
        if i is None:
            i = self.ids[s] = self.next
            self.next += 1
            self.pending.append((i, s))
        return i

    def rec(self, fields: list) -> int:
        if self.records is None:
            self.conn.execute(f"CREATE TABLE IF NOT EXISTS {dq(RECORD_TABLE)} (id INTEGER PRIMARY KEY, fields TEXT UNIQUE)")
            self.records = {f: i for i, f in self.conn.execute(f"SELECT id, fields FROM {dq(RECORD_TABLE)}")}
            self.next_rec = max(self.records.values(), default=0) + 1
        key = json.dumps(fields, separators=(",", ":"))
        i = self.records.get(key)
        if i is None:
            i = self.records[key] = self.next_rec
            self.next_rec += 1
            self.pending_rec.append((i, key))
        return i

    def flush(self) -> None:
        self.conn.executemany("INSERT INTO __SymbolTable (id, symbol) VALUES (?, ?)", self.pending)
        self.pending = []
        if self.pending_rec:
            self.conn.executemany(f"INSERT INTO {dq(RECORD_TABLE)} (id, fields) VALUES (?, ?)", self.pending_rec)
            self.pending_rec = []


def souffle_view_sql(name: str, cols: list) -> str:
    shadow = "_" + name
    sel, frm, where = [], [q(shadow)], []
    for i, c in enumerate(cols):
        if c["kind"] == "symbol":
            st = f"_symtab_{i}"
            sel.append(f"{q(st)}.symbol AS {q(c['attr'])}")
            frm.append(f"{q('__SymbolTable')} AS {q(st)}")
            where.append(f"{q(shadow)}.{q(str(i))} = {q(st)}.id")
        else:
            sel.append(f"{q(shadow)}.{q(str(i))} AS {q(c['attr'])}")
    sql = f"CREATE VIEW {q(name)} AS SELECT {','.join(sel)} FROM {','.join(frm)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql


def to_souffle(fl_path: str, out_path: str, schema: dict, append: bool = False) -> dict:
    """Returns {relation: row count}."""
    if not append and os.path.exists(out_path):
        os.unlink(out_path)
    fl = sqlite3.connect(fl_path) if os.path.exists(fl_path) else None
    have = existing_relations(fl) if fl is not None else set()
    out = sqlite3.connect(out_path)
    out.execute("PRAGMA journal_mode = OFF")
    out.execute("PRAGMA synchronous = OFF")
    symtab = SymbolTable(out)
    counts: dict = {}
    for rel in schema["outputs"]:
        name, cols = rel["name"], rel["columns"]
        shadow = "_" + name
        out.execute(f"DROP VIEW IF EXISTS {dq(name)}")
        out.execute(f"DROP TABLE IF EXISTS {dq(shadow)}")
        out.execute(f"CREATE TABLE {q(shadow)} ({','.join(q(str(i)) + ' INTEGER' for i in range(len(cols)))})")
        n = 0
        if name in have:
            flcols = [c for col in cols for c in col["fl"]]
            rows = fl.execute(f"SELECT {', '.join(dq(c) for c in flcols)} FROM {dq(name)}")

            def convert(row: tuple) -> list:
                vals, k = [], 0
                for col in cols:
                    width = len(col["fl"])
                    part = row[k:k + width]
                    k += width
                    if col["kind"] == "symbol":
                        vals.append(symtab.sym(part[0]))
                    elif col["kind"] == "record":
                        vals.append(symtab.rec(list(part)))
                    else:
                        vals.append(part[0])
                return vals

            ins = f"INSERT INTO {q(shadow)} VALUES ({','.join('?' * len(cols))})"
            batch: list = []
            for row in rows:
                batch.append(convert(row))
                if len(batch) >= 50000:
                    out.executemany(ins, batch)
                    n += len(batch)
                    batch = []
            out.executemany(ins, batch)
            n += len(batch)
        out.execute(souffle_view_sql(name, cols))
        counts[name] = n
    symtab.flush()
    out.commit()
    out.close()
    return counts


def to_flowlog(in_path: str, fl_path: str, schema: dict) -> dict:
    """Returns {relation: row count}; relations missing from INDB are
    created empty (and reported with count -1)."""
    src = sqlite3.connect(in_path)
    have = existing_relations(src)
    records: dict = {}
    if RECORD_TABLE in have:
        records = {i: json.loads(f) for i, f in src.execute(f"SELECT id, fields FROM {dq(RECORD_TABLE)}")}
    if os.path.exists(fl_path):
        os.unlink(fl_path)
    dst = sqlite3.connect(fl_path)
    dst.execute("PRAGMA journal_mode = OFF")
    dst.execute("PRAGMA synchronous = OFF")
    counts: dict = {}
    for rel in schema["inputs"]:
        name, cols = rel["name"], rel["columns"]
        defs = []
        for col in cols:
            kinds = col.get("fl_kinds", [col["kind"]])
            defs.extend(f"{dq(c)} {SQL_TYPE[k]} NOT NULL" for c, k in zip(col["fl"], kinds))
        dst.execute(f"CREATE TABLE {dq(name)} ({', '.join(defs)})")
        if not rel.get("read", True):
            # no rule reads it (see souffle2flowlog.py's record_io)
            counts[name] = 0
            continue
        if name not in have:
            counts[name] = -1
            continue
        src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({dq(name)})")]
        if len(src_cols) != len(cols):
            raise SystemExit(f"{name}: {len(src_cols)} columns in {in_path}, expected {len(cols)}")

        def convert(row: tuple) -> list:
            vals: list = []
            for col, v in zip(cols, row):
                if col["kind"] == "record":
                    fields = records.get(v)
                    if fields is None:
                        if RECORD_TABLE not in have:
                            raise SystemExit(f"{name}: {in_path} does not store record contents (it has no "
                                             f"{RECORD_TABLE}; Souffle writes record columns as ids that are "
                                             "only meaningful to the process that wrote them)")
                        # nil (0) or a dangling id: the translation has no nil
                        raise SystemExit(f"{name}: record id {v} is nil or unknown in {in_path} "
                                         "(nil records are not supported)")
                    vals.extend(fields)
                else:
                    vals.append(v)
            return vals

        width = sum(len(c["fl"]) for c in cols)
        rows = [convert(r) for r in src.execute(f"SELECT {', '.join(dq(c) for c in src_cols)} FROM {dq(name)}")]
        dst.executemany(f"INSERT INTO {dq(name)} VALUES ({','.join('?' * width)})", rows)
        counts[name] = len(rows)
    dst.commit()
    dst.close()
    return counts


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("to-souffle")
    a.add_argument("fldb")
    a.add_argument("outdb")
    a.add_argument("--schema", action="append", required=True)
    a.add_argument("--append", action="store_true")
    b = sub.add_parser("to-flowlog")
    b.add_argument("indb")
    b.add_argument("fldb")
    b.add_argument("--schema", action="append", required=True)
    args = ap.parse_args(argv)
    schema = load_schema(args.schema)
    if args.cmd == "to-souffle":
        counts = to_souffle(args.fldb, args.outdb, schema, args.append)
    else:
        counts = to_flowlog(args.indb, args.fldb, schema)
        missing = sorted(n for n, c in counts.items() if c < 0)
        if missing:
            print(f"warning: inputs missing from {args.indb} (created empty): {', '.join(missing)}", file=sys.stderr)
    print(f"{args.cmd}: {len(counts)} relations, {sum(max(c, 0) for c in counts.values())} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
