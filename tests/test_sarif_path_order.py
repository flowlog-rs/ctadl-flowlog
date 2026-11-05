"""The paths in a SARIF report depend on the database's contents, not on the
numbers that the engine gave its vertices"""

import sqlite3
import unittest
from contextlib import closing

from ctadl.vis.sarif import Context, _load_taint_graph
from ctadl.vis.types import SliceDirection

# The source s reaches sink g through a and through b, and sink h through a
# only. The first search reaches g through whichever of a and b it meets
# first; the next avoids the second vertex of each path found. So meeting a
# first finds a second path to g, through b, and meeting b first does not.
VERTICES = ["s", "a", "b", "g", "h"]
EDGES = [("s", "a"), ("s", "b"), ("a", "g"), ("b", "g"), ("a", "h")]


def find_paths(dir: SliceDirection, ids: dict[str, int]) -> list[list[str]]:
    """Returns the paths that the report finds, as vertex names, in a database
    where vertex v has id ids[v]"""
    names = {id: v for v, id in ids.items()}
    flow = "forward_flow" if dir.is_forward() else "backward_flow"
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        for f in ["forward_flow", "backward_flow"]:
            conn.execute(
                f"""CREATE TABLE "{f}.ReachableVertex"
                (id, v1, p1, label, call_state, suspicion, ctx)"""
            )
            conn.execute(
                f"""CREATE TABLE "{f}.ReachableEdge"
                (vertex_to, vertex_from, insn, kind)"""
            )
        conn.execute("CREATE TABLE TaintSourceVertex (tag, v, p)")
        conn.execute("CREATE TABLE LeakingSinkVertex (tag, v, p)")
        conn.executemany(
            f"""INSERT INTO "{flow}.ReachableVertex"
            VALUES (?, ?, '', 'secret', 'nocall', 0, '')""",
            [(ids[v], v) for v in VERTICES],
        )
        # The backward slice stores edges against the flow of data
        conn.executemany(
            f"""INSERT INTO "{flow}.ReachableEdge" VALUES (?, ?, 'i', 'move')""",
            [
                (ids[dst], ids[src]) if dir.is_forward() else (ids[src], ids[dst])
                for src, dst in EDGES
            ],
        )
        conn.execute("INSERT INTO TaintSourceVertex VALUES ('secret', 's', '')")
        conn.executemany(
            "INSERT INTO LeakingSinkVertex VALUES ('secret', ?, '')", [("g",), ("h",)]
        )
        cx = Context(conn, ["path_result"], "front")
        graph = _load_taint_graph(cx, dir)
        return [
            [names[id] for id in path]
            for path in graph.iter_paths(taskid=None, strategy=cx.strategy)
        ]


class TestPathOrder(unittest.TestCase):
    def test_paths_do_not_depend_on_ids(self):
        expected = [["s", "a", "g"], ["s", "a", "h"], ["s", "b", "g"]]
        n = len(VERTICES)
        for dir in [SliceDirection.forward, SliceDirection.backward]:
            # Numbers a before b, then b before a
            for ids in [
                {v: i for i, v in enumerate(VERTICES)},
                {v: n - i for i, v in enumerate(VERTICES)},
            ]:
                with self.subTest(dir=dir, ids=ids):
                    self.assertEqual(find_paths(dir, ids), expected)


if __name__ == "__main__":
    unittest.main()
