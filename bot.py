#!/usr/bin/env python3
"""ClaudeLaude Telegram bot — Forum Topics UI for Claude Code.

Each person runs their own instance of this bot on their machine.
See README.md or run setup.sh for first-time configuration.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(__file__))

from config import (OWNER_ID, PROJECTS_DIR, HOOK_PORT,
                    AUTO_UPDATE, AUTO_UPDATE_POLICY, BOT_DIR,
                    UNLOCK_WORD,
                    get_forum_chat_id, set_forum_chat_id,
                    get_pinned_help_id, set_pinned_help_id,
                    get_dashboard_id, set_dashboard_id,
                    is_killed, activate_kill, deactivate_kill)
import audit
import telegram as tg
from sessions import MODE_PRESETS, Session, SessionManager
from hooks import HookBridge
from terminal_mirror import (
    TerminalMirrorManager, push_to_dtach, dtach_socket_alive,
    read_logical_events,
)

# ── state ────────────────────────────────────────────────────────────

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
DEFAULT_DISPLAY = "mobile"
_UPLOAD_DIR = "/tmp/bot_uploads"

_ICON_ACTIVE = "5417915203100613993"   # 💬
_ICON_TERMINAL = "5350554349074391003" # 💻
_ICON_STOPPED = ""                     # removes custom emoji → color dot

# How many recent messages to copy from the parent topic into a fork.
# 5 is enough to see the last user prompt + assistant answer; more clutters
# the fresh fork. copyMessages is capped at 100 by the API anyway.
_FORK_BACKFILL = 5

# Telegram free-tier reaction set is small; these are picked for clarity.
_REACT_RECEIVED = "👀"   # bot got the message
_REACT_STREAMING = "🔥"  # claude is producing text
_REACT_TOOL = "⚡"        # claude reached for a tool
_REACT_DONE = "👍"
_REACT_INTERRUPTED = "🤷"
_REACT_ERROR = "😨"


@dataclass
class TurnState:
    """Tracks messages and tool ops within one Claude turn."""
    msg_ids: list[int] = field(default_factory=list)
    msg_texts: list[str] = field(default_factory=list)
    tool_ops: list[str] = field(default_factory=list)
    status_msg_id: int | None = None
    _last_status_text: str = ""
    started_at: float = field(default_factory=time.time)
    stop_event: threading.Event = field(default_factory=threading.Event)
    _timer_thread: threading.Thread | None = None
    interrupted: bool = False
    # User-message ids (in the topic) whose response is awaited; their
    # reaction is upgraded from 👀 → 🔥/⚡ → 👍 across the turn.
    user_msg_ids: list[int] = field(default_factory=list)
    # Progress flags: once True, the relevant 🔥/⚡ reaction has fired and
    # later events do not regress (⚡ tool wins over 🔥 stream).
    streamed: bool = False
    tooled: bool = False
    # Raw name of most recent tool ("Read", "Bash", "WebFetch", …) — drives
    # the contextual sendChatAction indicator. Stays after the tool ends so
    # the timer thread keeps refreshing the same action mid-thinking.
    last_tool_name: str | None = None


class BotState:
    def __init__(self):
        self.lock = threading.Lock()
        # short_id -> (msg_id, chat_id, sid_or_None)
        self.pending_permissions: dict[str, tuple[int, int, str | None]] = {}
        self.perm_key_map: dict[str, str] = {}
        self.pending_project_picks: dict[str, list[str]] = {}
        self.pending_resume_picks: dict[str, list[tuple]] = {}
        self.topic_display_mode: dict[int, str] = {}
        self.turns: dict[str, TurnState] = {}
        self.topic_counter: dict[str, int] = {}
        self.renamed_topics: set[int] = set()
        self.saved_turns: dict[str, tuple[list[int], list[str], list[str]]] = {}
        self.topic_labels: dict[int, str] = {}
        # terminal watcher: claude_session_id → [(msg_id, chat_id, kind)]
        # kind: "perm:<short_id>" | "notification"
        self.pending_terminal_msgs: dict[str, list[tuple[int, int, str]]] = {}
        # Rolling window of recent message_ids per session topic, used to
        # backfill context when a fork is created. Trimmed to _FORK_BACKFILL.
        self.recent_msgs: dict[int, list[int]] = {}


state = BotState()
bot_running = True


def forum():
    return get_forum_chat_id()


# ── security helpers ────────────────────────────────────────────────

_unkill_attempts: list[float] = []
_UNKILL_MAX_ATTEMPTS = 3
_UNKILL_COOLDOWN = 300  # 5 minutes


def _try_unkill(text: str, chat_id: int, msg_id: int | None,
                thread_id: int | None) -> bool:
    """Accept unlock word in General only. Constant-time compare, no regex."""
    if not UNLOCK_WORD or not is_killed():
        return False
    fid = forum()
    if not fid or chat_id != fid or thread_id:
        return False
    now = time.time()
    # Prune old attempts
    _unkill_attempts[:] = [t for t in _unkill_attempts
                           if now - t < _UNKILL_COOLDOWN]
    if len(_unkill_attempts) >= _UNKILL_MAX_ATTEMPTS:
        audit.log("kill_switch", "unlock rate-limited")
        remaining = int(_UNKILL_COOLDOWN - (now - _unkill_attempts[0]))
        _ephemeral(chat_id, f"Rate limited. Try again in {remaining}s.",
                   seconds=10)
        if msg_id:
            tg.delete(msg_id, chat_id)
        return True
    import hmac
    clean = text.strip()
    if not hmac.compare_digest(clean.encode(), UNLOCK_WORD.encode()):
        _unkill_attempts.append(now)
        audit.log("kill_switch", "unlock failed attempt")
        left = _UNKILL_MAX_ATTEMPTS - len(_unkill_attempts)
        if left > 0:
            _ephemeral(chat_id, f"Wrong. {left} attempt(s) left.", seconds=5)
        if msg_id:
            tg.delete(msg_id, chat_id)
        return True
    _unkill_attempts.clear()
    deactivate_kill()
    audit.log("kill_switch", "deactivated via unlock word")
    if msg_id:
        tg.delete(msg_id, chat_id)
    _ephemeral(chat_id, "\U0001f513 Bot unlocked.", seconds=5)
    _sync_dashboard()
    return True


def _do_kill():
    activate_kill()
    audit.log("kill_switch", "activated")
    for s in mgr.list_sessions():
        if s.alive:
            mgr.stop(s.sid, reason="kill switch")
    fid = forum()
    if fid:
        _sync_dashboard()
        if UNLOCK_WORD:
            _ephemeral(fid, "\U0001f512 Bot killed. All sessions stopped.\n"
                       "Send unlock word in General to restore.",
                       seconds=15)
        else:
            _ephemeral(fid, "\U0001f512 Bot killed. All sessions stopped.\n"
                       "Delete .kill file on the machine to restore.",
                       seconds=15)


# ── markdown table → list (mobile) ──────────────────────────────────

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


# ── helpers ──────────────────────────────────────────────────────────

_CLOSE_ROW = [{"text": "✕ Close", "callback_data": "close"}]

_PICKER_TTL = 60


def send_to_topic(topic_id, text, buttons=None):
    fid = forum()
    if fid and topic_id:
        return tg.send(text, fid, thread_id=topic_id, buttons=buttons)
    return None


def send_general(text, buttons=None):
    fid = forum()
    if fid:
        return tg.send(text, fid, buttons=buttons)
    return tg.send(text, OWNER_ID, buttons=buttons)


def _reply(chat_id, thread_id, text, buttons=None):
    if chat_id:
        return tg.send(text, chat_id, thread_id=thread_id, buttons=buttons)
    return send_general(text, buttons=buttons)


def _ephemeral(chat_id, text, thread_id=None, seconds=15, buttons=None):
    """Send a message that auto-deletes after `seconds`."""
    mid = tg.send(text, chat_id, thread_id=thread_id, buttons=buttons)
    if mid:
        def _cleanup():
            time.sleep(seconds)
            tg.delete(mid, chat_id)
        threading.Thread(target=_cleanup, daemon=True).start()
    return mid


# ── turn tracking ────────────────────────────────────────────────────

_NOISE_TOOLS = {"Glob", "Grep", "Agent", "TodoWrite", "ToolSearch", "Read"}
_NOISE_PATHS = ("/.claude/", "/memory/", "MEMORY.md")



def _get_turn(session) -> TurnState:
    with state.lock:
        if session.sid not in state.turns:
            state.turns[session.sid] = TurnState()
        return state.turns[session.sid]


def _enqueue_user_input(session, text: str, chat_id: int,
                         msg_id: int | None, thread_id: int | None):
    """Send a user message into Claude and wire up the 👀→👍 reaction.

    Used for plain text, sticker descriptors, and file-attached prompts.
    Reacting up-front gives the user immediate feedback that the bot saw
    the message even before Claude starts streaming.
    """
    ok = mgr.send_user_message(session.sid, text)
    if not ok:
        tg.send("⚠️ Session died", chat_id, thread_id=thread_id)
        return
    if msg_id and chat_id:
        _react_user_msg(msg_id, chat_id, _REACT_RECEIVED)
        turn = _get_turn(session)
        turn.user_msg_ids.append(msg_id)
        _record_topic_msg(session.topic_id, msg_id)


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


def _mirror_welcome_text(mirror) -> str:
    if mirror.dtach_socket:
        return ("\U0001fa9e Mirror attached. Type in this topic — keystrokes "
                "go into the terminal claude.")
    return ("\U0001f50c Mirror is output-only — start your terminal "
            "claude inside dtach to enable typing from here.")


_MIRROR_MODE_CYCLE = ("default", "acceptEdits", "plan", "auto")


def _mirror_mode_name(mirror) -> str:
    try:
        return _MIRROR_MODE_CYCLE[mirror.mode_index % len(_MIRROR_MODE_CYCLE)]
    except Exception:
        return _MIRROR_MODE_CYCLE[0]


def _mirror_welcome_buttons(mirror) -> list:
    """Build the inline-keyboard rows for a mirror's welcome message.

    Row 1: filter toggle. Label shows CURRENT level; clicking switches
           to the other one. Callback `mf:<csid12>:<next>`.
    Row 2 (only if dtach is wired): mode cycle. Label shows the bot's
           best-guess CURRENT mode; clicking pushes Shift+Tab into the
           dtach socket and advances our index. Cycle:
           default → acceptEdits → plan → default. Drifts only if the
           owner presses Shift+Tab directly in the terminal, since the
           bot can't read Claude's TUI back. Callback `mm:<csid12>`.
    """
    short = mirror.csid[:12]
    cur = "lite" if mirror.filter_level == "lite" else "all"
    nxt = "all" if cur == "lite" else "lite"
    cur_label = "lite (hide noise)" if cur == "lite" else "all (chat only)"
    rows = [[
        {"text": f"\U0001f9f0 Filter: {cur_label}",
         "callback_data": f"mf:{short}:{nxt}"},
    ]]
    if mirror.dtach_socket:
        rows.append([
            {"text": f"⇄ Mode: {_mirror_mode_name(mirror)}",
             "callback_data": f"mm:{short}"},
        ])
    return rows


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


def _react_user_msg(msg_id: int, chat_id: int, emoji: str | None):
    """Set/clear a reaction on a user message. Silent on failure."""
    try:
        tg.set_message_reaction(chat_id, msg_id, emoji)
    except Exception as e:
        print(f"[react] {emoji!r} on {msg_id} failed: {e}",
              file=sys.stderr, flush=True)


def _record_topic_msg(topic_id: int | None, msg_id: int | None):
    """Push a message id into the per-topic rolling window used for fork
    backfill. Trims to _FORK_BACKFILL most-recent ids. Silently no-ops on
    missing inputs so callers don't need to branch."""
    if not topic_id or not msg_id:
        return
    with state.lock:
        buf = state.recent_msgs.setdefault(topic_id, [])
        buf.append(int(msg_id))
        if len(buf) > _FORK_BACKFILL:
            del buf[:-_FORK_BACKFILL]


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


def _react_progress(turn: TurnState, emoji: str):
    """Refresh in-flight reaction on every user message awaiting a reply.

    Used during the turn to swap 👀 → 🔥 (text streaming) or 👀 → ⚡
    (tool use). Idempotent; safe if user_msg_ids is empty.
    """
    if not turn.user_msg_ids:
        return
    fid = forum()
    if not fid:
        return
    for mid in turn.user_msg_ids:
        _react_user_msg(mid, fid, emoji)


def _react_for_turn(turn: TurnState, kind: str = "completed"):
    """Upgrade per-user-message reactions to a final-state emoji.

    Called from _end_turn. kind ∈ {"completed", "error"}; if the turn was
    interrupted, that takes precedence.
    """
    if not turn.user_msg_ids:
        return
    fid = forum()
    if not fid:
        return
    if turn.interrupted:
        emoji = _REACT_INTERRUPTED
    elif kind == "error":
        emoji = _REACT_ERROR
    else:
        emoji = _REACT_DONE
    for mid in turn.user_msg_ids:
        _react_user_msg(mid, fid, emoji)
    turn.user_msg_ids.clear()
    # Progress flags are per-turn; the turn object is reused across turns
    # in the same session, so reset them now or the next turn would skip
    # 🔥/⚡ entirely. last_tool_name resets too so the indicator falls back
    # to "typing" at the next turn's start.
    turn.streamed = False
    turn.tooled = False
    turn.last_tool_name = None


