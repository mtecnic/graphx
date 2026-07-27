"""Optional LLM-judge check for an eval case — reuses the critic node handler
so the judge runs the exact same separate-context code path as production."""

from __future__ import annotations

from ..model.graph import Graph
from ..model.refs import RefContext, resolve
from ..runtime import graph_services
from .assertions import CheckResult
from .dataset import JudgeSpec


async def judge_case(judge: JudgeSpec, final_state: dict, node_outputs: dict,
                     graph: Graph) -> CheckResult:
    from ..nodes.critic import CriticConfig, critic_node
    from ..nodes.registry import NodeContext
    from ..model.graph import NodeSpec

    artifact = resolve(judge.artifact,
                       RefContext(state=final_state, node_outputs=node_outputs))
    cfg = CriticConfig(artifact=artifact, criteria=judge.criteria,
                       model=judge.model, min_score=judge.min_score)
    spec = NodeSpec(id="__judge__", type="critic", config=cfg.model_dump())
    async with graph_services(graph) as services:
        ctx = NodeContext(node=spec, config=cfg, state_values=final_state,
                          node_outputs=node_outputs, services=services,
                          thread_id="eval", run_id="eval", step=0)
        result = await critic_node(ctx)
    out = result.output
    ok = out.get("verdict") == "pass"
    score = out.get("score")
    return CheckResult("judge",
                       f"judge score {score} >= {judge.min_score} — "
                       f"{str(out.get('reasons',''))[:80]}", ok)
