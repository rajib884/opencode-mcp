"""OpenCode delegation MCP server (stdio).

Lets an orchestrating Claude hand small, well-specified tasks to opencode's
FREE models — codebase summaries, web research, boilerplate, contained bulk
edits — to save paid tokens.

Register (adjust paths per OS):
  claude mcp add -s user opencode -- /root/agent-mcp/.venv/bin/python /root/agent-mcp/server.py

Safety model: read-only by default (opencode --agent plan), write mode is
opt-in and refused for system/home directories, hard timeouts tree-kill the
worker, and every changed file is reported back for review.
"""
from __future__ import annotations

import asyncio
import functools
import os
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

import guards
import runner
from sessions_store import SessionStore, default_state_dir

STATE_DIR = default_state_dir()
STORE = SessionStore(STATE_DIR / "sessions.json")
PING_DIR = STATE_DIR / "ping"


def _default_model() -> str:
    return os.environ.get("OPENCODE_MCP_MODEL", runner.DEFAULT_MODEL)


def _default_timeout() -> float:
    try:
        return float(os.environ.get("OPENCODE_MCP_TIMEOUT", guards.TIMEOUT_DEFAULT))
    except ValueError:
        return guards.TIMEOUT_DEFAULT


mcp = FastMCP(
    "opencode",
    instructions=(
        "Delegate small, well-specified tasks to opencode's FREE models (cost $0) "
        "to save paid tokens. Good delegations: summarizing/exploring a codebase, "
        "web research (the worker has live websearch), boilerplate, first drafts, "
        "contained mechanical edits. The free model is cheap but weak — give it ONE "
        "mechanical task at a time with explicit instructions and concrete file "
        "paths, and verify its output. Keep mode='read' (default, cannot modify "
        "anything) unless the task must edit files; then use mode='write' pointed "
        "at a git-tracked project directory and review files_changed afterwards. "
        "Typical latency 5–15 s per call, occasionally minutes — call ping before "
        "batches."
    ),
)


async def _heartbeat(ctx: Context | None, acc: runner.EventAccumulator, timeout: float) -> None:
    """Progress notifications every 10 s so long runs don't look frozen or time out."""
    if ctx is None:
        return
    start = time.monotonic()
    while True:
        await asyncio.sleep(10)
        elapsed = int(time.monotonic() - start)
        tools = ", ".join(acc.tools_used) or "none yet"
        try:
            await ctx.report_progress(
                progress=min(elapsed, timeout),
                total=timeout,
                message=f"opencode running: {elapsed}s elapsed, {acc.events} events, tools: {tools}",
            )
        except Exception:
            return  # progress is best-effort; never break the call


async def _run_in_thread(fn, acc: runner.EventAccumulator, ctx: Context | None, timeout: float):
    """Run the blocking runner in a worker thread, heartbeat alongside.

    Cancellation-safe: if the client cancels the tool call, the finally block
    tree-kills the opencode child, which unblocks the abandoned thread.
    """
    hb = asyncio.create_task(_heartbeat(ctx, acc, timeout))
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, fn)
    finally:
        hb.cancel()
        if acc.proc is not None:
            runner.kill_tree(acc.proc)


