"""Pure presentation helpers — text formatting and tool-noise filters.

No dependencies on bot state, the session manager, or the Telegram
client: every function here is a pure transform of its arguments. Split
out of bot.py so the rendering rules can be read and unit-tested in
isolation.
"""

import json
import os
import re

from config import HOOK_PORT

# ── markdown tables → vertical lists ─────────────────────────────────

_MD_TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*$\n?){2,})",
    re.MULTILINE,
)
_SEP_RE = re.compile(r"^[ \t]*\|[-| :]+\|[ \t]*$")


def _md_table_to_list(text: str) -> str:
    """Convert markdown tables to list format (outputs markdown, not HTML)."""
    def _replace(m: re.Match) -> str:
        lines = m.group(1).strip().splitlines()
        rows = []
        for line in lines:
            if _SEP_RE.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(cells)
        if len(rows) < 2:
            return m.group(0)
        header = rows[0]
        result = []
        for row in rows[1:]:
            val = row[0] if row else ""
            if len(val) <= 3 or val.isdigit():
                title = f"**{header[0]} {val}**"
            else:
                title = f"**{val}**"
            details = [f"  {h}: {v}" for h, v in zip(header[1:], row[1:], strict=False)]
            result.append(title + "\n" + "\n".join(details))
        return "\n\n".join(result)
    return _MD_TABLE_RE.sub(_replace, text)


# ── tool-noise filters ───────────────────────────────────────────────

_NOISE_TOOLS = {"Glob", "Grep", "Agent", "TodoWrite", "ToolSearch", "Read"}
_NOISE_PATHS = ("/.claude/", "/memory/", "MEMORY.md")

_BASH_READONLY_PREFIXES = (
    "cat ", "ls", "pwd", "echo ", "printf ",
    "find ", "grep ", "rg ", "ag ",
    "head ", "tail ", "wc ", "awk ", "sed -n",
    "git log", "git status", "git diff", "git blame",
    "git branch", "git show", "git ls-files",
    "gh pr list", "gh issue list", "gh release list",
    "gh pr view", "gh issue view",
    "which ", "type ", "command -v",
    "stat ", "file ", "du ", "df ",
    "ps ", "top ", "htop",
)


def _is_noisy_tool(tool, inp):
    if tool in _NOISE_TOOLS:
        return True
    if tool in ("Write", "Edit"):
        path = inp.get("file_path", "")
        if any(p in path for p in _NOISE_PATHS):
            return True
    if tool == "Bash":
        cmd = inp.get("command", "") or ""
        # Hide the slash command's own plumbing:
        #  - direct hook curls (legacy inlined slash-command body),
        #  - the `source …/bot-mirror-cmd.sh` form (current body that
        #    delegates to the external script).
        if "/hook/open_in_bot" in cmd or f":{HOOK_PORT}/hook/" in cmd:
            return True
        if "bot-mirror-cmd.sh" in cmd:
            return True
    return False


def _is_mirror_noisy_tool(tool, inp):
    """Stricter filter for the mirror channel: hide observational Bash
    too (cat/grep/git log/etc) so the topic stays a clean conversation
    transcript. Full tool trace is still visible in the terminal.

    Used by the "lite" filter level (default). The "all" level hides
    every tool_use upstream of this check.
    """
    if _is_noisy_tool(tool, inp):
        return True
    if tool == "Bash":
        cmd = (inp.get("command", "") or "").lstrip()
        cmd_head = cmd.split(" 2>")[0].split(" |")[0].split(" &&")[0].lstrip()
        for prefix in _BASH_READONLY_PREFIXES:
            if cmd_head.startswith(prefix):
                return True
        # python3 <<'PYEOF' … PYEOF style: implementation-detail
        # work the owner doesn't need to see in the mirror.
        if "<<'PYEOF'" in cmd or '<<"PYEOF"' in cmd or "<< PYEOF" in cmd:
            return True
    # Write/Edit/MultiEdit projections double up with the permission
    # prompt the owner already approved — hide them on the lite level.
    if tool in ("Write", "Edit", "MultiEdit"):
        return True
    return False


def _normalize_tool_input(tool, ti) -> str:
    """Return a stable signature for a tool_use's "what it does" so
    a pending permission can be matched against the eventual tool_use
    in the JSONL. Tool-specific to ignore spurious metadata like
    `description` or buffer offsets.
    """
    if not isinstance(ti, dict):
        return ""
    if tool == "Bash":
        return (ti.get("command", "") or "").strip()
    if tool in ("Write", "Edit", "MultiEdit", "Read"):
        return ti.get("file_path", "") or ""
    # Fallback: full JSON of sorted keys.
    try:
        return json.dumps(ti, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(ti)


def _chat_action_for_tool(name: str | None) -> str:
    """Map a Claude tool name to a Telegram chat-action verb.

    Telegram shows the action ("typing", "uploading photo", …) beneath
    the chat title. Matching the verb to what Claude is actually doing
    turns the indicator into a live status hint without adding a UI
    element. Unknown / no-tool → typing.
    """
    if not name:
        return "typing"
    n = name.lower()
    if n in ("read", "glob", "grep", "ls", "edit", "write", "notebookedit",
             "multiedit"):
        return "upload_document"
    if n in ("webfetch", "websearch"):
        return "find_location"
    if "image" in n or "screenshot" in n or n in ("notebookread",):
        return "upload_photo"
    return "typing"


def _compact_tool_msg(tool, inp):
    if tool == "Bash":
        cmd = inp.get("command", "?")
        # Collapse multi-line shell snippets into one displayable line.
        cmd = cmd.replace("\n", " ; ").strip()
        return f"$ {cmd[:50]}" if len(cmd) <= 50 else f"$ {cmd[:47]}…"
    if tool in ("Write", "Edit"):
        path = inp.get("file_path", "?")
        return f"{tool}: {os.path.basename(path)}"
    if tool == "Read":
        path = inp.get("file_path", "?")
        return f"Read: {os.path.basename(path)}"
    return tool


def _short_cwd(cwd: str, limit: int = 48) -> str:
    if not cwd or len(cwd) <= limit:
        return cwd or "?"
    return "…" + cwd[-(limit - 1):]


_MD_INLINE = re.compile(r"[*_`]")


def _strip_md(s: str) -> str:
    """Strip leading markdown markers and inline emphasis chars from a preview."""
    s = re.sub(r"^[#>\-*\s]+", "", s)
    s = _MD_INLINE.sub("", s)
    return " ".join(s.split())


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds/60)}m ago"
    if seconds < 86400:
        return f"{int(seconds/3600)}h ago"
    return f"{int(seconds/86400)}d ago"


# ── per-topic control panel ──────────────────────────────────────────

def topic_control_rows(alive: bool, display_mode: str) -> list:
    """Inline-keyboard rows for a session topic's control panel.

    Pinned to the topic's opening message so a long history is one tap
    away. Callbacks route through the `m:` dispatcher, which resolves the
    session by topic id at click time — no per-button session id needed.
    """
    mode_label = ("\U0001f4f1 → \U0001f5a5" if display_mode == "mobile"
                  else "\U0001f5a5 → \U0001f4f1")
    if alive:
        return [
            [{"text": "\U0001f3af Mode", "callback_data": "m:mode"},
             {"text": f"{mode_label} Display", "callback_data": "m:display"}],
            [{"text": "\U0001f4ca Usage", "callback_data": "m:usage"},
             {"text": "\U0001f4dc History", "callback_data": "m:history"}],
            [{"text": "⏹ Stop", "callback_data": "m:stop"}],
        ]
    return [[{"text": "▶️ Restart", "callback_data": "m:restart"}]]
