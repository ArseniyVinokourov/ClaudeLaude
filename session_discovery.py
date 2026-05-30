"""Filesystem/JSONL session discovery — read-only probing of Claude
Code's project transcripts and running processes.

Pure helpers with no dependency on bot state or the Telegram client:
they list project dirs, resolve a session's cwd from its JSONL, locate a
transcript file, compute last-activity time, and enumerate live
`claude --resume` processes. The bot-facing pickers (`cmd_sessions`,
`_discover_resumable_sessions`) live in bot.py and call into these.
"""

import json
import os
import re
import subprocess

from config import PROJECTS_DIR

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def _discover_projects() -> list[str]:
    paths: set[str] = set()
    if os.path.isdir(PROJECTS_DIR):
        for name in sorted(os.listdir(PROJECTS_DIR)):
            full = os.path.join(PROJECTS_DIR, name)
            if os.path.isdir(full) and not name.startswith("."):
                paths.add(full)
    if os.path.isdir(CLAUDE_PROJECTS_DIR):
        for name in sorted(os.listdir(CLAUDE_PROJECTS_DIR)):
            decoded = name.replace("-", "/")
            if not decoded.startswith("/"):
                decoded = "/" + decoded
            if os.path.isdir(decoded) and decoded not in paths:
                paths.add(decoded)
    return sorted(paths, key=lambda p: os.path.getmtime(p), reverse=True)


def _resolve_session_cwd(claude_session_id: str) -> str | None:
    try:
        for d in os.listdir(CLAUDE_PROJECTS_DIR):
            jsonl = os.path.join(CLAUDE_PROJECTS_DIR, d,
                                 f"{claude_session_id}.jsonl")
            if not os.path.isfile(jsonl):
                continue
            with open(jsonl) as f:
                for line in f:
                    obj = json.loads(line)
                    cwd = obj.get("cwd")
                    if cwd:
                        return cwd
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        cwd = msg.get("cwd")
                        if cwd:
                            return cwd
                    if obj.get("type") == "user":
                        break
    except Exception:
        pass
    return None


def _session_jsonl_path(claude_sid: str) -> str | None:
    try:
        for d in os.listdir(CLAUDE_PROJECTS_DIR):
            p = os.path.join(CLAUDE_PROJECTS_DIR, d, f"{claude_sid}.jsonl")
            if os.path.isfile(p):
                return p
    except Exception:
        return None
    return None


def _session_last_active(s) -> float:
    if s.history:
        return s.history[-1].ts
    if s.claude_session_id:
        p = _session_jsonl_path(s.claude_session_id)
        if p:
            try:
                return os.path.getmtime(p)
            except OSError:
                pass
    return s.started


def _live_claude_session_ids() -> set[str]:
    """Sids of currently running `claude --resume <uuid>` processes."""
    sids: set[str] = set()
    try:
        out = subprocess.run(
            ["pgrep", "-af", "claude"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return sids
    for line in out.splitlines():
        m = re.search(r"--resume\s+([0-9a-f-]{36})", line)
        if m:
            sids.add(m.group(1))
    return sids
