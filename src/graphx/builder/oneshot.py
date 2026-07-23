"""One-shot engine: emit the whole workflow, validate, repair, repeat.

Mirrors the validation re-ask loop in nodes/agent.py: on any validator
error, feed the exact messages (plus the schema of an offending node
type) back and ask for the corrected JSON.
"""

from __future__ import annotations

import json

from ..model.validate import Issue, has_errors
from ..model.yaml_loader import LoadError
from ..nodes.agent import extract_json
from .catalog import Catalog
from .draft import WorkflowDraft
from .prompts import ONESHOT_SYSTEM
from .result import BuildResult


def _errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


def _repair_prompt(issues: list[Issue], catalog: Catalog) -> str:
    lines = ["The workflow has errors:"]
    seen_types: set[str] = set()
    for issue in _errors(issues):
        lines.append(f"- {issue.where}: {issue.message}")
        for type_ in catalog.node_lines:
            if f"'{type_}'" in issue.message and type_ not in seen_types:
                schema = catalog.schema_for(type_)
                if schema:
                    lines.append(f"  schema for '{type_}': "
                                 f"{json.dumps(schema.get('properties', {}))[:600]}")
                    seen_types.add(type_)
    lines.append("Return the full corrected JSON workflow object, nothing else.")
    return "\n".join(lines)


async def _loop(llm, model: str, catalog: Catalog, messages: list[dict], *,
                max_repairs: int, temperature: float, max_tokens: int,
                seed_provider=None) -> BuildResult:
    draft: WorkflowDraft | None = None
    issues: list[Issue] = []
    usage_total = 0
    for attempt in range(max_repairs + 1):
        resp = await llm.chat(model, messages, force_json=True,
                              temperature=temperature, max_tokens=max_tokens)
        usage_total += resp.usage.total
        try:
            data = extract_json(resp.text)
            draft = WorkflowDraft.from_dict(data)
            if seed_provider:
                draft.set_provider(*seed_provider)
            issues = draft.validate()
        except (ValueError, LoadError) as exc:
            issues = [Issue("error", "parse", str(exc))]
            draft = draft if isinstance(draft, WorkflowDraft) else None
        if draft is not None and not has_errors(issues):
            return BuildResult(ok=True, yaml=draft.to_yaml(), draft=draft,
                               issues=issues, tokens=usage_total,
                               engine="oneshot", model=model)
        if attempt == max_repairs:
            break
        messages = [*messages,
                    {"role": "assistant", "content": resp.text},
                    {"role": "user", "content": _repair_prompt(issues, catalog)}]
    return BuildResult(ok=False, exhausted=True,
                       yaml=(draft.to_yaml() if draft else ""), draft=draft,
                       issues=issues, tokens=usage_total, engine="oneshot", model=model)


async def generate_oneshot(llm, model: str, catalog: Catalog, description: str, *,
                           name: str | None = None, max_repairs: int = 3,
                           temperature: float = 0.2, max_tokens: int = 4000,
                           seed_provider=None) -> BuildResult:
    hint = f' Name it "{name}".' if name else ""
    messages = [
        {"role": "system", "content": ONESHOT_SYSTEM + "\n\n" + catalog.render()},
        {"role": "user", "content": f"Create a workflow.\n\nRequest: {description}{hint}\n"
         "Return ONLY the JSON object."},
    ]
    return await _loop(llm, model, catalog, messages, max_repairs=max_repairs,
                       temperature=temperature, max_tokens=max_tokens,
                       seed_provider=seed_provider)


async def edit_oneshot(llm, model: str, catalog: Catalog, current_yaml: str,
                       instruction: str, *, max_repairs: int = 3,
                       temperature: float = 0.2, max_tokens: int = 4000) -> BuildResult:
    messages = [
        {"role": "system", "content": ONESHOT_SYSTEM + "\n\n" + catalog.render()},
        {"role": "user", "content":
         f"Here is an existing workflow:\n```yaml\n{current_yaml}\n```\n\n"
         f"Apply this change: {instruction}\n"
         "Return the FULL revised workflow as a JSON object; keep everything else "
         "identical. Return ONLY the JSON object."},
    ]
    return await _loop(llm, model, catalog, messages, max_repairs=max_repairs,
                       temperature=temperature, max_tokens=max_tokens)
