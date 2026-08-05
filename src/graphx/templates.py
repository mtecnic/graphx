"""Starter workflow templates for `graphx new` and the TUI's `n` key.

Placeholders are literal tokens (__NAME__, __PROVIDERS__, __MODEL__)
substituted with str.replace — YAML is full of braces, so str.format
is a trap here. Templates that use an LLM get their provider + model
filled from endpoint discovery when available.

Three tiers:
- starters: blank / agent / approval / pipeline — one concept each.
- patterns: review / fanout / triage — the canonical agentic workflow
  shapes (evaluator-optimizer, parallelization, routing).
- real world: inbox / digest / issueops / watchdog — trigger- and
  connector-wired workflows you can point at your own accounts.

Templates with LLM steps also scaffold a paired NAME.eval.yaml so every
generated workflow starts life with an eval harness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .llm.discovery import Endpoint, best_endpoint

_NO_PROVIDER_BLOCK = """\
# No inference server was discovered when this file was created.
# Built-in localhost providers: ollama / vllm / llamacpp / lmstudio.
# Add your own here, e.g.:
# providers:
#   myserver: { base_url: "http://host:8000/v1", protocol: openai }"""

_FALLBACK_MODEL = "ollama/llama3.2"


@dataclass(frozen=True)
class Template:
    key: str
    description: str
    body: str
    needs_llm: bool = False
    eval_body: str | None = None   # scaffolds NAME.eval.yaml when set


# ------------------------------------------------------------------ starters

_BLANK = Template("blank", "one shell node — the minimal starting point", """\
version: 1
name: __NAME__
description: A new graphx workflow.

state:
  result: { type: str, default: "" }

entry: [start]

nodes:
  - id: start
    type: shell
    command: ["echo", "hello from __NAME__"]
    updates: { result: "<self.stdout>" }

edges:
  - { from: start, to: end }
""")

_AGENT = Template("agent", "LLM agent with a tool, schema validation, and save",
                  needs_llm=True, body="""\
version: 1
name: __NAME__
description: LLM agent pipeline — edit the prompt, press r to run.

__PROVIDERS__

config:
  budget: { tokens: 50000, deadline: 10m }

state:
  topic:  { type: str, default: "the machine you are running on" }
  answer: { type: str, default: "" }

entry: [think]

nodes:
  - id: think
    type: agent
    model: "__MODEL__"
    system: "You are a helpful, concise assistant. Use your tools when they apply."
    prompt: "Write three interesting facts about <state.topic>."
    tools:
      - function: "graphx.demo:sysinfo"
        description: "Report the OS platform and Python version of this machine."
    output_schema: { answer: str }
    validation: { reasks: 2 }
    fallbacks:
      - static: { answer: "LLM unavailable — check your providers config." }
    timeout: 120s
    updates: { answer: "<self.answer>" }

  - id: save
    type: shell
    command: ["tee", ".graphx/__NAME__.out"]
    stdin: "<state.answer>"

edges:
  - { from: think, to: save }
  - { from: save, to: end }
""", eval_body="""\
version: 1
dataset: __NAME__
# Run:  graphx eval __NAME__.yaml __NAME__.eval.yaml
# Deterministic assertions run on the finished state; uncomment `judge`
# to add LLM grading on top.
cases:
  - name: default_topic
    input: {}
    expect:
      status: finished
      no_dead_letters: true
      assert:
        - "answer != ''"
      budget: { tokens: 20000 }
  - name: custom_topic
    input: { topic: "checkpointing in workflow engines" }
    expect:
      status: finished
      assert:
        - "len(answer) > 40"
      # judge:
      #   artifact: "<state.answer>"
      #   criteria: "Three distinct, factually plausible facts about the topic."
      #   model: "__MODEL__"
      #   min_score: 0.7
""")

