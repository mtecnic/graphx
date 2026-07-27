"""End-to-end eval runner over the real engine (LLM-free hello workflow)."""

from pathlib import Path

from graphx.eval.dataset import EvalCase, EvalDataset, Expect
from graphx.eval.runner import run_case, run_dataset
from graphx.eval.trace import diff
from graphx.model.yaml_loader import load_graph

_ROOT = Path(__file__).resolve().parents[2]
_HELLO = _ROOT / "examples" / "hello.yaml"


class TestRunCase:
    async def test_passing_case(self, tmp_path):
        graph = load_graph(_HELLO)
        case = EvalCase(name="named", input={"name": "graphx"},
                        expect=Expect(status="finished",
                                      **{"assert": ["shouts == ['HELLO,', 'GRAPHX!']"]}))
        result, events = await run_case(graph, case, tmp_path / "e.db", "t")
        assert result.passed, [c for c in result.checks if not c.ok]
        assert result.metrics.status == "finished"
        assert events                                # a real trace was captured

    async def test_failing_assertion(self, tmp_path):
        graph = load_graph(_HELLO)
        case = EvalCase(name="wrong", input={},
                        expect=Expect(**{"assert": ["count == 999"]}))
        result, _ = await run_case(graph, case, tmp_path / "e.db", "t")
        assert not result.passed
        assert any(c.kind == "assert" and not c.ok for c in result.checks)

    async def test_isolated_thread_ids(self, tmp_path):
        # two cases in one dataset must not collide in the shared DB
        graph = load_graph(_HELLO)
        ds = EvalDataset(dataset="hs", cases=[
            EvalCase(name="a", input={},
                     expect=Expect(**{"assert": ["count == 3"]})),
            EvalCase(name="b", input={"name": "zzz"},
                     expect=Expect(**{"assert": ["shouts == ['HELLO,', 'ZZZ!']"]})),
        ])
        report, traces = await run_dataset(graph, ds, tmp_path / "e.db")
        assert report.passed == 2
        assert set(traces) == {"a", "b"}
        assert traces["a"][0].thread_id != traces["b"][0].thread_id


class TestReproducibility:
    async def test_same_case_same_trace(self, tmp_path):
        graph = load_graph(_HELLO)
        case = EvalCase(name="c", input={}, expect=Expect())
        _, ev1 = await run_case(graph, case, tmp_path / "a.db", "t")
        _, ev2 = await run_case(graph, case, tmp_path / "b.db", "t")
        assert diff(ev1, ev2) == []                  # deterministic workflow → stable trace
