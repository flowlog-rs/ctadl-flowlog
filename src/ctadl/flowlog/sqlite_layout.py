#!/usr/bin/env python3
"""Convert relations between FlowLog's sqlite layout and Souffle's.

FlowLog reads and writes each relation as a plain table, one column per
(flattened) attribute, named after the FlowLog attribute (reserved words get
a `_k` suffix, record attributes are flattened to `attr_field`). Souffle --
and CTADL's Python tooling, which reads, indexes and extends the index
database -- uses a symbol-table layout: `__SymbolTable(id, symbol)`, a shadow
table `_R('0' INTEGER, ...)` holding symbol ids and numbers, and a view `R`
that joins them back under the original attribute names.

Both directions are driven by the schema manifest that souffle2flowlog.py
writes with --schema (its "outputs"/"inputs" entries list the sqlite
relations with their original and FlowLog column names and types). Each is a
few INSERT ... SELECT statements per relation, run by SQLite on the two
databases attached to one connection; no rows pass through Python.

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
      created empty; inputs missing from INDB are not (like Souffle, FlowLog
      fails if it reads one).

Souffle does not persist record contents; record columns are stored as
synthetic record ids, with the flattened fields kept in `__FlowLogRecord`
(as a JSON array) so that to-flowlog can restore them. A DB written by
Souffle has no such table, so it can supply record columns only to inputs
nothing reads.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Iterable

SQL_TYPE = {
    "symbol": "TEXT",
    "number": "INTEGER",
    "unsigned": "INTEGER",
    "float": "REAL",
}
SYMBOL_TABLE = "__SymbolTable"
RECORD_TABLE = "__FlowLogRecord"
CACHE_KIB = 1 << 20  # page cache per database (SQLite's default is 2 MiB)


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


def existing_relations(conn: sqlite3.Connection, schema: str = "main") -> set:
    return {
        r[0]
        for r in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type IN ('table', 'view')"
        )
    }


def connect(path: str, attach: str, alias: str) -> sqlite3.Connection:
    """Opens PATH (which is written) with ATTACH attached as ALIAS (which is
    only read), in autocommit mode: the callers make one transaction."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("ATTACH DATABASE ? AS " + alias, (attach,))
    for schema in ("main", alias):
        conn.execute(f"PRAGMA {schema}.cache_size = -{CACHE_KIB}")
    return conn


def record_json(col: dict, alias: str) -> str:
    return f"json_array({', '.join(f'{alias}.{dq(f)}' for f in col['fl'])})"


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
    # Without FlowLog's DB every output is created empty.
    out = connect(out_path, fl_path if os.path.exists(fl_path) else ":memory:", "fl")
    have = existing_relations(out, "fl")
    out.execute("BEGIN")
    out.execute(
        f"CREATE TABLE IF NOT EXISTS {q(SYMBOL_TABLE)} (id INTEGER PRIMARY KEY, symbol TEXT UNIQUE)"
    )
    counts: dict = {}
    for rel in schema["outputs"]:
        name, cols = rel["name"], rel["columns"]
        shadow = "_" + name
        out.execute(f"DROP VIEW IF EXISTS main.{dq(name)}")
        out.execute(f"DROP TABLE IF EXISTS main.{dq(shadow)}")
        out.execute(
            f"CREATE TABLE main.{q(shadow)} ({','.join(q(str(i)) + ' INTEGER' for i in range(len(cols)))})"
        )
        counts[name] = copy_to_souffle(out, name, cols) if name in have else 0
        out.execute(souffle_view_sql(name, cols))
    out.execute("COMMIT")
    out.close()
    return counts


def copy_to_souffle(conn: sqlite3.Connection, name: str, cols: list) -> int:
    """Interns fl.NAME's symbols and records, then fills main._NAME with
    their ids. New ids continue from the largest one, as Souffle's do."""
    src = f"fl.{dq(name)}"
    recs = [c for c in cols if c["kind"] == "record"]
    if recs and not conn.execute(f"SELECT EXISTS (SELECT 1 FROM {src})").fetchone()[0]:
        return 0  # the record table is created with the first record
    syms = [dq(c["fl"][0]) for c in cols if c["kind"] == "symbol"]
    if syms:
        conn.execute(
            f"INSERT OR IGNORE INTO main.{q(SYMBOL_TABLE)} (symbol) "
            + " UNION ALL ".join(f"SELECT {c} FROM {src}" for c in syms)
        )
    if recs:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS main.{dq(RECORD_TABLE)} (id INTEGER PRIMARY KEY, fields TEXT UNIQUE)"
        )
        conn.execute(
            f"INSERT OR IGNORE INTO main.{dq(RECORD_TABLE)} (fields) "
            + " UNION ALL ".join(
                f"SELECT {record_json(c, 'r')} FROM {src} AS r" for c in recs
            )
        )
    # CROSS JOIN keeps FlowLog's table as the outer loop, so that each row
    # makes one lookup in the symbol (record) table's unique index.
    sel, frm, where = [], [f"{src} AS r"], []
    for i, c in enumerate(cols):
        if c["kind"] == "symbol":
            frm.append(f"main.{q(SYMBOL_TABLE)} AS t{i}")
            where.append(f"t{i}.symbol = r.{dq(c['fl'][0])}")
        elif c["kind"] == "record":
            frm.append(f"main.{dq(RECORD_TABLE)} AS t{i}")
            where.append(f"t{i}.fields = {record_json(c, 'r')}")
        else:
            sel.append(f"r.{dq(c['fl'][0])}")
            continue
        sel.append(f"t{i}.id")
    sql = f"INSERT INTO main.{q('_' + name)} SELECT {', '.join(sel)} FROM {' CROSS JOIN '.join(frm)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql).rowcount