_APPROVAL = Template("approval", "human-in-the-loop gate: draft → approve → publish", """\
version: 1
name: __NAME__
description: Human approval gate — the run parks until you answer.

state:
  doc:    { type: str, default: "" }
  result: { type: str, default: "" }

entry: [draft]

nodes:
  - id: draft
    type: shell
    command: ["echo", "This is the draft."]
    updates: { doc: "<self.stdout>" }

  - id: gate
    type: human
    prompt: "Publish this draft?"
    choices: [approve, reject]
    payload: { doc: "<state.doc>" }

  - id: publish
    type: shell
    command: ["echo", "PUBLISHED"]
    updates: { result: "<self.stdout>" }

  - id: discard
    type: shell
    command: ["echo", "DISCARDED"]
    updates: { result: "<self.stdout>" }

edges:
  - { from: draft, to: gate }
  - { from: gate, to: publish, when: "gate.choice == 'approve'" }
  - { from: gate, to: discard, when: "gate.choice == 'reject'" }
  - { from: publish, to: end }
  - { from: discard, to: end }
""")

_PIPELINE = Template("pipeline", "api fetch → transform → bounded loop → save", """\
version: 1
name: __NAME__
description: API pipeline with a bounded loop — refs, retries, caching.

state:
  count: { type: int, default: 0 }

entry: [fetch]

nodes:
  - id: fetch
    type: api
    method: GET
    url: "https://api.github.com/repos/python/cpython"
    output: { stars: "$.stargazers_count", repo: "$.full_name" }
    retry: { attempts: 3 }
    timeout: 20s
    cache: true

  - id: transform
    type: function
    handler: "graphx.demo:increment"
    args: { count: "<state.count>" }
    updates: { count: "<self.count>" }

  - id: check
    type: condition
    branches:
      - { if: "count < 3", goto: transform }
      - { else: save }
    max_iterations: 5
    on_exhausted: save

  - id: save
    type: shell
    command: ["echo", "<fetch.repo> has <fetch.stars> stars; looped <state.count>x"]

edges:
  - { from: fetch, to: transform }
  - { from: transform, to: check }
  - { from: save, to: end }
""")


# ------------------------------------------------------------------ patterns

_REVIEW = Template("review", "evaluator-optimizer: write → critic → loop until it passes",
                   needs_llm=True, body="""\
version: 1
name: __NAME__
description: Write → independent critic review → loop until it passes → publish.

__PROVIDERS__

config:
  budget: { tokens: 40000, deadline: 10m }

state:
  topic:    { type: str, default: "why checkpoints beat retries" }
  draft:    { type: str, default: "" }
  feedback: { type: str, default: "" }

entry: [write]

nodes:
  - id: write
    type: agent
    model: "__MODEL__"
    system: "You write tight, factual two-sentence explainers."
    prompt: "Explain <state.topic> in exactly two sentences. Prior feedback: <state.feedback>"
    output_schema: { text: str }
    validation: { reasks: 2 }
    timeout: 120s
    updates: { draft: "<self.text>" }
    max_iterations: 3
    on_exhausted: publish

  # The critic runs in a FRESH context — it only ever sees the draft +
  # criteria, never the writer's conversation, so it can't rubber-stamp
  # its own work. For a truly independent review, point it at a
  # DIFFERENT model than the writer.
  - id: review
    type: critic
    model: "__MODEL__"
    artifact: "<state.draft>"
    criteria: "Exactly two sentences, factually correct, no marketing fluff."
    min_score: 0.8
    feedback_channel: feedback

  - id: publish
    type: shell
    command: ["tee", ".graphx/__NAME__.out"]
    stdin: "<state.draft>"

edges:
  - { from: write,  to: review }
  - { from: review, to: publish, when: "review.score >= 0.8" }
  - { from: review, to: write,   when: "review.score < 0.8" }
  - { from: publish, to: end }
""", eval_body="""\
version: 1
dataset: __NAME__
# Run:  graphx eval __NAME__.yaml __NAME__.eval.yaml
cases:
  - name: passes_review
    input: { topic: "why bounded loops beat unbounded retries" }
    expect:
      status: finished
      no_dead_letters: true
      assert:
        - "draft != ''"
      budget: { tokens: 40000 }
""")

