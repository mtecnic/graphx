<div align="center">

# graphx

**Point it at your own LLM and describe what you want — it builds the agent workflow, and runs it in your terminal.**

A TUI-native designer and runner for agentic workflows: flow-chart pipelines with loops, branches, parallel fan-out, human approval gates, and real resilience — defined in plain YAML, generated from natural language, and driven live from the terminal, an HTTP API, or code.

*Local-first · no SDK lock-in · your models, your machine, your data.*

</div>

---

```console
$ graphx providers --add http://localhost:8000        # point at any OpenAI-compatible server
  ✔ added openai_local_8000  →  qwen3.6-27b

$ graphx generate "fetch a GitHub repo's JSON, summarize the description
                   with the local model, then save it to a file"
  ✔ valid workflow generated  (oneshot, ~2.8k tokens)

$ graphx run repo_summary.yaml
  ✔ fetch → ✔ summarize → ✔ save        # ran on your own model, start to finish
```

That's the whole loop: **connect a model by URL, describe a pipeline in English, get a working workflow, run it** — all offline on your hardware.

---

## Why graphx

Most "agent builders" are either web-based node canvases you can't script, or code-only engines with no UI. graphx is the missing middle, in the terminal:

- 🧠 **Build from natural language** — a description becomes a real, valid workflow, self-correcting against a schema validator. Two engines (reliable one-shot + agentic tool-driven).
- 🔌 **Point at any endpoint** — type a URL for vLLM / llama.cpp / Ollama / LM Studio / a gateway; graphx probes it and auto-discovers the model. It also scans your LAN on startup.
- ⚙️ **A real engine, not a toy** — Pregel-style supersteps, cyclic graphs with loops, shared state with reducers, per-step SQLite checkpointing. Kill a run mid-flight; `resume` continues from the last step.
- 🛡️ **Resilience per node** — retries with backoff, model fallback chains, validation re-asks, timeouts, token/cost/deadline budgets, `on_error` edges, dead-letters.
- 🔐 **Credentials done right** — `secret://NAME` resolves only at the point of use and never leaks into files, checkpoints, logs, SSE, or the screen.
- 🧩 **Batteries included** — 12 credential-wired connectors (Slack, Discord, Telegram, SendGrid, SMTP, Gmail, GitHub, GitLab, Postgres, S3, webhooks) and OpenAPI auto-scaffolding for anything else.
- ⏰ **Runs itself** — `triggers:` fire a workflow on a cron schedule, an interval, or an inbound webhook; the `serve` daemon or a systemd timer keeps it going.
- 📦 **Portable** — `graphx export` turns a workflow into a self-contained folder (bundled wheel + Dockerfile) that runs on any machine, no graphx install required.
- 📄 **YAML is the source of truth** — layout-free, diffable, hand-editable. The TUI renders it; it never owns it.

---

## Install

```bash
python3 -m venv venv
./venv/bin/pip install -e ".[tui,mcp,server]"     # extras: keyring, postgres, s3
./venv/bin/graphx tui                             # opens a workflow here, or scaffolds a demo
```

Python 3.12+. Nothing leaves your machine unless a node you add reaches out.

---

## The workflow, in one file

```yaml
version: 1
name: research_review
providers:
  local: { base_url: "http://localhost:8000/v1", protocol: openai }
state:
  topic:   { type: str }
  draft:   { type: str, default: "" }
entry: [fetch, search]                 # parallel fan-out
nodes:
  - { id: fetch,  type: api,  url: "https://api/trends?q=<state.topic>",
      headers: { Authorization: "Bearer secret://trends_key" } }
  - { id: search, type: mcp,  server: docs, tool: search, args: { q: "<state.topic>" } }
  - { id: gather, type: merge, success_threshold: 1 }             # barrier join
  - id: write
    type: agent
    model: "local/qwen3.6-27b"
    prompt: "Topic <state.topic>. Trends <fetch.json>. Docs <search.text>. Write a brief."
    output_schema: { draft: str }                                # validated JSON
    fallbacks: [ { static: { draft: "unavailable" } } ]          # degrade gracefully
    updates: { draft: "<self.draft>" }
  - id: review
    type: human                                                  # pause for approval
    prompt: "Ship this?"
    choices: [approve, revise]
edges:
  - { from: fetch,  to: gather }
  - { from: search, to: gather }
  - { from: gather, to: write }
  - { from: write,  to: review }
  - { from: review, to: end,   when: "review.choice == 'approve'" }
  - { from: review, to: write, when: "review.choice == 'revise'" }   # loop back
```

