"""Async scheduler — fires workflows on schedule/interval triggers.

Pure asyncio, no OS coupling: each schedule/interval trigger gets a
background task that sleeps until its next fire time and calls the
injected `fire(workflow_path, input)` coroutine. Webhook triggers are
handled by the server (routes), not here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .triggers import Trigger

FireFn = Callable[[Path, dict], Awaitable[None]]


def next_delay(trigger: Trigger, now: datetime) -> float:
    """Seconds from `now` until this trigger should next fire."""
    if trigger.type == "interval":
        return trigger.every_s or 0.0
    if trigger.type == "schedule":
        from croniter import croniter
        nxt = croniter(trigger.cron, now).get_next(datetime)
        return max(0.0, (nxt - now).total_seconds())
    raise ValueError(f"{trigger.type} triggers are not time-scheduled")


@dataclass
class ScheduledJob:
    workflow: Path
    trigger: Trigger
    next_fire: datetime | None = None


class Scheduler:
    def __init__(self, fire: FireFn, clock=None):
        self._fire = fire
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._tasks: list[asyncio.Task] = []
        self.jobs: list[ScheduledJob] = []

    def add(self, workflow: Path, triggers: list[Trigger]) -> None:
        for trigger in triggers:
            if trigger.type not in ("schedule", "interval"):
                continue
            if trigger.type == "schedule" and not self._cron_ok(trigger.cron):
                continue                      # bad cron / no croniter — skip, don't crash
            self.jobs.append(ScheduledJob(Path(workflow), trigger))

    @staticmethod
    def _cron_ok(cron: str | None) -> bool:
        try:
            from croniter import croniter
        except ImportError:
            import logging
            logging.getLogger("graphx.scheduler").warning(
                "croniter not installed; schedule trigger '%s' ignored", cron)
            return False
        if not croniter.is_valid(str(cron)):
            import logging
            logging.getLogger("graphx.scheduler").warning(
                "invalid cron '%s'; schedule ignored", cron)
            return False
        return True

    def start(self) -> None:
        for job in self.jobs:
            self._tasks.append(asyncio.create_task(self._run_job(job)))

    async def _run_job(self, job: ScheduledJob) -> None:
        while True:
            try:
                delay = next_delay(job.trigger, self._clock())
            except Exception:  # noqa: BLE001 — a cron eval failure must not kill the job
                import logging
                logging.getLogger("graphx.scheduler").exception(
                    "next-fire computation failed for %s; retrying in 60s", job.workflow)
                delay = 60.0
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            try:
                await self._fire(job.workflow, dict(job.trigger.input))
            except Exception:  # noqa: BLE001 — one bad run must not kill the schedule
                pass

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    def next_fire_times(self) -> list[dict]:
        now = self._clock()
        out = []
        for job in self.jobs:
            secs = next_delay(job.trigger, now)
            out.append({"workflow": str(job.workflow),
                        "trigger": job.trigger.describe(),
                        "next_fire_in_s": round(secs, 1)})
        return out
