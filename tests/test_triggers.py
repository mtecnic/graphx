"""Trigger parsing + OS unit generation."""

from datetime import datetime, timezone

import pytest

from graphx.scheduler import Scheduler, next_delay
from graphx.triggers import Trigger, TriggerError, load_triggers, parse_trigger
from graphx.units import cron_to_oncalendar, crontab_line, systemd_units


class TestParse:
    def test_schedule(self):
        t = parse_trigger({"type": "schedule", "cron": "0 7 * * *", "input": {"x": 1}})
        assert t.type == "schedule" and t.cron == "0 7 * * *" and t.input == {"x": 1}

    def test_interval_duration(self):
        assert parse_trigger({"type": "interval", "every": "15m"}).every_s == 900.0
        assert parse_trigger({"type": "interval", "every": "2h"}).every_s == 7200.0
        assert parse_trigger({"type": "interval", "every": 30}).every_s == 30.0

    def test_webhook(self):
        t = parse_trigger({"type": "webhook", "path": "/orders/"})
        assert t.type == "webhook" and t.path == "orders" and t.input_from == "body"

    def test_bad_cron_rejected(self):
        with pytest.raises(TriggerError):
            parse_trigger({"type": "schedule", "cron": "not a cron"})

    def test_unknown_type(self):
        with pytest.raises(TriggerError):
            parse_trigger({"type": "carrier-pigeon"})

    def test_missing_fields(self):
        with pytest.raises(TriggerError):
            parse_trigger({"type": "schedule"})
        with pytest.raises(TriggerError):
            parse_trigger({"type": "webhook"})

    def test_load_from_dict(self):
        triggers = load_triggers({"triggers": [
            {"type": "interval", "every": "5m"},
            {"type": "webhook", "path": "hook"}]})
        assert [t.type for t in triggers] == ["interval", "webhook"]

    def test_no_triggers_section(self):
        assert load_triggers({"name": "x"}) == []


class TestNextDelay:
    def test_interval(self):
        t = Trigger("interval", every_s=120.0)
        assert next_delay(t, datetime.now(timezone.utc)) == 120.0

    def test_schedule(self):
        t = Trigger("schedule", cron="0 7 * * *")
        now = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)
        assert next_delay(t, now) == 3600.0     # 06:00 → 07:00


class TestScheduler:
    async def test_fires_interval(self):
        fired: list = []

        async def fire(wf, inp):
            fired.append((wf, inp))

        sched = Scheduler(fire)
        sched.add("wf.yaml", [Trigger("interval", every_s=0.01, input={"k": "v"})])
        sched.start()
        import asyncio
        for _ in range(100):
            await asyncio.sleep(0.01)
            if fired:
                break
        await sched.stop()
        assert fired and fired[0][1] == {"k": "v"}

    def test_add_only_time_triggers(self):
        sched = Scheduler(lambda w, i: None)
        sched.add("wf.yaml", [Trigger("webhook", path="x"),
                              Trigger("interval", every_s=60)])
        assert len(sched.jobs) == 1          # webhook is not a scheduled job


class TestUnits:
    def test_oncalendar(self):
        assert cron_to_oncalendar("0 7 * * *") == "*-*-* 7:0:00"
        assert "Mon" in cron_to_oncalendar("30 9 * * 1")

    def test_bad_field_count(self):
        with pytest.raises(ValueError):
            cron_to_oncalendar("0 7 * *")

    def test_systemd_units(self, tmp_path):
        units = systemd_units("myflow", tmp_path / "f.yaml", "0 7 * * *",
                              graphx_bin="/usr/bin/graphx", unit_dir=tmp_path)
        assert "ExecStart=/usr/bin/graphx run" in units.service
        assert "OnCalendar=*-*-* 7:0:00" in units.timer
        assert units.timer_path.name == "graphx-myflow.timer"

    def test_crontab_line(self, tmp_path):
        line = crontab_line(tmp_path / "f.yaml", "0 7 * * *", graphx_bin="graphx")
        assert line.startswith("0 7 * * * graphx run")
