"""FlowLog runtime I/O and compilation-cache regression checks."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ctadl.flowlog import FlowLogAnalysis, runtime_fingerprint
from ctadl.flowlog.native_sqlite import Output, check_inputs


def relation(name="R", columns=None):
    return {
        "name": name,
        "file": "shared.db",
        "columns": columns
        or [
            {"attr": "type", "kind": "symbol", "fl": ["type_k"]},
            {"attr": "n", "kind": "number", "fl": ["n"]},
        ],
    }


class NativeSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "shared.db"
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE __SymbolTable(id INTEGER PRIMARY KEY, symbol TEXT UNIQUE);
                INSERT INTO __SymbolTable VALUES(1,'kept'),(2,'next');
                CREATE TABLE _R("0" INTEGER,"1" INTEGER);
                INSERT INTO _R VALUES(1,99);
                CREATE VIEW R AS SELECT symbol AS type, "1" AS n FROM _R JOIN __SymbolTable ON "0"=id;
                CREATE INDEX kept_index ON _R("1");
                CREATE TABLE unrelated(x);
                INSERT INTO unrelated VALUES(42);
            """)

    def tearDown(self):
        self.tmp.cleanup()

    def test_replaced_rows_keep_ids_and_restore_names_indexes(self):
        output = Output(self.path, [relation()])
        with sqlite3.connect(self.path) as db:
            db.executescript(
                """DROP VIEW R; DROP TABLE _R;
                CREATE TABLE _R("0" INTEGER NOT NULL,"1" INTEGER NOT NULL);
                INSERT INTO _R(rowid,"0","1") VALUES(17,2,7);
                CREATE VIEW R AS SELECT symbol AS type_k,"1" AS n FROM _R JOIN __SymbolTable ON "0"=id;"""
            )
        output.finish()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(
                db.execute("SELECT rowid,* FROM _R").fetchall(), [(17, 2, 7)]
            )
            self.assertEqual(
                db.execute("SELECT type,n FROM R").fetchall(), [("next", 7)]
            )
            self.assertEqual(
                db.execute("SELECT * FROM __SymbolTable").fetchall(),
                [(1, "kept"), (2, "next")],
            )
            self.assertEqual(db.execute("SELECT * FROM unrelated").fetchall(), [(42,)])
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='kept_index'"
                ).fetchone()
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
                ).fetchone()[0],
                0,
            )

    def test_pruned_existing_and_missing_outputs_are_empty(self):
        output = Output(self.path, [relation(), relation("Empty")])
        output.finish()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT * FROM R").fetchall(), [])
            self.assertEqual(db.execute("SELECT * FROM Empty").fetchall(), [])
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='kept_index'"
                ).fetchone()
            )

    def test_cleanup_after_failed_engine_preserves_rows(self):
        output = Output(self.path, [relation()])
        output.cleanup()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT * FROM R").fetchall(), [("kept", 99)])
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
                ).fetchone()[0],
                0,
            )

    def test_preparation_failure_rolls_back_markers(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE VIEW _Invalid AS SELECT 1 AS "0"')
        with self.assertRaisesRegex(ValueError, "expected a native SQLite table"):
            Output(self.path, [relation(), relation("Invalid")])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT * FROM R").fetchall(), [("kept", 99)])
            self.assertEqual(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall(),
                [],
            )

    def test_record_views_keep_native_flattened_rows(self):
        rel = relation(
            "Rec",
            [
                {"attr": "name", "kind": "symbol", "fl": ["name"]},
                {
                    "attr": "info",
                    "kind": "record",
                    "fl": ["info_n", "info_s"],
                    "fl_kinds": ["number", "symbol"],
                    "format": "[{}, {}]",
                },
            ],
        )
        output = Output(self.path, [rel])
        with sqlite3.connect(self.path) as db:
            db.executescript(
                'CREATE TABLE _Rec("0" INTEGER,"1" INTEGER,"2" INTEGER); INSERT INTO _Rec VALUES(1,7,2),(2,7,2);'
            )
        output.finish()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(
                db.execute("SELECT * FROM _Rec").fetchall(), [(1, 7, 2), (2, 7, 2)]
            )
            result = db.execute("SELECT name,info FROM Rec ORDER BY name").fetchall()
            self.assertEqual(result[0][1], result[1][1])
            self.assertEqual(
                json.loads(
                    db.execute(
                        "SELECT fields FROM __FlowLogRecord WHERE id=?", (result[0][1],)
                    ).fetchone()[0]
                ),
                [7, "next"],
            )
        check_inputs(self.path, [rel])
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE __FlowLogNativeSchema")
        with self.assertRaisesRegex(ValueError, "record ids do not store contents"):
            check_inputs(self.path, [rel])
        check_inputs(self.path, [{**rel, "read": False}])

    def test_input_and_output_links_share_the_original_inode(self):
        analysis = FlowLogAnalysis.__new__(FlowLogAnalysis)
        analysis.schema = {"inputs": [relation()], "outputs": [relation()]}
        analysis.files = {}
        scratch = self.root / "scratch"
        scratch.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            inp, out = analysis.read_inputs(self.root, scratch)
            self.assertTrue((inp / "shared.db").samefile(self.path))
            self.assertTrue((out / "shared.db").samefile(self.path))
            self.assertTrue((inp / "shared.db").is_symlink())
            self.assertTrue((out / "shared.db").is_symlink())
        finally:
            analysis.cleanup()
            os.chdir(previous)


class TestRuntimeCache(unittest.TestCase):
    def test_sources_and_dependency_lock_invalidate_but_build_products_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runtime = workspace / "flowlog-runtime"
            (runtime / "src/operators").mkdir(parents=True)
            source = runtime / "src/operators/example.rs"
            source.write_text("first")
            before = runtime_fingerprint(runtime)
            (runtime / "target").mkdir()
            (runtime / "target/generated.rs").write_text("temporary build output")
            self.assertEqual(before, runtime_fingerprint(runtime))
            source.write_text("second")
            changed = runtime_fingerprint(runtime)
            self.assertNotEqual(before, changed)
            (workspace / "Cargo.lock").write_text("new dependency resolution")
            self.assertNotEqual(changed, runtime_fingerprint(runtime))


if __name__ == "__main__":
    unittest.main()
