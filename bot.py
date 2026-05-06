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
                    get_forum_chat_id, set_forum_chat_id,
                    get_pinned_help_id, set_pinned_help_id)
import telegram as tg
from sessions import Session, SessionManager
from hooks import HookBridge

# ── state ────────────────────────────────────────────────────────────

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
DEFAULT_DISPLAY = "mobile"
_UPLOAD_DIR = "/tmp/bot_uploads"


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


state = BotState()
bot_running = True


def forum():
    return get_forum_chat_id()


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


def _is_noisy_tool(tool, inp):
    if tool in _NOISE_TOOLS:
        return True
    if tool in ("Write", "Edit"):
        path = inp.get("file_path", "")
        if any(p in path for p in _NOISE_PATHS):
            return True
    return False


def _format_status(turn: TurnState) -> str:
    elapsed = int(time.time() - turn.started_at)
    mins, secs = divmod(elapsed, 60)
    ts = f"{mins}:{secs:02d}"
    n = len(turn.tool_ops)
    if n == 0:
        return f"⏳ {ts}"
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
    """Stop timer thread and clean up status message."""
    turn.stop_event.set()
    if turn._timer_thread:
        turn._timer_thread.join(timeout=5)
        turn._timer_thread = None
    fid = forum()
    if fid and turn.status_msg_id:
        tg.delete(turn.status_msg_id, fid)
        turn.status_msg_id = None