def _format_status(turn: TurnState) -> str:
    elapsed = int(time.time() - turn.started_at)
    mins, secs = divmod(elapsed, 60)
    ts = f"{mins}:{secs:02d}"
    n = len(turn.tool_ops)
    # Hourglass rotates each 3s tick so the timer feels alive even
    # when no tool ops have happened yet.
    glass = "⌛" if (elapsed // 3) % 2 else "⏳"
    if n == 0:
        return f"{glass} {ts}"
    last = turn.tool_ops[-1]
    if n > 1:
        return f"⚙️ <code>{tg.esc(last)}</code> ({n}) — {ts}"
    return f"⚙️ <code>{tg.esc(last)}</code> — {ts}"


def _interrupt_button(session):
    return [[{"text": "⏹ Interrupt", "callback_data": f"int:{session.sid}"}]]


def _update_status(session, turn: TurnState):
    fid = forum()
    if not fid or not session.topic_id:
        return
    text = _format_status(turn)
    if text == turn._last_status_text:
        return
    btn = _interrupt_button(session)
    if turn.status_msg_id:
        tg.edit(turn.status_msg_id, text, fid, buttons=btn)
    else:
        mid = tg.send(text, fid, thread_id=session.topic_id, buttons=btn)
        if mid:
            turn.status_msg_id = mid
    turn._last_status_text = text


def _finish_status(session, turn: TurnState):
    """Remove status message (timer thread keeps running)."""
    fid = forum()
    if not fid:
        return
    if turn.status_msg_id:
        tg.delete(turn.status_msg_id, fid)
        turn.status_msg_id = None


def _end_turn(turn: TurnState):
    """Stop timer thread and clean up status message.

    On a normal turn end the status message is deleted right away. On an
    interrupted turn it sticks as "⏹ Interrupted" for ~3s so the user sees
    the final state before it disappears.
    """
    turn.stop_event.set()
    if turn._timer_thread:
        turn._timer_thread.join(timeout=5)
        turn._timer_thread = None
    fid = forum()
    if fid and turn.status_msg_id:
        mid = turn.status_msg_id
        if turn.interrupted:
            try:
                tg.edit(mid, "⏹ Interrupted", fid)
            except Exception:
                pass

            def _del_later(mid=mid, fid=fid):
                time.sleep(3)
                tg.delete(mid, fid)
            threading.Thread(target=_del_later, daemon=True).start()
        else:
            tg.delete(mid, fid)
        turn.status_msg_id = None
    _react_for_turn(turn, "completed")


def _turn_timer(session, turn: TurnState):
    """Background thread: update status timer."""
    fid = forum()
    while not turn.stop_event.wait(3):
        mid = turn.status_msg_id
        if mid and fid:
            text = _format_status(turn)
            if text != turn._last_status_text:
                tg.edit(mid, text, fid, buttons=_interrupt_button(session))
                turn._last_status_text = text
        # Telegram hides the chat-action indicator after ~5s; refresh it
        # on every tick (3s) so the verb stays visible for the full turn.
        if fid and session.topic_id:
            tg.send_chat_action(fid,
                                action=_chat_action_for_tool(turn.last_tool_name),
                                thread_id=session.topic_id)


# ── compact / expand ────────────────────────────────────────────────

_MAX_SAVED_TURNS = 50


def _build_summary(texts: list[str], ops: list[str]) -> str:
    combined = "\n".join(t.strip() for t in texts if t.strip())
    combined = re.sub(r'<[^>]+>', '', combined)
    combined = re.sub(r'[*_~`#]', '', combined)
    combined = re.sub(r'\n{2,}', '\n', combined).strip()
    try:
        r = subprocess.run(
            [_CLAUDE_BIN, '-p',
             'Summarize in 1-2 sentences. Reply in the same language '
             f'as the original text. Be very brief:\n{combined[:2000]}',
             '--no-session-persistence', '--tools', ''],
            capture_output=True, text=True, timeout=15,
            cwd='/tmp',
        )
        summary = r.stdout.strip()
        if summary:
            parts = []
            if ops:
                parts.append(f"⚙️ {len(ops)} ops")
            parts.append(tg.esc(summary))
            return "\n".join(parts)
    except Exception:
        pass
    if len(combined) > 200:
        combined = combined[:197] + "…"
    parts = []
    if ops:
        parts.append(f"⚙️ {len(ops)} ops")
    parts.append(tg.esc(combined))
    return "\n".join(parts)


# ── session context for claude --append-system-prompt ────────────────

def _session_context(session) -> str:
    """Return the per-turn context appended to Claude's system prompt.

    Tells Claude it's running inside the ClaudeLaude Telegram bot, plus
    the topic/display-mode/mode it currently lives in and the bot
    commands the user can hit from Telegram.
    """
    display = "mobile"
    if session.topic_id:
        with state.lock:
            display = state.topic_display_mode.get(
                session.topic_id, DEFAULT_DISPLAY)
    lines = [
        "## ClaudeLaude bot session",
        "You are running inside ClaudeLaude — a Telegram bot that exposes "
        "Claude Code over Telegram Forum Topics. Each topic is one session; "
        "the owner reads your output in the Telegram client.",
        "",
        f"- topic_id: {session.topic_id}",
        f"- session_id (bot): {session.sid}",
        f"- cwd: {session.cwd}",
        f"- display: {display} (mobile = 35-char width, no tables, no <pre>)",
        f"- mode: {session.mode}",
        "",
        "Owner-side commands (sent from Telegram, not by you):",
        "/new /sessions /resume /history /stop /restart /interrupt "
        "/usage /display /mode /menu /help /update /stop_bot.",
        "",
        "Constraints:",
        "- Telegram messages cap at 4096 chars; the bot splits long output.",
        "- Markdown tables and <pre> blocks render badly in Telegram — "
        "prefer key:value lists.",
        "- Inline buttons truncate at ~25 chars on mobile.",
        "- Photos/files the owner sends arrive as attachment paths in "
        "your user message.",
    ]
    return "\n".join(lines)


# ── noise filter ─────────────────────────────────────────────────────

_NOISE_TEXTS = {
    "claude is waiting for your input",
}


# ── callbacks from SessionManager ────────────────────────────────────

def on_assistant(session, text):
    if not session.topic_id:
        return
    if text.strip().lower() in _NOISE_TEXTS:
        return
    turn = _get_turn(session)
    # Upgrade reaction to 🔥 on first real assistant text, but not over ⚡
    # if a tool was already used in this turn.
    if not turn.streamed and not turn.tooled:
        _react_progress(turn, _REACT_STREAMING)
        turn.streamed = True
    _finish_status(session, turn)
    with state.lock:
        mode = state.topic_display_mode.get(session.topic_id, DEFAULT_DISPLAY)
    if mode == "mobile":
        text = _md_table_to_list(text)
    fid = forum()
    if fid:
        ids = tg.send_long(text, fid, thread_id=session.topic_id, markdown=True)
        turn.msg_ids.extend(ids)
        turn.msg_texts.append(text)
        for mid in ids:
            _record_topic_msg(session.topic_id, mid)
        mid = tg.send(_format_status(turn), fid, thread_id=session.topic_id,
                      buttons=_interrupt_button(session))
        turn.status_msg_id = mid


def on_result(session, result_text, summary):
    if not session.topic_id:
        return
    with state.lock:
        turn = state.turns.pop(session.sid, TurnState())
    _end_turn(turn)
    fid = forum()
    if not fid:
        return

    # Send pending images. 2+ → bundle into a single sendMediaGroup album
    # so multi-chart output renders as one block instead of N separate
    # photos. Telegram caps an album at 10 items; the rest spill over as
    # individual sendPhoto calls.
    imgs = [p for p in session.pending_images if os.path.isfile(p)]
    if len(imgs) >= 2:
        tg.send_media_group(fid, imgs[:10], thread_id=session.topic_id)
        for extra in imgs[10:]:
            tg.send_photo(fid, extra, thread_id=session.topic_id)
    elif imgs:
        tg.send_photo(fid, imgs[0], thread_id=session.topic_id)
    session.pending_images.clear()

    if turn.msg_ids:
        with state.lock:
            if len(state.saved_turns) >= _MAX_SAVED_TURNS:
                oldest = next(iter(state.saved_turns))
                state.saved_turns.pop(oldest)
            compact_id = str(time.time_ns())[-10:]
            state.saved_turns[compact_id] = (
                turn.msg_ids[:], turn.msg_texts[:], turn.tool_ops[:])
        btn = [[{"text": "\U0001f5dc Compact", "callback_data": f"c:{compact_id}"}]]
        last_mid = turn.msg_ids[-1]
        try:
            tg._req("editMessageReplyMarkup", {
                "chat_id": fid,
                "message_id": last_mid,
                "reply_markup": {"inline_keyboard": btn},
            })
        except Exception:
            pass

    if not session.alive:
        stop_label = session.name
        tg.edit_forum_topic(fid, session.topic_id, stop_label,
                            icon_custom_emoji_id=_ICON_STOPPED)
        with state.lock:
            state.topic_labels[session.topic_id] = stop_label
        session.topic_label = stop_label
    elif session.topic_id not in state.renamed_topics and result_text.strip():
        with state.lock:
            state.renamed_topics.add(session.topic_id)
        _auto_rename_topic(session, result_text, fid)


def _auto_rename_topic(session, result_text, fid):
    user_msg = None
    for h in session.history:
        if h.kind == "user":
            user_msg = h.text.strip()
            break
    if not user_msg:
        return

    def _clean_title(raw):
        if not raw:
            return None
        t = raw.strip().strip('"\'')
        t = re.sub(r'\d{4}[-/]\d{2}[-/]\d{2}', '', t).strip(' -–')
        t = re.sub(r'[-_]{2,}', ' ', t)
        if re.fullmatch(r'[\w]+([-_][\w]+){2,}', t):
            t = t.replace('-', ' ').replace('_', ' ')
        t = re.sub(r'\s+', ' ', t).strip()
        return t if t and len(t) <= 30 else None

    def _do_rename():
        context = f'User message: {user_msg[:300]}'
        if result_text:
            context += f'\nAssistant response (start): {result_text[:200]}'
        try:
            r = subprocess.run(
                [_CLAUDE_BIN, '-p',
                 'Reply with ONLY a short 2-4 word human-readable topic title '
                 'that captures the INTENT of the conversation. '
                 'Use natural words separated by spaces. '
                 'No dashes, no dates, no file paths, no quotes, no explanation. '
                 'Examples: "Library API Setup", "Fix Auth Bug", "Timesheet Review". '
                 f'\n{context}',
                 '--no-session-persistence', '--tools', ''],
                capture_output=True, text=True, timeout=20,
                cwd='/tmp',
            )
            title = _clean_title(r.stdout)
        except Exception:
            title = None
        if not title:
            title = _clean_title(result_text[:60] if result_text else None)
        if not title:
            text = re.sub(r'\d{4}[-/]\d{2}[-/]\d{2}', '', user_msg)
            text = re.sub(r'[/\-_]+', ' ', text)
            words = [w for w in text.split() if len(w) > 1][:3]
            title = ' '.join(words).strip()
            if len(title) > 20:
                title = title[:17] + "…"
        if title:
            label = title
            tg.edit_forum_topic(fid, session.topic_id, label[:128])
            with state.lock:
                state.topic_labels[session.topic_id] = label[:128]
            session.topic_label = label[:128]
            session.name = title
            mgr._persist()

    threading.Thread(target=_do_rename, daemon=True).start()


def _send_fork_summary(parent, topic_id):
    """Background: summarize parent session history and send to fork topic."""
    history_lines = []
    for h in parent.history:
        if h.kind in ("user", "assistant", "result"):
            text = h.text[:200] if len(h.text) > 200 else h.text
            history_lines.append(f"{h.kind}: {text}")
    if not history_lines:
        return
    digest = '\n'.join(history_lines[-30:])

    def _do():
        try:
            r = subprocess.run(
                [_CLAUDE_BIN, '-p',
                 f'Summarize this conversation in 3-5 bullet points. '
                 f'Reply in the same language as the conversation:\n{digest[:2000]}',
                 '--no-session-persistence', '--tools', ''],
                capture_output=True, text=True, timeout=20,
                cwd='/tmp',
            )
            summary = r.stdout.strip()
        except Exception:
            summary = None
        if summary:
            send_to_topic(topic_id,
                          f"📋 <b>Parent session summary:</b>\n{tg.esc(summary)}")

    threading.Thread(target=_do, daemon=True).start()


def on_tool_use(session, tool, inp):
    if not session.topic_id:
        return
    audit.log("tool_use", f"{tool}: {json.dumps(inp, ensure_ascii=False)}"
              if isinstance(inp, dict) else f"{tool}: {inp}",
              sid=session.sid)
    turn = _get_turn(session)
    turn.last_tool_name = tool
    # Push a fresh contextual chat-action so the indicator under the chat
    # title matches what Claude is doing right now.
    fid = forum()
    if fid:
        tg.send_chat_action(fid, action=_chat_action_for_tool(tool),
                            thread_id=session.topic_id)
    # Upgrade reaction to ⚡ on first tool use; ⚡ overrides 🔥.
    if not turn.tooled:
        _react_progress(turn, _REACT_TOOL)
        turn.tooled = True
    if not _is_noisy_tool(tool, inp):
        compact = _compact_tool_msg(tool, inp)
        turn.tool_ops.append(compact)
        _update_status(session, turn)


def on_thinking(session):
    if not session.topic_id:
        return
    fid = forum()
    if not fid:
        return
    turn = _get_turn(session)
    if turn._timer_thread is None:
        mid = tg.send("⏳ 0:00", fid, thread_id=session.topic_id,
                      buttons=_interrupt_button(session))
        turn.status_msg_id = mid
        tg.send_chat_action(fid, thread_id=session.topic_id)
        t = threading.Thread(
            target=_turn_timer, args=(session, turn), daemon=True)
        turn._timer_thread = t
        t.start()


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


# ── callbacks from HookBridge ────────────────────────────────────────

def on_hook_notification(text, claude_session_id, data=None):
    try:
        session = (mgr.by_claude_session_id(claude_session_id)
                   if claude_session_id else None)
        if not (session and session.topic_id):
            _resolve_hook_session(claude_session_id, data or {})
            session = (mgr.by_claude_session_id(claude_session_id)
                       if claude_session_id else None)

        if session and session.topic_id:
            mid = send_to_topic(session.topic_id,
                                f"\U0001f514 {tg.esc(text)}")
            if mid and claude_session_id and not session.is_bot_spawned:
                _track_terminal_msg(claude_session_id, mid,
                                    forum(), "notification")
        else:
            tg.send(f"\U0001f514 {tg.esc(text)}", OWNER_ID)
    except Exception as e:
        print(f"hook notification error: {e}", file=sys.stderr, flush=True)


def _resolve_hook_session(claude_session_id, data):
    _log = lambda m: print(f"[resolve] {m}", file=sys.stderr, flush=True)
    _log(f"keys={list(data.keys())} sid={claude_session_id!r}")

    if claude_session_id:
        session = mgr.by_claude_session_id(claude_session_id)
        if session and session.topic_id:
            _log(f"found by claude_id → topic {session.topic_id}")
            return session

    cwd = data.get("cwd", "")
    if cwd:
        existing = mgr.by_cwd(cwd)
        if existing and existing.topic_id and not existing.is_bot_spawned:
            _log(f"found by cwd → topic {existing.topic_id}")
            if claude_session_id:
                mgr.link_claude_id(claude_session_id, existing)
            return existing
        if existing and existing.is_bot_spawned:
            _log(f"skipping bot-spawned session for cwd={cwd}")

    # DoS guard: with neither a session_id nor a cwd we have nothing to
    # anchor a topic to — refuse rather than spawning a "terminal — HH:MM"
    # topic for every malformed POST that slipped past hooks.py.
    if not claude_session_id and not cwd:
        _log("refuse: no session_id and no cwd, not creating topic")
        return None

    fid = forum()
    if not fid:
        _log("no forum chat configured")
        return None

    dirname = os.path.basename(cwd) if cwd else "terminal"
    with state.lock:
        state.topic_counter[dirname] = state.topic_counter.get(dirname, 0) + 1
        n = state.topic_counter[dirname]
    ts = time.strftime("%H:%M")
    label = (f"{dirname} #{n} — {ts}" if n > 1
             else f"{dirname} — {ts}")
    try:
        topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0,
                                          icon_custom_emoji_id=_ICON_TERMINAL)
        _log(f"created topic {topic_id}")
    except Exception as e:
        _log(f"create_forum_topic FAILED: {e}")
        return None
    if not topic_id:
        _log("create_forum_topic returned None")
        return None
    with state.lock:
        state.topic_labels[topic_id] = label
    if claude_session_id:
        s = mgr.register_terminal(claude_session_id, topic_id, cwd=cwd)
        s.topic_label = label
        mgr._persist()
        return s
    _log(f"registered topic {topic_id} (no claude session_id)")
    sid = uuid.uuid4().hex[:8]
    session = Session(
        sid=sid, topic_id=topic_id, cwd=cwd,
        name=dirname, is_bot_spawned=False,
        topic_label=label,
    )
    mgr._sessions[sid] = session
    mgr._topic_map[topic_id] = sid
    if cwd:
        mgr._cwd_map[cwd] = sid
    mgr._persist()
    return session


