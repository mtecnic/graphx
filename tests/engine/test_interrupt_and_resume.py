"""Human gates, interrupt/resume, and crash-resume equivalence."""

from graphx.engine.checkpoint import MemoryCheckpointer
from graphx.engine.interrupts import Command

from conftest import Harness, chan, edge, graph, node


def gate_graph():
    return graph(
        [node("work", value={"draft": "v1"}, channel="draft"),
         node("gate", type="human", prompt="Approve?", choices=["approve", "reject"],
              payload={"draft": "<state.draft>"}),
         node("publish", value="published", channel="result"),
         node("trash", value="discarded", channel="result")],
        [edge("work", "gate"),
         edge("gate", "publish", when="gate.choice == 'approve'"),
         edge("gate", "trash", when="gate.choice == 'reject'")],
        entry=["work"],
        channels={"draft": chan("draft"), "result": chan("result")},
    )


class TestInterruptResume:
    async def test_gate_interrupts_then_resumes(self):
        h = Harness(gate_graph())
        outcome = await h.run()
        assert outcome.status == "interrupted"
        assert outcome.interrupt.node_id == "gate"
        assert outcome.interrupt.payload["prompt"] == "Approve?"
        # payload refs were resolved against checkpointed state
        assert outcome.interrupt.payload["payload"] == {"draft": {"draft": "v1"}}

        outcome2 = await h.run(Command(resume="approve"))
        assert outcome2.status == "finished"
        assert outcome2.state["result"] == "published"

    async def test_reject_routes_to_trash(self):
        h = Harness(gate_graph())
        await h.run()
        outcome = await h.run(Command(resume="reject"))
        assert outcome.status == "finished"
        assert outcome.state["result"] == "discarded"

    async def test_bad_choice_fails(self):
        h = Harness(gate_graph())
        await h.run()
        outcome = await h.run(Command(resume="maybe"))
        assert outcome.status == "failed"

    async def test_resume_with_state_update(self):
        h = Harness(gate_graph())
        await h.run()
        outcome = await h.run(Command(resume="approve", update={"draft": "edited"}))
        assert outcome.status == "finished"
        assert outcome.state["draft"] == "edited"


def loop_graph():
    return graph(
        [node("start", value=0, channel="n"),
         node("bump", type="function", handler="graphx.demo:increment",
              args={"count": "<state.n>"}, updates={"n": "<self.count>"}),
         node("split", type="function", handler="graphx.demo:split_words",
              args={"text": "a b c"}),
         node("m", type="merge"),
         node("check", type="condition",
              branches=[{"if": "n < 4", "goto": "bump"}, {"else": "done"}]),
         node("done", value="fin", channel="result")],
        [edge("start", "bump"), edge("start", "split"),
         edge("bump", "m"), edge("split", "m"), edge("m", "check")],
        entry=["start"],
        channels={"n": chan("n"), "result": chan("result")},
    )


class TestResumeEquivalence:
    async def test_resume_from_every_checkpoint_matches_full_run(self):
        # note: 'split'+'bump' rejoin at a merge, then loop via condition
        full = Harness(loop_graph())
        final = await full.run(thread_id="full")
        assert final.status == "finished"

        checkpoints = full.checkpointer._checkpoints["full"]
        assert len(checkpoints) > 3

        for cut in range(len(checkpoints) - 1):
            # simulate a crash after checkpoint `cut`: preload only the prefix
            partial = MemoryCheckpointer()
            partial._checkpoints["crashed"] = [
                # rewrite thread id so resume finds them
                type(c)(**{**c.__dict__, "thread_id": "crashed"})
                for c in checkpoints[:cut + 1]
            ]
            h = Harness(loop_graph(), checkpointer=partial)
            outcome = await h.run(Command(), thread_id="crashed")
            assert outcome.status == "finished", f"cut at checkpoint {cut}"
            assert outcome.state == final.state, f"state diverged when cut at {cut}"
