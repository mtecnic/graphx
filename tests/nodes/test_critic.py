"""Critic node: routing, separate-context safeguard, deterministic path, loop bound."""

import json
import sys
import types

from graphx.nodes.registry import load_builtin_nodes

from conftest import Harness, chan, edge, graph, node

load_builtin_nodes()

# a deterministic critic handler for the handler-path test
_crit_mod = types.ModuleType("tests_crit")
_crit_mod.always_pass = lambda artifact, criteria: {"verdict": "pass", "score": 1.0,
                                                    "reasons": "ok"}
_crit_mod.always_fail = lambda artifact, criteria: {"verdict": "fail", "score": 0.0,
                                                    "reasons": "nope"}
sys.modules["tests_crit"] = _crit_mod


def _verdict(v, s=None):
    return json.dumps({"verdict": v, "score": s if s is not None else (1.0 if v == "pass"
                                                                       else 0.0),
                       "reasons": "because"})


class TestCriticRouting:
    async def test_pass_routes_forward(self):
        from conftest import ScriptedLLM
        from graphx.engine.services import FakeClock, Services
        llm = ScriptedLLM([_verdict("pass")])
        h = Harness(graph(
            [node("seed", value="a draft", channel="draft"),
             node("review", type="critic", model="local/m", artifact="<state.draft>",
                  criteria="good?", require="pass"),
             node("publish", value="PUB", channel="out"),
             node("revise", value="REV", channel="out")],
            [edge("seed", "review"),
             edge("review", "publish", when="review.verdict == 'pass'"),
             edge("review", "revise", when="review.verdict == 'fail'")],
            entry=["seed"],
            channels={"draft": chan("draft"), "out": chan("out")},
        ), services=Services(clock=FakeClock(), llm=llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["out"] == "PUB"

    async def test_fail_writes_feedback_and_routes_back(self):
        from conftest import ScriptedLLM
        from graphx.engine.services import FakeClock, Services
        llm = ScriptedLLM([_verdict("fail", 0.2)])
        h = Harness(graph(
            [node("seed", value="bad", channel="draft"),
             node("review", type="critic", model="local/m", artifact="<state.draft>",
                  criteria="good?", min_score=0.8, feedback_channel="fb"),
             node("out", value="X", channel="result")],
            [edge("seed", "review"),
             edge("review", "out", when="review.verdict == 'pass'")],
            entry=["seed"],
            channels={"draft": chan("draft"), "fb": chan("fb", default=""),
                      "result": chan("result", default="")},
        ), services=Services(clock=FakeClock(), llm=llm))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["fb"] == "because"       # feedback written to state
        assert outcome.state["result"] == ""           # did not pass forward


class TestSeparateContext:
    async def test_critic_context_is_criteria_and_artifact_only(self):
        from conftest import ScriptedLLM
        from graphx.engine.services import FakeClock, Services
        llm = ScriptedLLM([_verdict("pass")])
        h = Harness(graph(
            [node("seed", value="THE-ARTIFACT-TEXT", channel="draft"),
             node("review", type="critic", model="local/m", artifact="<state.draft>",
                  criteria="MY-CRITERIA", require="pass")],
            [edge("seed", "review"), edge("review", "end")],
            entry=["seed"], channels={"draft": chan("draft")},
        ), services=Services(clock=FakeClock(), llm=llm))
        await h.run()
        msgs = llm.calls[0]["messages"]
        blob = json.dumps(msgs)
        assert "MY-CRITERIA" in blob and "THE-ARTIFACT-TEXT" in blob
        # the critic must NOT have any assistant/tool turns from a producer
        assert all(m["role"] in ("system", "user") for m in msgs)


class TestDeterministicCritic:
    async def test_handler_critic_no_llm(self):
        # no llm service configured → must still work via the handler path
        from graphx.engine.services import FakeClock, Services
        h = Harness(graph(
            [node("seed", value="x", channel="draft"),
             node("review", type="critic", handler="tests_crit:always_pass",
                  artifact="<state.draft>", criteria="c", require="pass"),
             node("pub", value="P", channel="out")],
            [edge("seed", "review"),
             edge("review", "pub", when="review.verdict == 'pass'")],
            entry=["seed"], channels={"draft": chan("draft"), "out": chan("out")},
        ), services=Services(clock=FakeClock()))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["out"] == "P"


class TestValidateWarning:
    def test_same_model_warns(self):
        from graphx.model.validate import validate_graph
        from graphx.nodes.registry import known_types
        g = graph(
            [node("w", type="agent", model="local/m", prompt="p"),
             node("review", type="critic", model="local/m", artifact="<w.text>",
                  criteria="c", require="pass")],
            [edge("w", "review")], entry=["w"],
        )
        issues = validate_graph(g, known_types=known_types())
        assert any("same model" in i.message for i in issues if i.severity == "warning")