_FANOUT = Template("fanout", "parallelization: map over items concurrently, then synthesize",
                   needs_llm=True, body="""\
version: 1
name: __NAME__
description: Parallel fan-out — one agent per item, collected, then synthesized.

__PROVIDERS__

config:
  max_concurrency: 4
  budget: { tokens: 60000, deadline: 10m }

state:
  items:   { type: list, default: ["checkpointing", "retries", "human gates"] }
  notes:   { type: list, reducer: extend, default: [] }
  summary: { type: str, default: "" }

entry: [research]

nodes:
  - id: research
    type: map
    over: "<state.items>"
    item_as: topic
    max_concurrency: 3
    node:
      type: agent
      model: "__MODEL__"
      system: "You are a precise researcher. No preamble."
      prompt: "In 2-3 sentences: what is <item.topic> and why does it matter in workflow engines?"
      output_schema: { note: str }
      validation: { reasks: 2 }
      timeout: 120s
    collect: { channel: notes }

  - id: synthesize
    type: agent
    model: "__MODEL__"
    system: "You merge notes into one coherent answer."
    prompt: "Combine these notes into a single short briefing:\\n<state.notes>"
    output_schema: { text: str }
    validation: { reasks: 2 }
    timeout: 120s
    updates: { summary: "<self.text>" }

  - id: save
    type: shell
    command: ["tee", ".graphx/__NAME__.out"]
    stdin: "<state.summary>"

edges:
  - { from: research,   to: synthesize }
  - { from: synthesize, to: save }
  - { from: save,       to: end }
""", eval_body="""\
version: 1
dataset: __NAME__
# Run:  graphx eval __NAME__.yaml __NAME__.eval.yaml
cases:
  - name: three_items
    input: {}
    expect:
      status: finished
      no_dead_letters: true
      assert:
        - "len(notes) == 3"
        - "summary != ''"
      budget: { tokens: 60000 }
""")

_TRIAGE = Template("triage", "routing: classify a request, send it down the right branch",
                   needs_llm=True, body="""\
version: 1
name: __NAME__
description: LLM routing — classify the request, branch to the right handler.

__PROVIDERS__

config:
  budget: { tokens: 30000, deadline: 10m }

state:
  request: { type: str, default: "How do I list files modified in the last hour?" }
  reply:   { type: str, default: "" }

entry: [route]

nodes:
  # Routing is context-free: the router only classifies, it never answers.
  # Point the branches at different-size models — cheap for `quick`,
  # capable for `deep` — to spend tokens only where they matter.
  - id: route
    type: router
    model: "__MODEL__"
    prompt: "Request: <state.request>\\nWhere should this go?"
    options:
      quick: "a simple factual or how-to question with a short answer"
      deep: "needs multi-step reasoning, research, or a long-form answer"
      escalate: "sensitive, destructive, or anything a human must see first"
    timeout: 120s

  - id: quick
    type: agent
    model: "__MODEL__"
    system: "Answer in at most three sentences."
    prompt: "<state.request>"
    output_schema: { text: str }
    validation: { reasks: 2 }
    timeout: 120s
    updates: { reply: "<self.text>" }

  - id: deep
    type: agent
    model: "__MODEL__"
    system: "You are a thorough expert. Structure your answer."
    prompt: "<state.request>"
    output_schema: { text: str }
    validation: { reasks: 2 }
    timeout: 180s
    updates: { reply: "<self.text>" }

  - id: escalate
    type: human
    prompt: "This request needs a human decision."
    choices: [handled, dismissed]
    payload: { request: "<state.request>" }

edges:
  - { from: quick,    to: end }
  - { from: deep,     to: end }
  - { from: escalate, to: end }
""", eval_body="""\
version: 1
dataset: __NAME__
# Run:  graphx eval __NAME__.yaml __NAME__.eval.yaml
cases:
  - name: quick_question
    input: { request: "What does the -r flag of cp do?" }
    expect:
      status: finished
      no_dead_letters: true
      assert:
        - "reply != ''"
      budget: { tokens: 30000 }
      # judge:
      #   artifact: "<state.reply>"
      #   criteria: "Correctly explains recursive copy; concise."
      #   model: "__MODEL__"
      #   min_score: 0.7
""")


