"""HTTP API tests over ASGITransport (no sockets)."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from graphx.server.app import create_app

EXAMPLES = Path(__file__).parents[2] / "examples"


@pytest.fixture()
def api(tmp_path):
    app = create_app(EXAMPLES, tmp_path / "server.db")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def wait_status(client, thread_id, wanted, timeout=15.0):
    for _ in range(int(timeout / 0.05)):
        response = await client.get(f"/runs/{thread_id}")
        if response.status_code == 200:
            body = response.json()
            if body["status"] in wanted and not body["running"]:
                return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"thread {thread_id} never reached {wanted}")


class TestServer:
    async def test_list_workflows(self, api):
        async with api as client:
            response = await client.get("/workflows")
            names = {w["name"] for w in response.json()}
            assert {"hello", "approval"} <= names

    async def test_run_hello_to_completion(self, api):
        async with api as client:
            response = await client.post("/runs", json={
                "workflow": "hello.yaml", "input": {"name": "server"}})
            assert response.status_code == 201
            thread_id = response.json()["thread_id"]
            body = await wait_status(client, thread_id, {"finished"})
            assert body["state"]["count"] == 3
            assert body["state"]["shouts"] == ["HELLO,", "SERVER!"]

    async def test_interrupt_resume_flow(self, api):
        async with api as client:
            response = await client.post("/runs", json={"workflow": "approval.yaml"})
            thread_id = response.json()["thread_id"]
            body = await wait_status(client, thread_id, {"interrupted"})
            assert body["interrupt"]["node_id"] == "gate"
            assert body["interrupt"]["payload"]["prompt"] == "Publish this draft?"

            response = await client.post(f"/runs/{thread_id}/resume",
                                         json={"answer": "approve"})
            assert response.status_code == 200
            body = await wait_status(client, thread_id, {"finished"})
            assert body["state"]["result"].strip() == "PUBLISHED"

    async def test_events_sse_replay(self, api):
        async with api as client:
            response = await client.post("/runs", json={
                "workflow": "hello.yaml", "input": {"name": "sse"}})
            thread_id = response.json()["thread_id"]
            await wait_status(client, thread_id, {"finished"})

            types = []
            last_id = None
            async with client.stream("GET", f"/runs/{thread_id}/events") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("id:"):
                        last_id = int(line[3:].strip())
                    if line.startswith("data:"):
                        event = json.loads(line[5:].strip())
                        types.append(event["type"])
                        if event["type"] == "run_finished":
                            break
            assert "run_started" in types and "run_finished" in types
            assert types.count("run_started") == 1

            # Last-Event-ID resumes mid-stream without duplicates
            replay_types = []
            headers = {"last-event-id": str(last_id - 2)}
            async with client.stream("GET", f"/runs/{thread_id}/events",
                                     headers=headers) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data:"):
                        replay_types.append(json.loads(line[5:].strip())["type"])
                        if replay_types[-1] == "run_finished":
                            break
            assert len(replay_types) == 2

    async def test_unknown_thread_404(self, api):
        async with api as client:
            assert (await client.get("/runs/nope")).status_code == 404
            assert (await client.get("/runs/nope/events")).status_code == 404

    async def test_bad_workflow_422(self, api, tmp_path):
        bad = EXAMPLES / "definitely_missing.yaml"
        async with api as client:
            response = await client.post("/runs", json={"workflow": str(bad)})
            assert response.status_code == 404