@mcp.tool()
async def delegate(
    task: str,
    dir: str,
    mode: str = "read",
    session: str | None = None,
    new: bool = False,
    model: str | None = None,
    files: list[str] | None = None,
    variant: str | None = None,
    timeout_sec: float | None = None,
    opencode_config: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Run one task on a free opencode model and return its answer + what it did.

    The worker is a cheap model with tools (read/glob/grep, live websearch/webfetch,
    and — in write mode — file edits + shell). Give it ONE small, mechanical,
    fully-specified task; verify important output yourself.

    Args:
        task: The full instruction for the worker. Be explicit; include file paths
            and the exact output you want.
        dir: Absolute path the worker operates in. Its file access and edits are
            relative to this directory.
        mode: "read" (default) = worker cannot modify anything (summaries,
            exploration, web research). "write" = worker may create/edit files in
            dir; refused for system/home directories. Prefer git-tracked dirs so
            changes can be reviewed and reverted.
        session: Optional logical session name. Reusing a name continues the same
            worker conversation (it remembers context); omit for one-shot tasks.
        new: With session, start a fresh conversation under that name instead of
            resuming.
        model: Override model id (see the models tool); default is the free model.
        files: Optional file paths to attach into the worker's context.
        variant: Optional model variant/reasoning-effort passthrough.
        timeout_sec: Hard kill timeout, clamped to 10–570 s (default 180).
        opencode_config: Absolute path to an extra opencode config JSON, e.g. to
            expose MCP servers to the worker ({"mcp": {name: {"type": "local",
            "command": [...]}}}).

    Returns:
        ok, text (the worker's answer), files_changed, tools_used,
        tokens/cost/duration_ms, session_name/session_id/resumed (for follow-ups),
        and on failure error/exit_code/stderr_tail plus any partial results.
    """
    mode = guards.validate_mode(mode)
    if not task or not task.strip():
        raise guards.GuardError("task must be a non-empty string")
    wdir = guards.validate_dir(dir, mode)
    timeout = guards.clamp_timeout(
        timeout_sec if timeout_sec is not None else _default_timeout()
    )
    cfg = guards.validate_opencode_config(opencode_config)
    use_model = model or _default_model()

    async def _run() -> dict[str, Any]:
        session_id: str | None = None
        resumed = False
        title = None
        if session:
            prior = STORE.get(session)
            if prior and not new and prior.get("id"):
                session_id = prior["id"]
                resumed = True
            else:
                title = f"mcp:{session}"

        acc = runner.EventAccumulator()
        fn = functools.partial(
            runner.run_opencode,
            task=task,
            dir=wdir,
            model=use_model,
            read_only=(mode == "read"),
            session_id=session_id,
            title=title,
            files=files,
            variant=variant,
            timeout=timeout,
            opencode_config=cfg,
            acc=acc,
        )
        result = await _run_in_thread(fn, acc, ctx, timeout)

        out: dict[str, Any] = {
            "ok": result.ok,
            "session_name": session,
            "session_id": acc.session_id,
            "resumed": resumed,
            "mode": mode,
            "text": acc.text,
            "files_changed": acc.files_changed,
            "tools_used": acc.tools_used,
            "tokens": acc.tokens,
            "cost": acc.cost,
            "duration_ms": result.duration_ms,
            "model": use_model,
            "dir": str(wdir),
        }
        cap = guards.max_text()
        if len(out["text"]) > cap:
            out["text"] = out["text"][:cap]
            out["truncated"] = True
        if not result.ok:
            out["error"] = result.error
            out["exit_code"] = result.exit_code
            out["stderr_tail"] = result.stderr_tail
            if result.timed_out:
                out["timed_out"] = True
        # Persist the mapping even on failure: a captured ses_ id stays resumable.
        if session and acc.session_id:
            STORE.record_turn(
                session,
                session_id=acc.session_id,
                dir=wdir,
                mode=mode,
                model=use_model,
                tokens=acc.tokens,
                cost=acc.cost,
            )
        return out

    if session:
        with STORE.lease(session):
            return await _run()
    return await _run()


@mcp.tool()
async def list_sessions() -> dict[str, Any]:
    """List named delegation sessions: name, opencode session id, dir, mode,
    model, turns, cumulative tokens/cost, last-used time. Resume one by passing
    its name as `session` to delegate."""
    return {"sessions": STORE.list()}


@mcp.tool()
async def forget_session(name: str) -> dict[str, Any]:
    """Drop a named session from local bookkeeping (opencode's server-side
    session is untouched; the raw ses_ id could still be resumed manually)."""
    return {"removed": STORE.remove(name), "name": name}


@mcp.tool()
async def ping(ctx: Context | None = None) -> dict[str, Any]:
    """Liveness + doctor check: resolves the opencode binary, reports its
    version, and round-trips one trivial free-model prompt (60 s budget).
    Call before batches of delegations."""
    loop = asyncio.get_running_loop()
    try:
        bin_ = runner.resolve_binary()
    except runner.BinaryNotFound as e:
        return {"ok": False, "error": str(e)}
    try:
        version = await loop.run_in_executor(
            None, functools.partial(runner.get_version, bin_)
        )
    except Exception as e:  # doctor reports problems, never crashes
        version = f"version check failed: {e}"

    PING_DIR.mkdir(parents=True, exist_ok=True)
    acc = runner.EventAccumulator()
    fn = functools.partial(
        runner.run_opencode,
        task="Reply with exactly: pong",
        dir=PING_DIR,
        model=_default_model(),
        read_only=True,
        timeout=60.0,
        acc=acc,
    )
    result = await _run_in_thread(fn, acc, ctx, 60.0)
    out = {
        "ok": result.ok and bool(acc.text.strip()),
        "latency_ms": result.duration_ms,
        "model": _default_model(),
        "bin": bin_,
        "opencode_version": version,
    }
    if not out["ok"]:
        out["error"] = result.error or "empty reply"
        out["stderr_tail"] = result.stderr_tail
    return out


@mcp.tool()
async def models(free_only: bool = True) -> dict[str, Any]:
    """List available opencode model ids (free ones by default — the free ids
    rotate over time, so re-check here if a delegation fails on model errors)."""
    loop = asyncio.get_running_loop()
    try:
        found = await loop.run_in_executor(
            None, functools.partial(runner.list_models, free_only=free_only)
        )
    except runner.BinaryNotFound as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "models": found,
        "default": _default_model(),
        "free_only": free_only,
    }


if __name__ == "__main__":
    mcp.run()