# ---------------------------------------------------------------- real world

_INBOX = Template("inbox", "email triage on a local model: classify, draft, human gate",
                  needs_llm=True, body="""\
version: 1
name: __NAME__
description: >
  Classify incoming email and draft replies on a LOCAL model, then park
  at a human gate so nothing goes out without you. The `emails` input is
  a list of {id, from, subject, body} — wire it to a mail MCP server or
  pass it via --input for testing. Mail content never leaves the box.

__PROVIDERS__

# For fully autonomous runs, connect a Gmail MCP server (which owns its
# own one-time OAuth) and add fetch / create-draft `mcp` nodes. Store the
# token once with `graphx secret set gmail_token`; graphx injects it into
# the server's env at spawn — it never touches this file, state, or
# checkpoints.
# mcp_servers:
#   gmail:
#     transport: stdio
#     command: ["npx", "-y", "@gongrzhe/server-gmail-autoauth-mcp"]
#     env: { GMAIL_OAUTH_TOKEN: "secret://gmail_token" }

config:
  max_steps: 20
  max_concurrency: 3
  budget: { tokens: 120000, deadline: 10m }

state:
  emails:  { type: list, default: [] }              # input: [{id, from, subject, body}]
  triaged: { type: list, reducer: extend, default: [] }

entry: [triage]

nodes:
  - id: triage
    type: map
    over: "<state.emails>"
    item_as: email
    max_concurrency: 3
    node:
      type: agent
      model: "__MODEL__"
      system: >
        You triage incoming email for a small business owner. Be accurate
        and concise. Draft replies in a warm, plain, professional voice;
        never invent order numbers, prices, tracking, or policies — if
        you lack a fact, write a placeholder in [brackets] for the owner
        to fill in. Only set needs_reply for real messages that warrant
        a personal response (not newsletters, receipts, or spam).
      prompt: |
        From: <item.email.from>
        Subject: <item.email.subject>
        Body:
        <item.email.body>

        Classify this email and, if it needs a personal reply, draft one.
      output_schema:
        category: str        # order | shipping | question | wholesale | spam | other
        priority: str        # high | normal | low
        needs_reply: bool
        draft_reply: str     # the reply text, or "" if needs_reply is false
      validation: { reasks: 2 }
      timeout: 120s
    collect: { channel: triaged }

  - id: compile
    type: function
    handler: "graphx.examples.email_tools:compile_triage"
    args: { emails: "<state.emails>", triaged: "<state.triaged>" }

  - id: review
    type: human
    prompt: "Review triage — save these drafts?"
    choices: [approve, skip]
    payload: "<compile.summary>"

  - id: save
    type: shell
    command: ["tee", ".graphx/__NAME__.drafts.json"]
    stdin: "<compile.drafts>"

edges:
  - { from: triage,  to: compile }
  - { from: compile, to: review }
  - { from: review,  to: save, when: "review.choice == 'approve'" }
  - { from: review,  to: end,  when: "review.choice == 'skip'" }
  - { from: save,    to: end }
""")

