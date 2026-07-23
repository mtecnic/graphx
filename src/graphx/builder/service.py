"""build_workflow — engine selection + LLM wiring for the NL builder."""

from __future__ import annotations

from pathlib import Path

from ..llm.client import LLMClient, LLMError
from ..llm.discovery import best_endpoint
from ..secrets import SecretResolver
from .catalog import build_catalog
from .result import BuildResult

__all__ = ["BuildResult", "build_workflow"]


def _resolve_model(model: str | None, endpoints: list | None):
    """Return (model_string, provider_seed) from an explicit model or discovery."""
    if model and "/" in model:
        alias = model.split("/", 1)[0]
        for ep in endpoints or []:
            if ep.alias == alias:
                return model, (alias, ep.provider_config())
        return model, None            # explicit provider must exist in the graph/defaults
    ep = best_endpoint(endpoints or [])
    if ep is None or not ep.models:
        raise LLMError("no inference endpoint available — run `graphx providers --scan` "
                       "or `graphx providers --add <url>`, or pass --model")
    return f"{ep.alias}/{ep.models[0]}", (ep.alias, ep.provider_config())


async def build_workflow(*, description: str | None = None, instruction: str | None = None,
                         current_path: str | Path | None = None,
                         engine: str = "oneshot", model: str | None = None,
                         endpoints: list | None = None,
                         name: str | None = None) -> BuildResult:
    from . import agentic, oneshot

    model_str, seed = _resolve_model(model, endpoints)
    providers = {seed[0]: seed[1]} if seed else {}
    llm = LLMClient(providers=providers, resolver=SecretResolver())
    catalog = build_catalog(endpoints)
    editing = current_path is not None

    try:
        if engine == "agentic":
            if editing:
                return await agentic.edit_agentic(llm, model_str, catalog,
                                                  current_path, instruction)
            return await agentic.generate_agentic(llm, model_str, catalog, description,
                                                  name=name or "generated", seed_provider=seed)
        if engine == "auto":
            result = await _try_agentic(agentic, llm, model_str, catalog,
                                        description, instruction, current_path, name, seed)
            if result is not None:
                return result
            # fall through to oneshot
        if editing:
            current_yaml = Path(current_path).read_text()
            return await oneshot.edit_oneshot(llm, model_str, catalog,
                                              current_yaml, instruction)
        return await oneshot.generate_oneshot(llm, model_str, catalog, description,
                                              name=name, seed_provider=seed)
    finally:
        await llm.aclose()


async def _try_agentic(agentic, llm, model, catalog, description, instruction,
                       current_path, name, seed) -> BuildResult | None:
    """auto mode: attempt agentic; None if the model can't tool-call (→ oneshot)."""
    try:
        if current_path is not None:
            result = await agentic.edit_agentic(llm, model, catalog, current_path,
                                                instruction, max_rounds=12)
        else:
            result = await agentic.generate_agentic(llm, model, catalog, description,
                                                    name=name or "generated",
                                                    seed_provider=seed, max_rounds=12)
    except LLMError:
        return None          # provider rejected the tools param
    # if the model never called a tool, the draft is empty → let oneshot handle it
    if not result.draft or not result.draft._raw().get("nodes"):
        return None
    return result
