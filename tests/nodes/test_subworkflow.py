"""subworkflow + wait nodes."""

from pathlib import Path

from graphx.engine.services import FakeClock, Services

from conftest import Harness, chan, edge, graph, node

CHILD = """\
version: 1
name: child
state:
  n: { type: int, default: 0 }
entry: [bump]
nodes:
  - id: bump
    type: function
    handler: "graphx.demo:increment"
    args: { count: "<state.n>" }
    updates: { n: "<self.count>" }
edges:
  - { from: bump, to: end }
"""


class TestSubworkflow:
    async def test_child_runs_and_returns_state(self, tmp_path: Path):
        child_path = tmp_path / "child.yaml"
        child_path.write_text(CHILD)
        h = Harness(graph(
            [node("sub", type="subworkflow", workflow=str(child_path),
                  input={"n": 41})],
            [edge("sub", "end")], entry=["sub"],
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        output = h.events_of("node_finished")[0].data["output"]
        assert output["state"]["n"] == 42

    async def test_missing_child_fails_deterministically(self):
        h = Harness(graph(
            [node("sub", type="subworkflow", workflow="/nope/missing.yaml")],
            [edge("sub", "end")], entry=["sub"],
        ))
        outcome = await h.run()
        assert outcome.status == "failed"
        assert h.events_of("node_retrying") == []


class TestWait:
    async def test_wait_uses_clock(self):
        clock = FakeClock()
        h = Harness(graph(
            [node("w", type="wait", seconds=3.5),
             node("after", value="done", channel="out")],
            [edge("w", "after")], entry=["w"],
            channels={"out": chan("out")},
        ), services=Services(clock=clock))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert 3.5 in clock.sleeps
        assert outcome.state["out"] == "done"
