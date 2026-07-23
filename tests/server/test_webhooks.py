"""Webhook triggers + /schedules on the daemon."""

import asyncio

import httpx
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("croniter")

from graphx.server.app import create_app


def _write(dirpath, name, body):
    (dirpath / name).write_text(body)


WEBHOOK_WF = """\
version: 1
name: on_demand
triggers:
  - { type: webhook, path: "orders", input_from: body }
state:
  who: { type: str, default: "" }
entry: [greet]
nodes:
  - id: greet
    type: shell
    command: ["echo", "hi <state.who>"]
    updates: { who: "<state.who>" }
edges:
  - { from: greet, to: end }
"""

INTERVAL_WF = """\
version: 1
name: ticker
triggers:
  - { type: interval, every: 300s }
entry: [t]
nodes:
  - { id: t, type: shell, command: ["echo", "tick"] }
edges:
  - { from: t, to: end }
"""


async def _client(app):
    """Client that also drives the ASGI lifespan (so triggers get indexed)."""
    transport = httpx.ASGITransport(app=app)
    return transport


class TestWebhooks:
    async def test_webhook_fires_run_with_body_as_input(self, tmp_path):
        _write(tmp_path, "on_demand.yaml", WEBHOOK_WF)
        app = create_app(tmp_path, tmp_path / "db.sqlite")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as client:
                r = await client.post("/hooks/orders", json={"who": "sam"})
                assert r.status_code == 201
                thread = r.json()["thread_id"]
                # the run was created with the body as input
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    status = (await client.get(f"/runs/{thread}")).json()
                    if not status["running"]:
                        break
                assert status["status"] == "finished"
                assert status["state"]["who"] == "sam"

    async def test_unknown_hook_404(self, tmp_path):
        _write(tmp_path, "on_demand.yaml", WEBHOOK_WF)
        app = create_app(tmp_path, tmp_path / "db.sqlite")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as client:
                assert (await client.post("/hooks/nope", json={})).status_code == 404

    async def test_schedules_endpoint_lists_triggers(self, tmp_path):
        _write(tmp_path, "ticker.yaml", INTERVAL_WF)
        _write(tmp_path, "on_demand.yaml", WEBHOOK_WF)
        app = create_app(tmp_path, tmp_path / "db.sqlite")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as client:
                data = (await client.get("/schedules")).json()
                assert any("interval" in s["trigger"] for s in data["scheduled"])
                assert "/hooks/orders" in data["webhooks"]

    async def test_triggers_disabled(self, tmp_path):
        _write(tmp_path, "on_demand.yaml", WEBHOOK_WF)
        app = create_app(tmp_path, tmp_path / "db.sqlite", triggers=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as client:
                # no triggers indexed → webhook not registered
                assert (await client.post("/hooks/orders", json={})).status_code == 404
