"""End-to-end leak regression: secrets reach consumption, never persistence."""

import json

import httpx
import pytest
import respx

from graphx.engine.events import EventBus, RunEvent
from graphx.engine.executor import Executor
from graphx.engine.services import Services
from graphx.model.yaml_loader import build_graph
from graphx.nodes.registry import load_builtin_nodes
from graphx.persistence.db import open_db
from graphx.persistence.sqlite_checkpointer import SqliteCheckpointer
from graphx.secrets import Redactor, SecretResolver, SecretStore

load_builtin_nodes()

SECRET = "sk-topsecret-abcdef123456"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHX_HOME", str(tmp_path))
    monkeypatch.setattr("graphx.secrets._keyring", lambda: None)


def _services_with_secret(name="k", value=SECRET):
    store = SecretStore()
    store.set(name, value)
    resolver = SecretResolver(store)
    return Services(secrets=resolver, redactor=Redactor(resolver.used_values)), resolver


async def _run(graph, services, db_path, thread="t1", inp=None):
    db = await open_db(db_path)
    checkpointer = SqliteCheckpointer(db)
    events: list[RunEvent] = []
    bus = EventBus(run_id="r1", thread_id=thread, sink=checkpointer.event_sink)
    bus_events = bus.subscribe()

    async def collect():
        async for e in bus_events:
            events.append(e)

    import asyncio
    task = asyncio.create_task(collect())
    executor = Executor(graph, checkpointer, bus, services)
    try:
        outcome = await executor.run(thread, inp or {})
    finally:
        bus.close()
        await task
        await db.close()
    return outcome, events


SHELL_WF = {
    "version": 1, "name": "leaky_shell",
    "state": {"seen": {"type": "str", "default": ""}},
    "entry": ["use"],
    "nodes": [{
        "id": "use", "type": "shell",
        "command": ["sh", "-c", "echo got-$TOKEN"],
        "env": {"TOKEN": "secret://k"},
        "updates": {"seen": "<self.stdout>"},
    }],
    "edges": [{"from": "use", "to": "end"}],
}


class TestShellLeak:
    async def test_subprocess_gets_value_but_nothing_persists_it(self, tmp_path):
        graph = build_graph(dict(SHELL_WF))
        services, _ = _services_with_secret()
        db_path = tmp_path / "run.db"
        outcome, events = await _run(graph, services, db_path)

        assert outcome.status == "finished"
        # outcome.state is redacted (this is what the server API returns)
        assert SECRET not in json.dumps(outcome.state)
        assert "***" in outcome.state["seen"]

        # no event carries the raw value
        for event in events:
            assert SECRET not in json.dumps(event.data), event.type

        # the SQLite file contains no copy of the secret, anywhere
        raw = db_path.read_bytes()
        assert SECRET.encode() not in raw

    async def test_the_subprocess_actually_received_the_real_value(self, tmp_path, monkeypatch):
        # prove resolution really happened: write the env value to a file and read it
        outfile = tmp_path / "captured.txt"
        wf = dict(SHELL_WF)
        wf["nodes"] = [{
            "id": "use", "type": "shell",
            "command": ["sh", "-c", f"printf %s \"$TOKEN\" > {outfile}"],
            "env": {"TOKEN": "secret://k"},
        }]
        wf["edges"] = [{"from": "use", "to": "end"}]
        graph = build_graph(wf)
        services, _ = _services_with_secret()
        await _run(graph, services, tmp_path / "r.db")
        assert outfile.read_text() == SECRET   # subprocess saw the true value


API_WF = {
    "version": 1, "name": "leaky_api",
    "entry": ["call"],
    "nodes": [{
        "id": "call", "type": "api", "method": "GET",
        "url": "https://api.test/data",
        "headers": {"Authorization": "Bearer secret://k"},
    }],
    "edges": [{"from": "call", "to": "end"}],
}


class TestApiLeak:
    @respx.mock
    async def test_outbound_header_has_value_db_has_placeholder(self, tmp_path):
        route = respx.get("https://api.test/data").mock(
            return_value=httpx.Response(200, json={"ok": True}))
        graph = build_graph(dict(API_WF))
        services, _ = _services_with_secret()
        db_path = tmp_path / "api.db"
        outcome, events = await _run(graph, services, db_path)

        assert outcome.status == "finished"
        # (a) the real secret went out on the wire
        sent = route.calls[0].request.headers["authorization"]
        assert sent == f"Bearer {SECRET}"
        # (b) nothing durable holds the value
        assert SECRET.encode() not in db_path.read_bytes()
        for event in events:
            assert SECRET not in json.dumps(event.data)


class TestNoSecretsIsNoop:
    async def test_redaction_noop_without_secrets(self, tmp_path):
        # a plain workflow with default Services must be byte-identical behavior
        graph = build_graph({
            "version": 1, "name": "plain",
            "state": {"x": {"type": "str", "default": ""}},
            "entry": ["e"],
            "nodes": [{"id": "e", "type": "shell", "command": ["echo", "hello"],
                       "updates": {"x": "<self.stdout>"}}],
            "edges": [{"from": "e", "to": "end"}],
        })
        outcome, events = await _run(graph, Services(), tmp_path / "p.db")
        assert outcome.status == "finished"
        assert outcome.state["x"] == "hello\n"     # untouched, no masking