Loops, parallelism, secrets, LLM fallback, and a human gate — in ~25 lines.

---

## Build it from a sentence

```bash
graphx providers --add http://192.168.1.50:8000   # probe a URL → discovers the model
graphx generate "poll an RSS feed hourly, summarize new items, post to Slack"
graphx edit myflow.yaml "add a human approval gate before the Slack post"
```

- **Grounded, not guessing.** The model is handed a compact catalog of every node type + connector + the reference syntax, and its output is checked by the same validator `graphx run` uses. Invalid → the exact errors are fed back for a bounded repair loop. The result is *always* either valid or clearly flagged for a one-line fix.
- **Two engines.** `oneshot` (default) emits the whole workflow and repairs it — reliable even on small local models. `--agentic` drives builder tools step by step for capable models.
- **In the TUI**, press `g`, type your idea, and the generated graph renders in place, ready to run or tweak.

It's an editable first draft, not an oracle — quality scales with your model, and the validator keeps it honest.

---

## Design & run in the TUI

```bash
graphx tui examples/hello.yaml
```

A lazygit-style shell: the graph on the left (live node status as it runs), node detail and streaming logs on the right.

| key | action | | key | action |
|---|---|---|---|---|
| `r` | run live | | `g` | **generate from a description** |
| `n` | new from template | | `a` | add node |
| `o` | node from an OpenAPI spec | | `i` | add a service connector |
| `c` | connect nodes | | `k` | manage secrets |
| `e` | edit YAML in `$EDITOR` | | `q` | quit |

Every edit writes straight back to the YAML, and the file is watched — edit in vim in another pane and the graph redraws itself.

---

## Node types

| group | types |
|---|---|
| **work** | `agent` (LLM + tools + validated JSON output) · `api` (HTTP + `$.json.path` extraction) · `mcp` (MCP tool call) · `function` (Python) · `shell` (subprocess / CLI agents) |
| **flow** | `condition` (branch + loop) · `router` (LLM picks the path) · `map` (fan-out over a collection) · `merge` (barrier join with a success threshold) · `subworkflow` |
| **control** | `human` (approval gate — interrupts, resumable) · `wait` |

References: `<node.field>`, `<state.key>`, `<item.x>`, `secret://NAME`. Edges carry `when:` expressions. LLM providers are per-workflow — anything OpenAI-compatible plus native Anthropic, no SDKs.

---

## Connectors — batteries included

Drop-in, credential-wired nodes for popular services. Each declares the `secret://` it needs, so you're prompted for it automatically.

```bash
graphx add slack notify.yaml message="deploy done"
graphx add github_issue bug.yaml owner=me repo=app title="broken"
```

| category | connectors |
|---|---|
| **messaging** | `slack` · `discord` · `telegram` · `webhook` |
| **email** | `sendgrid` · `smtp` · `gmail` (via MCP) |
| **dev** | `github_issue` · `github_comment` · `gitlab_issue` |
| **data** | `postgres_query` · `s3_put` |

Anything with an OpenAPI spec: `graphx scaffold-api wf.yaml http://service` builds the `api` node for you — path params, request body, and response-field extraction included.

---

## Credentials that don't leak

Reference secrets as `secret://NAME`. They resolve **only at the point of use** — the outbound request, subprocess env, or MCP server — and are never written into the workflow file, checkpoints, the event log, SSE, or the TUI (a redaction net masks any value that slips into output).

```bash
graphx secret set slack_webhook_url        # hidden prompt (or --value / --stdin)
graphx secret list                         # names only, never values
```

Stored `0600` in `~/.graphx/secrets.json` (or the OS keyring via the `[keyring]` extra), with env-var fallback. `graphx run` refuses to start on a missing secret and tells you exactly how to set it; the TUI prompts inline.

---

## Resilience & durability

