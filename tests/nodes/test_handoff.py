"""Dynamic handoffs: synthetic tool → goto + context in a state channel; bounded."""

from graphx.engine.services import FakeClock, Services
from graphx.llm.client import LLMResponse, ToolCall, Usage
from graphx.nodes.registry import load_builtin_nodes

from conftest import Harness, ScriptedLLM, chan, edge, graph, node

load_builtin_nodes()


def _handoff(target, reason, call_id="c1"):
    return LLMResponse(
        text="", tool_calls=[ToolCall(id=call_id, name=f"handoff_to_{target}",
                                      arguments={"reason": reason})],
        usage=Usage(5, 5), model="m")


class TestHandoff:
    async def test_transfers_control_and_context(self):
        llm = ScriptedLLM(by_model={
            "local/triage": [_handoff("specialist", "needs SQL expert")],
            "local/spec": ["done: query optimized"],
        })
        g = graph(
            [node("triage", type="agent", model="local/triage", prompt="Route this.",
                  handoffs=["specialist"]),
             node("specialist", type="agent", model="local/spec",
                  prompt="Handle it. Why: <state.handoff.reason>")],
            [edge("specialist", "end")],
            entry=["triage"],
            channels={"handoff": chan("handoff", default={})},
        )
        h = Harness(g, services=Services(clock=FakeClock(), llm=llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        # DATA: conversation + reason landed in the state channel
        handoff = outcome.state["handoff"]
        assert handoff["reason"] == "needs SQL expert"
        assert handoff["from"] == "triage"
        assert isinstance(handoff["messages"], list)
        # FLOW: the specialist actually ran (goto routed to it, no edge needed)
        spec_call = next(c for c in llm.calls if c["model"] == "local/spec")
        blob = str(spec_call["messages"])
        assert "needs SQL expert" in blob            # STATE ref reached the target prompt

    async def test_no_handoff_runs_normally(self):
        # a plain agent (no tool call) does not touch the handoff channel
        llm = ScriptedLLM(by_model={"local/m": ["just an answer"]})
        g = graph(
            [node("solo", type="agent", model="local/m", prompt="hi",
                  handoffs=["other"]),
             node("other", type="agent", model="local/m", prompt="unused")],
            [edge("solo", "end")],
            entry=["solo"],
            channels={"handoff": chan("handoff", default="UNSET")},
        )
        h = Harness(g, services=Services(clock=FakeClock(), llm=llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["handoff"] == "UNSET"    # channel untouched

    async def test_pingpong_bounded_by_max_iterations(self):
        # two agents that keep handing off to each other — must terminate on the guard
        llm = ScriptedLLM(by_model={
            "local/a": [_handoff("b", "over to b", "a1")] * 10,
            "local/b": [_handoff("a", "over to a", "b1")] * 10,
        })
        g = graph(
            [node("a", type="agent", model="local/a", prompt="p", handoffs=["b"],
                  max_iterations=3),
             node("b", type="agent", model="local/b", prompt="p", handoffs=["a"],
                  max_iterations=3)],
            [], entry=["a"],
            channels={"handoff": chan("handoff", default={})},
        )
        h = Harness(g, services=Services(clock=FakeClock(), llm=llm))
        outcome = await h.run()
        # bounded: it stops (guard trips) rather than looping forever
        assert outcome.status in ("finished", "error", "failed")
        assert outcome.metrics.nodes_run < 20