def _invalidate_session(session):
    """Remove a session with a stale/deleted topic."""
    sid = session.sid
    if session.topic_id and mgr._topic_map.get(session.topic_id) == sid:
        del mgr._topic_map[session.topic_id]
    if session.cwd and mgr._cwd_map.get(session.cwd) == sid:
        del mgr._cwd_map[session.cwd]
    if session.claude_session_id and mgr._claude_id_map.get(session.claude_session_id) == sid:
        del mgr._claude_id_map[session.claude_session_id]
    mgr._sessions.pop(sid, None)
    mgr._persist()


_watcher_offsets: dict[str, int] = {}


def _track_terminal_msg(claude_session_id: str, msg_id: int,
                        chat_id: int, kind: str):
    if claude_session_id not in _watcher_offsets:
        path = _session_jsonl_path(claude_session_id)
        try:
            _watcher_offsets[claude_session_id] = (
                os.path.getsize(path) if path else 0)
        except OSError:
            _watcher_offsets[claude_session_id] = 0
        print(f"[watcher] track csid={claude_session_id[:8]}… "
              f"offset={_watcher_offsets[claude_session_id]} "
              f"kind={kind} msg={msg_id}",
              file=sys.stderr, flush=True)
    with state.lock:
        state.pending_terminal_msgs.setdefault(
            claude_session_id, []).append((msg_id, chat_id, kind))


def _cancel_session_perms(sid: str, reason: str):
    """Cancel pending permission requests tied to a session.

    Edits the Allow/Deny message to "✗ Cancelled — {reason}", schedules
    deletion, and unblocks the hook handler with deny so it can return.
    """
    with state.lock:
        victims = [
            (short_id, msg_id, chat_id)
            for short_id, (msg_id, chat_id, p_sid)
            in list(state.pending_permissions.items())
            if p_sid == sid
        ]
        for short_id, _, _ in victims:
            state.pending_permissions.pop(short_id, None)
    for short_id, msg_id, chat_id in victims:
        full_id = None
        with state.lock:
            full_id = state.perm_key_map.pop(short_id, None)
        try:
            tg.edit(msg_id, f"✗ Cancelled — {tg.esc(reason)}", chat_id)
        except Exception as e:
            print(f"[cancel_perm] edit failed: {e}",
                  file=sys.stderr, flush=True)
        def _delete_later(mid=msg_id, cid=chat_id):
            time.sleep(5)
            tg.delete(mid, cid)
        threading.Thread(target=_delete_later, daemon=True).start()
        if full_id:
            bridge.abandon_permission(full_id)


def _invalidate_and_stop(session, reason: str):
    """Topic gone → clean up turn, stop session, drop maps.

    Order matters: mgr.stop() fires the on_session_stop callback (which
    cancels pending perms) only while the session is still in the
    manager's tables.  _invalidate_session() removes the entry, so it has
    to come last.
    """
    sid = session.sid
    print(f"[lifecycle] invalidate_and_stop sid={sid} reason={reason}",
          file=sys.stderr, flush=True)
    with state.lock:
        turn = state.turns.pop(sid, None)
    if turn:
        _end_turn(turn)
    mgr.stop(sid, reason=reason)
    _invalidate_session(session)


def _valid_topic_id(session) -> int | None:
    """Return session's topic_id only if it looks like a real session topic.

    Filters out None, 0, and General (id 1) so a permission request can
    never land in the General topic.
    """
    if not session:
        return None
    tid = session.topic_id
    if not tid or tid == 1:
        return None
    return tid


def on_hook_permission(req_id, data):
    try:
        claude_session_id = (data.get("session_id")
                             or data.get("sessionId") or "")
        # If the terminal claude that fired this hook is already
        # mirrored, route the permission Allow/Deny into the SAME
        # mirror topic instead of spawning a separate "terminal — HH:MM"
        # topic. Keeps the conversation + tool prompts in one place.
        mirror = mirror_mgr.by_csid(claude_session_id) if claude_session_id else None
        session = None if mirror else _resolve_hook_session(
            claude_session_id, data)

        tool = data.get("tool_name", "?")
        ti = data.get("tool_input", {})

        if tool == "Bash":
            cmd = ti.get("command", "?")
            if len(cmd) > 500:
                cmd = cmd[:500] + "..."
            detail = tg.esc(cmd)
        elif tool in ("Write", "Edit", "Read"):
            detail = tg.esc(ti.get("file_path", "?"))
        else:
            raw = json.dumps(ti, ensure_ascii=False)
            if len(raw) > 400:
                raw = raw[:400] + "..."
            detail = tg.esc(raw)

        short_id = req_id[-12:]
        body = f"<b>{tg.esc(tool)}</b>\n<code>{detail}</code>"
        buttons = [[
            {"text": "✅ Allow", "callback_data": f"p:{short_id}:a"},
            {"text": "❌ Deny", "callback_data": f"p:{short_id}:d"},
        ]]

        if mirror:
            topic_id = mirror.topic_id
            chat_id = forum()
            msg_id = tg.send(body, chat_id, thread_id=topic_id,
                             buttons=buttons) if chat_id else None
            # Track the in-flight permission so its tool_use can be
            # filtered from mirror projection (avoids duplicate signal
            # of "permission asked + tool ran" both projected).
            mirror.pending_perm_tool = (tool, _normalize_tool_input(tool, ti))
        else:
            topic_id = _valid_topic_id(session)
            msg_id = None
            chat_id = None
            if topic_id:
                chat_id = forum()
                msg_id = tg.send(body, chat_id, thread_id=topic_id,
                                 buttons=buttons)

            if msg_id is None and session and topic_id:
                print(f"[resolve] stale topic {topic_id}, recreating",
                      file=sys.stderr, flush=True)
                _invalidate_and_stop(session, "topic gone")
                session = _resolve_hook_session(claude_session_id, data)
                topic_id = _valid_topic_id(session)
                if topic_id:
                    chat_id = forum()
                    msg_id = tg.send(body, chat_id, thread_id=topic_id,
                                     buttons=buttons)

        if msg_id is None:
            # No valid topic anywhere — fall back to OWNER DM so the user
            # can still decide.  Never leak into General.
            cwd = data.get("cwd", "?")
            print(f"[perm] no topic, falling back to DM — tool={tool} "
                  f"cwd={cwd} sid={claude_session_id}",
                  file=sys.stderr, flush=True)
            chat_id = OWNER_ID
            topic_id = None
            msg_id = tg.send(
                f"⚠️ <i>no topic</i>\n"
                f"<b>{tg.esc(tool)}</b>\n<code>{detail}</code>\n"
                f"cwd: <code>{tg.esc(cwd)}</code>",
                OWNER_ID, buttons=buttons,
            )
            if msg_id is None:
                # DM also failed — last resort: abandon, claude decides.
                print("[perm] DM fallback failed; abandoning",
                      file=sys.stderr, flush=True)
                bridge.abandon_permission(req_id)
                return

        sid = session.sid if session else None
        with state.lock:
            state.perm_key_map[short_id] = req_id
            state.pending_permissions[short_id] = (msg_id, chat_id, sid)

        if (session and not session.is_bot_spawned
                and claude_session_id and msg_id and chat_id):
            _track_terminal_msg(claude_session_id, msg_id, chat_id,
                                f"perm:{short_id}")

        bridge.set_perm_context(req_id, chat_id=chat_id, topic_id=topic_id)

    except Exception as e:
        print(f"hook permission error: {e}", file=sys.stderr, flush=True)


# ── command handlers ─────────────────────────────────────────────────

def cmd_setup(chat_id):
    try:
        r = tg._req("getChat", {"chat_id": chat_id})
        is_forum = r.get("result", {}).get("is_forum", False)
    except Exception:
        is_forum = False
    if not is_forum:
        tg.send("❌ Enable Topics in this group first.", chat_id)
        return
    set_forum_chat_id(chat_id)
    tg.send("✅ Forum linked. Use /new to start a session.", chat_id)


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


def _spawn_session(cwd, name=None):
    fid = forum()
    if not fid:
        send_general("❌ Run /setup in a forum group first.")
        return
    if not name:
        name = os.path.basename(cwd.rstrip("/"))
    with state.lock:
        state.topic_counter[name] = state.topic_counter.get(name, 0) + 1
        n = state.topic_counter[name]
    ts = time.strftime("%H:%M")
    label = (f"{name} #{n} — {ts}" if n > 1
             else f"{name} — {ts}")
    try:
        topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0)
    except Exception as e:
        _ephemeral(fid, f"❌ Failed to create topic: {tg.esc(str(e))}", seconds=7)
        return
    if not topic_id:
        _ephemeral(fid, "❌ Failed to create topic. Check bot admin rights.", seconds=7)
        return
    with state.lock:
        state.topic_labels[topic_id] = label
    s = mgr.create(cwd=cwd, name=name, topic_id=topic_id)
    s.topic_label = label
    mgr._persist()
    audit.log("session_start", cwd, sid=s.sid)
    send_to_topic(topic_id,
                  f"▶️ <code>{tg.esc(cwd)}</code>")
    url = _topic_url(topic_id)
    if url:
        _ephemeral(fid, f"▶ {name}",
                   buttons=[[{"text": "Open", "url": url}]],
                   seconds=5)


def cmd_new(args: str, chat_id=None, thread_id=None):
    fid = forum()
    if not fid:
        send_general("❌ Run /setup in a forum group first.")
        return
    parts = args.strip().split(None, 1)
    if parts:
        cwd = parts[0]
        name = parts[1] if len(parts) > 1 else None
        if not os.path.isdir(cwd):
            if not thread_id and chat_id:
                _ephemeral(chat_id, f"❌ Not a directory: <code>{tg.esc(cwd)}</code>", seconds=7)
            else:
                _reply(chat_id, thread_id,
                       f"❌ Not a directory: <code>{tg.esc(cwd)}</code>")
            return
        _spawn_session(cwd, name)
        return
    projects = _discover_projects()
    if not projects:
        if not thread_id and chat_id:
            _ephemeral(chat_id, "❌ No projects found. Use: /new /path/to/project", seconds=7)
        else:
            _reply(chat_id, thread_id,
                   "❌ No projects found. Use: /new /path/to/project")
        return
    pick_id = str(time.time_ns())[-10:]
    with state.lock:
        state.pending_project_picks[pick_id] = projects
    RECENT_LIMIT = 4
    rows = []
    for i, p in enumerate(projects[:RECENT_LIMIT]):
        label = os.path.basename(p.rstrip("/"))
        rows.append([{"text": label, "callback_data": f"n:{pick_id}:{i}"}])
    if len(projects) > RECENT_LIMIT:
        rows.append([{"text": f"\U0001f4cb Show all ({len(projects)})",
                       "callback_data": f"na:{pick_id}"}])
    rows.append(_CLOSE_ROW)
    mid = _reply(chat_id, thread_id, "\U0001f4c2 Choose project:", buttons=rows)
    if not thread_id and mid and chat_id:
        def _cleanup_picker():
            time.sleep(_PICKER_TTL)
            with state.lock:
                state.pending_project_picks.pop(pick_id, None)
            tg.delete(mid, chat_id)
        threading.Thread(target=_cleanup_picker, daemon=True).start()


def _topic_url(topic_id):
    fid = forum()
    if not fid:
        return None
    short_id = str(fid).replace("-100", "")
    return f"https://t.me/c/{short_id}/{topic_id}"


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


def _short_cwd(cwd: str, limit: int = 48) -> str:
    if not cwd or len(cwd) <= limit:
        return cwd or "?"
    return "…" + cwd[-(limit - 1):]


def cmd_sessions(chat_id, thread_id=None):
    all_sessions = [s for s in mgr._sessions.values()
                    if s.topic_id]
    all_sessions.sort(key=lambda s: (not s.alive, -_session_last_active(s)))
    if not all_sessions:
        if not thread_id:
            _ephemeral(chat_id, "No sessions.", seconds=5)
        else:
            tg.send("No sessions.", chat_id, thread_id=thread_id)
        return
    blocks = []
    buttons = []
    now = time.time()
    for i, s in enumerate(all_sessions):
        age = int(now - _session_last_active(s))
        if age < 60:
            age_str = "just now"
        elif age < 3600:
            age_str = f"{age // 60}m ago"
        elif age < 86400:
            age_str = f"{age // 3600}h ago"
        else:
            age_str = f"{age // 86400}d ago"
        icon = "▶" if s.alive else "·"
        num = i + 1
        name = s.name if len(s.name) <= 25 else s.name[:22] + "…"
        blocks.append(
            f"<b>{num}.</b> {icon} {tg.esc(name)} · <i>{age_str}</i>\n"
            f"    <code>{tg.esc(_short_cwd(s.cwd))}</code>"
        )
        btn_label = f"{num}. {name}"
        if len(btn_label) > 25:
            btn_label = btn_label[:22] + "…"
        if s.claude_session_id:
            buttons.append({"text": btn_label,
                            "callback_data": f"fork:{s.sid}"})
        else:
            url = _topic_url(s.topic_id) if s.topic_id else None
            if url:
                buttons.append({"text": btn_label, "url": url})
            else:
                buttons.append({"text": btn_label,
                                "callback_data": "noop"})
    rows = [buttons[j:j+3] for j in range(0, len(buttons), 3)]
    rows.append(_CLOSE_ROW)
    text = "\U0001f4cb <b>Sessions</b>\n\n" + "\n\n".join(blocks)
    if not thread_id:
        mid = tg.send(text, chat_id, buttons=rows)
        if mid:
            def _cleanup():
                time.sleep(_PICKER_TTL)
                tg.delete(mid, chat_id)
            threading.Thread(target=_cleanup, daemon=True).start()
    else:
        tg.send(text, chat_id, thread_id=thread_id, buttons=rows)


