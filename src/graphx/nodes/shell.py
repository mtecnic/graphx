"""shell node: run a subprocess (including whole CLI agents).

Runs in its own process group so cancellation and timeouts kill the
entire tree (SIGTERM, then SIGKILL after a grace period). stdout is
streamed line-by-line as node_output_chunk events.
"""

from __future__ import annotations

import asyncio
import os
import signal

from pydantic import BaseModel, Field

from ..engine.errors import GraphxError
from .registry import NodeContext, NodeResult, node_type

_MAX_CAPTURE = 256 * 1024  # keep outputs checkpoint-friendly
_KILL_GRACE_S = 5.0


class ShellConfig(BaseModel):
    command: list[str] | str
    stdin: str | None = None
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    check: bool = True          # nonzero exit -> node failure


class ShellFailed(GraphxError):
    pass


async def _read_stream(stream: asyncio.StreamReader, chunks: list[str],
                       emit) -> None:
    captured = 0
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode(errors="replace")
        if captured < _MAX_CAPTURE:
            chunks.append(text)
            captured += len(text)
        if emit is not None:
            await emit(text)


@node_type("shell", config_model=ShellConfig)
async def shell_node(ctx: NodeContext) -> NodeResult:
    config: ShellConfig = ctx.config  # type: ignore[assignment]
    env = {**os.environ, **config.env} if config.env else None

    if isinstance(config.command, str):
        create = asyncio.create_subprocess_shell(
            config.command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=config.cwd, env=env, start_new_session=True)
    else:
        create = asyncio.create_subprocess_exec(
            *config.command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=config.cwd, env=env, start_new_session=True)
    proc = await create

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(_read_stream(proc.stdout, stdout_chunks, ctx.emit_chunk))
            group.create_task(_read_stream(proc.stderr, stderr_chunks, None))
            group.create_task(_feed_stdin(proc, config.stdin))
        returncode = await proc.wait()
    except BaseException:
        await _kill_group(proc)
        raise

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    if config.check and returncode != 0:
        raise ShellFailed(
            f"command exited {returncode}: {stderr.strip()[:500] or stdout.strip()[:500]}")
    return NodeResult(output={
        "stdout": stdout, "stderr": stderr, "returncode": returncode,
    })


async def _feed_stdin(proc: asyncio.subprocess.Process, data: str | None) -> None:
    if proc.stdin is None:
        return
    if data:
        proc.stdin.write(data.encode())
        await proc.stdin.drain()
    proc.stdin.close()


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        try:
            async with asyncio.timeout(_KILL_GRACE_S):
                await proc.wait()
        except TimeoutError:
            os.killpg(pgid, signal.SIGKILL)
            await proc.wait()
    except ProcessLookupError:
        pass
