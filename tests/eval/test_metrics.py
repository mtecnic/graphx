"""derive_run_report / aggregate: metrics reconstructed from the event stream."""

from graphx.eval.metrics import aggregate, derive_run_report
from graphx.model.graph import RetryPolicy

from conftest import Harness, chan, edge, graph, node


class TestDeriveReport:
    async def test_retries_and_nodes_counted(self):
        # a node that fails twice then succeeds → 2 retries, 1 run
        h = Harness(graph(
            [node("flaky", type="t_flaky", key="k", fail_times=2, transient=True,
                  retry=RetryPolicy(attempts=5, base=0.0, jitter=False))],
            [edge("flaky", "end")], entry=["flaky"],
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        report = derive_run_report(h.events)
        assert report.status == "finished"
        assert report.retries == 2
        flaky = next(n for n in report.per_node if n.node_id == "flaky")
        assert flaky.runs == 1
        assert flaky.retries == 2

    async def test_loop_counts_per_node_runs(self):
        # bump increments; again loops back until count == 3
        h = Harness(graph(
            [node("bump", type="t_emit", value=1, channel="count"),
             node("again", type="t_emit", value="x")],
            [edge("bump", "again"),
             edge("again", "bump", when="count < 3"),
             edge("again", "end", when="count >= 3")],
            entry=["bump"],
            channels={"count": chan("count", reducer="sum", default=0)},
        ))
        outcome = await h.run()
        assert outcome.status == "finished"
        report = derive_run_report(h.events)
        bump = next(n for n in report.per_node if n.node_id == "bump")
        assert bump.runs == 3          # looped three times

    async def test_failed_run_status(self):
        h = Harness(graph(
            [node("boom", type="t_boom", transient=False)],
            [edge("boom", "end")], entry=["boom"],
        ))
        outcome = await h.run()
        report = derive_run_report(h.events)
        assert outcome.status == "failed"
        assert report.status == "failed"


class TestAggregate:
    async def test_aggregate_over_reports(self):
        reports = []
        for i in range(3):
            h = Harness(graph(
                [node("go", type="t_emit", value=i, channel="v")],
                [edge("go", "end")], entry=["go"],
                channels={"v": chan("v")},
            ))
            await h.run()
            reports.append(derive_run_report(h.events))
        agg = aggregate(reports)
        assert agg.runs == 3
        assert agg.passed == 3
        assert agg.failed == 0

    def test_aggregate_empty(self):
        agg = aggregate([])
        assert agg.runs == 0
