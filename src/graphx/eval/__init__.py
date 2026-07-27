"""Eval / ops — a read-only observer over the run data (events + checkpoints).

This layer only READS the durable data and drives runs; it never touches
the executor, state, or graph flow.
"""

from .dataset import EvalCase, EvalDataset, load_dataset
from .metrics import NodeMetricsReport, RunMetricsReport, derive_run_report
from .report import CaseResult, EvalReport
from .store import EventStore, RunRow

__all__ = [
    "CaseResult", "EvalCase", "EvalDataset", "EvalReport", "EventStore",
    "NodeMetricsReport", "RunMetricsReport", "RunRow", "derive_run_report",
    "load_dataset",
]
