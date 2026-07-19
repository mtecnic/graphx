"""Retry classes, fallbacks, timeouts, and the four run guards."""

from graphx.engine.services import FakeClock, Services
from graphx.model.graph import (
    Budget, GraphConfig, RetryPolicy, StaticFallback,
)

from conftest import Harness, edge, graph, node


class TestTransientRetry:
    async def test_retries_transient_until_success(self):
        h = Harness(graph(
            [node("flaky", type="t_flaky", key="k1", fail_times=2,
                  retry=RetryPolicy(attempts=3, jitter=False))],
            [edge("flaky", "end")], entry=["flaky"],
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        retries = h.events_of("node_retrying")
        assert len(retries) == 2
        # exponential backoff: 0.5, then 1.0
        clock: FakeClock = h.services.clock
        assert clock.sleeps == [0.5, 1.0]

    async def test_deterministic_error_not_retried(self):
        h = Harness(graph(
            [node("flaky", type="t_flaky", key="k2", fail_times=1, transient=False,
                  retry=RetryPolicy(attempts=5))],
            [edge("flaky", "end")], entry=["flaky"],
        ))
        outcome = await h.run()
        assert outcome.status == "failed"
        assert h.events_of("node_retrying") == []
        assert outcome.dead_letters[0].attempts == 1

    async def test_retry_exhaustion_dead_letters(self):
        h = Harness(graph(
            [node("bad", type="t_boom", transient=True,
                  retry=RetryPolicy(attempts=3, jitter=False))],
            [edge("bad", "end")], entry=["bad"],
        ))
        outcome = await h.run()
        assert outcome.status == "failed"
        assert outcome.dead_letters[0].attempts == 3


class TestFallbacks:
    async def test_static_fallback_degrades_gracefully(self):
        h = Harness(graph(
            [node("bad", type="t_boom", transient=True,
                  retry=RetryPolicy(attempts=2, jitter=False),
                  fallbacks=[StaticFallback(output={"answer": "degraded"})]),
             node("after", value="ran")],
            [edge("bad", "after")], entry=["bad"],
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        fallback_events = h.events_of("node_fallback")
        assert fallback_events and fallback_events[0].data["to"] == "static"
        finished = h.events_of("node_finished")
        assert finished[0].data["output"] == {"answer": "degraded"}
        assert finished[0].data["degraded"] is True


class TestTimeout:
    async def test_timeout_is_transient_then_dead_letters(self):
        # real asyncio sleep beats a 1ms timeout; retries then exhaust
        services = Services()  # real clock so asyncio.timeout works
        h = Harness(graph(
            [node("slow", type="shell", command=["sleep", "5"],
                  timeout=0.05, retry=RetryPolicy(attempts=2, base=0.01, jitter=False))],
            [edge("slow", "end")], entry=["slow"],
        ), services=services)
        outcome = await h.run()
        assert outcome.status == "failed"
        assert outcome.dead_letters[0].error_type == "NodeTimeout"
        assert outcome.dead_letters[0].attempts == 2


class TestGuards:
    async def test_max_steps_stops_unguarded_loop(self):
        h = Harness(graph(
            [node("a", value=1),
             node("loop", type="condition", branches=[{"if": "True", "goto": "a"}])],
            [edge("a", "loop")], entry=["a"],
            config=GraphConfig(max_steps=6),
        ))
        outcome = await h.run()
        assert outcome.status == "guard_tripped"
        assert "max_steps" in (outcome.error or "")

    async def test_deadline_guard(self):
        h = Harness(graph(
            [node("s1", type="t_sleep", seconds=50.0),
             node("s2", type="t_sleep", seconds=50.0)],
            [edge("s1", "s2"), edge("s2", "s1")], entry=["s1"],
            config=GraphConfig(max_steps=1000, budget=Budget(deadline_s=75.0)),
        ))
        outcome = await h.run()
        assert outcome.status == "guard_tripped"
        assert "deadline" in (outcome.error or "")


class TestCache:
    async def test_cached_node_skips_execution(self):
        g = graph(
            [node("flaky", type="t_flaky", key="c1", fail_times=0, value="v1",
                  cache=True)],
            [edge("flaky", "end")], entry=["flaky"],
        )
        h = Harness(g)
        await h.run(thread_id="t1")
        # second thread, same inputs: served from cache, handler not called again
        from conftest import FLAKY_CALLS
        assert FLAKY_CALLS["c1"] == 1
        outcome = await h.run(thread_id="t2")
        assert outcome.status == "finished"
        assert FLAKY_CALLS["c1"] == 1
        assert h.events_of("node_cached")
