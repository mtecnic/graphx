"""Emit OS scheduling units (systemd user timer / crontab) for a workflow.

For schedule-only workflows that should run without the daemon up.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# 5-field cron → systemd OnCalendar (best-effort for the common cases).
_DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def cron_to_oncalendar(cron: str) -> str:
    """Translate a standard 5-field cron to a systemd OnCalendar string."""
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"expected a 5-field cron, got {cron!r}")
    minute, hour, dom, month, dow = parts

    def norm(field: str, star: str) -> str:
        return star if field == "*" else field

    dow_out = "*"
    if dow != "*":
        try:
            dow_out = ",".join(_DOW[int(d) % 7] for d in dow.split(","))
        except ValueError:
            dow_out = dow  # already names
    date = f"{norm(month, '*')}-{norm(dom, '*')}"
    time = f"{norm(hour, '*')}:{norm(minute, '*')}:00"
    prefix = f"{dow_out} " if dow_out != "*" else ""
    return f"{prefix}*-{date} {time}".replace("*-*-*", "*-*-*").strip()


@dataclass(frozen=True)
class SystemdUnits:
    name: str
    service: str
    timer: str
    service_path: Path
    timer_path: Path


def systemd_units(name: str, workflow: Path, cron: str,
                  graphx_bin: str | None = None,
                  unit_dir: Path | None = None) -> SystemdUnits:
    exe = graphx_bin or shutil.which("graphx") or "graphx"
    workflow = Path(workflow).resolve()
    oncal = cron_to_oncalendar(cron)
    service = f"""\
[Unit]
Description=graphx workflow: {name}
After=network-online.target

[Service]
Type=oneshot
ExecStart={exe} run {workflow}
"""
    timer = f"""\
[Unit]
Description=graphx timer: {name} ({cron})

[Timer]
OnCalendar={oncal}
Persistent=true

[Install]
WantedBy=timers.target
"""
    unit_dir = unit_dir or (Path.home() / ".config" / "systemd" / "user")
    return SystemdUnits(
        name=name, service=service, timer=timer,
        service_path=unit_dir / f"graphx-{name}.service",
        timer_path=unit_dir / f"graphx-{name}.timer",
    )


def crontab_line(workflow: Path, cron: str, graphx_bin: str | None = None) -> str:
    exe = graphx_bin or shutil.which("graphx") or "graphx"
    return f"{cron} {exe} run {Path(workflow).resolve()}"
