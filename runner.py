"""Spawn `opencode run --format json` and parse its JSONL event stream.

Verified against opencode 1.18.2 (Linux) and 1.18.1 (Windows):

- The task prompt MUST be piped via stdin (write, then close). Passing it in
  argv after a `--` separator makes opencode hang forever waiting on stdin,
  and Windows caps argv at 32 KB anyway.
- Every event carries `sessionID`. The final answer is the concatenation of
  `text` parts; token/cost totals arrive in `step-finish` parts; file writes
  show up as completed `tool` parts whose input has a `filePath`.
- opencode spawns child server processes, so timeouts must kill the whole
  process tree, not just the direct child.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"
WRITE_TOOLS = {"write", "edit", "patch", "multiedit"}
STDERR_TAIL_CHARS = 2000


class BinaryNotFound(RuntimeError):
    pass


def resolve_binary() -> str:
    """Locate the opencode executable: env override -> PATH -> known fallbacks."""
    env = os.environ.get("OPENCODE_MCP_BIN")
    if env:
        if Path(env).exists() or shutil.which(env):
            return env
        raise BinaryNotFound(f"OPENCODE_MCP_BIN={env!r} does not exist")
    found = shutil.which("opencode")
    if found:
        p = Path(found)
        if os.name == "nt" and p.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            # npm shim: subprocess can't exec these directly; find the real exe
            candidates = [
                p.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
                *sorted(p.parent.glob("node_modules/opencode-windows-*/bin/opencode.exe")),
            ]
            for candidate in candidates:
                if candidate.exists():
                    return str(candidate)
            raise BinaryNotFound(
                f"found npm shim {p} but no opencode.exe under its node_modules; "
                "set OPENCODE_MCP_BIN to the full path of opencode.exe"
            )
        return found
    posix_fallback = Path.home() / ".opencode" / "bin" / "opencode"
    if posix_fallback.exists():
        return str(posix_fallback)
    raise BinaryNotFound("opencode not found: add it to PATH or set OPENCODE_MCP_BIN")


@dataclass
class EventAccumulator:
    """Mutable parse state.

    The runner thread writes it while a heartbeat task may read the counters,
    and the server's cancellation path uses `.proc` to kill the child tree.
    """

    session_id: str | None = None
    events: int = 0
    texts: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)  # unique, first-seen order
    files_changed: list[str] = field(default_factory=list)  # completed write-tools only
    tokens: dict[str, int] = field(
        default_factory=lambda: {"input": 0, "output": 0, "reasoning": 0}
    )
    cost: float = 0.0
    proc: subprocess.Popen | None = None

    def feed_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        self.events += 1
        sid = ev.get("sessionID")
        if sid and not self.session_id:
            self.session_id = sid
        part = ev.get("part") or {}
        ptype = part.get("type")
        if ptype == "text":
            self.texts.append(part.get("text") or "")
        elif ptype == "tool":
            tool = part.get("tool") or "?"
            if tool not in self.tools_used:
                self.tools_used.append(tool)
            state = part.get("state") or {}
            if tool in WRITE_TOOLS and state.get("status") == "completed":
                fp = (state.get("input") or {}).get("filePath")
                if fp and fp not in self.files_changed:
                    self.files_changed.append(fp)
        elif ptype in ("step-finish", "step_finish"):
            toks = part.get("tokens") or {}
            for k in ("input", "output", "reasoning"):
                v = toks.get(k)
                if isinstance(v, (int, float)):
                    self.tokens[k] += int(v)
            c = part.get("cost")
            if isinstance(c, (int, float)):
                self.cost += float(c)

    @property
    def text(self) -> str:
        return "".join(self.texts)


@dataclass
class RunResult:
    ok: bool
    acc: EventAccumulator
    duration_ms: int
    exit_code: int | None = None
    timed_out: bool = False
    error: str | None = None
    stderr_tail: str = ""


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the opencode process and every child it spawned. Idempotent."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True
        )
        if proc.poll() is None:
            proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)  # pgid == pid via start_new_session
    except ProcessLookupError:
        return
    except PermissionError:
        proc.kill()
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_opencode(
    *,
    task: str,
    dir: Path,
    model: str,
    read_only: bool,
    session_id: str | None = None,
    title: str | None = None,
    files: list[str] | None = None,
    variant: str | None = None,
    timeout: float = 180.0,
    opencode_config: str | None = None,
    bin_path: str | None = None,
    acc: EventAccumulator | None = None,
) -> RunResult:
    """Run one opencode turn. Blocking — call from a worker thread.

    Never uses `--auto` (opencode's own permission prompts stay denied) and
    never passes the task on the command line.
    """
    acc = acc if acc is not None else EventAccumulator()
    try:
        bin_ = bin_path or resolve_binary()
    except BinaryNotFound as e:
        return RunResult(ok=False, acc=acc, duration_ms=0, error=str(e))

    args = [bin_, "run", "--format", "json", "-m", model, "--dir", str(dir)]
    if read_only:
        args += ["--agent", "plan"]
    if session_id:
        args += ["-s", session_id]
    if title:
        args += ["--title", title]
    for f in files or []:
        args += ["-f", f]
    if variant:
        args += ["--variant", variant]

    env = os.environ.copy()
    if opencode_config:
        env["OPENCODE_CONFIG"] = opencode_config

    popen_kw: dict[str, Any] = {}
    if os.name != "nt":
        popen_kw["start_new_session"] = True  # own process group for kill_tree

    start = time.monotonic()
    timed_out = threading.Event()
    with tempfile.TemporaryFile(
        mode="w+", encoding="utf-8", errors="replace"
    ) as errf:
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errf,
                cwd=str(dir),
                env=env,
                encoding="utf-8",
                errors="replace",
                **popen_kw,
            )
        except OSError as e:
            return RunResult(
                ok=False, acc=acc, duration_ms=0, error=f"failed to start opencode: {e}"
            )
        acc.proc = proc

        def _on_timeout() -> None:
            timed_out.set()
            kill_tree(proc)

        timer = threading.Timer(timeout, _on_timeout)
        timer.daemon = True
        timer.start()
        try:
            try:
                assert proc.stdin is not None
                proc.stdin.write(task)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # child died early; exit code + stderr tell the story
            assert proc.stdout is not None
            for line in proc.stdout:
                acc.feed_line(line)
            exit_code = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                kill_tree(proc)
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        errf.seek(0)
        stderr_tail = errf.read()[-STDERR_TAIL_CHARS:]

    duration_ms = int((time.monotonic() - start) * 1000)
    if timed_out.is_set():
        return RunResult(
            ok=False,
            acc=acc,
            duration_ms=duration_ms,
            exit_code=exit_code,
            timed_out=True,
            stderr_tail=stderr_tail,
            error=(
                f"timed out after {timeout:.0f}s and was killed"
                " (partial results included; the session may still be resumable)"
            ),
        )
    if exit_code != 0:
        return RunResult(
            ok=False,
            acc=acc,
            duration_ms=duration_ms,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
            error=f"opencode exited with code {exit_code}",
        )
    return RunResult(
        ok=True, acc=acc, duration_ms=duration_ms, exit_code=0, stderr_tail=stderr_tail
    )


def get_version(bin_path: str | None = None, timeout: float = 20.0) -> str:
    bin_ = bin_path or resolve_binary()
    out = subprocess.run(
        [bin_, "--version"],
        capture_output=True,
        stdin=subprocess.DEVNULL,  # else opencode inherits the MCP stdio pipe and hangs
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return ((out.stdout or "") + (out.stderr or "")).strip()


def list_models(
    bin_path: str | None = None, free_only: bool = True, timeout: float = 30.0
) -> list[str]:
    bin_ = bin_path or resolve_binary()
    out = subprocess.run(
        [bin_, "models"],
        capture_output=True,
        stdin=subprocess.DEVNULL,  # else opencode inherits the MCP stdio pipe and hangs
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    models = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip() and "/" in ln]
    if free_only:
        models = [m for m in models if "free" in m.lower()]
    return models
