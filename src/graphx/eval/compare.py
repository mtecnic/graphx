"""Version compare: same dataset against two workflows / model overrides."""

from __future__ import annotations

from pathlib import Path

from ..model.graph import Graph
from .dataset import EvalDataset
from .report import CaseDiff, CompareReport
from .runner import run_dataset
from .trace import diff as trace_diff


async def compare(dataset: EvalDataset, graph_a: Graph, graph_b: Graph,
                  db_path: str | Path, label_a: str = "A", label_b: str = "B",
                  ) -> CompareReport:
    report_a, traces_a = await run_dataset(graph_a, dataset, db_path)
    report_b, traces_b = await run_dataset(graph_b, dataset, db_path)
    by_name_a = {c.name: c for c in report_a.cases}
    by_name_b = {c.name: c for c in report_b.cases}

    out = CompareReport(dataset=dataset.dataset, label_a=label_a, label_b=label_b)
    for case in dataset.cases:
        a, b = by_name_a[case.name], by_name_b[case.name]
        out.diffs.append(CaseDiff(
            name=case.name, a=a, b=b,
            trace_diff=trace_diff(traces_a[case.name], traces_b[case.name])))
    return out