_DIGEST = Template("digest", "self-running daily digest: cron + webhook → fetch → LLM → Slack",
                   needs_llm=True, body="""\
version: 1
name: __NAME__
description: >
  A workflow that runs itself. Under `graphx serve` it fires every
  morning and on demand via POST /hooks/__NAME__, fetches fresh data,
  summarizes it on your model, and posts the digest to Slack.

triggers:
  - { type: schedule, cron: "0 7 * * *" }               # daily at 07:00
  - { type: webhook, path: "__NAME__", input_from: body }

__PROVIDERS__

config:
  budget: { tokens: 30000, deadline: 10m }

state:
  summary: { type: str, default: "" }

nodes:
  # Swap for whatever you want to wake up to: repo stats, a dashboard
  # API, yesterday's orders, an RSS-to-JSON feed…
  - id: fetch
    type: api
    method: GET
    url: "https://api.github.com/repos/python/cpython"
    output: { stars: "$.stargazers_count", issues: "$.open_issues_count", repo: "$.full_name" }
    retry: { attempts: 3 }
    timeout: 20s

  - id: summarize
    type: agent
    model: "__MODEL__"
    system: "You write one-paragraph morning digests. Plain text, no markdown."
    prompt: "Repo <fetch.repo>: <fetch.stars> stars, <fetch.issues> open issues. Write a one-paragraph status digest."
    output_schema: { text: str }
    validation: { reasks: 2 }
    timeout: 120s
    updates: { summary: "<self.text>" }

  # Store the webhook once:  graphx secret set slack_webhook_url
  # The URL is late-resolved at request time — it never appears in
  # state, checkpoints, events, or the TUI. Swap for any connector
  # via `graphx add`.
  - id: post
    type: api
    method: POST
    url: "secret://slack_webhook_url"
    json_body: { text: "<state.summary>" }
    retry: { attempts: 3 }
    timeout: 20s

  - id: save
    type: shell
    command: ["tee", ".graphx/__NAME__.out"]
    stdin: "<state.summary>"

entry: [fetch]

edges:
  - { from: fetch,     to: summarize }
  - { from: summarize, to: post }
  - { from: summarize, to: save }
  - { from: post,      to: end }
  - { from: save,      to: end }
""")

_ISSUEOPS = Template("issueops", "GitHub issue responder: webhook → draft → human gate → comment",
                     needs_llm=True, body="""\
version: 1
name: __NAME__
description: >
  First-response bot for GitHub issues. Point an org/repo webhook
  (issues, action=opened) at POST /hooks/__NAME__ under `graphx serve`;
  the payload becomes this workflow's input. A local model drafts the
  reply, you approve it, graphx posts it.

triggers:
  - { type: webhook, path: "__NAME__", input_from: body }

__PROVIDERS__

config:
  budget: { tokens: 30000, deadline: 15m }

state:
  owner:  { type: str, default: "your-org" }
  repo:   { type: str, default: "your-repo" }
  number: { type: int, default: 1 }
  title:  { type: str, default: "Example: app crashes on start" }
  body:   { type: str, default: "Steps: run it. It crashes." }

entry: [draft]

nodes:
  - id: draft
    type: agent
    model: "__MODEL__"
    system: >
      You draft first-response comments for GitHub issues on behalf of a
      maintainer. Be warm and specific; ask for missing repro details;
      never promise fixes or timelines. If you lack a fact, write a
      placeholder in [brackets].
    prompt: |
      Issue #<state.number> in <state.owner>/<state.repo>
      Title: <state.title>

      <state.body>

      Draft a reply comment.
    output_schema: { reply: str }
    validation: { reasks: 2 }
    timeout: 120s

  - id: gate
    type: human
    prompt: "Post this reply to GitHub?"
    choices: [post, skip]
    payload: { reply: "<draft.reply>" }

  # Store the token once:  graphx secret set github_token
  # (a PAT with Issues: read/write — late-resolved, never persisted)
  - id: comment
    type: api
    method: POST
    url: "https://api.github.com/repos/<state.owner>/<state.repo>/issues/<state.number>/comments"
    headers:
      Authorization: "Bearer secret://github_token"
      Accept: "application/vnd.github+json"
      X-GitHub-Api-Version: "2022-11-28"
      User-Agent: "graphx"
    json_body: { body: "<draft.reply>" }
    expect_status: [201]
    timeout: 20s

edges:
  - { from: draft,   to: gate }
  - { from: gate,    to: comment, when: "gate.choice == 'post'" }
  - { from: gate,    to: end,     when: "gate.choice == 'skip'" }
  - { from: comment, to: end }
""")

