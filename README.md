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
./venv/bin/graphx tui                            # no args: opens the workflow here, or
                                                 # scaffolds the bundled gpu_report demo
./venv/bin/graphx validate examples/hello.yaml
./venv/bin/graphx run examples/hello.yaml --input name=world
./venv/bin/graphx tui examples/hello.yaml        # r run, e edit, a add node, o api-from-spec
./venv/bin/graphx serve examples --port 8420     # REST + SSE API
./venv/bin/graphx tui examples/hello.yaml --attach http://localhost:8420 --thread <id>
```

The bundled **gpu_report** demo is a real workflow: it probes GPUs
(nvidia-smi), disk, and your local inference server in parallel, has a
local LLM write a markdown health report with a validated schema,
branches on severity through a human acknowledgement gate, and saves
the report — resilience (retries, LLM fallback, budgets) included.

Create a new workflow from a template (the `agent` template auto-fills a
discovered local/LAN inference server):

```bash
./venv/bin/graphx new my_flow --template agent --tui   # blank|agent|approval|pipeline
./venv/bin/graphx providers --scan                     # list Ollama/vLLM/llama.cpp/LM Studio endpoints
```

The TUI scans localhost and the LAN for inference servers on startup
(ports 11434/1234/5000/8000/8080). When you add an `agent` node (`a`), the
discovered models are offered one-click, and the matching `providers:`
block is written into the workflow so it stays self-contained. Press `n`
for a new workflow from a template without leaving the TUI.
`GRAPHX_NO_LAN_SCAN=1` restricts scanning to localhost.

## Secrets

Credentials are referenced in workflows as `secret://NAME` and resolved
**only at the point of use** (the outbound request, subprocess env, or
MCP server) — never baked into the workflow file, and never written to
checkpoints, the event log, SSE, or the TUI (a redaction net masks any
value that slips into output). Store them once:

```bash
graphx secret set openai_key           # hidden prompt; or --value / --stdin
graphx secret list                     # names only, never values
```

Stored 0600 in `~/.graphx/secrets.json` (or the OS keyring with the
`[keyring]` extra). Resolution falls back to the process environment, so
`secret://GITHUB_TOKEN` also picks up an exported `GITHUB_TOKEN`. Use in
any node:

```yaml
- id: call
  type: api
  url: "https://api.example.com/data"
  headers: { Authorization: "Bearer secret://api_key" }
```

`graphx run` refuses to start if a referenced secret is unset (with the
exact `graphx secret set` command to fix it); the TUI (`k` opens the
manager) prompts for any missing secret before a run.

CLI: `new` · `providers` · `secret set/list/rm` · `validate` · `run` · `resume <thread> --answer …` · `events <run> [--json]` · `history <thread>` · `scaffold-api` · `tui` · `serve`.

## HTTP API

`GET /workflows` · `POST /runs {workflow, input}` · `GET /runs/{thread}` ·
`POST /runs/{thread}/resume {answer}` · `POST /runs/{thread}/cancel` ·
`GET /runs/{thread}/events` (SSE, honors `Last-Event-ID`).

## Status

v0.4 — engine, all node types above, CLI, TUI (viewer/runner/editor palette + file watch), HTTP API + SSE, ~90 tests. Roadmap: richer TUI edge routing, per-thread run browser, remote run control from the TUI, provider pricing tables.