_MD_INLINE = re.compile(r"[*_`]")


def _strip_md(s: str) -> str:
    """Strip leading markdown markers and inline emphasis chars from a preview."""
    s = re.sub(r"^[#>\-*\s]+", "", s)
    s = _MD_INLINE.sub("", s)
    return " ".join(s.split())


_RESUME_RECENT_SECONDS = 5 * 60


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


def _discover_resumable_sessions(limit=10):
    bot_sids = (mgr._known_bot_sids |
                {s.claude_session_id for s in mgr._sessions.values()
                 if s.claude_session_id})
    live_terminal_sids = _live_claude_session_ids() - bot_sids
    recent_cutoff = time.time() - _RESUME_RECENT_SECONDS
    raw = []
    try:
        for d in os.listdir(CLAUDE_PROJECTS_DIR):
            proj_dir = os.path.join(CLAUDE_PROJECTS_DIR, d)
            if not os.path.isdir(proj_dir):
                continue
            for f in os.listdir(proj_dir):
                if not f.endswith(".jsonl") or len(f) != 42:
                    continue
                sid = f[:-6]
                if sid in bot_sids:
                    continue
                path = os.path.join(proj_dir, f)
                mtime = os.path.getmtime(path)
                if sid not in live_terminal_sids and mtime < recent_cutoff:
                    continue
                cwd = None
                first_msg = None
                title = None
                try:
                    with open(path) as fh:
                        for i, line in enumerate(fh):
                            if i > 50:
                                break
                            obj = json.loads(line)
                            if not cwd:
                                cwd = obj.get("cwd")
                            t = obj.get("type")
                            if t == "ai-title" and not title:
                                title = (obj.get("aiTitle") or "").strip()
                            elif t == "user" and not first_msg:
                                msg = obj.get("message", {})
                                if isinstance(msg, dict):
                                    c = msg.get("content", "")
                                    if isinstance(c, list):
                                        c = "".join(
                                            b.get("text", "") for b in c
                                            if isinstance(b, dict)
                                            and b.get("type") == "text"
                                        )
                                    if isinstance(c, str) and c.strip():
                                        first_msg = _strip_md(c)[:80]
                            if title and first_msg and cwd:
                                break
                except Exception:
                    continue
                desc = title or first_msg
                if cwd and os.path.isdir(cwd):
                    raw.append((sid, cwd, desc, mtime))
    except Exception:
        pass
    # Dedup by (cwd, desc) — keep newest mtime per group
    seen: dict[tuple, tuple] = {}
    for sid, cwd, desc, mtime in raw:
        key = (cwd, desc or "")
        if key not in seen or mtime > seen[key][3]:
            seen[key] = (sid, cwd, desc, mtime)
    found = list(seen.values())
    found.sort(key=lambda x: x[3], reverse=True)
    return found[:limit]


def _do_resume(claude_session_id: str, chat_id, thread_id=None):
    fid = forum()
    if not fid:
        return
    existing = mgr.by_claude_session_id(claude_session_id)
    if existing and existing.alive and existing.is_bot_spawned:
        url = _topic_url(existing.topic_id) if existing.topic_id else None
        if url:
            _reply(chat_id, thread_id,
                   f"ℹ️ Already active: <b>{tg.esc(existing.name)}</b>",
                   buttons=[[{"text": "Open", "url": url}]])
        else:
            _reply(chat_id, thread_id,
                   f"ℹ️ Already active: <b>{tg.esc(existing.name)}</b>")
        return
    if existing:
        mgr.detach_terminal(existing.sid)
    cwd = _resolve_session_cwd(claude_session_id)
    if not cwd or not os.path.isdir(cwd):
        _reply(chat_id, thread_id,
               f"❌ Can't find cwd for session <code>{tg.esc(claude_session_id[:12])}…</code>")
        return
    name = os.path.basename(cwd.rstrip("/"))
    with state.lock:
        state.topic_counter[name] = state.topic_counter.get(name, 0) + 1
        n = state.topic_counter[name]
    ts = time.strftime("%H:%M")
    label = (f"{name} #{n} — {ts}" if n > 1
             else f"{name} — {ts}")
    try:
        topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0)
    except Exception as e:
        _ephemeral(fid, f"❌ Failed to create topic: {tg.esc(str(e))}", seconds=7)
        return
    if not topic_id:
        _ephemeral(fid, "❌ Failed to create topic.", seconds=7)
        return
    with state.lock:
        state.topic_labels[topic_id] = label
    s = mgr.resume(claude_session_id, topic_id, name, cwd)
    s.topic_label = label
    send_to_topic(topic_id,
                  f"▶️ <code>{tg.esc(cwd)}</code>")
    url = _topic_url(topic_id)
    if url:
        _ephemeral(fid, f"▶ {name}",
                   buttons=[[{"text": "Open", "url": url}]],
                   seconds=5)


_RESUME_RECENT_LIMIT = 4


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds/60)}m ago"
    if seconds < 86400:
        return f"{int(seconds/3600)}h ago"
    return f"{int(seconds/86400)}d ago"


def _build_resume_picker(sessions, pick_id, max_items=None):
    show = sessions if max_items is None else sessions[:max_items]
    blocks = []
    rows = []
    now = time.time()
    for i, (_sid, cwd, desc, mtime) in enumerate(show):
        proj = os.path.basename(cwd.rstrip("/"))
        age_str = _format_age(now - mtime)
        hint = ""
        if desc:
            hint = desc[:50].rstrip()
            if len(desc) > 50:
                hint += "…"
        num = i + 1
        head = f"<b>{num}.</b> {tg.esc(proj)} · <i>{age_str}</i>"
        blocks.append(f"{head}\n    <i>{tg.esc(hint)}</i>" if hint else head)
        btn_label = f"{num}. {proj}"
        if len(btn_label) > 25:
            btn_label = btn_label[:22] + "…"
        rows.append([{"text": btn_label,
                      "callback_data": f"r:{pick_id}:{i}"}])
    if max_items is not None and len(sessions) > max_items:
        rows.append([{"text": f"\U0001f4cb Show all ({len(sessions)})",
                      "callback_data": f"ra:{pick_id}"}])
    rows.append(_CLOSE_ROW)
    text = "▶️ <b>Resume</b>\n\n" + "\n\n".join(blocks)
    return text, rows


def cmd_resume(args: str, chat_id, thread_id=None):
    fid = forum()
    if not fid:
        send_general("❌ Run /setup in a forum group first.")
        return
    claude_session_id = args.strip()
    if claude_session_id:
        _do_resume(claude_session_id, chat_id, thread_id)
        return
    sessions = _discover_resumable_sessions()
    if not sessions:
        _reply(chat_id, thread_id, "No resumable sessions found.")
        return
    pick_id = str(time.time_ns())[-10:]
    with state.lock:
        state.pending_resume_picks[pick_id] = sessions
    text, rows = _build_resume_picker(sessions, pick_id,
                                      max_items=_RESUME_RECENT_LIMIT)
    mid = _reply(chat_id, thread_id, text, buttons=rows)
    if not thread_id and mid and chat_id:
        def _cleanup():
            time.sleep(_PICKER_TTL)
            with state.lock:
                state.pending_resume_picks.pop(pick_id, None)
            tg.delete(mid, chat_id)
        threading.Thread(target=_cleanup, daemon=True).start()


def cmd_history(session, chat_id, thread_id, args):
    if not session:
        if not thread_id:
            _ephemeral(chat_id, "Use in a session topic", seconds=5)
        else:
            tg.send("Use in a session topic", chat_id, thread_id=thread_id)
        return
    n = 30
    if args.strip().isdigit():
        n = int(args.strip())
    entries = session.history[-n:]
    if not entries:
        tg.send("Empty history", chat_id, thread_id=thread_id)
        return
    lines = []
    for e in entries:
        t = time.strftime("%H:%M:%S", time.localtime(e.ts))
        icon = {"user": "\U0001f464", "assistant": "\U0001f916",
                "tool": "\U0001f527", "system": "⚙️",
                "result": "✅"}.get(e.kind, "·")
        text = e.text[:200] if len(e.text) > 200 else e.text
        lines.append(f"<code>{t}</code> {icon} {tg.esc(text)}")
    tg.send("\n".join(lines), chat_id, thread_id=thread_id)


def cmd_mode(session, chat_id, thread_id, args):
    if not session:
        if thread_id:
            tg.send("Use in a session topic", chat_id, thread_id=thread_id)
        else:
            fid = forum()
            if fid:
                _ephemeral(fid, "Use in a session topic", seconds=5)
        return
    name = args.strip().lower()
    if not name:
        lines = [f"<b>Current mode:</b> {tg.esc(session.mode)}", "", "Modes:"]
        for key, preset in MODE_PRESETS.items():
            marker = "•" if key == session.mode else " "
            lines.append(f"{marker} <code>{key}</code> — {tg.esc(preset['label'])}")
        lines.append("")
        lines.append("Set with <code>/mode &lt;name&gt;</code>.")
        tg.send("\n".join(lines), chat_id, thread_id=thread_id)
        return
    if not mgr.set_mode(session.sid, name):
        valid = ", ".join(MODE_PRESETS.keys())
        tg.send(f"Unknown mode: <code>{tg.esc(name)}</code>\nAvailable: {valid}",
                chat_id, thread_id=thread_id)
        return
    preset = MODE_PRESETS[name]
    tg.send(f"\U0001f3af Mode: <b>{tg.esc(name)}</b> — {tg.esc(preset['label'])}",
            chat_id, thread_id=thread_id)


def cmd_display(chat_id, thread_id, args):
    if not thread_id:
        fid = forum()
        if fid:
            _ephemeral(fid, "Use in a session topic", seconds=5)
        return
    mode = args.strip().lower()
    if mode not in ("mobile", "desktop"):
        with state.lock:
            current = state.topic_display_mode.get(thread_id, DEFAULT_DISPLAY)
        mode = "desktop" if current == "mobile" else "mobile"
    with state.lock:
        state.topic_display_mode[thread_id] = mode
    icon = "\U0001f4f1" if mode == "mobile" else "\U0001f5a5"
    tg.send(f"{icon} Display: <b>{mode}</b>", chat_id, thread_id=thread_id)


def cmd_stop(session, chat_id, thread_id):
    if not session:
        if not thread_id:
            _ephemeral(chat_id, "Use in a session topic", seconds=5)
        else:
            tg.send("Use in a session topic", chat_id, thread_id=thread_id)
        return
    with state.lock:
        turn = state.turns.pop(session.sid, None)
    if turn:
        _end_turn(turn)
    mgr.stop(session.sid)
    audit.log("session_stop", "user stop", sid=session.sid)
    fid = forum()
    if fid and session.topic_id:
        stop_label = session.name
        tg.edit_forum_topic(fid, session.topic_id, stop_label,
                            icon_custom_emoji_id=_ICON_STOPPED)
        with state.lock:
            state.topic_labels[session.topic_id] = stop_label
        session.topic_label = stop_label
        tg.close_forum_topic(fid, session.topic_id)
    tg.send("⏹ Stopped", chat_id, thread_id=thread_id)


def cmd_interrupt(session, chat_id, thread_id):
    if not session:
        if not thread_id:
            _ephemeral(chat_id, "Use in a session topic", seconds=5)
        else:
            tg.send("Use in a session topic", chat_id, thread_id=thread_id)
        return
    if not session.is_bot_spawned:
        tg.send("ℹ️ Terminal — Ctrl-C in terminal",
                chat_id, thread_id=thread_id)
        return
    _do_interrupt(session, chat_id, thread_id)


def _do_interrupt(session, chat_id, thread_id):
    if not mgr.interrupt(session.sid):
        _ephemeral(chat_id, "Nothing to interrupt.",
                   thread_id=thread_id, seconds=5)
        return
    edited = False
    with state.lock:
        turn = state.turns.get(session.sid)
    if turn:
        turn.interrupted = True
        fid = forum()
        if fid and turn.status_msg_id:
            try:
                tg.edit(turn.status_msg_id, "⏹ Interrupted", fid)
                turn._last_status_text = "⏹ Interrupted"
                edited = True
            except Exception as e:
                print(f"[interrupt] status edit failed: {e}",
                      file=sys.stderr, flush=True)
    _cancel_session_perms(session.sid, "interrupted")
    if not edited:
        # No live status to repaint (or edit failed) — fall back to ephemeral.
        _ephemeral(chat_id, "⏹ Turn interrupted.",
                   thread_id=thread_id, seconds=5)


def cmd_restart(chat_id, thread_id):
    if not thread_id:
        fid = forum()
        if fid:
            _ephemeral(fid, "Use in a session topic", seconds=5)
        return
    session = mgr.by_topic(thread_id)
    if not session:
        tg.send("No session here", chat_id, thread_id=thread_id)
        return
    if session.alive:
        tg.send("Already running", chat_id, thread_id=thread_id)
        return
    if mgr.restart(session.sid):
        fid = forum()
        if fid:
            tg.reopen_forum_topic(fid, session.topic_id)
            tg.edit_forum_topic(fid, session.topic_id, session.name,
                                icon_custom_emoji_id=_ICON_ACTIVE)
            with state.lock:
                state.topic_labels[session.topic_id] = session.name
            session.topic_label = session.name
        tg.send("▶️ Restarted", chat_id,
                thread_id=thread_id)
    else:
        tg.send("❌ Failed to restart", chat_id, thread_id=thread_id)