def _turn_timer(session, turn: TurnState):
    """Background thread: update status timer + typing indicator."""
    fid = forum()
    while not turn.stop_event.wait(3):
        mid = turn.status_msg_id
        if mid and fid:
            text = _format_status(turn)
            if text != turn._last_status_text:
                tg.edit(mid, text, fid, buttons=_interrupt_button(session))
                turn._last_status_text = text
        if fid and session.topic_id:
            tg.send_chat_action(fid, thread_id=session.topic_id)


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
             f'Summarize in 1-2 sentences, be very brief:\n{combined[:2000]}',
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

    parts = ["✅"]
    if turn.tool_ops:
        parts.append(f"⚙️ {len(turn.tool_ops)}")
    if turn.msg_texts:
        parts.append(f"\U0001f4ac {len(turn.msg_texts)}")
    if session.turn_input_tokens or session.turn_output_tokens:
        total = session.turn_input_tokens + session.turn_output_tokens
        parts.append(f"\U0001f524 {total // 1000}k" if total >= 1000
                     else f"\U0001f524 {total}")
    finish_text = "  ".join(parts)

    # Send pending images
    for img_path in list(session.pending_images):
        if os.path.isfile(img_path):
            tg.send_photo(fid, img_path, thread_id=session.topic_id)
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
        send_to_topic(session.topic_id, finish_text, buttons=btn)
    else:
        send_to_topic(session.topic_id, finish_text)

    if not session.alive:
        tg.edit_forum_topic(fid, session.topic_id, f"⏹ {session.name}")
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
            label = f"\U0001f7e2 {title}"
            tg.edit_forum_topic(fid, session.topic_id, label[:128])
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
    turn = _get_turn(session)
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
        return f"$ {cmd[:60]}" if len(cmd) <= 60 else f"$ {cmd[:57]}…"
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
        if session and session.topic_id:
            send_to_topic(session.topic_id, f"\U0001f514 {tg.esc(text)}")
        else:
            _resolve_hook_session(claude_session_id, data or {})
            session = (mgr.by_claude_session_id(claude_session_id)
                       if claude_session_id else None)
            if session and session.topic_id:
                send_to_topic(session.topic_id,
                              f"\U0001f514 {tg.esc(text)}")
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

    fid = forum()
    if not fid:
        _log("no forum chat configured")
        return None

    dirname = os.path.basename(cwd) if cwd else "terminal"
    with state.lock:
        state.topic_counter[dirname] = state.topic_counter.get(dirname, 0) + 1
        n = state.topic_counter[dirname]
    ts = time.strftime("%H:%M")
    label = (f"\U0001f517 {dirname} #{n} — {ts}" if n > 1
             else f"\U0001f517 {dirname} — {ts}")
    try:
        topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0)
        _log(f"created topic {topic_id}")
    except Exception as e:
        _log(f"create_forum_topic FAILED: {e}")
        return None
    if not topic_id:
        _log("create_forum_topic returned None")
        return None
    if claude_session_id:
        return mgr.register_terminal(claude_session_id, topic_id, cwd=cwd)
    _log(f"registered topic {topic_id} (no claude session_id)")
    sid = uuid.uuid4().hex[:8]
    session = Session(
        sid=sid, topic_id=topic_id, cwd=cwd,
        name=dirname, is_bot_spawned=False,
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
        session = _resolve_hook_session(claude_session_id, data)

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
    label = (f"\U0001f7e2 {name} #{n} — {ts}" if n > 1
             else f"\U0001f7e2 {name} — {ts}")
    try:
        topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0)
    except Exception as e:
        _ephemeral(fid, f"❌ Failed to create topic: {tg.esc(str(e))}", seconds=7)
        return
    if not topic_id:
        _ephemeral(fid, "❌ Failed to create topic. Check bot admin rights.", seconds=7)
        return
    mgr.create(cwd=cwd, name=name, topic_id=topic_id)
    send_to_topic(topic_id,
                  f"▶️ Session started\ncwd: <code>{tg.esc(cwd)}</code>")
    url = _topic_url(topic_id)
    if url:
        _ephemeral(fid, "▶️ Session created",
                   buttons=[[{"text": f"Open {name}", "url": url}]],
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
    mid = _reply(chat_id, thread_id, "\U0001f4c2 Choose project:", buttons=rows)
    if not thread_id and mid and chat_id:
        def _cleanup_picker():
            time.sleep(15)
            with state.lock:
                if pick_id in state.pending_project_picks:
                    state.pending_project_picks.pop(pick_id, None)
                    tg.delete(mid, chat_id)
        threading.Thread(target=_cleanup_picker, daemon=True).start()


def _topic_url(topic_id):
    fid = forum()
    if not fid:
        return None
    short_id = str(fid).replace("-100", "")
    return f"https://t.me/c/{short_id}/{topic_id}"


def cmd_sessions(chat_id, thread_id=None):
    all_sessions = [s for s in mgr._sessions.values()
                    if s.claude_session_id and s.topic_id]
    all_sessions.sort(key=lambda s: (not s.alive, -s.started))
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
        age = int(now - s.started)
        if age < 60:
            age_str = "just now"
        elif age < 3600:
            age_str = f"{age // 60}m ago"
        elif age < 86400:
            age_str = f"{age // 3600}h ago"
        else:
            age_str = f"{age // 86400}d ago"
        icon = "\U0001f7e2" if s.alive else "⏹"
        num = i + 1
        name = s.name if len(s.name) <= 25 else s.name[:22] + "…"
        blocks.append(
            f"<b>{num}.</b> {icon} {tg.esc(name)} · <i>{age_str}</i>"
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
    text = "\U0001f4cb <b>Sessions</b>\n\n" + "\n\n".join(blocks)
    if not thread_id:
        mid = tg.send(text, chat_id, buttons=rows)
        if mid:
            def _cleanup():
                time.sleep(15)
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


def _discover_resumable_sessions(limit=10):
    active_ids = {s.claude_session_id for s in mgr._sessions.values()
                  if s.alive and s.claude_session_id}
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
                if sid in active_ids:
                    continue
                path = os.path.join(proj_dir, f)
                mtime = os.path.getmtime(path)
                cwd = None
                first_msg = None
                try:
                    with open(path) as fh:
                        for line in fh:
                            obj = json.loads(line)
                            if not cwd:
                                cwd = obj.get("cwd")
                            if obj.get("type") == "user":
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
                                break
                except Exception:
                    continue
                if cwd and os.path.isdir(cwd):
                    raw.append((sid, cwd, first_msg, mtime))
    except Exception:
        pass
    # Dedup by (cwd, first_msg) — keep newest mtime per group
    seen: dict[tuple, tuple] = {}
    for sid, cwd, first_msg, mtime in raw:
        key = (cwd, first_msg or "")
        if key not in seen or mtime > seen[key][3]:
            seen[key] = (sid, cwd, first_msg, mtime)
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
    if existing and not existing.is_bot_spawned:
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
    label = (f"▶️ {name} #{n} — {ts}" if n > 1
             else f"▶️ {name} — {ts}")
    try:
        topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0)
    except Exception as e:
        _ephemeral(fid, f"❌ Failed to create topic: {tg.esc(str(e))}", seconds=7)
        return
    if not topic_id:
        _ephemeral(fid, "❌ Failed to create topic.", seconds=7)
        return
    mgr.resume(claude_session_id, topic_id, name, cwd)
    send_to_topic(topic_id,
                  f"▶️ Resumed session\n"
                  f"cwd: <code>{tg.esc(cwd)}</code>\n"
                  f"session: <code>{tg.esc(claude_session_id[:12])}…</code>")
    url = _topic_url(topic_id)
    if url:
        _ephemeral(fid, "▶️ Session resumed",
                   buttons=[[{"text": f"Open {name}", "url": url}]],
                   seconds=5)


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
    blocks = []
    buttons = []
    now = time.time()
    for i, (_sid, cwd, first_msg, mtime) in enumerate(sessions):
        age = now - mtime
        if age < 60:
            age_str = "just now"
        elif age < 3600:
            age_str = f"{int(age/60)}m ago"
        elif age < 86400:
            age_str = f"{int(age/3600)}h ago"
        else:
            age_str = f"{int(age/86400)}d ago"
        proj = os.path.basename(cwd.rstrip("/"))
        hint = ""
        if first_msg:
            hint = first_msg[:50].rstrip()
            if len(first_msg) > 50:
                hint += "…"
        num = i + 1
        head = f"<b>{num}.</b> {tg.esc(proj)} · <i>{age_str}</i>"
        if hint:
            blocks.append(f"{head}\n    <i>{tg.esc(hint)}</i>")
        else:
            blocks.append(head)
        btn_label = f"{num}. {proj}"
        if len(btn_label) > 25:
            btn_label = btn_label[:22] + "…"
        buttons.append({"text": btn_label,
                        "callback_data": f"r:{pick_id}:{i}"})
    rows = [buttons[j:j+3] for j in range(0, len(buttons), 3)]
    text = "▶️ <b>Resume</b>\n\n" + "\n\n".join(blocks)
    mid = _reply(chat_id, thread_id, text, buttons=rows)
    if not thread_id and mid and chat_id:
        def _cleanup():
            time.sleep(15)
            with state.lock:
                state.pending_resume_picks.pop(pick_id, None)
            tg.delete(mid, chat_id)
        threading.Thread(target=_cleanup, daemon=True).start()


def cmd_history(session, chat_id, thread_id, args):
    if not session:
        if not thread_id:
            _ephemeral(chat_id, "Send this in a session topic.", seconds=5)
        else:
            tg.send("Send this in a session topic.", chat_id, thread_id=thread_id)
        return
    n = 30
    if args.strip().isdigit():
        n = int(args.strip())
    entries = session.history[-n:]
    if not entries:
        tg.send("Empty history.", chat_id, thread_id=thread_id)
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


def cmd_display(chat_id, thread_id, args):
    if not thread_id:
        fid = forum()
        if fid:
            _ephemeral(fid, "Send this in a session topic.", seconds=5)
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
            _ephemeral(chat_id, "Send this in a session topic.", seconds=5)
        else:
            tg.send("Send this in a session topic.", chat_id, thread_id=thread_id)
        return
    with state.lock:
        turn = state.turns.pop(session.sid, None)
    if turn:
        _end_turn(turn)
    mgr.stop(session.sid)
    fid = forum()
    if fid and session.topic_id:
        tg.edit_forum_topic(fid, session.topic_id, f"⏹ {session.name}")
        tg.close_forum_topic(fid, session.topic_id)
    tg.send("⏹ Session stopped.", chat_id, thread_id=thread_id)


def cmd_interrupt(session, chat_id, thread_id):
    if not session:
        if not thread_id:
            _ephemeral(chat_id, "Send this in a session topic.", seconds=5)
        else:
            tg.send("Send this in a session topic.", chat_id, thread_id=thread_id)
        return
    if not session.is_bot_spawned:
        tg.send("ℹ️ Terminal session — interrupt from terminal (Ctrl-C).",
                chat_id, thread_id=thread_id)
        return
    _do_interrupt(session, chat_id, thread_id)


def _do_interrupt(session, chat_id, thread_id):
    if not mgr.interrupt(session.sid):
        _ephemeral(chat_id, "Nothing to interrupt.",
                   thread_id=thread_id, seconds=5)
        return
    _cancel_session_perms(session.sid, "interrupted")
    _ephemeral(chat_id, "⏹ Turn interrupted.",
               thread_id=thread_id, seconds=5)


def cmd_restart(chat_id, thread_id):
    if not thread_id:
        fid = forum()
        if fid:
            _ephemeral(fid, "Send this in a session topic.", seconds=5)
        return
    session = mgr.by_topic(thread_id)
    if not session:
        tg.send("No session in this topic.", chat_id, thread_id=thread_id)
        return
    if session.alive:
        tg.send("Session is already running.", chat_id, thread_id=thread_id)
        return
    if mgr.restart(session.sid):
        fid = forum()
        if fid:
            tg.reopen_forum_topic(fid, session.topic_id)
            tg.edit_forum_topic(fid, session.topic_id,
                                f"\U0001f7e2 {session.name}")
        tg.send("▶️ Session restarted.", chat_id,
                thread_id=thread_id)
    else:
        tg.send("❌ Failed to restart.", chat_id, thread_id=thread_id)


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
                    "permissionDecision", "?")
                tg.send(
                    f"✅ Test OK! Decision: <b>{tg.esc(dec)}</b>",
                    chat_id, thread_id=thread_id)
        except Exception as e:
            tg.send(
                f"❌ Test failed: <code>{tg.esc(str(e))}</code>",
                chat_id, thread_id=thread_id)

    threading.Thread(target=_do_test, daemon=True).start()
    tg.send("\U0001f9ea Test permission sent — click Allow/Deny "
            "when buttons appear.", chat_id, thread_id=thread_id)


