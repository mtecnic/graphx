"""Golden-trace normalization: stable across identical runs, diffs on divergence."""

from graphx.eval.trace import diff, normalize

from conftest import Harness, chan, edge, graph, node


def _loop_graph(threshold: int):
    return graph(
        [node("bump", type="t_emit", value=1, channel="count"),
         node("again", type="t_emit", value="x")],
        [edge("bump", "again"),
         edge("again", "bump", when=f"count < {threshold}"),
         edge("again", "end", when=f"count >= {threshold}")],
        entry=["bump"],
        channels={"count": chan("count", reducer="sum", default=0)},
    )


class TestNormalize:
    async def test_identical_runs_normalize_equal(self):
        h1 = Harness(_loop_graph(3))
        h2 = Harness(_loop_graph(3))
        await h1.run()
        await h2.run()
        assert normalize(h1.events) == normalize(h2.events)
        assert diff(h1.events, h2.events) == []

    async def test_normalize_strips_volatile(self):
        h = Harness(graph(
            [node("go", type="t_emit", value="hello", channel="v")],
            [edge("go", "end")], entry=["go"], channels={"v": chan("v")},
        ))
        await h.run()
        blob = "\n".join(normalize(h.events))
        # volatile fields must not leak into the behavioral skeleton
        assert "duration_s" not in blob
        assert "hello" not in blob          # output value stripped

    async def test_divergent_routing_diffs(self):
        h1 = Harness(_loop_graph(2))
        h2 = Harness(_loop_graph(4))
        await h1.run()
        await h2.run()
        d = diff(h1.events, h2.events)
        assert d != []                       # different loop counts → non-empty diff