def to_flowlog(in_path: str, fl_path: str, schema: dict) -> dict:
    """Returns {relation: row count}; relations missing from INDB are left
    out (count -1): like Souffle, FlowLog fails if it loads one. Raises
    ValueError if an input cannot be converted."""
    if os.path.exists(fl_path):
        os.unlink(fl_path)
    dst = connect(fl_path, in_path, "src")
    have = existing_relations(dst, "src")
    dst.execute("BEGIN")
    counts: dict = {}
    for rel in schema["inputs"]:
        name, cols = rel["name"], rel["columns"]
        read = rel.get("read", True)
        if read and name not in have:
            counts[name] = -1
            continue
        defs = []
        for col in cols:
            kinds = col.get("fl_kinds", [col["kind"]])
            defs.extend(
                f"{dq(c)} {SQL_TYPE[k]} NOT NULL" for c, k in zip(col["fl"], kinds)
            )
        dst.execute(f"CREATE TABLE main.{dq(name)} ({', '.join(defs)})")
        if not read:
            # no rule reads it (see souffle2flowlog.py's record_io)
            counts[name] = 0
            continue
        src_cols = [r[1] for r in dst.execute(f"PRAGMA src.table_info({dq(name)})")]
        if len(src_cols) != len(cols):
            raise ValueError(
                f"{name}: {len(src_cols)} columns in {in_path}, expected {len(cols)}"
            )
        if any(col["kind"] == "record" for col in cols) and RECORD_TABLE not in have:
            if dst.execute(f"SELECT EXISTS (SELECT 1 FROM src.{dq(name)})").fetchone()[
                0
            ]:
                raise ValueError(
                    f"{name}: {in_path} does not store record contents (it has no "
                    f"{RECORD_TABLE}; Souffle writes record columns as ids that are "
                    "only meaningful to the process that wrote them)"
                )
            counts[name] = 0
            continue
        sel, frm = [], [f"src.{dq(name)} AS r"]
        for i, (col, sc) in enumerate(zip(cols, src_cols)):
            v = f"r.{dq(sc)}"
            if col["kind"] != "record":
                sel.append(v)
                continue
            check_records(dst, in_path, name, v)
            frm.append(f"JOIN src.{dq(RECORD_TABLE)} AS t{i} ON t{i}.id = {v}")
            sel.extend(
                f"json_extract(t{i}.fields, '$[{k}]')" for k in range(len(col["fl"]))
            )
        counts[name] = dst.execute(
            f"INSERT INTO main.{dq(name)} SELECT {', '.join(sel)} FROM {' '.join(frm)}"
        ).rowcount
    dst.execute("COMMIT")
    dst.close()
    return counts


def check_records(conn: sqlite3.Connection, in_path: str, name: str, col: str) -> None:
    """Fails unless every id in src.NAME's record column COL is in the record table."""
    bad = conn.execute(
        f"SELECT {col} FROM src.{dq(name)} AS r WHERE NOT EXISTS "
        f"(SELECT 1 FROM src.{dq(RECORD_TABLE)} AS t WHERE t.id = {col}) LIMIT 1"
    ).fetchone()
    if bad is not None:
        # nil (0) or a dangling id: the translation has no nil
        raise ValueError(
            f"{name}: record id {bad[0]} is nil or unknown in {in_path} "
            "(nil records are not supported)"
        )


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
        try:
            counts = to_flowlog(args.indb, args.fldb, schema)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        missing = sorted(n for n, c in counts.items() if c < 0)
        if missing:
            print(
                f"warning: inputs missing from {args.indb} (not copied): {', '.join(missing)}",
                file=sys.stderr,
            )
    print(
        f"{args.cmd}: {len(counts)} relations, {sum(max(c, 0) for c in counts.values())} rows"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