def _fetch_account_usage() -> str | None:
    """Run interactive claude /usage via PTY, parse account limits."""
    import pty as _pty
    import fcntl
    import struct
    import termios

    master, slave = _pty.openpty()
    winsize = struct.pack('HHHH', 50, 120, 0, 0)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)
    try:
        proc = subprocess.Popen(
            [_CLAUDE_BIN],
            stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, preexec_fn=os.setsid,
        )
    except Exception:
        os.close(master)
        os.close(slave)
        return None
    os.close(slave)

    def read_until_idle(timeout=2):
        import select as _sel
        out = b''
        while _sel.select([master], [], [], timeout)[0]:
            try:
                out += os.read(master, 8192)
            except OSError:
                break
        return out

    try:
        read_until_idle(8)
        os.write(master, b'/usage\r')
        time.sleep(2)
        raw = read_until_idle(3)
        os.write(master, b'/exit\r')
        time.sleep(0.5)
    finally:
        proc.kill()
        proc.wait()
        os.close(master)

    text = raw.decode('utf-8', errors='replace')
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', ' ', text)
    text = re.sub(r'\x1b\][^\x07]*\x07', ' ', text)
    text = re.sub(r'\x1b[()][A-Za-z0-9]', ' ', text)
    text = re.sub(r'[\x00-\x08\x0e-\x1f\x7f]', ' ', text)
    text = re.sub(r' {2,}', ' ', text)

    blocks = []
    heading = None
    for line in text.split('\n'):
        s = line.strip()
        if not s:
            continue
        s = re.sub(r'[█▌▐░▏▎▍▋▊▉]+\s*', '', s).strip()
        if re.match(r'(?i)current (session|week)', s):
            heading = s
        elif re.search(r'\d+%\s*used', s) and heading:
            pct = re.search(r'(\d+)%\s*used', s).group(1)
            blocks.append(f"{heading}: {pct}%")
            heading = None
        elif re.match(r'(?i)resets?\s', s) and blocks:
            blocks[-1] += f"\n  {s}"
    return '\n'.join(blocks) if blocks else None


def cmd_usage(session, chat_id, thread_id):
    if session and (session.total_input_tokens or session.total_output_tokens):
        lines = ["<b>Session</b>"]
        total = session.total_input_tokens + session.total_output_tokens
        lines.append(f"Tokens: {total:,} (in {session.total_input_tokens:,} / out {session.total_output_tokens:,})")
        if session.total_cache_read:
            lines.append(f"Cache read: {session.total_cache_read:,}")
        if session.total_cost_usd > 0:
            lines.append(f"Cost: ${session.total_cost_usd:.4f}")
        text = "\n".join(lines)
        if not thread_id:
            _ephemeral(chat_id, text, seconds=7)
        else:
            tg.send(text, chat_id, thread_id=thread_id)

    mid = tg.send("⏳ Fetching account usage...", chat_id, thread_id=thread_id)

    in_general = not thread_id

    def _do_fetch():
        try:
            result = _fetch_account_usage()
            if result:
                text = f"<b>Account</b>\n{tg.esc(result)}"
                if in_general:
                    _ephemeral(chat_id, text, seconds=7)
                else:
                    tg.send(text, chat_id, thread_id=thread_id)
            else:
                if in_general:
                    _ephemeral(chat_id, "⚠️ Could not fetch account usage.", seconds=7)
                else:
                    tg.send("⚠️ Could not fetch account usage.", chat_id,
                            thread_id=thread_id)
        except Exception as e:
            if in_general:
                _ephemeral(chat_id, f"⚠️ Error: {tg.esc(str(e))}", seconds=7)
            else:
                tg.send(f"⚠️ Error: {tg.esc(str(e))}", chat_id,
                        thread_id=thread_id)
        finally:
            if mid:
                tg.delete(mid, chat_id)

    threading.Thread(target=_do_fetch, daemon=True).start()


def cmd_audit(args, chat_id, thread_id=None):
    n = 20
    if args.strip().isdigit():
        n = min(int(args.strip()), 100)
    entries = audit.tail(n)
    if not entries:
        _ephemeral(chat_id, "No audit events.", seconds=5,
                   thread_id=thread_id)
        return
    lines = []
    for e in entries:
        ts = e.get("ts", "?")[11:]  # HH:MM:SS
        ev = e.get("event", "?")
        detail = e.get("detail", "")
        if len(detail) > 60:
            detail = detail[:57] + "..."
        lines.append(f"<code>{ts}</code> <b>{tg.esc(ev)}</b> {tg.esc(detail)}")
    text = "\n".join(lines)
    _ephemeral(chat_id, text, seconds=30, thread_id=thread_id)


def cmd_test_perm(chat_id, thread_id=None):
    """Simulate a permission request to test the hook flow end-to-end."""
    def _do_test():
        import urllib.request
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo 'test permission flow'"},
            "session_id": f"test-{uuid.uuid4().hex[:8]}",
            "cwd": os.path.expanduser("~"),
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{HOOK_PORT}/hook/permission",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=130) as resp:
                body = json.loads(resp.read().decode())
                dec = body.get("hookSpecificOutput", {}).get(
                    "decision", {}).get("behavior", "?")
                if thread_id:
                    tg.send(f"Test OK! Decision: {tg.esc(dec)}",
                            chat_id, thread_id=thread_id)
                else:
                    _ephemeral(chat_id, f"Test OK! Decision: {tg.esc(dec)}",
                               seconds=7)
        except Exception as e:
            msg = f"Test failed: {tg.esc(str(e))}"
            if thread_id:
                tg.send(msg, chat_id, thread_id=thread_id)
            else:
                _ephemeral(chat_id, msg, seconds=7)

    threading.Thread(target=_do_test, daemon=True).start()
    if thread_id:
        tg.send("Test permission sent — click Allow/Deny.",
                chat_id, thread_id=thread_id)
    else:
        _ephemeral(chat_id, "Test permission sent — click Allow/Deny.",
                   seconds=10)


_usage_cache: str | None = None
_usage_cache_ts: float = 0
_USAGE_CACHE_TTL = 300


def _build_dashboard() -> str:
    from version import get_version
    ver = get_version().split("+")[0]
    active = sum(1 for s in mgr._sessions.values() if s.alive)
    parts = [f"<b>ClaudeLaude</b> v{tg.esc(ver)}"]
    parts.append(f"▶ {active} active")
    if _usage_cache:
        parts.append(_usage_cache)
    if is_killed():
        parts.append("\U0001f512 <b>KILLED</b>")
    return "\n".join(parts)


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", BOT_DIR, *args],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return ""


def _check_update() -> tuple[str, str] | None:
    """Return (current_ver, latest_ver) if an update is available, else None."""
    _git("fetch", "--tags", "origin")
    from version import get_version
    current = get_version()
    tags = _git("tag", "-l", "v*")
    if not tags:
        return None
    latest_tag = sorted(tags.splitlines(), key=lambda t: [
        int(x) for x in t.lstrip("v").split(".") if x.isdigit()
    ])[-1]
    latest = latest_tag.lstrip("v")
    local_head = _git("rev-parse", "HEAD")
    remote_head = _git("rev-parse", "origin/main")
    if local_head == remote_head and local_head:
        return None
    if current.split("+")[0] == latest:
        return None
    return current, latest


