"""Regression tests for the audit bug fixes."""

import pytest

from graphx.nodes.registry import load_builtin_nodes

from conftest import Harness, chan, edge, graph, node

load_builtin_nodes()


# ---- #1 merge vs condition-skipped branches ----

class TestMergeConditionSkip:
    async def test_direct_skip_does_not_fail_merge(self):
        # A always → M; B → M only when flag (false here). B's edge to M is
        # skipped; the merge must still proceed (skipped ≠ failed).
        h = Harness(graph(
            [node("a", value="A"), node("b", value="B"),
             node("m", type="merge"), node("after", value="done", channel="out")],
            [edge("a", "m"), edge("b", "m", when="flag"), edge("m", "after")],
            entry=["a", "b"],
            channels={"out": chan("out"), "flag": chan("flag", default=False)},
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["out"] == "done"

    async def test_transitive_skip_still_fires_merge(self):
        # condition routes to A only, pruning the B subtree that also feeds M.
        # M must still fire (flushed with B=skipped), not be silently dropped.
        h = Harness(graph(
            [node("start", value=1, channel="x"),
             node("pick", type="condition",
                  branches=[{"if": "x > 0", "goto": "a"}, {"else": "b"}]),
             node("a", value="A"), node("b", value="B"),
             node("m", type="merge"), node("done", value="fin", channel="out")],
            [edge("start", "pick"), edge("a", "m"), edge("b", "m"),
             edge("m", "done")],
            entry=["start"],
            channels={"x": chan("x"), "out": chan("out")},
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        assert outcome.state["out"] == "fin"          # merge + downstream ran

    async def test_genuine_failure_still_respects_threshold(self):
        # both branches fail, threshold default = non-skipped count (2) → fatal
        h = Harness(graph(
            [node("a", type="t_boom"), node("b", type="t_boom"),
             node("m", type="merge"), node("after", value="x")],
            [edge("a", "m"), edge("b", "m"), edge("m", "after")],
            entry=["a", "b"],
        ))
        outcome = await h.run()
        assert outcome.status == "failed"


# ---- #2 ${ENV} unset is lenient at load/validate ----

class TestEnvLenient:
    def test_unset_env_var_still_loads(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_UNSET_XYZ", raising=False)
        from graphx.model.yaml_loader import build_graph
        g = build_graph({
            "version": 1, "name": "e", "entry": ["n"],
            "nodes": [{"id": "n", "type": "api", "url": "https://x",
                       "headers": {"Authorization": "Bearer ${DEFINITELY_UNSET_XYZ}"}}],
            "edges": [{"from": "n", "to": "end"}],
        })
        # placeholder left in place, not a LoadError
        assert g.nodes["n"].config["headers"]["Authorization"] == \
            "Bearer ${DEFINITELY_UNSET_XYZ}"

    def test_set_env_var_still_interpolates(self, monkeypatch):
        monkeypatch.setenv("SET_VAR_ABC", "resolved")
        from graphx.model.yaml_loader import build_graph
        g = build_graph({
            "version": 1, "name": "e", "entry": ["n"],
            "nodes": [{"id": "n", "type": "shell", "command": ["echo", "${SET_VAR_ABC}"]}],
            "edges": [{"from": "n", "to": "end"}],
        })
        assert g.nodes["n"].config["command"] == ["echo", "resolved"]


# ---- #3 map item transient error retries ----

class TestMapRetry:
    async def test_transient_item_error_is_retried(self):
        h = Harness(graph(
            [node("m", type="map", over=[1], item_as="i",
                  node={"type": "t_flaky", "key": "mapk", "fail_times": 1,
                        "value": "ok"})],
            [edge("m", "end")], entry=["m"],
        ))
        # t_flaky raises TransientError once then succeeds; with the ExceptionGroup
        # unwrap fix the map node's retry policy applies.
        outcome = await h.run()
        assert outcome.status == "finished"
        assert h.events_of("node_retrying")           # it retried, didn't dead-letter

    async def test_map_keeps_none_output_items(self):
        # a function returning None is a valid successful item, not a dropped slot
        h = Harness(graph(
            [node("m", type="map", over=[1, 2], item_as="i", on_item_error="skip",
                  node={"type": "function", "handler": "tests_none:noop"},
                  collect={"channel": "out"})],
            [edge("m", "end")], entry=["m"],
            channels={"out": chan("out", reducer="extend", default=[])},
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        finished = h.events_of("node_finished")[0]
        assert finished.data["output"]["count"] == 2   # both items counted


# ---- #4-#7 triggers + shell ----

class TestTriggersAndShell:
    def test_interval_zero_rejected(self):
        from graphx.triggers import TriggerError, parse_trigger
        with pytest.raises(TriggerError):
            parse_trigger({"type": "interval", "every": 0})
        with pytest.raises(TriggerError):
            parse_trigger({"type": "interval", "every": "0s"})

    def test_one_bad_trigger_keeps_the_good(self):
        from graphx.triggers import load_triggers
        triggers = load_triggers({"triggers": [
            {"type": "schedule", "cron": "not a cron"},   # bad → skipped
            {"type": "webhook", "path": "ok"}]})           # good → kept
        assert [t.type for t in triggers] == ["webhook"]

    def test_scheduler_skips_bad_cron(self):
        from graphx.scheduler import Scheduler
        from graphx.triggers import Trigger
        s = Scheduler(lambda w, i: None)
        s.add("wf.yaml", [Trigger("schedule", cron="bogus"),
                          Trigger("interval", every_s=60)])
        assert len(s.jobs) == 1                            # bad cron not scheduled

    async def test_shell_stdin_ignored_command_succeeds(self):
        # `true` exits without reading stdin → BrokenPipe must not fail the node
        h = Harness(graph(
            [node("s", type="shell", command=["true"], stdin="ignored data")],
            [edge("s", "end")], entry=["s"],
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
