"""Live end-to-end test: drives server.py over stdio exactly like Claude Code.

Makes real free-model calls (cost $0); needs network + opencode on PATH.
Takes ~1-2 minutes.

Usage: .venv/bin/python tests/e2e_client.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def payload_of(res):
    if res.structuredContent is not None:
        p = res.structuredContent
        if isinstance(p, dict) and set(p) == {"result"}:
            p = p["result"]
        return p
    if res.content and res.content[0].type == "text":
        try:
            return json.loads(res.content[0].text)
        except ValueError:
            return res.content[0].text
    return None


async def call(sess, name, args=None):
    res = await sess.call_tool(name, args or {})
    p = payload_of(res)
    shown = json.dumps(p, indent=2) if isinstance(p, (dict, list)) else str(p)
    print(f"\n--- {name}({json.dumps(args or {})[:120]}):\n{shown[:800]}")
    assert not res.isError, f"{name} unexpectedly errored: {shown[:400]}"
    return p


async def main() -> None:
    state_dir = tempfile.mkdtemp(prefix="ocmcp-state-")
    work = tempfile.mkdtemp(prefix="ocmcp-e2e-")
    env = {**os.environ, "OPENCODE_MCP_STATE": state_dir}
    params = StdioServerParameters(
        command=sys.executable, args=[str(ROOT / "server.py")], env=env
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as sess:
            await sess.initialize()

            tools = sorted(t.name for t in (await sess.list_tools()).tools)
            print("tools:", tools)
            assert tools == sorted(
                ["delegate", "list_sessions", "ping", "models", "forget_session"]
            ), tools

            pong = await call(sess, "ping")
            assert pong["ok"], pong
            assert pong["opencode_version"], pong

            # Read mode must not write, even when asked to.
            before = set(os.listdir(work))
            rd = await call(sess, "delegate", {
                "task": "Create a file named hack.txt containing 'x'. "
                        "If you cannot, reply exactly NO-WRITE.",
                "dir": work,
                "mode": "read",
            })
            assert set(os.listdir(work)) == before, "read mode wrote files!"
            assert rd["files_changed"] == [], rd
            assert rd["mode"] == "read" and rd["cost"] == 0, rd

            # Write mode, 2-turn named session.
            t1 = await call(sess, "delegate", {
                "task": "Create a file fib.py containing exactly one function "
                        "fib(n) that returns the nth Fibonacci number "
                        "iteratively. No other code or prose.",
                "dir": work,
                "mode": "write",
                "session": "e2e",
            })
            assert t1["ok"] and t1["session_id"], t1
            assert t1["resumed"] is False, t1
            assert any(f.endswith("fib.py") for f in t1["files_changed"]), t1

            t2 = await call(sess, "delegate", {
                "task": "In fib.py, add a one-line docstring to fib. "
                        "Change nothing else.",
                "dir": work,
                "mode": "write",
                "session": "e2e",
            })
            assert t2["resumed"] is True, t2
            assert t2["session_id"] == t1["session_id"], (t1["session_id"], t2["session_id"])

            content = (Path(work) / "fib.py").read_text(encoding="utf-8")
            print("\nfib.py on disk:\n" + content)
            assert "def fib" in content, content

            ls = await call(sess, "list_sessions")
            entry = next(s for s in ls["sessions"] if s["name"] == "e2e")
            assert entry["turns"] == 2 and entry["id"] == t1["session_id"], entry
            assert entry["tokens_total"]["input"] > 0, entry

            fg = await call(sess, "forget_session", {"name": "e2e"})
            assert fg["removed"] is True, fg
            ls2 = await call(sess, "list_sessions")
            assert all(s["name"] != "e2e" for s in ls2["sessions"]), ls2

            mo = await call(sess, "models")
            assert mo["ok"] and any("free" in m for m in mo["models"]), mo

            # Guards must refuse write mode into a protected path.
            g = await sess.call_tool("delegate", {"task": "x", "dir": "/etc", "mode": "write"})
            assert g.isError, "guard should have refused write mode in /etc"
            print("\nguard refusal OK:", g.content[0].text[:200])

            # And refuse a busy session name... (cheap check: bad mode)
            b = await sess.call_tool("delegate", {"task": "x", "dir": work, "mode": "yolo"})
            assert b.isError, "bad mode should be refused"

    print("\nE2E PASSED")


if __name__ == "__main__":
    asyncio.run(main())
