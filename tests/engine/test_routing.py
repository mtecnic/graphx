"""Scheduler semantics through the full executor: joins, branches, loops, errors."""


from conftest import Harness, chan, edge, graph, node


class TestLinearAndParallel:
    async def test_linear_chain(self):
        h = Harness(graph(
            [node("a", value=1), node("b", value=2)],
            [edge("a", "b"), edge("b", "end")],
            entry=["a"],
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        finished = [e.node_id for e in h.events_of("node_finished")]
        assert finished == ["a", "b"]

    async def test_parallel_fanout_barrier_merge(self):
        h = Harness(graph(
            [node("a", value="A"), node("b", value="B"),
             node("m", type="merge"), node("after", value="done")],
            [edge("a", "m"), edge("b", "m"), edge("m", "after")],
            entry=["a", "b"],
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        merge_event = h.events_of("node_finished")[2]
        assert merge_event.node_id == "m"
        assert merge_event.data["output"]["arrivals"] == {"a": "ok", "b": "ok"}

    async def test_merge_threshold_tolerates_failure(self):
        h = Harness(graph(
            [node("a", value="A"), node("bad", type="t_boom"),
             node("m", type="merge", success_threshold=1), node("after", value="x")],
            [edge("a", "m"), edge("bad", "m"), edge("m", "after")],
            entry=["a", "bad"],
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert len(outcome.dead_letters) == 1
        assert outcome.dead_letters[0].node_id == "bad"

    async def test_merge_threshold_failure_is_fatal_without_on_error(self):
        h = Harness(graph(
            [node("a", type="t_boom"), node("b", type="t_boom"),
             node("m", type="merge", success_threshold=1), node("after", value="x")],
            [edge("a", "m"), edge("b", "m"), edge("m", "after")],
            entry=["a", "b"],
        ))
        outcome = await h.run()
        assert outcome.status == "failed"
        assert "m" in (outcome.error or "")


class TestConditionalRouting:
    def loop_graph(self, max_iterations=None, on_exhausted=None):
        return graph(
            [node("start", value=0, channel="n"),
             node("bump", type="function", handler="graphx.demo:increment",
                  args={"count": "<state.n>"}, updates={"n": "<self.count>"}),
             node("check", type="condition",
                  branches=[{"if": "n < 3", "goto": "bump"}, {"else": "done"}],
                  max_iterations=max_iterations, on_exhausted=on_exhausted),
             node("done", value="fin")],
            [edge("start", "bump"), edge("bump", "check")],
            entry=["start"],
            channels={"n": chan("n")},
        )

    async def test_loop_until_condition(self):
        h = Harness(self.loop_graph())
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["n"] == 3

    async def test_loop_guard_routes_on_exhausted(self):
        h = Harness(graph(
            [node("start", value=0, channel="n"),
             node("bump", type="function", handler="graphx.demo:increment",
                  args={"count": "<state.n>"}, updates={"n": "<self.count>"},
                  max_iterations=2, on_exhausted="done"),
             node("check", type="condition",
                  branches=[{"if": "n < 100", "goto": "bump"}, {"else": "done"}]),
             node("done", value="fin")],
            [edge("start", "bump"), edge("bump", "check")],
            entry=["start"],
            channels={"n": chan("n")},
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["n"] == 2  # bump ran exactly max_iterations times

    async def test_edge_when_clause(self):
        h = Harness(graph(
            [node("a", value=5, channel="x"),
             node("big", value="big"), node("small", value="small")],
            [edge("a", "big", when="x > 3"), edge("a", "small", when="x <= 3")],
            entry=["a"],
            channels={"x": chan("x")},
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        ran = [e.node_id for e in h.events_of("node_finished")]
        assert "big" in ran and "small" not in ran


class TestErrorRouting:
    async def test_on_error_edge(self):
        h = Harness(graph(
            [node("bad", type="t_boom", message="kaput"), node("handler", value="handled")],
            [edge("bad", "end"), edge("bad", "handler", kind="on_error")],
            entry=["bad"],
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert [e.node_id for e in h.events_of("node_finished")] == ["handler"]
        assert outcome.dead_letters[0].error == "kaput"

    async def test_failure_without_route_is_fatal(self):
        h = Harness(graph(
            [node("bad", type="t_boom"), node("next", value=1)],
            [edge("bad", "next")],
            entry=["bad"],
        ))
        outcome = await h.run()
        assert outcome.status == "failed"


class TestMap:
    async def test_map_collects_to_channel(self):
        h = Harness(graph(
            [node("seed", value=["x", "y", "z"], channel="items"),
             node("mapper", type="map", over="<state.items>", item_as="w",
                  node={"type": "function", "handler": "graphx.demo:upper",
                        "args": {"word": "<item.w>"}},
                  collect={"channel": "out"}),
             node("done", value="fin")],
            [edge("seed", "mapper"), edge("mapper", "done")],
            entry=["seed"],
            channels={"items": chan("items"), "out": chan("out", reducer="extend", default=[])},
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["out"] == ["X", "Y", "Z"]

    async def test_map_skip_item_errors(self):
        h = Harness(graph(
            [node("mapper", type="map", over=[1, 0, 2], item_as="d",
                  on_item_error="skip",
                  node={"type": "function", "handler": "tests_div:divide",
                        "args": {"x": "<item.d>"}})],
            [edge("mapper", "end")],
            entry=["mapper"],
        ))
        # helper module registered below via sys.modules
        outcome = await h.run()
        assert outcome.status == "finished"
        finished = h.events_of("node_finished")[0]
        assert finished.data["output"]["count"] == 2
        assert len(finished.data["output"]["errors"]) == 1
