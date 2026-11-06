"""CTADL metadata over FlowLog's shared SQLite dictionary tables.

Relation rows and symbol ids are read and written by FlowLog itself. This
module restores CTADL attribute names and indexes after FlowLog replaces an
output table. Record-valued attributes retain native flattened storage;
their public views expose record ids backed by a small record dictionary.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

IO_MODE = "native-shared-dictionary-v1"
RECORDS = "__FlowLogRecord"
SCHEMAS = "__FlowLogNativeSchema"


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@contextmanager
def connect(path: Path):
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA synchronous=OFF")
        # Include schema edits in the transaction too. A partially prepared
        # output must not leave marker triggers behind if preparation fails.
        db.execute("BEGIN")
        with db:
            yield db


def flat_columns(rel):
    return [
        (name, kind)
        for col in rel["columns"]
        for name, kind in zip(col["fl"], col.get("fl_kinds", [col["kind"]]))
    ]


def check_inputs(path: Path, relations: list) -> None:
    """Reject record ids from a foreign process instead of guessing contents."""
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        for rel in relations:
            if not rel.get("read", True) or not any(
                c["kind"] == "record" for c in rel["columns"]
            ):
                continue
            try:
                row = db.execute(
                    f"SELECT columns_json FROM {quote(SCHEMAS)} WHERE relation=?",
                    (rel["name"],),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is None or json.loads(row[0]) != rel["columns"]:
                raise ValueError(
                    f"{rel['name']}: native record input requires flattened storage "
                    "written by this adapter; Souffle record ids do not store contents"
                )


class Output:
    """Metadata needed to finish one shared database's output, without copies."""

    def __init__(self, path: Path, relations: list):
        self.path, self.relations = path, relations
        self.indexes = []
        self.markers = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        with connect(path) as db:
            # A trigger is a constant-size marker: no scan or modification of
            # relation rows. FlowLog drops it when replacing a written table.
            for rel in relations:
                table = "_" + rel["name"]
                kind = db.execute(
                    "SELECT type FROM sqlite_master WHERE name=?", (table,)
                ).fetchone()
                if kind is None:
                    continue
                if kind[0] != "table":
                    raise ValueError(f"{table}: expected a native SQLite table")
                self.indexes.extend(
                    (index, sql)
                    for index, sql in db.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type='index' "
                        "AND tbl_name=? AND sql IS NOT NULL",
                        (table,),
                    )
                )
                marker = "__ctadl_write_" + uuid.uuid4().hex
                db.execute(
                    f"CREATE TRIGGER {quote(marker)} AFTER INSERT ON {quote(table)} "
                    "BEGIN SELECT 1; END"
                )
                self.markers[rel["name"]] = marker

    def finish(self):
        with connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS __SymbolTable "
                "(id INTEGER PRIMARY KEY, symbol TEXT UNIQUE)"
            )
            db.execute(
                f"CREATE TABLE IF NOT EXISTS {quote(SCHEMAS)} "
                "(relation TEXT PRIMARY KEY, columns_json TEXT NOT NULL)"
            )
            for rel in self.relations:
                name, columns = rel["name"], rel["columns"]
                table = "_" + name
                marker = self.markers.get(name)
                untouched = (
                    marker
                    and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                        (marker,),
                    ).fetchone()
                )
                if untouched:
                    # A pruned output must become empty, including on a rerun.
                    db.execute(f"DELETE FROM {quote(table)}")
                    db.execute(f"DROP TRIGGER {quote(marker)}")
                flat = flat_columns(rel)
                defs = (
                    ",".join(
                        f'{quote(str(i))} {"REAL" if kind == "float" else "INTEGER"}'
                        for i, (_, kind) in enumerate(flat)
                    )
                    or '"__flowlog_present" INTEGER'
                )
                db.execute(f"CREATE TABLE IF NOT EXISTS {quote(table)} ({defs})")
                actual = [
                    r[1] for r in db.execute(f"PRAGMA table_info({quote(table)})")
                ]
                expected = [str(i) for i in range(len(flat))] or ["__flowlog_present"]
                if actual != expected:
                    raise ValueError(
                        f"{name}: expected native columns {expected}, got {actual}"
                    )
                select, joins, expressions = [], [], []
                for i, (_, kind) in enumerate(flat):
                    expr = f"r.{quote(str(i))}"
                    if kind == "symbol":
                        alias = f"s{i}"
                        joins.append(f"JOIN __SymbolTable {alias} ON {alias}.id={expr}")
                        expr = f"{alias}.symbol"
                    expressions.append(expr)
                offset = 0
                for i, col in enumerate(columns):
                    exprs = expressions[offset : offset + len(col["fl"])]
                    offset += len(col["fl"])
                    if col["kind"] == "record":
                        db.execute(
                            f"CREATE TABLE IF NOT EXISTS {quote(RECORDS)} "
                            "(id INTEGER PRIMARY KEY, fields TEXT UNIQUE)"
                        )
                        record = f"json_array({','.join(exprs)})"
                        db.execute(
                            f"INSERT OR IGNORE INTO {quote(RECORDS)} (fields) "
                            f"SELECT DISTINCT {record} FROM {quote(table)} r {' '.join(joins)}"
                        )
                        expr = (
                            f"(SELECT id FROM {quote(RECORDS)} WHERE fields={record})"
                        )
                    else:
                        expr = exprs[0]
                    select.append(f"{expr} AS {quote(col['attr'])}")
                db.execute(f"DROP VIEW IF EXISTS {quote(name)}")
                db.execute(
                    f"CREATE VIEW {quote(name)} AS SELECT "
                    f"{','.join(select) or '1 AS __flowlog_present'} "
                    f"FROM {quote(table)} r {' '.join(joins)}"
                )
                db.execute(
                    f"INSERT OR REPLACE INTO {quote(SCHEMAS)} VALUES (?,?)",
                    (name, json.dumps(columns)),
                )
            for index, sql in self.indexes:
                # Existing indexes belong to pruned tables; rewritten tables
                # lost theirs in FlowLog's DROP/CREATE and need them restored.
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                    (index,),
                ).fetchone():
                    db.execute(sql)

    def cleanup(self):
        """Remove surviving markers after an engine failure."""
        with connect(self.path) as db:
            for marker in self.markers.values():
                db.execute(f"DROP TRIGGER IF EXISTS {quote(marker)}")
