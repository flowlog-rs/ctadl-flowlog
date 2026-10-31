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
relations with their original and FlowLog column names and types). Each is
an INSERT ... SELECT statement per relation, run by SQLite on the two
databases attached to one connection; no rows pass through Python, and
to-souffle numbers new symbols (and records) with a SQL function, a lookup
in a Python dict that runs in C. Each is one transaction: an error, Ctrl-C or
kill leaves the DB it writes (with --append, CTADL's index) as it was.

  to-souffle FLDB OUTDB --schema S.json [--append]
      Copy the manifest's outputs from the FlowLog DB into OUTDB in Souffle's
      layout. Outputs FlowLog did not write (it prunes relations without
      rules) are created empty, as Souffle would. Without --append OUTDB is
      recreated; with it, OUTDB's symbol table is extended and the rows of
      the listed relations are replaced, as Souffle's sqlite writer does: a
      relation OUTDB has keeps its table, view and indexes (CTADL indexes
      tables of its index DB that a query writes again), and must have as
      many columns.
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
from collections import defaultdict
from contextlib import contextmanager
from itertools import count
from typing import Iterable, Iterator

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


@contextmanager
def transaction(path: str, attach: str, alias: str) -> Iterator[sqlite3.Connection]:
    """Opens PATH (which is written) with ATTACH attached as ALIAS (which is
    only read), for one transaction: committed if the block completes, and
    otherwise rolled back, when the connection closes or, if the process was
    killed, by the next one to open PATH. That takes SQLite's default,
    on-disk rollback journal: Souffle's sqlite I/O keeps it in memory, where
    a kill loses it, and where it holds the old pages of the relations it
    replaces."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        # no fsync: a kill is rolled back, an OS crash may not be
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("ATTACH DATABASE ? AS " + alias, (attach,))
        for schema in ("main", alias):
            conn.execute(f"PRAGMA {schema}.cache_size = -{CACHE_KIB}")
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    finally:
        conn.close()


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
    sql = f"CREATE VIEW IF NOT EXISTS {q(name)} AS SELECT {','.join(sel)} FROM {','.join(frm)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql


class Interner:
    """Numbers the values of TABLE(id INTEGER PRIMARY KEY, KEY TEXT UNIQUE),
    Souffle's symbol table or the record table: number(V, ALIAS) is SQL for
    the id of V's value, its id in TABLE, else (a new value) the id after
    the largest, as SQLite numbers the symbols Souffle adds. flush() adds
    the new values to TABLE.

    The new values' ids come from the SQL function NAME, a lookup in a dict
    of them that runs in C, with the ids from a C iterator. A value thus
    costs a lookup in TABLE's index (none when TABLE starts empty, as when
    CTADL writes its index) and, if new, a hash lookup, where interning the
    values into TABLE first, then looking them up, costs two lookups in the
    index. As no Python code runs in NAME, Ctrl-C takes effect after the
    statement, as it does for one without it."""

    def __init__(self, conn: sqlite3.Connection, table: str, key: str, name: str):
        self.conn, self.table, self.key, self.name = conn, table, key, name
        self.last = 0
        if table in existing_relations(conn):
            self.last = (
                conn.execute(f"SELECT max(id) FROM main.{dq(table)}").fetchone()[0] or 0
            )
        self.new: defaultdict = defaultdict(count(self.last + 1).__next__)
        conn.create_function(name, 1, self.new.__getitem__)

    def number(self, v: str, alias: str) -> tuple:
        """SQL for the id of V's value, and the join of TABLE AS ALIAS that
        it needs ("" if none)."""
        if not self.last:
            return f"{self.name}({v})", ""
        join = f"LEFT JOIN main.{dq(self.table)} AS {alias} ON {alias}.{dq(self.key)} = {v}"
        return f"coalesce({alias}.id, {self.name}({v}))", join

    def flush(self) -> None:
        if self.new:
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS main.{dq(self.table)} "
                f"(id INTEGER PRIMARY KEY, {dq(self.key)} TEXT UNIQUE)"
            )
            self.conn.executemany(
                f"INSERT INTO main.{dq(self.table)} (id, {dq(self.key)}) VALUES (?, ?)",
                ((i, v) for v, i in self.new.items()),
            )


def to_souffle(fl_path: str, out_path: str, schema: dict, append: bool = False) -> dict:
    """Returns {relation: row count}. Raises ValueError if a relation in
    OUTDB has another number of columns (Souffle fails too)."""
    if not append and os.path.exists(out_path):
        os.unlink(out_path)
    counts: dict = {}
    # Without FlowLog's DB every output is created empty.
    fl = fl_path if os.path.exists(fl_path) else ":memory:"
    with transaction(out_path, fl, "fl") as out:
        have = existing_relations(out, "fl")
        out.execute(
            f"CREATE TABLE IF NOT EXISTS {q(SYMBOL_TABLE)} (id INTEGER PRIMARY KEY, symbol TEXT UNIQUE)"
        )
        # flush() creates the record table, with the first record
        ids = {
            "symbol": Interner(out, SYMBOL_TABLE, "symbol", "ctadl_symbol"),
            "record": Interner(out, RECORD_TABLE, "fields", "ctadl_record"),
        }
        for rel in schema["outputs"]:
            name, cols = rel["name"], rel["columns"]
            shadow = q("_" + name)
            out.execute(
                f"CREATE TABLE IF NOT EXISTS main.{shadow} ({','.join(q(str(i)) + ' INTEGER' for i in range(len(cols)))})"
            )
            width = len(out.execute(f"PRAGMA main.table_info({shadow})").fetchall())
            if width != len(cols):
                raise ValueError(
                    f"{name}: {width} columns in {out_path}, expected {len(cols)}"
                )
            out.execute(f"DELETE FROM main.{shadow}")
            out.execute(souffle_view_sql(name, cols))
            counts[name] = copy_to_souffle(out, name, cols, ids) if name in have else 0
        for interner in ids.values():
            interner.flush()
    return counts


def copy_to_souffle(conn: sqlite3.Connection, name: str, cols: list, ids: dict) -> int:
    """Fills main._NAME with fl.NAME's rows, its symbols and records
    numbered by the Interners IDS (by kind)."""
    sel, joins = [], []
    for i, c in enumerate(cols):
        v = record_json(c, "r") if c["kind"] == "record" else f"r.{dq(c['fl'][0])}"
        if c["kind"] in ids:
            v, join = ids[c["kind"]].number(v, f"t{i}")
            joins.append(join)
        sel.append(v)
    return conn.execute(
        f"INSERT INTO main.{q('_' + name)} SELECT {', '.join(sel)} "
        f"FROM fl.{dq(name)} AS r {' '.join(joins)}"
    ).rowcount


def to_flowlog(in_path: str, fl_path: str, schema: dict) -> dict:
    """Returns {relation: row count}; relations missing from INDB are left
    out (count -1): like Souffle, FlowLog fails if it loads one. Raises
    ValueError if an input cannot be converted."""
    if os.path.exists(fl_path):
        os.unlink(fl_path)
    counts: dict = {}
    with transaction(fl_path, in_path, "src") as dst:
        have = existing_relations(dst, "src")
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
            if (
                any(col["kind"] == "record" for col in cols)
                and RECORD_TABLE not in have
            ):
                if dst.execute(
                    f"SELECT EXISTS (SELECT 1 FROM src.{dq(name)})"
                ).fetchone()[0]:
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
                    f"json_extract(t{i}.fields, '$[{k}]')"
                    for k in range(len(col["fl"]))
                )
            counts[name] = dst.execute(
                f"INSERT INTO main.{dq(name)} SELECT {', '.join(sel)} FROM {' '.join(frm)}"
            ).rowcount
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
    try:
        if args.cmd == "to-souffle":
            counts = to_souffle(args.fldb, args.outdb, schema, args.append)
        else:
            counts = to_flowlog(args.indb, args.fldb, schema)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.cmd == "to-flowlog":
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