_PINNED_TEXT = (
    "<b>ClaudeLaude Bot</b>\n"
    "Claude Code → Telegram\n"
    "\n"
    "/new — new session\n"
    "/sessions — list + fork\n"
    "/resume — continue session\n"
    "/usage — tokens + limits"
)

_HELP_TEXT = (
    "<b>ClaudeLaude Bot — Help</b>\n"
    "\n"
    "<b>Sessions</b>\n"
    "/new — project picker\n"
    "/new &lt;path&gt; [name] — session in dir\n"
    "/sessions — list sessions + fork\n"
    "/resume — pick session to continue\n"
    "/resume &lt;id&gt; — continue by session id\n"
    "/stop — stop current session\n"
    "/restart — restart stopped session\n"
    "/interrupt — abort current turn (session stays alive)\n"
    "\n"
    "<b>In topic</b>\n"
    "/history [N] — last N events (default 30)\n"
    "/usage — session tokens + account limits\n"
    "/display [mobile|desktop] — toggle view\n"
    "\n"
    "<b>Other</b>\n"
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
    "/interrupt", "/restart", "/usage", "/display", "/test_perm",
    "/stop_bot", "/help", "/start", "/menu",
}

_HELP_DOCUMENTED_COMMANDS = {
    "/new", "/sessions", "/resume", "/stop", "/interrupt", "/restart",
    "/history", "/usage", "/display",
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
        _ephemeral(chat_id, _HELP_TEXT, seconds=7)
    else:
        tg.send(_HELP_TEXT, chat_id, thread_id=thread_id)


_HIDDEN_COMMANDS = {"/setup", "/test_perm", "/start"}


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


def _sync_pinned_help():
    """Send or update the pinned help+menu message in General."""
    _validate_help()
    fid = forum()
    if not fid:
        return
    old_id = get_pinned_help_id()
    if old_id:
        try:
            tg._req("editMessageText", {
                "chat_id": fid,
                "message_id": old_id,
                "text": _PINNED_TEXT,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": _MENU_ROWS},
            })
        except Exception as e:
            body = ""
            if hasattr(e, "response") and e.response is not None:
                try:
                    body = e.response.text
                except Exception:
                    pass
            if "not modified" in body.lower():
                return
            print(f"[pinned] edit failed: {e} {body}",
                  file=sys.stderr, flush=True)
            set_pinned_help_id(None)
        else:
            return
    # Convention: General has exactly one pinned message (this help).
    # Clear everything before pinning the fresh one.
    try:
        tg._req("unpinAllChatMessages", {"chat_id": fid})
    except Exception:
        pass
    msg_id = tg.send(_PINNED_TEXT, fid, buttons=_MENU_ROWS)
    if msg_id:
        tg.pin(msg_id, fid)
        set_pinned_help_id(msg_id)


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
    mid = tg.send("\U0001f3ae <b>Quick actions</b>", chat_id,
                   thread_id=thread_id, buttons=rows)
    if not thread_id and mid:
        def _cleanup():
            time.sleep(15)
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
)
bridge = HookBridge(
    on_notification=on_hook_notification,
    on_permission=on_hook_permission,
)