def _has_local_changes() -> list[str]:
    """Return list of files modified compared to .dist_checksums."""
    checksums_path = os.path.join(BOT_DIR, ".dist_checksums")
    if not os.path.isfile(checksums_path):
        return []
    import hashlib
    modified = []
    with open(checksums_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("  ", 1)
            if len(parts) != 2:
                continue
            expected_hash, filepath = parts
            full = os.path.join(BOT_DIR, filepath)
            if not os.path.isfile(full):
                continue
            with open(full, "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
            if actual != expected_hash:
                modified.append(filepath)
    return modified


def _run_update(non_interactive=False, policy=None) -> tuple[bool, str]:
    """Run update.sh; return (success, output)."""
    cmd = ["bash", os.path.join(BOT_DIR, "update.sh")]
    if non_interactive:
        cmd.append("--non-interactive")
    if policy:
        cmd.append(f"--policy={policy}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=120, cwd=BOT_DIR)
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)


def _restart_bot():
    """Re-exec the bot process."""
    print("[update] restarting bot...", file=sys.stderr, flush=True)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def cmd_update(chat_id, thread_id=None):
    mid = _reply(chat_id, thread_id, "⏳ Checking for updates...")

    def _do_check():
        result = _check_update()
        if mid:
            tg.delete(mid, chat_id)
        if not result:
            if not thread_id:
                _ephemeral(chat_id, "✅ Already up to date.", seconds=5)
            else:
                tg.send("✅ Already up to date.", chat_id,
                        thread_id=thread_id)
            return

        current, latest = result
        modified = _has_local_changes()

        text = f"<b>Update available</b>\n{tg.esc(current)} → <b>{tg.esc(latest)}</b>"
        if modified:
            text += f"\n\n⚠️ {len(modified)} locally modified file(s):"
            for f in modified[:5]:
                text += f"\n  <code>{tg.esc(f)}</code>"
            if len(modified) > 5:
                text += f"\n  … and {len(modified) - 5} more"

        buttons = [[
            {"text": "⬆️ Update now", "callback_data": "upd:go"},
            {"text": "❌ Cancel", "callback_data": "upd:no"},
        ]]
        _reply(chat_id, thread_id, text, buttons=buttons)

    threading.Thread(target=_do_check, daemon=True).start()


def _auto_update_loop():
    """Background thread: check for updates at startup + every hour."""
    if not AUTO_UPDATE:
        return
    time.sleep(10)
    while bot_running:
        try:
            result = _check_update()
            if result:
                current, latest = result
                modified = _has_local_changes()
                if not modified:
                    print(f"[auto-update] {current} → {latest}, no local changes, updating",
                          file=sys.stderr, flush=True)
                    ok, output = _run_update(non_interactive=True)
                    if ok:
                        _restart_bot()
                    else:
                        print(f"[auto-update] failed: {output[:200]}",
                              file=sys.stderr, flush=True)
                elif AUTO_UPDATE_POLICY == "replace":
                    print(f"[auto-update] {current} → {latest}, {len(modified)} modified, policy=replace",
                          file=sys.stderr, flush=True)
                    ok, output = _run_update(non_interactive=True, policy="replace")
                    if ok:
                        _restart_bot()
                    else:
                        print(f"[auto-update] failed: {output[:200]}",
                              file=sys.stderr, flush=True)
                else:
                    fid = forum()
                    if fid:
                        _ephemeral(fid,
                                   f"⬆️ Update available: {tg.esc(current)} → <b>{tg.esc(latest)}</b>\n"
                                   f"{len(modified)} locally modified file(s). Use /update to review.",
                                   seconds=30)
        except Exception as e:
            print(f"[auto-update] error: {e}", file=sys.stderr, flush=True)
        for _ in range(3600):
            if not bot_running:
                return
            time.sleep(1)

_HELP_TEXT = (
    "<b>ClaudeLaude — Help</b>\n"
    "\n"
    "<b>Sessions</b>\n"
    "/new — project picker\n"
    "/new &lt;path&gt; [name] — session in dir\n"
    "/sessions — list sessions + fork\n"
    "/resume — pick session to continue\n"
    "/resume &lt;id&gt; — continue by session id\n"
    "/stop — stop current session\n"
    "/restart — restart stopped session\n"
    "/interrupt — abort current turn\n"
    "\n"
    "<b>In topic</b>\n"
    "/history [N] — last N events (default 30)\n"
    "/usage — session tokens + account limits\n"
    "/display [mobile|desktop] — toggle view\n"
    "/mode [default|terse|verbose|beginner|plan|burn] — response style\n"
    "\n"
    "<b>Other</b>\n"
    "/update — check for bot updates\n"
    "/menu — quick actions\n"
    "/help — this message\n"
    "/stop_bot — shutdown bot\n"
    "\n"
    "Unknown /commands in bot sessions are\n"
    "forwarded to Claude as user messages.\n"
    "Photos/files in bot topics are sent\n"
    "to the session as attachments."
)

_KNOWN_COMMANDS = {
    "/setup", "/new", "/sessions", "/resume", "/history", "/stop",
    "/interrupt", "/restart", "/usage", "/display", "/mode", "/test_perm",
    "/update", "/stop_bot", "/help", "/start", "/menu",
    "/kill", "/audit",
}

_HELP_DOCUMENTED_COMMANDS = {
    "/new", "/sessions", "/resume", "/stop", "/interrupt", "/restart",
    "/history", "/usage", "/display", "/mode", "/update",
    "/menu", "/help", "/stop_bot",
}

_MENU_ROWS = [
    [{"text": "\U0001f195 New session", "callback_data": "m:new"},
     {"text": "\U0001f4cb Sessions", "callback_data": "m:sessions"}],
    [{"text": "▶️ Resume", "callback_data": "m:resume"},
     {"text": "❓ Help", "callback_data": "m:help"}],
]


def cmd_help(chat_id, thread_id=None):
    if not thread_id:
        _ephemeral(chat_id, _HELP_TEXT, seconds=_PICKER_TTL)
    else:
        tg.send(_HELP_TEXT, chat_id, thread_id=thread_id)


_HIDDEN_COMMANDS = {"/setup", "/test_perm", "/start", "/kill", "/audit"}


def _validate_help():
    """Warn if any non-hidden command is missing from help text."""
    should_document = _KNOWN_COMMANDS - _HIDDEN_COMMANDS
    missing = should_document - _HELP_DOCUMENTED_COMMANDS
    extra = _HELP_DOCUMENTED_COMMANDS - _KNOWN_COMMANDS
    if missing:
        print(f"[WARN] commands not in help: {missing}",
              file=sys.stderr, flush=True)
    if extra:
        print(f"[WARN] help references unknown commands: {extra}",
              file=sys.stderr, flush=True)


def _cleanup_general():
    """Delete stale messages in General (before the pinned help)."""
    fid = forum()
    pinned = get_pinned_help_id()
    if not fid or not pinned:
        return
    count = 0
    for msg_id in range(pinned - 1, max(pinned - 50, 0), -1):
        try:
            tg._req("deleteMessage", {"chat_id": fid, "message_id": msg_id})
            count += 1
        except Exception:
            pass
    if count:
        print(f"[cleanup] deleted {count} stale General messages",
              file=sys.stderr, flush=True)


def _sync_dashboard():
    """Send or update the pinned dashboard message in General."""
    _validate_help()
    fid = forum()
    if not fid:
        return
    text = _build_dashboard()
    old_id = get_dashboard_id()
    if not old_id:
        old_id = get_pinned_help_id()
    if old_id:
        try:
            tg._req("editMessageText", {
                "chat_id": fid,
                "message_id": old_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": _MENU_ROWS},
            })
            if not get_dashboard_id():
                set_dashboard_id(old_id)
            return
        except Exception as e:
            body = ""
            if hasattr(e, "response") and e.response is not None:
                try:
                    body = e.response.text
                except Exception:
                    pass
            if "not modified" in body.lower():
                return
            print(f"[dashboard] edit failed: {e} {body}",
                  file=sys.stderr, flush=True)
            set_dashboard_id(None)
            set_pinned_help_id(None)
    if old_id:
        tg.delete(old_id, fid)
    try:
        tg._req("unpinAllChatMessages", {"chat_id": fid})
    except Exception:
        pass
    msg_id = tg.send(text, fid, buttons=_MENU_ROWS)
    if msg_id:
        tg.pin(msg_id, fid)
        set_dashboard_id(msg_id)


def cmd_menu(chat_id, thread_id=None, session=None):
    rows = [
        [{"text": "\U0001f195 New session", "callback_data": "m:new"},
         {"text": "\U0001f4cb Sessions", "callback_data": "m:sessions"}],
    ]
    if session:
        with state.lock:
            mode = state.topic_display_mode.get(
                session.topic_id, DEFAULT_DISPLAY)
        mode_label = ("\U0001f4f1 → \U0001f5a5" if mode == "mobile"
                      else "\U0001f5a5 → \U0001f4f1")
        if session.alive:
            rows.append([
                {"text": f"{mode_label} Display",
                 "callback_data": "m:display"},
                {"text": "⏹ Stop", "callback_data": "m:stop"},
            ])
            rows.append([
                {"text": "\U0001f4ca Usage", "callback_data": "m:usage"},
                {"text": "\U0001f4dc History", "callback_data": "m:history"},
            ])
        else:
            rows.append([
                {"text": "▶️ Restart", "callback_data": "m:restart"},
            ])
    rows.append([
        {"text": "❓ Help", "callback_data": "m:help"},
    ])
    rows.append(_CLOSE_ROW)
    mid = tg.send("\U0001f3ae <b>Quick actions</b>", chat_id,
                   thread_id=thread_id, buttons=rows)
    if not thread_id and mid:
        def _cleanup():
            time.sleep(_PICKER_TTL)
            tg.delete(mid, chat_id)
        threading.Thread(target=_cleanup, daemon=True).start()


# ── main loop ────────────────────────────────────────────────────────

def _on_session_stop(session, reason: str):
    _cancel_session_perms(session.sid, reason)


mgr = SessionManager(
    on_assistant_message=on_assistant,
    on_result=on_result,
    on_tool_use=on_tool_use,
    on_thinking=on_thinking,
    on_session_stop=_on_session_stop,
    on_session_context=_session_context,
)


def on_mirror_event(mirror, event):
    """Project a JSONL event from a terminal session into its mirror topic.

    Filters for content the owner cares about: user prompts (plain
    text only), assistant text blocks, and tool_use one-liners.
    Everything else (tool_result echoes, attachments, system events,
    thinking blocks) is dropped.
    """
    fid = forum()
    if not fid or not mirror.topic_id:
        return
    etype = event.get("type", "")

    if etype == "user":
        # System-injected user events (slash-command bodies, hook
        # outputs, etc.) carry `isMeta: true` at the top level. Real
        # user input is `isMeta: None/false`. Skip meta events
        # universally — they are not what the owner typed.
        if event.get("isMeta"):
            return
        msg = event.get("message") or {}
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
            text = "".join(parts)
        text = text.strip()
        if not text:
            return
        # Belt-and-braces for older Claude Code builds that don't set
        # isMeta yet: the wrapper-tags event still has the literal
        # <command-*> markers.
        if ("<command-message>" in text
                or "<command-name>" in text
                or "<command-args>" in text):
            return
        # Echo suppression: if this text was just pushed into the pane
        # from the same mirror topic, the TG message is already visible
        # there — projecting another `👤 …` blockquote would duplicate.
        if mirror.consume_recent_echo(text):
            return
        tg.send(f"<blockquote>\U0001f464 {tg.esc(text[:3000])}</blockquote>",
                fid, thread_id=mirror.topic_id)
        return

    if etype == "assistant":
        msg = event.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return
        text_parts = []
        tool_lines = []
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                text_parts.append(b.get("text", ""))
            elif btype == "tool_use":
                tool = b.get("name") or "?"
                inp = b.get("input") or {}
                # Level "all": no tool_use shown at all.
                if mirror.filter_level == "all":
                    continue
                # Permission-paired: tool_use matches the most-recent
                # permission prompt we showed in this mirror's topic.
                # Skip so the Allow/Deny notice isn't shadowed by a
                # duplicate "⚙️ …" line. Consume the pairing.
                if mirror.pending_perm_tool is not None:
                    pair = (tool, _normalize_tool_input(tool, inp))
                    if pair == mirror.pending_perm_tool:
                        mirror.pending_perm_tool = None
                        continue
                if not _is_mirror_noisy_tool(tool, inp):
                    tool_lines.append(_compact_tool_msg(tool, inp))
        text = "".join(text_parts).strip()
        # Drop the /bot-mirror echo: its assistant turn just prints
        # `mirror: <topic_url>` plus a `tip:` / `output-only` line —
        # info the owner already sees via the HTTP response. Projecting
        # it back into the topic it created produces visual noise.
        if text.startswith("mirror: https://t.me/c/"):
            text = ""
        if text:
            tg.send_long(text, fid, thread_id=mirror.topic_id,
                         markdown=True)
        for line in tool_lines:
            tg.send(f"⚙️ {tg.esc(line)}",
                    fid, thread_id=mirror.topic_id)
        # Acknowledge any pending TG message: 👀 (received) → 👍 (claude
        # answered) so the owner sees from Telegram that delivery worked
        # end-to-end, not just up to the pane.
        if (text or tool_lines) and mirror.pending_user_msg_id:
            try:
                tg.set_message_reaction(
                    fid, mirror.pending_user_msg_id, _REACT_DONE)
            except Exception:
                pass
            mirror.pending_user_msg_id = None
        return

    if etype == "tool_use":
        tool = event.get("name") or "?"
        inp = event.get("input") or event.get("tool_input") or {}
        if not _is_mirror_noisy_tool(tool, inp):
            tg.send(f"⚙️ {tg.esc(_compact_tool_msg(tool, inp))}",
                    fid, thread_id=mirror.topic_id)


mirror_mgr = TerminalMirrorManager(on_event=on_mirror_event)

# When the JSONL already contains more than this many logical events
# at /bot-mirror time, the bot asks the owner via inline buttons
# whether to backfill the full history (slow, 1 msg/sec) or a brief
# summary (single TG message with last few events). Below the
# threshold, full backfill runs silently.
_BACKFILL_ASK_THRESHOLD = 30
_BACKFILL_SHORT_TAIL = 12

# Pending backfill choices keyed by csid: {"snapshot": int,
# "button_msg_id": int|None}. Populated when on_open_in_bot sends the
# choice prompt; consumed when the user clicks Full / Short.
_pending_backfill: dict = {}
_pending_backfill_lock = threading.Lock()


def _send_logical_event(fid: int, topic_id: int, ev: dict) -> None:
    """Project one logical event (user or assistant) into a topic."""
    if ev["kind"] == "user":
        text = ev.get("text", "")[:3000]
        tg.send(f"<blockquote>\U0001f464 {tg.esc(text)}</blockquote>",
                fid, thread_id=topic_id)
        return
    # assistant
    text = (ev.get("text") or "").strip()
    if text:
        tg.send_long(text, fid, thread_id=topic_id, markdown=True)
    for tool in ev.get("tools") or []:
        name = tool.get("name") or "?"
        inp = tool.get("input") or {}
        if not _is_mirror_noisy_tool(name, inp):
            tg.send(f"⚙️ {tg.esc(_compact_tool_msg(name, inp))}",
                    fid, thread_id=topic_id)


def _backfill_full(mirror, fid: int, snapshot_offset: int) -> None:
    """Project every logical event in JSONL[0..snapshot_offset]."""
    try:
        events = read_logical_events(mirror.jsonl_path)
    except Exception as e:
        print(f"[mirror backfill] read failed: {e}",
              file=sys.stderr, flush=True)
        return
    events = [e for e in events
              if int(e.get("byte_end", 0)) <= snapshot_offset]
    for ev in events:
        if not mirror.alive:
            return
        try:
            _send_logical_event(fid, mirror.topic_id, ev)
        except Exception as e:
            print(f"[mirror backfill] send failed: {e}",
                  file=sys.stderr, flush=True)


def _backfill_short_summary(mirror, fid: int,
                            snapshot_offset: int, n: int) -> None:
    """Glue the last N logical events into one (or chunked-by-TG-cap)
    TG message — quick context preview instead of full history."""
    try:
        events = read_logical_events(mirror.jsonl_path)
    except Exception:
        events = []
    events = [e for e in events
              if int(e.get("byte_end", 0)) <= snapshot_offset]
    if not events:
        return
    if len(events) > n:
        events = events[-n:]
    parts = []
    for ev in events:
        if ev["kind"] == "user":
            parts.append(
                f"<blockquote>\U0001f464 "
                f"{tg.esc(ev['text'][:1500])}</blockquote>"
            )
        else:
            text = (ev.get("text") or "").strip()
            if text:
                parts.append(tg.esc(text[:1500]))
            for tool in ev.get("tools") or []:
                if not _is_mirror_noisy_tool(tool["name"], tool["input"]):
                    parts.append(
                        f"⚙️ {tg.esc(_compact_tool_msg(tool['name'], tool['input']))}"
                    )
    body = "\n\n".join(parts)
    if body:
        tg.send_long(body, fid, thread_id=mirror.topic_id)


def _start_backfill_thread(mirror, fid: int, mode: str,
                           snapshot_offset: int) -> None:
    """Spawn a daemon thread that runs the chosen backfill, then
    releases mirror.backfill_done so the follower can resume."""
    def run():
        try:
            if mode == "full":
                _backfill_full(mirror, fid, snapshot_offset)
            elif mode == "short":
                _backfill_short_summary(mirror, fid, snapshot_offset,
                                        _BACKFILL_SHORT_TAIL)
        finally:
            mirror.backfill_done.set()
    threading.Thread(target=run, daemon=True,
                     name=f"mirror-backfill-{mirror.csid[:8]}").start()


def on_open_in_bot(csid, cwd, dtach_socket):
    """Bot-side handler for POST /hook/open_in_bot.

    Creates a mirror topic if one doesn't exist for this csid, starts
    the JSONL follower, returns {status, topic_url}. dtach_socket is
    the path to the unix socket of the wrapped claude (e.g.
    `/tmp/clmirror-<pid>.sock`) — bot writes input via
    `dtach -p <socket>`. When empty, the mirror runs output-only.
    """
    fid = forum()
    if not fid:
        return {"error": "bot has no forum chat configured"}
    existing = mirror_mgr.by_csid(csid)
    if existing:
        url = _topic_url(existing.topic_id)
        # If the previous follower thread died (e.g. JSONL hadn't been
        # written yet on first registration), restart it now that the
        # user is invoking the command again with the file likely
        # already in place.
        if not existing.follower or not existing.follower.is_alive():
            mirror_mgr.start_follower(existing)
        # Refresh the dtach binding — the user may have re-launched
        # their terminal claude, getting a new socket path.
        if dtach_socket and existing.dtach_socket != dtach_socket:
            mirror_mgr.set_dtach_socket(csid, dtach_socket)
        return {"status": "ok", "topic_url": url, "existing": True,
                "input_bridge": bool(existing.dtach_socket)}
    name = os.path.basename(cwd.rstrip("/")) or "terminal"
    ts = time.strftime("%H:%M")
    # Topic icon is the 💻 terminal emoji (icon_custom_emoji_id), so we
    # don't double up with another decorative prefix here.
    label = f"{name} mirror — {ts}"[:128]
    try:
        topic_id = tg.create_forum_topic(
            fid, label, icon_color=0x6FB9F0,
            icon_custom_emoji_id=_ICON_TERMINAL)
    except Exception as e:
        return {"error": f"create_forum_topic failed: {e}"}
    if not topic_id:
        return {"error": "create_forum_topic returned no id"}
    with state.lock:
        state.topic_labels[topic_id] = label
    m = mirror_mgr.register(csid, cwd, topic_id, dtach_socket)
    snapshot_offset = m.last_offset  # JSONL size at registration time
    # Welcome with inline controls (filter toggle + mode cycle). Stays
    # at the top of the topic; we edit its buttons in place when state
    # changes (filter toggled), so the labels always reflect reality.
    welcome_text = _mirror_welcome_text(m)
    welcome_buttons = _mirror_welcome_buttons(m)
    welcome_id = tg.send(welcome_text, fid, thread_id=topic_id,
                         buttons=welcome_buttons)
    if welcome_id:
        mirror_mgr.set_welcome_msg_id(csid, welcome_id)

    # Count logical (user-visible) events already in the transcript.
    # ≤ threshold → silent full backfill. > threshold → ask the owner
    # via inline buttons whether they want the full slow stream or a
    # short single-message summary.
    try:
        existing_events = read_logical_events(m.jsonl_path)
        n_events = sum(
            1 for e in existing_events
            if int(e.get("byte_end", 0)) <= snapshot_offset
        )
    except Exception as e:
        print(f"[mirror] could not count history: {e}",
              file=sys.stderr, flush=True)
        n_events = 0

    if n_events > 0:
        # Suspend the follower until backfill is decided/done — keeps
        # ordering chronological.
        m.backfill_done.clear()
        if n_events <= _BACKFILL_ASK_THRESHOLD:
            _start_backfill_thread(m, fid, "full", snapshot_offset)
        else:
            eta_sec = max(n_events, 1)  # ~1 msg/sec rate-gate
            buttons = [[
                {"text": f"Полная история (~{eta_sec}с)",
                 "callback_data": f"mirror_history:full:{csid[:24]}"},
                {"text": "Кратко (последние 12)",
                 "callback_data": f"mirror_history:short:{csid[:24]}"},
            ]]
            prompt = (
                f"В этой сессии уже {n_events} сообщений. "
                f"Загрузить полностью (медленно, по ~1 сек/сообщение из-за "
                f"TG rate-limit) или короткую сводку одним сообщением?"
            )
            msg_id = tg.send(prompt, fid, thread_id=topic_id,
                             buttons=buttons)
            with _pending_backfill_lock:
                _pending_backfill[csid] = {
                    "snapshot": snapshot_offset,
                    "button_msg_id": msg_id,
                }

    mirror_mgr.start_follower(m)
    url = _topic_url(topic_id)
    return {"status": "ok", "topic_url": url, "existing": False,
            "input_bridge": bool(dtach_socket)}


bridge = HookBridge(
    on_notification=on_hook_notification,
    on_permission=on_hook_permission,
    on_open_in_bot=on_open_in_bot,
)


def _refresh_usage_cache():
    global _usage_cache, _usage_cache_ts
    try:
        raw = _fetch_account_usage()
        if raw:
            lines = raw.strip().splitlines()
            short = []
            for line in lines:
                m = re.search(r'(\d+)%', line)
                label = "Week" if "week" in line.lower() else "Session"
                if m:
                    short.append(f"{label}: {m.group(1)}%")
            _usage_cache = " · ".join(short) if short else None
        else:
            _usage_cache = None
        _usage_cache_ts = time.time()
    except Exception as e:
        print(f"[dashboard] usage fetch error: {e}",
              file=sys.stderr, flush=True)


def _dashboard_loop():
    """Background: update dashboard pin every 60s, refresh usage every 5m."""
    time.sleep(5)
    while bot_running:
        if time.time() - _usage_cache_ts > _USAGE_CACHE_TTL:
            _refresh_usage_cache()
        try:
            _sync_dashboard()
        except Exception as e:
            print(f"[dashboard] update error: {e}",
                  file=sys.stderr, flush=True)
        time.sleep(60)


def _topic_healthcheck():
    """Periodically check that session topics still exist; stop orphans.

    Uses editForumTopic with the stored label as a silent probe — no
    messages sent, no notifications.  Falls back to send-and-delete when
    no label is tracked.
    """
    while bot_running:
        time.sleep(30)
        fid = forum()
        if not fid:
            continue
        for session in mgr.list_sessions():
            if not session.topic_id:
                continue
            with state.lock:
                label = state.topic_labels.get(session.topic_id)
            if not label:
                label = session.topic_label or session.name
            if not tg.topic_alive(fid, session.topic_id, name=label):
                _invalidate_and_stop(session, "topic deleted")

        # Mirrors: probe topic existence and dtach-socket presence only.
        # A mirror with a vanished topic is dropped; a vanished dtach
        # socket flips the mirror to output-only. We deliberately do NOT
        # close on JSONL idleness — a long pause between turns is the
        # owner's choice (per [[no-hard-ceiling]]), not a signal to
        # tear down the mirror behind their back.
        for mirror in mirror_mgr.list():
            if not mirror.alive:
                continue
            with state.lock:
                label = state.topic_labels.get(mirror.topic_id)
            if not label:
                label = f"mirror {mirror.csid[:8]}"
            if not tg.topic_alive(fid, mirror.topic_id, name=label):
                print(f"[mirror] topic gone for {mirror.csid[:8]} — unregistering",
                      file=sys.stderr, flush=True)
                mirror_mgr.unregister(mirror.csid)
                continue
            if (mirror.dtach_socket and
                    not dtach_socket_alive(mirror.dtach_socket)):
                send_to_topic(
                    mirror.topic_id,
                    "\U0001f50c Terminal closed — mirror is now output-only")
                mirror_mgr.set_dtach_socket(mirror.csid, None)


def _cleanup_terminal_pending(csid: str):
    """Remove stale messages from a terminal topic after session progressed."""
    with state.lock:
        msgs = state.pending_terminal_msgs.pop(csid, [])
    if not msgs:
        return
    for msg_id, chat_id, kind in msgs:
        if kind.startswith("perm:"):
            short_id = kind[5:]
            with state.lock:
                full_id = state.perm_key_map.pop(short_id, None)
                state.pending_permissions.pop(short_id, None)
            try:
                tg.edit(msg_id, "✓ Resolved in terminal", chat_id)
            except Exception:
                pass
            if full_id:
                bridge.abandon_permission(full_id)
            def _del(mid=msg_id, cid=chat_id):
                time.sleep(5)
                tg.delete(mid, cid)
            threading.Thread(target=_del, daemon=True).start()
        else:
            try:
                tg.delete(msg_id, chat_id)
            except Exception:
                pass


def _terminal_watcher():
    """Poll JSONL files of terminal sessions; clean stale messages on progress."""
    while bot_running:
        time.sleep(5)
        with state.lock:
            watched = list(state.pending_terminal_msgs.keys())
        for csid in watched:
            path = _session_jsonl_path(csid)
            if not path:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            prev = _watcher_offsets.get(csid, 0)
            if size <= prev:
                continue
            print(f"[watcher] JSONL grew csid={csid[:8]}… "
                  f"{prev}→{size}, cleaning",
                  file=sys.stderr, flush=True)
            _watcher_offsets[csid] = size
            _cleanup_terminal_pending(csid)


def _device_monitor_loop():
    """Background: check for new TG sessions every 5 minutes."""
    import device_monitor
    if not device_monitor._available():
        return
    time.sleep(10)
    while bot_running:
        try:
            new = device_monitor.check_new_devices()
            for d in new:
                fid = forum()
                if not fid:
                    break
                ip_masked = d.get("ip", "?")
                if "." in ip_masked:
                    parts = ip_masked.split(".")
                    ip_masked = f"{parts[0]}.{parts[1]}.x.x"
                text = (
                    f"⚠️ <b>New TG session detected</b>\n"
                    f"{tg.esc(d.get('device_model', '?'))}, "
                    f"{tg.esc(d.get('platform', '?'))}\n"
                    f"{tg.esc(d.get('country', '?'))}, "
                    f"IP {tg.esc(ip_masked)}"
                )
                key = d.get("key", "")
                buttons = [
                    [{"text": "✓ Trust", "callback_data": f"dt:{key[:40]}"},
                     {"text": "\U0001f512 Kill bot", "callback_data": "dk:"}],
                ]
                audit.log("device_alert",
                          f"{d.get('device_model', '?')} "
                          f"{d.get('country', '?')} {ip_masked}")
                tg.send(text, fid, buttons=buttons)
        except Exception as e:
            print(f"[device_monitor] error: {e}",
                  file=sys.stderr, flush=True)
        time.sleep(300)


def main():
    global bot_running

    for s in mgr.list_sessions():
        if s.topic_id and s.topic_label:
            with state.lock:
                state.topic_labels[s.topic_id] = s.topic_label
    bridge.start()
    _cleanup_general()
    mirror_mgr.start_all_followers()
    threading.Thread(target=_topic_healthcheck, daemon=True).start()
    threading.Thread(target=_dashboard_loop, daemon=True).start()
    threading.Thread(target=_terminal_watcher, daemon=True).start()
    threading.Thread(target=_device_monitor_loop, daemon=True).start()
    tg.set_my_commands([
        {"command": "new", "description": "New Claude session"},
        {"command": "sessions", "description": "Active sessions"},
        {"command": "menu", "description": "Quick actions"},
        {"command": "stop", "description": "Stop session"},
        {"command": "restart", "description": "Restart stopped session"},
        {"command": "history", "description": "Last N events"},
        {"command": "usage", "description": "Token usage"},
        {"command": "display", "description": "Toggle mobile/desktop"},
        {"command": "update", "description": "Check for bot updates"},
        {"command": "help", "description": "Show help"},
    ])
    threading.Thread(target=_auto_update_loop, daemon=True).start()
    _refresh_usage_cache()
    _sync_dashboard()
    _admin_sanity_check()

    offset = None
    while bot_running:
        updates = tg.poll(offset)
        for u in updates:
            offset = u["update_id"] + 1
            try:
                _handle_update(u)
            except Exception as e:
                print(f"[update] error: {e}", file=sys.stderr, flush=True)

    for s in mgr.list_sessions():
        mgr.stop(s.sid)


def _admin_sanity_check():
    """On startup, verify the bot is an admin in the linked forum group.

    Fail-fast warning is much easier to read than the cryptic 400 from
    createForumTopic that surfaces only when the first session is opened.
    Silent no-op when no forum is linked yet (fresh install).
    """
    fid = forum()
    if not fid or not OWNER_ID:
        return
    try:
        r = tg._req("getChatMember", {"chat_id": fid, "user_id": OWNER_ID})
        status = (r.get("result", {}) or {}).get("status", "")
    except Exception as e:
        print(f"[setup] getChatMember failed: {e}",
              file=sys.stderr, flush=True)
        return
    if status not in ("creator", "administrator"):
        print(f"[setup] WARN: owner status in forum group is '{status}'. "
              "Bot expects owner to be admin so /new can manage topics.",
              file=sys.stderr, flush=True)


def _handle_my_chat_member(mcm: dict):
    """React when our own admin/member status changes in some chat.

    Useful for setup-flow visibility: the bot logs when it's added to a
    new group or promoted to admin so the owner can spot mis-configured
    state without diffing /audit. Forum auto-link only happens via
    /setup — we don't auto-claim a new group as the forum.
    """
    chat = mcm.get("chat", {}) or {}
    new_member = (mcm.get("new_chat_member") or {})
    old_member = (mcm.get("old_chat_member") or {})
    chat_id = chat.get("id")
    title = chat.get("title", "?")
    new_status = new_member.get("status", "?")
    old_status = old_member.get("status", "?")
    print(f"[my_chat_member] chat={chat_id} title={title!r} "
          f"{old_status}→{new_status}", file=sys.stderr, flush=True)
    audit.log("my_chat_member",
              f"{title} ({chat_id}): {old_status} -> {new_status}")


def _handle_update(u):
    global bot_running

    cb = u.get("callback_query")
    if cb:
        tg.answer_callback(cb["id"])
        if cb.get("from", {}).get("id") != OWNER_ID:
            return
        _handle_callback(cb, cb.get("data", ""))
        return

    mcm = u.get("my_chat_member")
    if mcm:
        _handle_my_chat_member(mcm)
        return

    msg = u.get("message", {})
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    from_user = msg.get("from", {}).get("id")

    text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()
    print(f"[msg] chat={chat_id} from={from_user} "
          f"text={text[:60] or caption[:60]}",
          file=sys.stderr, flush=True)

    # Auto-delete pin service messages from bot in General
    if msg.get("pinned_message"):
        fid = forum()
        if fid and chat_id == fid:
            mid = msg.get("message_id")
            if mid:
                tg.delete(mid, chat_id)
        return

    if from_user != OWNER_ID:
        return

    # Kill switch — ignore everything except unlock word in General
    if is_killed():
        if text:
            _try_unkill(text, chat_id, msg.get("message_id"),
                        msg.get("message_thread_id"))
        return

    thread_id = msg.get("message_thread_id")
    msg_id = msg.get("message_id")
    session = mgr.by_topic(thread_id) if thread_id else None
    mirror = mirror_mgr.by_topic(thread_id) if thread_id and not session else None

    fid = forum()
    if not thread_id and fid and chat_id == fid and msg_id:
        tg.delete(msg_id, chat_id)

    # ── Terminal-mirror topic: forward text into the terminal claude
    # via the dtach socket, or politely reject if input isn't bridged.
    if mirror:
        photos = msg.get("photo")
        document = msg.get("document")
        sticker = msg.get("sticker")
        if photos or document or sticker:
            _ephemeral(chat_id,
                       "\U0001f501 Mirror does not bridge files/stickers — type text",
                       thread_id=thread_id, seconds=5)
            return
        if not text:
            return
        if not mirror.dtach_socket:
            _ephemeral(chat_id,
                       "\U0001f501 Output-only mirror — terminal input is not bridged "
                       "(start your terminal claude inside dtach to enable it)",
                       thread_id=thread_id, seconds=8)
            return
        if msg_id:
            tg.set_message_reaction(chat_id, msg_id, _REACT_RECEIVED)
        ok = push_to_dtach(mirror.dtach_socket, text)
        if not ok:
            if msg_id:
                tg.set_message_reaction(chat_id, msg_id, _REACT_ERROR)
            _ephemeral(chat_id,
                       "❌ Could not deliver to terminal "
                       "(dtach socket missing or unresponsive)",
                       thread_id=thread_id, seconds=8)
            mirror_mgr.set_dtach_socket(mirror.csid, None)
        else:
            # Remember this msg so the JSONL follower can swap 👀 → 👍
            # once claude actually replies (proves end-to-end delivery,
            # not just send-keys success).
            mirror.pending_user_msg_id = msg_id
            # Mark the text so the follower's user-event projection
            # suppresses the echo back into this same topic.
            mirror.note_injection(text)
        audit.log("mirror_input", text[:200], sid=mirror.csid)
        return

    # Handle photo/document attachments
    photos = msg.get("photo")
    document = msg.get("document")
    if photos or document:
        if not (session and session.is_bot_spawned and session.alive):
            tg.send("Send files in an active session",
                    chat_id, thread_id=thread_id)
            return
        file_id = photos[-1]["file_id"] if photos else document["file_id"]
        filename = ("photo.jpg" if photos
                    else document.get("file_name", "file"))
        dest = os.path.join(_UPLOAD_DIR,
                            f"{int(time.time())}_{filename}")
        if tg.download_file(file_id, dest):
            user_text = (f"{caption}\n[Attached file: {dest}]" if caption
                         else f"[Attached file: {dest}]")
            audit.log("user_message", f"[file] {filename}",
                      sid=session.sid)
            _enqueue_user_input(session, user_text, chat_id, msg_id, thread_id)
        else:
            tg.send("❌ Download failed", chat_id,
                    thread_id=thread_id)
        return

    # Handle stickers: pass emoji + pack name as text so Claude has context.
    sticker = msg.get("sticker")
    if sticker:
        if not (session and session.is_bot_spawned and session.alive):
            tg.send("Send stickers in an active session",
                    chat_id, thread_id=thread_id)
            return
        emoji = sticker.get("emoji") or ""
        set_name = sticker.get("set_name") or ""
        if set_name:
            descr = f"[Sticker: {emoji} from \"{set_name}\"]"
        elif emoji:
            descr = f"[Sticker: {emoji}]"
        else:
            descr = "[Sticker]"
        audit.log("user_message", descr, sid=session.sid)
        _enqueue_user_input(session, descr, chat_id, msg_id, thread_id)
        return

    if not text:
        return

    if text.startswith("/"):
        cmd, _, args = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        audit.log("command", f"{cmd} {args}".strip(),
                  sid=session.sid if session else None)
        _handle_command(cmd, args, chat_id, thread_id, session)
    else:
        audit.log("user_message", text,
                  sid=session.sid if session else None)
        if session and session.is_bot_spawned:
            _enqueue_user_input(session, text, chat_id, msg_id, thread_id)
        elif session:
            tg.send("Terminal session — use terminal",
                    chat_id, thread_id=thread_id)
        elif thread_id:
            tg.send("No active session. Use /restart or /new",
                    chat_id, thread_id=thread_id)
        elif chat_id == OWNER_ID:
            tg.send("Use in a session topic, or /new",
                    chat_id)
        else:
            fid = forum()
            if fid and chat_id == fid:
                _ephemeral(chat_id, "Use in a session topic, or /new", seconds=5)


def _handle_command(cmd, args, chat_id, thread_id, session):
    global bot_running
    if cmd == "/setup":
        cmd_setup(chat_id)
    elif cmd == "/new":
        cmd_new(args, chat_id=chat_id, thread_id=thread_id)
    elif cmd == "/sessions":
        cmd_sessions(chat_id, thread_id)
    elif cmd == "/resume":
        cmd_resume(args, chat_id, thread_id)
    elif cmd == "/history":
        cmd_history(session, chat_id, thread_id, args)
    elif cmd == "/stop":
        cmd_stop(session, chat_id, thread_id)
    elif cmd == "/interrupt":
        cmd_interrupt(session, chat_id, thread_id)
    elif cmd == "/restart":
        cmd_restart(chat_id, thread_id)
    elif cmd == "/usage":
        cmd_usage(session, chat_id, thread_id)
    elif cmd == "/display":
        cmd_display(chat_id, thread_id, args)
    elif cmd == "/mode":
        cmd_mode(session, chat_id, thread_id, args)
    elif cmd == "/update":
        cmd_update(chat_id, thread_id)
    elif cmd == "/test_perm":
        cmd_test_perm(chat_id, thread_id)
    elif cmd == "/kill":
        _do_kill()
    elif cmd == "/audit":
        cmd_audit(args, chat_id, thread_id)
    elif cmd == "/stop_bot":
        fid = forum()
        if fid:
            _ephemeral(fid, "\U0001f44b Shutting down.", seconds=5)
        bot_running = False
    elif cmd in ("/help", "/start"):
        cmd_help(chat_id, thread_id)
    elif cmd == "/menu":
        cmd_menu(chat_id, thread_id, session)
    else:
        if session and session.is_bot_spawned:
            ok = mgr.send_user_message(session.sid,
                                       f"{cmd} {args}".strip())
            if not ok:
                tg.send("⚠️ Session died", chat_id,
                        thread_id=thread_id)
        elif session:
            tg.send("ℹ️ Terminal session — use terminal",
                    chat_id, thread_id=thread_id)


def _handle_callback(cb, data):
    cb_chat = cb.get("message", {}).get("chat", {}).get("id")
    cb_msg = cb.get("message", {}).get("message_id")
    cb_thread = cb.get("message", {}).get("message_thread_id")

    if data.startswith("int:"):
        sid = data[4:]
        session = mgr._sessions.get(sid)
        if session:
            _do_interrupt(session, cb_chat, cb_thread)
        return

    if data.startswith("p:"):
        parts = data.split(":")
        if len(parts) == 3:
            short_id = parts[1]
            decision = "allow" if parts[2] == "a" else "deny"
            with state.lock:
                full_id = state.perm_key_map.pop(short_id, short_id)
                entry = state.pending_permissions.pop(short_id, None)
            bridge.resolve_permission(full_id, decision)
            audit.log("permission_decision", f"{decision} {short_id}")
            if entry:
                msg_id, perm_chat, _ = entry
                mark = "✓ Allowed" if decision == "allow" else "✗ Denied"
                tg.edit(msg_id, mark, perm_chat)
                def _del_perm(mid=msg_id, cid=perm_chat):
                    time.sleep(1)
                    tg.delete(mid, cid)
                threading.Thread(target=_del_perm, daemon=True).start()

    elif data.startswith("na:"):
        pick_id = data.split(":")[1]
        with state.lock:
            projects = state.pending_project_picks.get(pick_id)
        if projects:
            rows = []
            for i, p in enumerate(projects):
                label = os.path.basename(p.rstrip("/"))
                rows.append([{"text": label,
                              "callback_data": f"n:{pick_id}:{i}"}])
            rows.append(_CLOSE_ROW)
            if cb_chat and cb_msg:
                tg.edit(cb_msg, "\U0001f4c2 Choose project:",
                        cb_chat, buttons=rows)

    elif data.startswith("n:"):
        parts = data.split(":")
        if len(parts) == 3:
            pick_id, idx_s = parts[1], parts[2]
            with state.lock:
                projects = state.pending_project_picks.pop(pick_id, None)
            if projects and idx_s.isdigit():
                idx = int(idx_s)
                if 0 <= idx < len(projects):
                    if cb_chat and cb_msg:
                        tg.delete(cb_msg, cb_chat)
                    _spawn_session(projects[idx])

    elif data.startswith("ra:"):
        pick_id = data.split(":")[1]
        with state.lock:
            sessions_list = state.pending_resume_picks.get(pick_id)
        if sessions_list and cb_chat and cb_msg:
            text, rows = _build_resume_picker(sessions_list, pick_id,
                                              max_items=None)
            tg.edit(cb_msg, text, cb_chat, buttons=rows)

    elif data.startswith("r:"):
        parts = data.split(":")
        if len(parts) == 3:
            pick_id, idx_s = parts[1], parts[2]
            with state.lock:
                sessions_list = state.pending_resume_picks.pop(pick_id, None)
            if sessions_list and idx_s.isdigit():
                idx = int(idx_s)
                if 0 <= idx < len(sessions_list):
                    if cb_chat and cb_msg:
                        tg.delete(cb_msg, cb_chat)
                    sid, cwd, _, _ = sessions_list[idx]
                    _do_resume(sid, cb_chat, cb_thread)

    elif data.startswith("c:"):
        compact_id = data[2:]
        with state.lock:
            saved = state.saved_turns.get(compact_id)
        if saved and cb_chat:
            msg_ids, texts, ops = saved
            for mid in msg_ids:
                if mid != cb_msg:
                    tg.delete(mid, cb_chat)
            if cb_msg:
                tg.edit(cb_msg, "⏳ Summarizing…", cb_chat)

            def _do_compact():
                summary = _build_summary(texts, ops)
                btn = [[{"text": "\U0001f4c2 Expand",
                          "callback_data": f"uc:{compact_id}"}]]
                if cb_msg:
                    tg.edit(cb_msg, summary, cb_chat, buttons=btn)

            threading.Thread(target=_do_compact, daemon=True).start()

    elif data.startswith("uc:"):
        compact_id = data[3:]
        with state.lock:
            saved = state.saved_turns.get(compact_id)
        if saved and cb_chat:
            _, texts, ops = saved
            if cb_msg:
                tg.delete(cb_msg, cb_chat)
            new_ids = []
            for t in texts:
                new_ids.extend(
                    tg.send_long(t, cb_chat, thread_id=cb_thread,
                                 markdown=True))
            btn = [[{"text": "\U0001f5dc Compact",
                      "callback_data": f"c:{compact_id}"}]]
            if new_ids:
                try:
                    tg._req("editMessageReplyMarkup", {
                        "chat_id": cb_chat,
                        "message_id": new_ids[-1],
                        "reply_markup": {"inline_keyboard": btn},
                    })
                except Exception:
                    pass
            with state.lock:
                state.saved_turns[compact_id] = (new_ids, texts, ops)

    elif data.startswith("fork:"):
        parent_sid = data[5:]
        parent = mgr._sessions.get(parent_sid)
        if parent and parent.claude_session_id:
            if cb_chat and cb_msg:
                tg.delete(cb_msg, cb_chat)
            fid = forum()
            if fid:
                ts = time.strftime("%H:%M")
                label = f"{parent.name} fork — {ts}"
                topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0)
                if topic_id:
                    with state.lock:
                        state.topic_labels[topic_id] = label
                    session = mgr.fork(parent, topic_id, parent.name)
                    if session:
                        session.topic_label = label
                        send_to_topic(topic_id,
                                      f"\U0001f500 Fork of <b>{tg.esc(parent.name)}</b>")
                        # Copy the last few messages from the parent topic
                        # so the fork doesn't open empty — the user sees
                        # the point they branched from.
                        if parent.topic_id:
                            with state.lock:
                                recent = list(state.recent_msgs.get(
                                    parent.topic_id, []))
                            if recent:
                                tg.copy_messages(
                                    fid, fid, recent[-_FORK_BACKFILL:],
                                    thread_id=topic_id)
                        _send_fork_summary(parent, topic_id)
                        url = _topic_url(topic_id)
                        if url:
                            _ephemeral(fid, "\U0001f500",
                                       buttons=[[{"text": "Open fork", "url": url}]],
                                       seconds=5)

    elif data == "close":
        if cb_chat and cb_msg:
            tg.delete(cb_msg, cb_chat)
        return

    elif data.startswith("upd:"):
        action = data[4:]
        if action == "go":
            if cb_msg and cb_chat:
                tg.edit(cb_msg, "⬆️ Updating...", cb_chat)

            def _do_update():
                modified = _has_local_changes()
                policy = AUTO_UPDATE_POLICY if modified else None
                ok, output = _run_update(non_interactive=True, policy=policy)
                if ok:
                    if cb_chat:
                        tg.send("✅ Updated. Restarting...", cb_chat,
                                thread_id=cb_thread)
                    time.sleep(1)
                    _restart_bot()
                else:
                    msg = f"❌ Update failed:\n<code>{tg.esc(output[:500])}</code>"
                    if cb_chat:
                        tg.send(msg, cb_chat, thread_id=cb_thread)

            threading.Thread(target=_do_update, daemon=True).start()
        elif action == "no":
            if cb_msg and cb_chat:
                tg.delete(cb_msg, cb_chat)

    elif data.startswith("dt:"):
        import device_monitor
        key = data[3:]
        device_monitor.trust_device(key)
        audit.log("device_trust", key)
        if cb_msg and cb_chat:
            tg.edit(cb_msg, "✓ Device trusted", cb_chat)
            def _del_dt(mid=cb_msg, cid=cb_chat):
                time.sleep(5)
                tg.delete(mid, cid)
            threading.Thread(target=_del_dt, daemon=True).start()

    elif data == "dk:":
        _do_kill()
        if cb_msg and cb_chat:
            tg.edit(cb_msg, "\U0001f512 Bot killed", cb_chat)

    elif data.startswith("mf:"):
        # mf:<csid_prefix>:<level>  — toggle filter level on a mirror.
        parts = data.split(":", 2)
        if len(parts) == 3:
            prefix, level = parts[1], parts[2]
            if level in ("all", "lite"):
                hit = None
                for full in [m.csid for m in mirror_mgr.list()]:
                    if full.startswith(prefix):
                        hit = full
                        break
                if hit:
                    mirror_mgr.set_filter_level(hit, level)
                    m = mirror_mgr.by_csid(hit)
                    if m and m.welcome_msg_id and cb_chat:
                        try:
                            tg.edit(m.welcome_msg_id,
                                    _mirror_welcome_text(m), cb_chat,
                                    buttons=_mirror_welcome_buttons(m))
                        except Exception as e:
                            print(f"[mirror] welcome edit failed: {e}",
                                  file=sys.stderr, flush=True)

    elif data.startswith("mm:"):
        # mm:<csid_prefix>  — push Shift+Tab into the dtach socket and
        # advance our local "current mode" index so the button label
        # reflects what Claude's TUI shows after the keystroke.
        prefix = data.split(":", 1)[1]
        hit = None
        for full in [m.csid for m in mirror_mgr.list()]:
            if full.startswith(prefix):
                hit = full
                break
        m = mirror_mgr.by_csid(hit) if hit else None
        if m and m.dtach_socket:
            ok = push_to_dtach(m.dtach_socket, "\x1b[Z", with_enter=False)
            if ok:
                mirror_mgr.advance_mode(hit)
                m_now = mirror_mgr.by_csid(hit)
                if m_now and m_now.welcome_msg_id and cb_chat:
                    try:
                        tg.edit(m_now.welcome_msg_id,
                                _mirror_welcome_text(m_now), cb_chat,
                                buttons=_mirror_welcome_buttons(m_now))
                    except Exception as e:
                        print(f"[mirror] welcome edit failed: {e}",
                              file=sys.stderr, flush=True)
            elif cb_chat:
                tg.send("⚠️ couldn't push Shift+Tab — dtach socket gone?",
                        cb_chat, thread_id=cb_thread)

    elif data.startswith("mirror_history:"):
        # mirror_history:<mode>:<csid_prefix>
        parts = data.split(":", 2)
        if len(parts) == 3:
            mode, csid_prefix = parts[1], parts[2]
            with _pending_backfill_lock:
                hit_csid = None
                for full_csid in list(_pending_backfill.keys()):
                    if full_csid.startswith(csid_prefix):
                        hit_csid = full_csid
                        break
                entry = _pending_backfill.pop(hit_csid, None) if hit_csid else None
            mirror = mirror_mgr.by_csid(hit_csid) if hit_csid else None
            if entry and mirror and cb_chat and cb_msg:
                # Drop the prompt; the chosen mode's content takes its place.
                try:
                    tg.delete(cb_msg, cb_chat)
                except Exception:
                    pass
                _start_backfill_thread(
                    mirror, cb_chat, mode, entry["snapshot"])
            elif cb_msg and cb_chat:
                tg.edit(cb_msg,
                        "История больше недоступна (сессия пересоздана).",
                        cb_chat)

    elif data.startswith("m:"):
        action = data[2:]
        session = mgr.by_topic(cb_thread) if cb_thread else None
        # Don't delete the source message — it may be the dashboard pin
        if action == "new":
            cmd_new("", chat_id=cb_chat, thread_id=cb_thread)
        elif action == "sessions":
            cmd_sessions(cb_chat, cb_thread)
        elif action == "resume":
            cmd_resume("", cb_chat, cb_thread)
        elif action == "display" and cb_thread:
            cmd_display(cb_chat, cb_thread, "")
        elif action == "stop" and session:
            cmd_stop(session, cb_chat, cb_thread)
        elif action == "restart":
            cmd_restart(cb_chat, cb_thread)
        elif action == "usage" and session:
            cmd_usage(session, cb_chat, cb_thread)
        elif action == "history" and session:
            cmd_history(session, cb_chat, cb_thread, "")
        elif action == "help":
            cmd_help(cb_chat, cb_thread)


if __name__ == "__main__":
    main()