- **Retries** — per-node exponential backoff + jitter, transient-only (429/5xx/timeout), honoring `Retry-After`.
- **Fallbacks** — ordered model chains ending in an optional static degraded output.
- **Guards** — four independent stops: max steps, token budget, cost budget, wall-clock deadline.
- **Checkpoint & resume** — full state snapshot to SQLite every superstep. `kill -9` a run; `graphx resume <thread>` continues exactly where it stopped.
- **Human-in-the-loop** — a `human` node interrupts and persists; resume from the CLI, TUI, or API.

---

## Run it anywhere

```bash
graphx run flow.yaml --input topic="graph engines"     # stream events in the terminal
graphx resume <thread> --answer approve                # answer a human gate
graphx serve examples --port 8420                      # REST + SSE server
graphx tui flow.yaml --attach http://localhost:8420 --thread <id>   # follow a remote run
```

The CLI, the TUI, and the HTTP API all consume the **same** `RunEvent` stream. The API: `GET /workflows` · `POST /runs` · `GET /runs/{thread}` · `POST /runs/{thread}/resume` · `POST /runs/{thread}/cancel` · `GET /runs/{thread}/events` (SSE, honors `Last-Event-ID`).

---

## Run it on a schedule, or on an event

Add a `triggers:` block and a workflow runs *itself* — the difference between "a pipeline I run" and "a pipeline that handled 40 emails while I slept."

```yaml
triggers:
  - { type: schedule, cron: "0 7 * * *" }              # daily at 07:00
  - { type: interval, every: 15m }                      # every 15 minutes
  - { type: webhook, path: "orders", input_from: body } # POST /hooks/orders → runs, body = input
```

```bash
graphx serve examples --port 8420      # daemon: fires schedules/intervals, receives webhooks
curl -X POST localhost:8420/hooks/orders -d '{"order_id": 4821}'   # trigger on demand
```

Prefer the OS to keep it alive? Emit a **systemd user timer** (or a crontab line) for a schedule-only workflow — no daemon required:

```bash
graphx schedule daily.yaml --cron "0 7 * * *" --install     # writes + enables a systemd timer
graphx schedule daily.yaml --cron "0 7 * * *" --crontab      # or print a crontab line
```

---

## Export it — runs on any machine

Turn a workflow into a **self-contained, portable program**. No graphx install on the target, no internet-to-a-private-repo, no secrets baked in.

```bash
graphx export myflow.yaml --docker
#  → myflow_export/  ·  myflow.yaml + run.py + requirements.txt + .env.example + Dockerfile
#                       + a bundled graphx wheel

cd myflow_export
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in any secrets (as env vars)
./venv/bin/python run.py    # …or: docker build -t myflow . && docker run --env-file .env myflow
```

graphx installs from the bundled wheel; its dependencies come from PyPI; credentials come from *your* `.env` (nothing sensitive is ever written into the export).

---

## Examples

| file | what it shows |
|---|---|
| `examples/hello.yaml` | LLM-free tour — parallel fan-out, merge, map, a counted loop |
| `examples/gpu_report.yaml` | Real host health report: probe GPUs/disk/inference server → local LLM writes it → human gate → save |
| `examples/email_triage.yaml` | Classify inbox mail and draft replies on your own model, behind a human gate |
| `examples/agent_demo.yaml` | Minimal live-LLM demo — agent writes, router branches on the result |
| `examples/approval.yaml` | Human-in-the-loop gate: draft → approve → publish |
| `examples/scheduled_report.yaml` | Triggers demo — runs daily on a cron and on a webhook |

---

## CLI reference

`generate` · `edit` · `new` · `connectors` · `add` · `providers [--scan|--add <url>]` · `secret set/list/rm` · `scaffold-api` · `schedule` · `export` · `validate` · `run` · `resume` · `events` · `history` · `tui` · `serve`

---

## Status

**v0.7.0** — engine, all node types, natural-language builder (both engines) + point-at-any-endpoint, triggers/scheduling (cron · interval · webhook + systemd/crontab), export-to-portable-program, 12 connectors, secrets, discovery, templates, OpenAPI scaffolding, TUI (designer + runner + editor), HTTP API + SSE. 250 tests, ruff-clean, Python 3.12+.

*Roadmap: richer TUI edge routing, a run browser, remote run control, provider pricing tables, PyPI publish, more connectors.*

---

<div align="center">
<sub>Built for engineers who want to orchestrate agents on their own hardware — and describe the pipeline instead of wiring it by hand.</sub>
</div>
