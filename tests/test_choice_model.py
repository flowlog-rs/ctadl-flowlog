import importlib.util
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "benchmark/check_choice_model.py"
spec = importlib.util.spec_from_file_location("check_choice_model", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TestChoiceModelOutputs(unittest.TestCase):
    def test_qualified_outputs_are_compared(self):
        program = module.Program("""
            .comp Graph {
                .decl Vertex(x: symbol)
                .decl Edge(x: symbol, y: symbol)
                .output Vertex
            }
            .init forward_flow = Graph
            .init backward_flow = Graph
            .output forward_flow.Edge
            .output backward_flow.Edge
            """)
        self.assertEqual(
            program.output_names(),
            [
                "forward_flow.Vertex",
                "backward_flow.Vertex",
                "forward_flow.Edge",
                "backward_flow.Edge",
            ],
        )


if __name__ == "__main__":
    unittest.main()
