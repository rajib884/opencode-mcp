# opencode MCP server

A small, dependency-light MCP (stdio) server that lets Claude Code delegate
cheap, mechanical work to [opencode](https://opencode.ai)'s **free** models —
codebase summaries, exploration, web research, boilerplate, first drafts,
contained bulk edits — so paid tokens go where they matter.

Python 3.10+, stdlib + the official `mcp` SDK. Runs on **Linux and Windows**.

## Tools

| Tool | Purpose |
|---|---|
| `delegate` | Run one task on a free model. `mode:"read"` (default) is enforced read-only; `mode:"write"` may edit files under `dir`. Supports named sessions (multi-turn), file attachments, per-call model/timeout, and `opencode_config` to hand the worker extra MCP servers. |
| `list_sessions` | Named sessions with opencode ids, dirs, turn counts, token/cost tallies. |
| `forget_session` | Drop a name from local bookkeeping (opencode's server-side session survives). |
| `ping` | Liveness + doctor: resolves the binary, reports version, round-trips one prompt. |
| `models` | List model ids (free only by default — free ids rotate). |

## Safety model

The worker is a cheap model; the guardrails assume it will occasionally do
something dumb.

- **Read-only by default.** `mode:"read"` maps to `opencode --agent plan`.
  Verified behavior: file writes and bash mutations are refused (nothing ever
  hit disk in testing); read-only shell commands and network (websearch) still
  work — read mode is *not* offline.
- **Write mode is confined.** `dir` must be an absolute, existing directory;
  the filesystem root, the home directory itself, and system paths
  (`/etc`, `/usr`, `/var`, …, `C:\Windows`, `C:\Program Files*`) are refused,
  including their subtrees. Optionally set `OPENCODE_MCP_ALLOWED_DIRS`
  (pathsep-separated roots) to allowlist where write mode may operate.
- **Never `--auto`.** opencode's own permission prompts stay denied in
  non-interactive runs.
- **Everything is reported.** `files_changed` and `tools_used` come back on
  every call; point write mode at git-tracked dirs so you can diff/revert.
- **Hard timeout with tree-kill** (opencode spawns child processes): POSIX
  process-group kill, Windows `taskkill /T /F`. Cancelling the MCP call kills
  the worker too. Timeouts return partial results and keep the session
  resumable.
- **One call per session name at a time**; parallel calls on different
  sessions are fine.

## Install & register

```sh
cd /root/agent-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# register with Claude Code (user scope):
claude mcp add -s user opencode -- /root/agent-mcp/.venv/bin/python /root/agent-mcp/server.py
```

Windows (PowerShell):

```powershell
cd C:\path\to\agent-mcp
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
claude mcp add -s user opencode -- C:\path\to\agent-mcp\.venv\Scripts\python.exe C:\path\to\agent-mcp\server.py
```

Windows smoke check (3 commands):

```powershell
opencode --version                                  # CLI reachable?
.venv\Scripts\python -m unittest discover -s tests  # parser/guards/kill-path
.venv\Scripts\python tests\e2e_client.py            # live free-model round-trip
```

## Configuration (env vars, all optional)

| Var | Default | Meaning |
|---|---|---|
| `OPENCODE_MCP_BIN` | auto-detect | Path to the opencode executable. Auto-detection: PATH, then the real `opencode.exe` behind npm `.cmd` shims on Windows, then `~/.opencode/bin/opencode`. |
| `OPENCODE_MCP_MODEL` | `opencode/deepseek-v4-flash-free` | Default model id. |
| `OPENCODE_MCP_STATE` | `~/.local/state/opencode-mcp` / `%LOCALAPPDATA%\opencode-mcp` | Where `sessions.json` lives. |
| `OPENCODE_MCP_TIMEOUT` | `180` | Default per-call timeout (s); per-call `timeout_sec` clamps to 10–570. |
| `OPENCODE_MCP_MAX_TEXT` | `40000` | Truncate the worker's answer beyond this many chars (`truncated: true`). |
| `OPENCODE_MCP_ALLOWED_DIRS` | unset | If set, write-mode `dir` must live under one of these roots. |

## Giving the worker MCP tools

Pass `opencode_config` (absolute path) to `delegate`, pointing at e.g.:

```json
{
  "mcp": {
    "gbk-fs": {
      "type": "local",
      "command": ["/root/gbk-fs/.venv/bin/python3", "-m", "gbk_fs"],
      "enabled": true
    }
  }
}
```

Verified live: the free model discovers and calls such tools (they surface to
it as `gbk-fs_list_files`, etc.).

## Tests

```sh
.venv/bin/python -m unittest discover -s tests   # offline: real captured fixtures
.venv/bin/python tests/e2e_client.py             # live: needs network, ~1-2 min, $0
```

## Design notes / gotchas (hard-won)

- The task prompt is **piped via stdin** (write, close). Never pass it in argv:
  a `--` separator makes opencode drop the message and hang waiting on stdin,
  and Windows caps argv at 32 KB.
- Resume sessions with `-s <ses_id>` (never `--continue`).
- `opencode run --format json` emits JSONL events; the answer is the
  concatenation of `text` parts, tokens/cost arrive in `step-finish` parts,
  file writes are completed `tool` parts (`write`/`edit`/`patch`/`multiedit`)
  with `state.input.filePath`.
- Pipes are forced to UTF-8 (`errors="replace"`) — Windows would otherwise
  default to cp125x. stderr goes to a temp file to avoid two-pipe deadlock.
- Free model ids rotate; the `models` tool exists so the orchestrator can
  self-correct when the default disappears.
