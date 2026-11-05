"""Facts writes facts (a query's models) into a database in Souffle's sqlite
layout, in one transaction per writer"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ctadl.vis.model import ColumnSpec, Facts


class TestFactsWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ctadlir.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                'CREATE TABLE "__SymbolTable" (id INTEGER PRIMARY KEY, symbol TEXT UNIQUE)'
            )
            conn.execute("""INSERT INTO "__SymbolTable" VALUES (1, 'f')""")
        self.facts = Facts(self.db)
        self.facts.add_input_relation(
            "R",
            [
                ColumnSpec("name", "TEXT NOT NULL"),
                ColumnSpec("n", "NUMBER NOT NULL"),
                ColumnSpec("pattern", "TEXT NOT NULL"),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, sql: str) -> list:
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql).fetchall()

    def test_symbols(self):
        """A symbol keeps its id, a new one gets the next, and a quote is a
        character like any other"""
        with self.facts.writer() as w:
            self.facts.write(w, "R", "f", 7, "it's")
            self.facts.write(w, "R", "g", 8, "f")
        self.assertEqual(
            self.rows('SELECT * FROM "R" ORDER BY n'),
            [("f", 7, "it's"), ("g", 8, "f")],
        )
        self.assertEqual(
            self.rows('SELECT * FROM "__SymbolTable" ORDER BY id'),
            [(1, "f"), (2, "it's"), (3, "g")],
        )

    def test_one_transaction(self):
        """The writes show when the writer exits, and an error undoes them"""
        with self.facts.writer() as w:
            self.facts.write(w, "R", "f", 7, "g")
            self.assertEqual(self.rows('SELECT count(*) FROM "_R"'), [(0,)])
        self.assertEqual(self.rows('SELECT * FROM "R"'), [("f", 7, "g")])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.facts.writer() as w:
                self.facts.write(w, "R", "h", 9, "h")
                self.facts.write(w, "R", "f", 7, "g")
        self.assertEqual(self.rows('SELECT * FROM "R"'), [("f", 7, "g")])
        self.assertEqual(
            self.rows('SELECT symbol FROM "__SymbolTable" ORDER BY id'),
            [("f",), ("g",)],
        )


if __name__ == "__main__":
    unittest.main()