_WATCHDOG = Template("watchdog", "self-running health check: interval trigger → alert to Slack", """\
version: 1
name: __NAME__
description: >
  LLM-free ops loop — under `graphx serve` this checks the box every
  five minutes and posts to Slack only when something crosses the line.

triggers:
  - { type: interval, every: 5m }

state:
  status: { type: str, default: "" }

entry: [check]

nodes:
  # Prints "ok" or "alert: …" — swap in any health check you like
  # (GPU temp via nvidia-smi, a service ping, a queue depth).
  - id: check
    type: shell
    command: ["sh", "-c", "u=$(df --output=pcent / | tail -1 | tr -dc 0-9); if [ \\"$u\\" -ge 90 ]; then printf 'alert: root disk at %s%%' \\"$u\\"; else printf ok; fi"]
    updates: { status: "<self.stdout>" }

  - id: decide
    type: condition
    branches:
      - { if: "status != 'ok'", goto: alert }
      # no branch matched → the run just ends quietly

  # Store the webhook once:  graphx secret set slack_webhook_url
  - id: alert
    type: api
    method: POST
    url: "secret://slack_webhook_url"
    json_body: { text: "<state.status>" }
    retry: { attempts: 3 }
    timeout: 20s

edges:
  - { from: check, to: decide }
  - { from: alert, to: end }
""")


TEMPLATES: dict[str, Template] = {t.key: t for t in (
    _BLANK, _AGENT, _APPROVAL, _PIPELINE,
    _REVIEW, _FANOUT, _TRIAGE,
    _INBOX, _DIGEST, _ISSUEOPS, _WATCHDOG,
)}

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _fill(text: str, template: Template, name: str,
          endpoint: Endpoint | None) -> str:
    text = text.replace("__NAME__", name)
    if template.needs_llm:
        if endpoint is not None and endpoint.models:
            config = endpoint.provider_config()
            providers = (f"providers:\n  {endpoint.alias}: "
                         f'{{ base_url: "{config["base_url"]}", protocol: openai }}')
            model = f"{endpoint.alias}/{endpoint.models[0]}"
        else:
            providers = _NO_PROVIDER_BLOCK
            model = _FALLBACK_MODEL
        text = text.replace("__PROVIDERS__", providers).replace("__MODEL__", model)
    return text


def render(template: Template, name: str,
           endpoint: Endpoint | None = None) -> str:
    return _fill(template.body, template, name, endpoint)


def render_eval(template: Template, name: str,
                endpoint: Endpoint | None = None) -> str | None:
    if template.eval_body is None:
        return None
    return _fill(template.eval_body, template, name, endpoint)


def eval_path_for(workflow_path: Path) -> Path:
    return workflow_path.with_suffix(".eval.yaml")


def create_workflow(name: str, template_key: str, directory: Path | str = ".",
                    endpoints: list[Endpoint] | None = None,
                    force: bool = False) -> Path:
    """Render a template, static-check it, and write NAME.yaml (plus a
    NAME.eval.yaml scaffold when the template defines one). Returns the
    workflow path."""
    if not _NAME_RE.match(name):
        raise ValueError(f"bad workflow name {name!r} "
                         "(letters/digits/underscore/dash, starting with a letter)")
    template = TEMPLATES.get(template_key)
    if template is None:
        raise ValueError(f"unknown template '{template_key}' "
                         f"(have: {', '.join(TEMPLATES)})")
    path = Path(directory) / f"{name}.yaml"
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists (use --force to overwrite)")

    endpoint = best_endpoint(endpoints) if endpoints else None
    text = render(template, name, endpoint)

    from .model.validate import has_errors, validate_graph
    from .model.yaml_loader import build_graph
    from .nodes.registry import known_types, load_builtin_nodes
    from ruamel.yaml import YAML

    load_builtin_nodes()
    data = YAML(typ="safe", pure=True).load(text)
    graph = build_graph(data, source_path=str(path))
    issues = validate_graph(graph, known_types=known_types())
    if has_errors(issues):
        raise ValueError("template rendered an invalid workflow: "
                         + "; ".join(str(i) for i in issues))

    eval_text = render_eval(template, name, endpoint)
    if eval_text is not None:
        from .eval.dataset import EvalDataset
        EvalDataset.model_validate(YAML(typ="safe", pure=True).load(eval_text))

    path.write_text(text)
    if eval_text is not None:
        eval_path = eval_path_for(path)
        if force or not eval_path.exists():
            eval_path.write_text(eval_text)
    return path
