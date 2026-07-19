"""agent + router nodes against the ScriptedLLM fake."""

import json

from graphx.engine.services import FakeClock, Services
from graphx.llm.client import LLMResponse, ToolCall, Usage
from graphx.model.graph import RetryPolicy, StaticFallback

from conftest import Harness, ScriptedLLM, chan, edge, graph, node


def llm_services(llm) -> Services:
    return Services(clock=FakeClock(), llm=llm)


def tool_response(name: str, arguments: dict, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(text="", tool_calls=[ToolCall(id=call_id, name=name,
                                                     arguments=arguments)],
                       usage=Usage(20, 5), model="fake")


class TestAgent:
    async def test_plain_text_output(self):
        llm = ScriptedLLM(["Hello from the model."])
        h = Harness(graph(
            [node("a", type="agent", model="ollama/fake", prompt="Say hello")],
            [edge("a", "end")], entry=["a"],
        ), services=llm_services(llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        finished = h.events_of("node_finished")[0]
        assert finished.data["output"]["text"] == "Hello from the model."
        # streaming chunk surfaced
        assert h.events_of("node_output_chunk")

    async def test_output_schema_reask_recovers(self):
        llm = ScriptedLLM(["not json at all",
                           '{"title": "T", "score": 5}'])
        h = Harness(graph(
            [node("a", type="agent", model="ollama/fake", prompt="Rate this",
                  output_schema={"title": "str", "score": "int"})],
            [edge("a", "end")], entry=["a"],
        ), services=llm_services(llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        output = h.events_of("node_finished")[0].data["output"]
        assert output["title"] == "T" and output["score"] == 5
        # second call contained the validation feedback
        assert "failed validation" in llm.calls[1]["messages"][-1]["content"]

    async def test_schema_exhaustion_falls_back_to_next_model(self):
        llm = ScriptedLLM(by_model={
            "ollama/bad": ["junk", "still junk", "never json"],
            "openai/good": ['{"title": "ok"}'],
        })
        h = Harness(graph(
            [node("a", type="agent", model="ollama/bad", prompt="p",
                  output_schema={"title": "str"},
                  retry=RetryPolicy(attempts=1),
                  fallbacks=["openai/good"])],
            [edge("a", "end")], entry=["a"],
        ), services=llm_services(llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert h.events_of("node_fallback")[0].data["to"] == "openai/good"
        assert h.events_of("node_finished")[0].data["output"]["title"] == "ok"

    async def test_tool_loop(self):
        llm = ScriptedLLM([
            tool_response("word_count", {"text": "one two three"}),
            "The text has 3 words.",
        ])
        h = Harness(graph(
            [node("a", type="agent", model="ollama/fake", prompt="Count",
                  tools=[{"name": "word_count", "function": "tests_tools:word_count"}])],
            [edge("a", "end")], entry=["a"],
        ), services=llm_services(llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        # the tool result was sent back to the model
        tool_message = llm.calls[1]["messages"][-1]
        assert tool_message["role"] == "tool"
        assert json.loads(tool_message["content"])["count"] == 3
        assert h.events_of("node_finished")[0].data["output"]["text"] == \
            "The text has 3 words."

    async def test_static_fallback_after_llm_down(self):
        from graphx.engine.errors import TransientError
        llm = ScriptedLLM([TransientError("503"), TransientError("503"),
                           TransientError("503")])
        h = Harness(graph(
            [node("a", type="agent", model="ollama/fake", prompt="p",
                  retry=RetryPolicy(attempts=3, jitter=False),
                  fallbacks=[StaticFallback(output={"text": "degraded answer"})])],
            [edge("a", "end")], entry=["a"],
        ), services=llm_services(llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        finished = h.events_of("node_finished")[0]
        assert finished.data["output"]["text"] == "degraded answer"
        assert finished.data["degraded"] is True

    async def test_token_metrics_feed_run_budget(self):
        from graphx.model.graph import Budget, GraphConfig
        # each scripted call costs 20 tokens; budget of 30 trips after first node
        llm = ScriptedLLM(["one", "two", "three"])
        h = Harness(graph(
            [node("a", type="agent", model="ollama/fake", prompt="p"),
             node("b", type="agent", model="ollama/fake", prompt="p"),
             node("c", type="agent", model="ollama/fake", prompt="p")],
            [edge("a", "b"), edge("b", "c")], entry=["a"],
            config=GraphConfig(budget=Budget(tokens=30)),
        ), services=llm_services(llm))
        outcome = await h.run()
        assert outcome.status == "guard_tripped"
        assert "tokens" in (outcome.error or "")


class TestRouter:
    async def test_routes_by_choice(self):
        llm = ScriptedLLM(['{"choice": "yes_path", "reason": "obvious"}'])
        h = Harness(graph(
            [node("r", type="router", model="ollama/fake", prompt="Decide",
                  options={"yes_path": "when yes", "no_path": "when no"}),
             node("yes_path", value="Y", channel="out"),
             node("no_path", value="N", channel="out")],
            [], entry=["r"],
            channels={"out": chan("out")},
        ), services=llm_services(llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["out"] == "Y"

    async def test_invalid_choice_reasks(self):
        llm = ScriptedLLM(['{"choice": "nonsense"}',
                           '{"choice": "no_path"}'])
        h = Harness(graph(
            [node("r", type="router", model="ollama/fake", prompt="Decide",
                  options={"yes_path": "y", "no_path": "n"}),
             node("yes_path", value="Y", channel="out"),
             node("no_path", value="N", channel="out")],
            [], entry=["r"],
            channels={"out": chan("out")},
        ), services=llm_services(llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["out"] == "N"
        assert len(llm.calls) == 2
