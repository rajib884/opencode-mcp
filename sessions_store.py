"""Persistent named-session state plus per-name busy locks.

Maps a logical session name (chosen by the orchestrator) to the opencode
`ses_…` id and bookkeeping (dir, mode, model, turns, token/cost tallies).
The JSON file is written atomically (tmp + os.replace). opencode's own
session data lives server-side; this store is only the name -> id mapping.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class SessionBusy(RuntimeError):
    """A delegate call is already in flight for this session name."""


def default_state_dir() -> Path:
    env = os.environ.get("OPENCODE_MCP_STATE")
    if env:
        p = Path(env)
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        p = Path(base) / "opencode-mcp"
    else:
        p = Path.home() / ".local" / "state" / "opencode-mcp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self._io_lock = threading.Lock()
        self._name_locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    @contextmanager
    def lease(self, name: str) -> Iterator[None]:
        """Hold the per-name lock for the duration of one delegate call."""
        with self._registry_lock:
            lock = self._name_locks.setdefault(name, threading.Lock())
        if not lock.acquire(blocking=False):
            raise SessionBusy(
                f"session {name!r} already has a delegate call in flight; "
                "wait for it or use a different session name"
            )
        try:
            yield
        finally:
            lock.release()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / (self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, name: str) -> dict[str, Any] | None:
        with self._io_lock:
            return self._load().get(name)

    def record_turn(
        self,
        name: str,
        *,
        session_id: str,
        dir: Path | str,
        mode: str,
        model: str,
        tokens: dict[str, int] | None = None,
        cost: float = 0.0,
    ) -> None:
        with self._io_lock:
            data = self._load()
            entry = data.get(name) or {
                "turns": 0,
                "tokens_total": {"input": 0, "output": 0, "reasoning": 0},
                "cost_total": 0.0,
            }
            entry["id"] = session_id
            entry["dir"] = str(dir)
            entry["mode"] = mode
            entry["model"] = model
            entry["turns"] = int(entry.get("turns", 0)) + 1
            totals = entry.setdefault(
                "tokens_total", {"input": 0, "output": 0, "reasoning": 0}
            )
            for k in ("input", "output", "reasoning"):
                totals[k] = int(totals.get(k, 0)) + int((tokens or {}).get(k, 0))
            entry["cost_total"] = round(float(entry.get("cost_total", 0.0)) + float(cost), 6)
            entry["updated"] = _now_iso()
            data[name] = entry
            self._write(data)

    def remove(self, name: str) -> bool:
        with self._io_lock:
            data = self._load()
            if name not in data:
                return False
            del data[name]
            self._write(data)
            return True

    def list(self) -> list[dict[str, Any]]:
        with self._io_lock:
            data = self._load()
        out = [{"name": name, **entry} for name, entry in data.items()]
        out.sort(key=lambda e: e.get("updated", ""), reverse=True)
        return out
