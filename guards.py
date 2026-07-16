"""Safety guards: directory validation, protected-path denylist, parameter clamps.

Write mode is opt-in and confined: the cheap worker model must never be
pointed at system directories or at the home directory itself. Read mode
(opencode `--agent plan`) cannot mutate anything, so it only needs the
directory to exist.
"""
from __future__ import annotations

import os
from pathlib import Path

TIMEOUT_DEFAULT = 180.0
TIMEOUT_MIN = 10.0
TIMEOUT_MAX = 570.0
MAX_TEXT_DEFAULT = 40_000

MODES = ("read", "write")


class GuardError(ValueError):
    """A request violated a safety guard; the message is safe to show the caller."""


def _protected_roots() -> list[Path]:
    """Directories whose subtrees write mode must never target."""
    if os.name == "nt":
        sysdrive = os.environ.get("SystemDrive", "C:")
        windir = os.environ.get("SystemRoot", sysdrive + "\\Windows")
        return [
            Path(windir),
            Path(sysdrive + "\\Program Files"),
            Path(sysdrive + "\\Program Files (x86)"),
        ]
    return [
        Path(p)
        for p in (
            "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
            "/var", "/boot", "/sys", "/proc", "/dev", "/run",
        )
    ]


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise GuardError(f"mode must be one of {MODES}, got {mode!r}")
    return mode


def validate_dir(dir_str: str, mode: str) -> Path:
    """Validate the working directory for a delegation; returns the resolved path."""
    if not dir_str:
        raise GuardError("dir is required and must be an absolute path")
    p = Path(dir_str)
    if not p.is_absolute():
        raise GuardError(f"dir must be an absolute path, got {dir_str!r}")
    p = p.resolve()
    if not p.is_dir():
        raise GuardError(f"dir does not exist or is not a directory: {p}")
    if mode != "write":
        return p

    if p == Path(p.anchor):
        raise GuardError("write mode refused: dir is the filesystem root")
    home = Path.home().resolve()
    if p == home:
        raise GuardError(
            "write mode refused: dir is the home directory itself; point at a project subdirectory"
        )
    for root in _protected_roots():
        try:
            root = root.resolve()
        except OSError:
            continue
        if p == root or root in p.parents:
            raise GuardError(f"write mode refused: {p} is under protected path {root}")

    allowed = os.environ.get("OPENCODE_MCP_ALLOWED_DIRS", "").strip()
    if allowed:
        roots = [Path(a).resolve() for a in allowed.split(os.pathsep) if a.strip()]
        if not any(p == r or r in p.parents for r in roots):
            raise GuardError(
                f"write mode refused: {p} is not under any OPENCODE_MCP_ALLOWED_DIRS root"
            )
    return p


def clamp_timeout(value: float | int | None) -> float:
    """Clamp a requested timeout into the safe band; None means the default."""
    if value is None:
        return TIMEOUT_DEFAULT
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise GuardError(f"timeout_sec must be a number, got {value!r}") from None
    return max(TIMEOUT_MIN, min(TIMEOUT_MAX, v))


def max_text() -> int:
    try:
        return max(1000, int(os.environ.get("OPENCODE_MCP_MAX_TEXT", MAX_TEXT_DEFAULT)))
    except ValueError:
        return MAX_TEXT_DEFAULT


def validate_opencode_config(path_str: str | None) -> str | None:
    """An extra opencode config (e.g. MCP servers for the worker) must be a real file."""
    if path_str is None:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        raise GuardError(f"opencode_config must be an absolute path, got {path_str!r}")
    if not p.is_file():
        raise GuardError(f"opencode_config does not exist: {p}")
    return str(p)
