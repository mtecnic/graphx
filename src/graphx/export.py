"""Export a workflow as a portable, self-contained project folder.

The folder installs graphx from a bundled pure-python wheel (no PyPI /
git-auth needed for graphx itself) + its deps from PyPI, supplies
secrets via a .env the operator fills in, and runs with `python run.py`
or Docker. No secret values are ever written.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .connectors.registry import all_connectors
from .model.yaml_loader import load_graph
from .nodes.registry import load_builtin_nodes
from .secrets import find_secret_refs


class ExportError(RuntimeError):
    pass


def _required_extras(graph) -> list[str]:
    extras: set[str] = set()
    if any(n.type == "mcp" for n in graph.nodes.values()) or graph.mcp_servers:
        extras.add("mcp")
    # function connectors behind extras (postgres/s3) — detect by their handler
    handler_extra = {c.render("_x", _dummy_values(c)).node.get("handler"): c.extra
                     for c in all_connectors() if c.extra}
    for node in graph.nodes.values():
        if node.type == "function":
            h = node.config.get("handler")
            if h in handler_extra and handler_extra[h]:
                extras.add(handler_extra[h])
    return sorted(extras)


def _dummy_values(connector) -> dict:
    return {f.name: f.default if f.default is not None else "x" for f in connector.fields}


def _graph_secret_refs(graph) -> set[str]:
    names: set[str] = set()
    for node in graph.nodes.values():
        names |= find_secret_refs(dict(node.config))
    names |= find_secret_refs(dict(graph.providers))
    names |= find_secret_refs(dict(graph.mcp_servers))
    return names


def _build_wheel(out_dir: Path) -> str:
    """Build the graphx wheel into out_dir; return its filename."""
    root = Path(__file__).resolve().parent.parent.parent   # repo root (src/graphx/..)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", str(root), "-w", str(out_dir)],
            check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise ExportError(f"could not build the graphx wheel: {detail[:400]}") from exc
    wheels = list(out_dir.glob("graphx-*.whl"))
    if not wheels:
        raise ExportError("wheel build produced no graphx-*.whl")
    return wheels[0].name


_RUN_PY = '''\
#!/usr/bin/env python3
"""Portable runner for the {name} workflow. `python run.py [--input k=v ...]`."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# load .env into the environment (secrets resolve from env)
env = HERE / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from graphx.cli import app
app(["run", str(HERE / "{name}.yaml"), *sys.argv[1:]])
'''

_README = """\
# {name} — exported graphx workflow

A self-contained, portable copy of the **{name}** workflow. Runs on any
machine with Python 3.12+.

## Run it

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in the values
./venv/bin/python run.py
```

## Or with Docker

```bash
docker build -t {name} .
docker run --rm --env-file .env {name}
```

## Credentials

This workflow references the secrets listed in `.env.example`. Set them
there (they're read as environment variables); nothing is committed.
{secrets_note}
"""

_DOCKERFILE = """\
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt {wheel} ./
RUN pip install --no-cache-dir -r requirements.txt
COPY {name}.yaml run.py ./
CMD ["python", "run.py"]
"""


def export_workflow(workflow: str | Path, out_dir: str | Path | None = None,
                    docker: bool = False) -> Path:
    load_builtin_nodes()
    workflow = Path(workflow)
    graph = load_graph(workflow)              # validates it loads
    name = graph.name
    out = Path(out_dir) if out_dir else Path(f"{name}_export")
    out.mkdir(parents=True, exist_ok=True)

    # 1. workflow
    shutil.copyfile(workflow, out / f"{name}.yaml")

    # 2. bundled graphx wheel
    wheel = _build_wheel(out)

    # 3. requirements (local wheel + detected extras)
    extras = _required_extras(graph)
    extra_str = f"[{','.join(extras)}]" if extras else ""
    (out / "requirements.txt").write_text(f"./{wheel}{extra_str}\n")

    # 4. .env.example (names only, never values)
    secrets = sorted(_graph_secret_refs(graph))
    env_lines = [f"{s}=" for s in secrets] or ["# (this workflow needs no secrets)"]
    (out / ".env.example").write_text("\n".join(env_lines) + "\n")

    # 5. runner + README
    (out / "run.py").write_text(_RUN_PY.format(name=name))
    secrets_note = ("\nNeeded: " + ", ".join(secrets)) if secrets else ""
    (out / "README.md").write_text(_README.format(name=name, secrets_note=secrets_note))

    # 6. Dockerfile
    if docker:
        (out / "Dockerfile").write_text(_DOCKERFILE.format(name=name, wheel=wheel))

    return out


def export_manifest(out: Path) -> list[str]:
    return sorted(p.name for p in out.iterdir())
