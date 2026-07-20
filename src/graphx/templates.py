"""Starter workflow templates for `graphx new` and the TUI's `n` key.

Placeholders are literal tokens (__NAME__, __PROVIDERS__, __MODEL__)
substituted with str.replace — YAML is full of braces, so str.format
is a trap here. Templates that use an LLM get their provider + model
filled from endpoint discovery when available.
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

_AGENT = Template("agent", "LLM agent with schema validation, fallback, and save",
                  needs_llm=True, body="""\
version: 1
name: __NAME__
description: LLM agent pipeline — edit the prompt, press r to run.

__PROVIDERS__

config:
  budget: { tokens: 50000, deadline: 10m }

state:
  topic:  { type: str, default: "terminal user interfaces" }
  answer: { type: str, default: "" }

entry: [think]

nodes:
  - id: think
    type: agent
    model: "__MODEL__"
    system: "You are a helpful, concise assistant."
    prompt: "Write three interesting facts about <state.topic>."
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

TEMPLATES: dict[str, Template] = {t.key: t for t in (_BLANK, _AGENT, _APPROVAL, _PIPELINE)}

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def render(template: Template, name: str,
           endpoint: Endpoint | None = None) -> str:
    body = template.body.replace("__NAME__", name)
    if template.needs_llm:
        if endpoint is not None and endpoint.models:
            config = endpoint.provider_config()
            providers = (f"providers:\n  {endpoint.alias}: "
                         f'{{ base_url: "{config["base_url"]}", protocol: openai }}')
            model = f"{endpoint.alias}/{endpoint.models[0]}"
        else:
            providers = _NO_PROVIDER_BLOCK
            model = _FALLBACK_MODEL
        body = body.replace("__PROVIDERS__", providers).replace("__MODEL__", model)
    return body


def create_workflow(name: str, template_key: str, directory: Path | str = ".",
                    endpoints: list[Endpoint] | None = None,
                    force: bool = False) -> Path:
    """Render a template, static-check it, and write NAME.yaml. Returns the path."""
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

    path.write_text(text)
    return path
