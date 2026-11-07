#!/usr/bin/env python3
"""Exercise the installed FlowLog compiler/runtime through CTADL's native I/O.

Run after sourcing benchmark/env.sh. This compiles two small analyses and
checks shared input/output, reruns, symbol IDs, indexes, pruned outputs, nullary
relations and record storage across two separate engine processes, with 1 and 4
workers (or $WORKERS, as in the other FlowLog suites: WORKERS="1 16").
"""

import json
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

from ctadl.flowlog import FlowLogAnalysis, SQLITE_IO_MODE


def run(analysis, directory, workers):
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        try:
            inputs, outputs = analysis.read_inputs(directory, Path(scratch))
            assert (inputs / "shared.db").samefile(directory / "shared.db")
            assert (outputs / "shared.db").samefile(directory / "shared.db")
            subprocess.run(
                [analysis.bin, "-F", inputs, "-D", outputs, "-w", str(workers)],
                check=True,
                timeout=60,
            )
            analysis.write_outputs(outputs)
        finally:
            analysis.cleanup()


def main():
    here = Path(__file__).resolve().parent
    writer = FlowLogAnalysis(here / "write.dl")
    reader = FlowLogAnalysis(here / "read.dl")
    previous = Path.cwd()
    try:
        for workers in map(int, os.environ.get("WORKERS", "1 4").split()):
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp).resolve()
                os.chdir(directory)
                path = directory / "shared.db"
                with closing(sqlite3.connect(path)) as db:
                    db.executescript("""
                        CREATE TABLE __SymbolTable(id INTEGER PRIMARY KEY, symbol TEXT UNIQUE);
                        INSERT INTO __SymbolTable VALUES(1,'kept'),(2,'unused');
                        CREATE TABLE _R("0" INTEGER,"1" INTEGER);
                        INSERT INTO _R VALUES(1,7);
                        CREATE VIEW R AS SELECT symbol AS type, "1" AS n
                            FROM _R JOIN __SymbolTable ON "0"=id;
                        CREATE INDEX r_number ON _R("1");
                        CREATE TABLE _Empty("0" INTEGER);
                        INSERT INTO _Empty VALUES(2);
                        CREATE VIEW Empty AS SELECT symbol AS x
                            FROM _Empty JOIN __SymbolTable ON "0"=id;
                    """)
                inode = path.stat().st_ino
                for repetition in range(2):
                    run(writer, directory, workers)
                    run(reader, directory, workers)
                    assert path.stat().st_ino == inode
                    with closing(sqlite3.connect(path)) as db:
                        assert db.execute(
                            "SELECT type,n FROM R ORDER BY type"
                        ).fetchall() == [("added", 8), ("kept", 7)]
                        assert db.execute(
                            "SELECT * FROM __SymbolTable WHERE id <= 2 ORDER BY id"
                        ).fetchall() == [(1, "kept"), (2, "unused")]
                        assert db.execute(
                            "SELECT * FROM Decoded ORDER BY name"
                        ).fetchall() == [("added", 8, "added"), ("kept", 7, "kept")]
                        records = db.execute(
                            "SELECT name,fields FROM Rec JOIN __FlowLogRecord ON info=id ORDER BY name"
                        ).fetchall()
                        assert [
                            (name, json.loads(fields)) for name, fields in records
                        ] == [("added", [8, "added"]), ("kept", [7, "kept"])]
                        assert db.execute("SELECT * FROM Empty").fetchall() == []
                        assert db.execute("SELECT * FROM Missing").fetchall() == []
                        assert db.execute("SELECT * FROM Unit").fetchall() == [(1,)]
                        assert db.execute(
                            "SELECT name FROM sqlite_master WHERE type='index' AND name='r_number'"
                        ).fetchone()
                        assert not db.execute(
                            "SELECT name FROM sqlite_master WHERE type='trigger'"
                        ).fetchall()
                    print(
                        f"PASS {SQLITE_IO_MODE}: workers={workers}, repetition={repetition + 1}",
                        flush=True,
                    )
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    main()
