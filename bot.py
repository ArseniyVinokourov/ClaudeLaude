#!/usr/bin/env python3
"""ClaudeLaude Telegram bot — Forum Topics UI for Claude Code.

Each person runs their own instance of this bot on their machine.
See README.md or run setup.sh for first-time configuration.
"""
import json
import os
import shutil
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(__file__))

from config import (OWNER_ID, HOOK_PORT,
                    AUTO_UPDATE, AUTO_UPDATE_POLICY,
                    UNLOCK_WORD,
                    get_forum_chat_id, set_forum_chat_id,
                    is_killed, activate_kill, deactivate_kill)
import audit
import telegram as tg
from botui import BotUI
from dashboard import Dashboard
from hookhandlers import HookHandlers
from lifecycle import SessionLifecycle
from mirrorbridge import MirrorProjector
from turncontroller import TurnController, TurnState
from formatting import (
    _format_age, _short_cwd, _strip_md,
)
import session_discovery
from session_discovery import (
    _discover_projects, _live_claude_session_ids,
    _resolve_session_cwd, _session_last_active,
)
from sessions import MODE_PRESETS, SessionManager
from updater import (
    _check_update, _has_local_changes, _restart_bot, _run_update,
)
from hooks import HookBridge
from terminal_mirror import (
    TerminalMirrorManager, push_to_dtach, dtach_socket_alive,
)

