# graphx

**TUI-native designer and runner for agentic workflows.**

graphx lets you define flow-chart-like agent pipelines — with loops, branches, parallel fan-outs, and human approval gates — in a plain YAML file, then run and watch them live in your terminal. Nodes can be LLM agents, external HTTP APIs, MCP tools, Python functions, or shell commands.

```
┌─────────────┐     ┌─────────────┐
│ fetch_trends│──┐  │ search_docs │
└─────────────┘  │  └──────┬──────┘
                 ▼         ▼
              ┌──────────────┐      ┌─────────────┐
              │    gather    │─────▶│ write_draft │◀─┐
              └──────────────┘      └──────┬──────┘  │
                                           ▼         │
                                    ┌─────────────┐  │
                                    │ score_draft │──┘ (loop until good)
                                    └─────────────┘
```

## Design principles

- **YAML is the source of truth** — layout-free, diffable, hand-editable. The TUI renders it; it never owns it.
- **Pregel-style engine** — shared state channels with reducers, supersteps, cycles bounded by guards, full-snapshot checkpointing to SQLite. Kill it mid-run; `graphx resume` continues from the last superstep.
- **Resilience built in per node** — transient retries with backoff, model fallback chains, validation re-asks, timeouts, budgets, `on_error` edges, dead-letters. Four independent guards (steps / tokens / deadline / retries) stop runaway loops.
- **One event stream** — the CLI, the TUI, and the HTTP API all consume the same `RunEvent` stream.

## Node types

| kind | types |
|---|---|
| work | `agent` (LLM + tools + validated JSON output), `api` (HTTP + `$.json.paths`), `mcp` (MCP tool call), `function` (Python), `shell` (subprocess/CLI agents) |
| flow | `condition`, `router` (LLM-chosen path), `map` (fan-out over a collection), `merge` (barrier join with success threshold), `subworkflow` |
| control | `human` (approval gate — interrupts, resumable), `wait` |

LLM providers are configured per workflow (`providers:`) — anything OpenAI-compatible (vLLM, Ollama, LM Studio, gateways) plus native Anthropic. No SDK dependencies.

## Quickstart

```bash
python3 -m venv venv && ./venv/bin/pip install -e ".[tui,mcp,server]"
./venv/bin/graphx validate examples/hello.yaml
./venv/bin/graphx run examples/hello.yaml --input name=world
./venv/bin/graphx tui examples/hello.yaml        # press r to run, e to edit, a to add a node
./venv/bin/graphx serve examples --port 8420     # REST + SSE API
./venv/bin/graphx tui examples/hello.yaml --attach http://localhost:8420 --thread <id>
```

CLI: `validate` · `run` · `resume <thread> --answer …` · `events <run> [--json]` · `history <thread>` · `tui` · `serve`.

## HTTP API

`GET /workflows` · `POST /runs {workflow, input}` · `GET /runs/{thread}` ·
`POST /runs/{thread}/resume {answer}` · `POST /runs/{thread}/cancel` ·
`GET /runs/{thread}/events` (SSE, honors `Last-Event-ID`).

## Status

v0.4 — engine, all node types above, CLI, TUI (viewer/runner/editor palette + file watch), HTTP API + SSE, ~90 tests. Roadmap: richer TUI edge routing, per-thread run browser, remote run control from the TUI, provider pricing tables.