def _topic_healthcheck():
    """Periodically check that session topics still exist; stop orphans."""
    while bot_running:
        time.sleep(30)
        fid = forum()
        if not fid:
            continue
        for session in mgr.list_sessions():
            if not session.topic_id:
                continue
            if not tg.topic_alive(fid, session.topic_id):
                _invalidate_and_stop(session, "topic deleted")


def main():
    global bot_running

    bridge.start()
    threading.Thread(target=_topic_healthcheck, daemon=True).start()
    tg.set_my_commands([
        {"command": "new", "description": "New Claude session"},
        {"command": "sessions", "description": "Active sessions"},
        {"command": "menu", "description": "Quick actions"},
        {"command": "stop", "description": "Stop session"},
        {"command": "restart", "description": "Restart stopped session"},
        {"command": "history", "description": "Last N events"},
        {"command": "usage", "description": "Token usage"},
        {"command": "display", "description": "Toggle mobile/desktop"},
        {"command": "help", "description": "Show help"},
    ])
    _sync_pinned_help()
    _cleanup_general()

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


def _handle_update(u):
    global bot_running

    cb = u.get("callback_query")
    if cb:
        tg.answer_callback(cb["id"])
        _handle_callback(cb, cb.get("data", ""))
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

    if from_user != OWNER_ID:
        return

    thread_id = msg.get("message_thread_id")
    msg_id = msg.get("message_id")
    session = mgr.by_topic(thread_id) if thread_id else None

    fid = forum()
    if not thread_id and fid and chat_id == fid and msg_id:
        tg.delete(msg_id, chat_id)

    # Handle photo/document attachments
    photos = msg.get("photo")
    document = msg.get("document")
    if photos or document:
        if not (session and session.is_bot_spawned and session.alive):
            tg.send("Send files in an active bot session topic.",
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
            if not mgr.send_user_message(session.sid, user_text):
                tg.send("⚠️ Session died.", chat_id,
                        thread_id=thread_id)
        else:
            tg.send("❌ Failed to download file.", chat_id,
                    thread_id=thread_id)
        return

    if not text:
        return

    if text.startswith("/"):
        cmd, _, args = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        _handle_command(cmd, args, chat_id, thread_id, session)
    else:
        if session and session.is_bot_spawned:
            ok = mgr.send_user_message(session.sid, text)
            if not ok:
                tg.send("⚠️ Session died.", chat_id,
                        thread_id=thread_id)
        elif session:
            tg.send("ℹ️ Terminal session — "
                    "send messages from terminal.",
                    chat_id, thread_id=thread_id)
        elif chat_id == OWNER_ID:
            tg.send("Send in a session topic, or /new to create one.",
                    chat_id)
        else:
            fid = forum()
            if fid and chat_id == fid:
                _ephemeral(chat_id, "Send in a session topic, or /new to create one.", seconds=5)


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
    elif cmd == "/test_perm":
        cmd_test_perm(chat_id, thread_id)
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
                tg.send("⚠️ Session died.", chat_id,
                        thread_id=thread_id)
        elif session:
            tg.send("ℹ️ Terminal session — "
                    "send messages from terminal.",
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
            if entry:
                msg_id, perm_chat, _ = entry
                mark = "✅" if decision == "allow" else "❌"
                tg.edit(msg_id, f"{mark} done", perm_chat)

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
            btn = [[{"text": "\U0001f5dc Compact",
                      "callback_data": f"c:{compact_id}"}]]
            if cb_msg:
                tg.edit(cb_msg, "✅ done", cb_chat, buttons=btn)
            new_ids = []
            for t in texts:
                new_ids.extend(
                    tg.send_long(t, cb_chat, thread_id=cb_thread,
                                 markdown=True))
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
                label = f"\U0001f500 {parent.name} fork — {ts}"
                topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0)
                if topic_id:
                    session = mgr.fork(parent, topic_id, parent.name)
                    if session:
                        send_to_topic(topic_id,
                                      f"\U0001f500 Forked from <b>{tg.esc(parent.name)}</b>\n"
                                      f"cwd: <code>{tg.esc(parent.cwd)}</code>")
                        _send_fork_summary(parent, topic_id)
                        url = _topic_url(topic_id)
                        if url:
                            _ephemeral(fid, "\U0001f500 Fork created",
                                       buttons=[[{"text": f"Open {parent.name} fork", "url": url}]],
                                       seconds=5)

    elif data.startswith("m:"):
        action = data[2:]
        session = mgr.by_topic(cb_thread) if cb_thread else None
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