# ── state ────────────────────────────────────────────────────────────

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
ui = BotUI()
turnctl = TurnController(
    state, _CLAUDE_BIN,
    default_display=DEFAULT_DISPLAY,
    icon_stopped=_ICON_STOPPED,
    fork_backfill=_FORK_BACKFILL,
)
lifecycle = SessionLifecycle(state, ui, turnctl)


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
        ui.ephemeral(chat_id, f"Rate limited. Try again in {remaining}s.",
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
            ui.ephemeral(chat_id, f"Wrong. {left} attempt(s) left.", seconds=5)
        if msg_id:
            tg.delete(msg_id, chat_id)
        return True
    _unkill_attempts.clear()
    deactivate_kill()
    audit.log("kill_switch", "deactivated via unlock word")
    if msg_id:
        tg.delete(msg_id, chat_id)
    ui.ephemeral(chat_id, "\U0001f513 Bot unlocked.", seconds=5)
    dashboard.sync()
    return True


def _do_kill():
    activate_kill()
    audit.log("kill_switch", "activated")
    for s in mgr.list_sessions():
        if s.alive:
            mgr.stop(s.sid, reason="kill switch")
    fid = forum()
    if fid:
        dashboard.sync()
        if UNLOCK_WORD:
            ui.ephemeral(fid, "\U0001f512 Bot killed. All sessions stopped.\n"
                       "Send unlock word in General to restore.",
                       seconds=15)
        else:
            ui.ephemeral(fid, "\U0001f512 Bot killed. All sessions stopped.\n"
                       "Delete .kill file on the machine to restore.",
                       seconds=15)


# ── helpers ──────────────────────────────────────────────────────────

_CLOSE_ROW = [{"text": "✕ Close", "callback_data": "close"}]

_PICKER_TTL = 60


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




def cmd_new(args: str, chat_id=None, thread_id=None):
    fid = forum()
    if not fid:
        ui.send_general("❌ Run /setup in a forum group first.")
        return
    parts = args.strip().split(None, 1)
    if parts:
        cwd = parts[0]
        name = parts[1] if len(parts) > 1 else None
        if not os.path.isdir(cwd):
            if not thread_id and chat_id:
                ui.ephemeral(chat_id, f"❌ Not a directory: <code>{tg.esc(cwd)}</code>", seconds=7)
            else:
                ui.reply(chat_id, thread_id,
                       f"❌ Not a directory: <code>{tg.esc(cwd)}</code>")
            return
        lifecycle.spawn_session(cwd, name)
        return
    projects = _discover_projects()
    if not projects:
        if not thread_id and chat_id:
            ui.ephemeral(chat_id, "❌ No projects found. Use: /new /path/to/project", seconds=7)
        else:
            ui.reply(chat_id, thread_id,
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
    mid = ui.reply(chat_id, thread_id, "\U0001f4c2 Choose project:", buttons=rows)
    if not thread_id and mid and chat_id:
        def _cleanup_picker():
            time.sleep(_PICKER_TTL)
            with state.lock:
                state.pending_project_picks.pop(pick_id, None)
            tg.delete(mid, chat_id)
        threading.Thread(target=_cleanup_picker, daemon=True).start()


def cmd_sessions(chat_id, thread_id=None):
    all_sessions = [s for s in mgr._sessions.values()
                    if s.topic_id]
    all_sessions.sort(key=lambda s: (not s.alive, -_session_last_active(s)))
    if not all_sessions:
        if not thread_id:
            ui.ephemeral(chat_id, "No sessions.", seconds=5)
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
            url = ui.topic_url(s.topic_id) if s.topic_id else None
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


_RESUME_RECENT_SECONDS = 5 * 60


def _discover_resumable_sessions(limit=10):
    bot_sids = (mgr._known_bot_sids |
                {s.claude_session_id for s in mgr._sessions.values()
                 if s.claude_session_id})
    live_terminal_sids = _live_claude_session_ids() - bot_sids
    recent_cutoff = time.time() - _RESUME_RECENT_SECONDS
    raw = []
    try:
        for d in os.listdir(session_discovery.CLAUDE_PROJECTS_DIR):
            proj_dir = os.path.join(session_discovery.CLAUDE_PROJECTS_DIR, d)
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
        url = ui.topic_url(existing.topic_id) if existing.topic_id else None
        if url:
            ui.reply(chat_id, thread_id,
                   f"ℹ️ Already active: <b>{tg.esc(existing.name)}</b>",
                   buttons=[[{"text": "Open", "url": url}]])
        else:
            ui.reply(chat_id, thread_id,
                   f"ℹ️ Already active: <b>{tg.esc(existing.name)}</b>")
        return
    if existing:
        mgr.detach_terminal(existing.sid)
    cwd = _resolve_session_cwd(claude_session_id)
    if not cwd or not os.path.isdir(cwd):
        ui.reply(chat_id, thread_id,
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
        ui.ephemeral(fid, f"❌ Failed to create topic: {tg.esc(str(e))}", seconds=7)
        return
    if not topic_id:
        ui.ephemeral(fid, "❌ Failed to create topic.", seconds=7)
        return
    with state.lock:
        state.topic_labels[topic_id] = label
    s = mgr.resume(claude_session_id, topic_id, name, cwd)
    s.topic_label = label
    ui.send_to_topic(topic_id,
                  f"▶️ <code>{tg.esc(cwd)}</code>")
    url = ui.topic_url(topic_id)
    if url:
        ui.ephemeral(fid, f"▶ {name}",
                   buttons=[[{"text": "Open", "url": url}]],
                   seconds=5)


_RESUME_RECENT_LIMIT = 4


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
        ui.send_general("❌ Run /setup in a forum group first.")
        return
    claude_session_id = args.strip()
    if claude_session_id:
        _do_resume(claude_session_id, chat_id, thread_id)
        return
    sessions = _discover_resumable_sessions()
    if not sessions:
        ui.reply(chat_id, thread_id, "No resumable sessions found.")
        return
    pick_id = str(time.time_ns())[-10:]
    with state.lock:
        state.pending_resume_picks[pick_id] = sessions
    text, rows = _build_resume_picker(sessions, pick_id,
                                      max_items=_RESUME_RECENT_LIMIT)
    mid = ui.reply(chat_id, thread_id, text, buttons=rows)
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
            ui.ephemeral(chat_id, "Use in a session topic", seconds=5)
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
                ui.ephemeral(fid, "Use in a session topic", seconds=5)
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
            ui.ephemeral(fid, "Use in a session topic", seconds=5)
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
            ui.ephemeral(chat_id, "Use in a session topic", seconds=5)
        else:
            tg.send("Use in a session topic", chat_id, thread_id=thread_id)
        return
    with state.lock:
        turn = state.turns.pop(session.sid, None)
    if turn:
        turnctl.end_turn(turn)
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
            ui.ephemeral(chat_id, "Use in a session topic", seconds=5)
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
        ui.ephemeral(chat_id, "Nothing to interrupt.",
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
    lifecycle.cancel_session_perms(session.sid, "interrupted")
    if not edited:
        # No live status to repaint (or edit failed) — fall back to ephemeral.
        ui.ephemeral(chat_id, "⏹ Turn interrupted.",
                   thread_id=thread_id, seconds=5)


def cmd_restart(chat_id, thread_id):
    if not thread_id:
        fid = forum()
        if fid:
            ui.ephemeral(fid, "Use in a session topic", seconds=5)
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
            ui.ephemeral(chat_id, text, seconds=7)
        else:
            tg.send(text, chat_id, thread_id=thread_id)

    mid = tg.send("⏳ Fetching account usage...", chat_id, thread_id=thread_id)

    in_general = not thread_id

    def _do_fetch():
        try:
            result = dashboard.fetch_account_usage()
            if result:
                text = f"<b>Account</b>\n{tg.esc(result)}"
                if in_general:
                    ui.ephemeral(chat_id, text, seconds=7)
                else:
                    tg.send(text, chat_id, thread_id=thread_id)
            else:
                if in_general:
                    ui.ephemeral(chat_id, "⚠️ Could not fetch account usage.", seconds=7)
                else:
                    tg.send("⚠️ Could not fetch account usage.", chat_id,
                            thread_id=thread_id)
        except Exception as e:
            if in_general:
                ui.ephemeral(chat_id, f"⚠️ Error: {tg.esc(str(e))}", seconds=7)
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
        ui.ephemeral(chat_id, "No audit events.", seconds=5,
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
    ui.ephemeral(chat_id, text, seconds=30, thread_id=thread_id)


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
                    ui.ephemeral(chat_id, f"Test OK! Decision: {tg.esc(dec)}",
                               seconds=7)
        except Exception as e:
            msg = f"Test failed: {tg.esc(str(e))}"
            if thread_id:
                tg.send(msg, chat_id, thread_id=thread_id)
            else:
                ui.ephemeral(chat_id, msg, seconds=7)

    threading.Thread(target=_do_test, daemon=True).start()
    if thread_id:
        tg.send("Test permission sent — click Allow/Deny.",
                chat_id, thread_id=thread_id)
    else:
        ui.ephemeral(chat_id, "Test permission sent — click Allow/Deny.",
                   seconds=10)


def cmd_update(chat_id, thread_id=None):
    mid = ui.reply(chat_id, thread_id, "⏳ Checking for updates...")

    def _do_check():
        result = _check_update()
        if mid:
            tg.delete(mid, chat_id)
        if not result:
            if not thread_id:
                ui.ephemeral(chat_id, "✅ Already up to date.", seconds=5)
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
        ui.reply(chat_id, thread_id, text, buttons=buttons)

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
                        ui.ephemeral(fid,
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

def cmd_help(chat_id, thread_id=None):
    if not thread_id:
        ui.ephemeral(chat_id, _HELP_TEXT, seconds=_PICKER_TTL)
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


mgr = SessionManager(
    on_assistant_message=turnctl.on_assistant,
    on_result=turnctl.on_result,
    on_tool_use=turnctl.on_tool_use,
    on_thinking=turnctl.on_thinking,
    on_session_stop=lifecycle.on_session_stop,
    on_session_context=turnctl.session_context,
)
turnctl.mgr = mgr
lifecycle.mgr = mgr


mirror = MirrorProjector(state, ui.topic_url, _ICON_TERMINAL)
mirror_mgr = TerminalMirrorManager(on_event=mirror.on_mirror_event)
mirror.mgr = mirror_mgr
dashboard = Dashboard(state, mirror_mgr, _validate_help, _CLAUDE_BIN)
hooks = HookHandlers(state, mgr, ui, mirror_mgr, lifecycle, _ICON_TERMINAL,
                     should_run=lambda: bot_running)


bridge = HookBridge(
    on_notification=hooks.on_hook_notification,
    on_permission=hooks.on_hook_permission,
    on_open_in_bot=mirror.on_open_in_bot,
)
lifecycle.bridge = bridge
hooks.bridge = bridge


def _dashboard_loop():
    """Background: update dashboard pin every 60s, refresh usage every 5m."""
    time.sleep(5)
    while bot_running:
        dashboard.tick()
        time.sleep(60)


def _on_topic_dead(chat_id: int, thread_id: int) -> None:
    """Callback fired by telegram.py when a write returned TOPIC_ID_INVALID.

    This replaces the old per-30s active probe: the only way the bot
    learns a topic is gone is by actually trying to send there. The send
    fails, telegram.py calls us here, we stop the session/mirror behind
    the dead topic.
    """
    # Bot session?
    sess = None
    for s in mgr.list_sessions():
        if s.topic_id == thread_id:
            sess = s
            break
    if sess and sess.alive:
        print(f"[topic_dead] session {sess.sid[:8]} — invalidating",
              file=sys.stderr, flush=True)
        try:
            lifecycle.invalidate_and_stop(sess, "topic deleted")
        except Exception as e:
            print(f"[topic_dead] stop error: {e}",
                  file=sys.stderr, flush=True)
        return
    # Mirror?
    for mirror in mirror_mgr.list():
        if mirror.topic_id == thread_id and mirror.alive:
            print(f"[topic_dead] mirror {mirror.csid[:8]} — unregistering",
                  file=sys.stderr, flush=True)
            try:
                mirror_mgr.unregister(mirror.csid)
            except Exception as e:
                print(f"[topic_dead] unregister error: {e}",
                      file=sys.stderr, flush=True)
            return


# ── 429 user-facing notice (owner DM) ───────────────────────────────

_RATE_NOTICE_LOCK = threading.Lock()
_rate_notice_msg_id: int | None = None
_rate_notice_until: float = 0.0


def _on_429_notify(retry_after_s: int) -> None:
    """Callback fired by telegram.py when a group send hit 429.

    Sends a soft "hold on" message to the owner DM (separate budget),
    but only when the wait is meaningful (>=15s) and we don't already
    have a fresh notice up. The notice is auto-deleted by a background
    cleaner once the pause window passes.
    """
    if retry_after_s < 15:
        return
    global _rate_notice_msg_id, _rate_notice_until
    with _RATE_NOTICE_LOCK:
        now = time.monotonic()
        if now < _rate_notice_until and _rate_notice_msg_id:
            # Already have an active notice — extend its life.
            _rate_notice_until = max(_rate_notice_until,
                                     now + retry_after_s + 2)
            return
        # Send fresh notice.
        try:
            mid = tg.send("⏳ Перегружено, секунду", OWNER_ID)
        except Exception:
            mid = None
        _rate_notice_msg_id = mid
        _rate_notice_until = now + retry_after_s + 2

    if mid:
        def _delete_when_stale():
            while True:
                with _RATE_NOTICE_LOCK:
                    if time.monotonic() >= _rate_notice_until:
                        target = _rate_notice_msg_id
                        break
                time.sleep(1.0)
            if target:
                try:
                    tg.delete(target, OWNER_ID)
                except Exception:
                    pass
            with _RATE_NOTICE_LOCK:
                globals()["_rate_notice_msg_id"] = None

        threading.Thread(target=_delete_when_stale, daemon=True).start()


# ── mirror dtach-socket watcher ─────────────────────────────────────


def _mirror_socket_watcher():
    """Local-only watcher: flip mirrors to output-only when their dtach
    socket vanishes. Uses no Telegram budget — only a filesystem stat."""
    while bot_running:
        time.sleep(30)
        for mirror in mirror_mgr.list():
            if not mirror.alive:
                continue
            if (mirror.dtach_socket and
                    not dtach_socket_alive(mirror.dtach_socket)):
                ui.send_to_topic(
                    mirror.topic_id,
                    "\U0001f50c Terminal closed — mirror is now output-only")
                mirror_mgr.set_dtach_socket(mirror.csid, None)


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

    # Wire telegram.py to know our group chat id (so it can route paid
    # writes through the budget) and to call us back on 429 / dead topic.
    fid = forum()
    if fid:
        tg.set_forum_chat_id(fid)
    tg.set_on_429(_on_429_notify)
    tg.set_on_topic_dead(_on_topic_dead)

    for s in mgr.list_sessions():
        if s.topic_id and s.topic_label:
            with state.lock:
                state.topic_labels[s.topic_id] = s.topic_label
    bridge.start()
    dashboard.cleanup_general()
    mirror_mgr.start_all_followers()
    # No active topic healthcheck — dead topics are detected lazily via
    # TOPIC_ID_INVALID on the next send (set_on_topic_dead callback).
    # The dtach-socket watcher only does local filesystem checks.
    threading.Thread(target=_mirror_socket_watcher, daemon=True).start()
    threading.Thread(target=_dashboard_loop, daemon=True).start()
    threading.Thread(target=hooks.terminal_watcher, daemon=True).start()
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
    dashboard.refresh_usage()
    dashboard.sync()
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
            ui.ephemeral(chat_id,
                       "\U0001f501 Mirror does not bridge files/stickers — type text",
                       thread_id=thread_id, seconds=5)
            return
        if not text:
            return
        if not mirror.dtach_socket:
            ui.ephemeral(chat_id,
                       "\U0001f501 Output-only mirror — terminal input is not bridged "
                       "(start your terminal claude inside dtach to enable it)",
                       thread_id=thread_id, seconds=8)
            return
        # Auto-reaction 👀 on mirror input removed for budget reasons.
        ok = push_to_dtach(mirror.dtach_socket, text)
        if not ok:
            # On delivery failure surface an ephemeral notice — that
            # is more visible than a small reaction and self-cleans.
            ui.ephemeral(chat_id,
                       "❌ Could not deliver to terminal "
                       "(dtach socket missing or unresponsive)",
                       thread_id=thread_id, seconds=8)
            mirror_mgr.set_dtach_socket(mirror.csid, None)
        else:
            # Track so the follower can clear pending state when claude
            # actually replies (end-to-end delivery confirmation).
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
            turnctl.enqueue_user_input(session, user_text, chat_id, msg_id, thread_id)
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
        turnctl.enqueue_user_input(session, descr, chat_id, msg_id, thread_id)
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
            turnctl.enqueue_user_input(session, text, chat_id, msg_id, thread_id)
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
                ui.ephemeral(chat_id, "Use in a session topic, or /new", seconds=5)


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
            ui.ephemeral(fid, "\U0001f44b Shutting down.", seconds=5)
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
                    lifecycle.spawn_session(projects[idx])

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
                summary = turnctl.build_summary(texts, ops)
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
                        ui.send_to_topic(topic_id,
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
                        turnctl.send_fork_summary(parent, topic_id)
                        url = ui.topic_url(topic_id)
                        if url:
                            ui.ephemeral(fid, "\U0001f500",
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
                                    mirror.welcome_text(m), cb_chat,
                                    buttons=mirror.welcome_buttons(m))
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
                                mirror.welcome_text(m_now), cb_chat,
                                buttons=mirror.welcome_buttons(m_now))
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
            hit_csid, entry = mirror.pop_pending_backfill(csid_prefix)
            m = mirror_mgr.by_csid(hit_csid) if hit_csid else None
            if entry and m and cb_chat and cb_msg:
                # Drop the prompt; the chosen mode's content takes its place.
                try:
                    tg.delete(cb_msg, cb_chat)
                except Exception:
                    pass
                mirror.start_backfill_thread(
                    m, cb_chat, mode, entry["snapshot"])
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
