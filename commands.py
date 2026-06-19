"""Telegram command handlers — the controller layer for /new, /sessions,
/resume, /stop, /mode, /usage, /update, /menu and friends.

Sits at the top of the dependency stack: a component with state, the
SessionManager, the BotUI helper, the Dashboard, the TurnController and
the SessionLifecycle injected, plus a few config literals (display
default, topic icons). bot.py's dispatch (`_handle_command`,
`_handle_callback`) routes into the `cmd_*` / `_do_resume` /
`_build_resume_picker` / `_do_interrupt` methods. Lower-layer helpers
(session discovery, update checks, formatting) and the Telegram client
are imported directly — tests fake Telegram at the `telegram._req`
layer.
"""

import json
import os
import sys
import threading
import time
import urllib.request
import uuid

import audit
import session_discovery
import telegram as tg
from botui import CLOSE_ROW
from branding import PRODUCT_NAME
from config import (HOOK_PORT, get_forum_chat_id, get_help_msg_id,
                    set_forum_chat_id, set_help_msg_id)
from formatting import (_format_age, _short_cwd, _strip_md,
                        topic_control_rows)
from lifecycle import create_tracked_topic, make_topic_label
from session_discovery import (_discover_projects, _live_claude_session_ids,
                               _resolve_session_cwd, _session_last_active)
from sessions import MODE_PRESETS
from updater import _check_update, _has_local_changes

_PICKER_TTL = 60
_RESUME_RECENT_SECONDS = 5 * 60
_RESUME_RECENT_LIMIT = 4

# ── /help as an interactive reference ───────────────────────────────
# A menu of category buttons; each opens a section that mixes commands with
# the no-command processes for that area. `hr:` callbacks (dispatched in
# bot.py) flip between the menu and a section, both editing one message.

_HELP_INTRO = (
    f"<b>{PRODUCT_NAME} — Reference</b>\n\n"
    "Tap a topic to explore:"
)

# Ordered: (key, button label, section body). Bodies are mobile-width.
_HELP_SECTIONS: list[tuple[str, str, str]] = [
    ("sessions", "\U0001f4c1 Sessions",
     "<b>\U0001f4c1 Sessions</b>\n\n"
     "A session is one Claude Code run,\n"
     "shown as a forum topic.\n\n"
     "/new — pick a project, open a topic\n"
     "/new &lt;path&gt; [name] — in a set dir\n"
     "/sessions — your bot sessions,\n"
     "  running or stopped; tap to fork\n"
     "/resume — pull in a Claude session\n"
     "  from outside the bot (e.g. one you\n"
     "  ran in a terminal)\n"
     "/resume &lt;id&gt; — by session id\n"
     "/stop — stop the one in this topic\n"
     "/restart — restart a stopped one\n"
     "/interrupt — abort the current turn\n\n"
     "Topics rename themselves from the\n"
     "conversation."),
    ("modes", "\U0001f3a8 Modes",
     "<b>\U0001f3a8 Modes</b>\n\n"
     "Set how Claude answers, per session.\n\n"
     "/mode — show current / list all\n"
     "/mode &lt;name&gt; — switch\n\n"
     "default · normal\n"
     "terse · short answers\n"
     "verbose · full reasoning\n"
     "beginner · explains as it goes\n"
     "plan · plans first, no changes\n"
     "burn · maximum effort\n\n"
     "Also one tap from a topic's panel."),
    ("media", "\U0001f4ce Send media",
     "<b>\U0001f4ce Send media</b> (no command)\n\n"
     "Just send it into a session topic:\n"
     "• Photos &amp; albums\n"
     "• Voice messages → transcribed\n"
     "• Videos &amp; video messages →\n"
     "  transcript + scene frames\n"
     "• Stickers\n"
     "• Files &amp; documents\n\n"
     "Claude receives it as an attachment.\n"
     "React to a reply with an emoji and\n"
     "Claude sees the reaction.\n\n"
     "Voice/video transcription is opt-in\n"
     "(installed on first use or via\n"
     "/settings)."),
    ("control", "\U0001f518 Control",
     "<b>\U0001f518 Control &amp; questions</b>\n\n"
     "Each topic has a pinned panel:\n"
     "Mode · Display · Stop · Usage ·\n"
     "History (Restart when stopped).\n\n"
     "/display [mobile|desktop] — layout\n"
     "/usage — context %, tokens, cost\n"
     "/history [N] — last N events\n\n"
     "When Claude asks a question, you\n"
     "answer with inline buttons.\n"
     "Unknown /commands in a session are\n"
     "forwarded to Claude as-is."),
    ("mirror", "\U0001f517 Mirror",
     "<b>\U0001f517 Mirror a terminal</b>\n\n"
     "Bring a terminal Claude session into\n"
     "Telegram.\n\n"
     "Run /bot-mirror inside a terminal\n"
     "Claude session: its output streams to\n"
     "a topic and you can type back from\n"
     "your phone.\n\n"
     "If the terminal closes, continue the\n"
     "session as a bot session with one tap."),
    ("safety", "⚙️ Settings & safety",
     "<b>⚙️ Settings &amp; safety</b>\n\n"
     "/settings — whisper model, media\n"
     "  cleanup TTL, storage alerts\n"
     "/update — check for bot updates\n"
     "/stop_bot — shut the bot down\n\n"
     "\U0001f512 /kill freezes the bot and stops\n"
     "every session. Unlock is one-way\n"
     "(needs your secret word), with\n"
     "brute-force protection. Every action\n"
     "is written to an audit log."),
    ("commands", "\U0001f4cb All commands",
     "<b>\U0001f4cb All commands</b>\n\n"
     "/tour — guided walkthrough\n"
     "/new — new session (or picker)\n"
     "/sessions — list + fork\n"
     "/resume — adopt a terminal session\n"
     "/stop · /restart · /interrupt\n"
     "/mode — response style\n"
     "/display — mobile/desktop\n"
     "/usage — tokens + limits\n"
     "/history [N] — last N events\n"
     "/settings — bot settings\n"
     "/update — check updates\n"
     "/menu — quick actions\n"
     "/help — this reference\n"
     "/stop_bot — shut down"),
]

_HELP_BODY = {key: body for key, _label, body in _HELP_SECTIONS}


def _help_menu_rows():
    cats = [{"text": label, "callback_data": f"hr:cat:{key}"}
            for key, label, _body in _HELP_SECTIONS]
    # Two per row, except the last (All commands) gets its own row.
    rows = [cats[i:i + 2] for i in range(0, len(cats) - 1, 2)]
    rows.append([cats[-1]])
    rows.append([{"text": "✕ Close", "callback_data": "hr:close"}])
    return rows


def _help_section_rows():
    return [[{"text": "◀ Back", "callback_data": "hr:menu"}],
            [{"text": "✕ Close", "callback_data": "hr:close"}]]


class Commands:
    def __init__(self, state, mgr, ui, dashboard, turnctl, lifecycle, *,
                 default_display, icon_stopped, icon_active):
        self.state = state
        self.mgr = mgr
        self.ui = ui
        self.dashboard = dashboard
        self.turnctl = turnctl
        self.lifecycle = lifecycle
        self._default_display = default_display
        self._icon_stopped = icon_stopped
        self._icon_active = icon_active

    def cmd_setup(self, chat_id):
        try:
            r = tg._req("getChat", {"chat_id": chat_id})
            is_forum = r.get("result", {}).get("is_forum", False)
        except Exception:
            is_forum = False
        if not is_forum:
            tg.send("❌ Enable Topics in this group first.", chat_id)
            return
        set_forum_chat_id(chat_id)
        tg.send("✅ Forum linked. A quick tour is below — "
                "or /new to jump straight in.", chat_id)

    def cmd_new(self, args: str, chat_id=None, thread_id=None):
        fid = get_forum_chat_id()
        if not fid:
            self.ui.send_general("❌ Run /setup in a forum group first.")
            return
        parts = args.strip().split(None, 1)
        if parts:
            cwd = parts[0]
            name = parts[1] if len(parts) > 1 else None
            if not os.path.isdir(cwd):
                if not thread_id and chat_id:
                    self.ui.ephemeral(chat_id, f"❌ Not a directory: <code>{tg.esc(cwd)}</code>", seconds=7)
                else:
                    self.ui.reply(chat_id, thread_id,
                                  f"❌ Not a directory: <code>{tg.esc(cwd)}</code>")
                return
            self.lifecycle.spawn_session(cwd, name)
            return
        projects = _discover_projects()
        if not projects:
            if not thread_id and chat_id:
                self.ui.ephemeral(chat_id, "❌ No projects found. Use: /new /path/to/project", seconds=7)
            else:
                self.ui.reply(chat_id, thread_id,
                              "❌ No projects found. Use: /new /path/to/project")
            return
        pick_id = str(time.time_ns())[-10:]
        with self.state.lock:
            self.state.pending_project_picks[pick_id] = projects
        RECENT_LIMIT = 4
        rows = []
        for i, p in enumerate(projects[:RECENT_LIMIT]):
            label = os.path.basename(p.rstrip("/"))
            rows.append([{"text": label, "callback_data": f"n:{pick_id}:{i}"}])
        if len(projects) > RECENT_LIMIT:
            rows.append([{"text": f"\U0001f4cb Show all ({len(projects)})",
                          "callback_data": f"na:{pick_id}"}])
        rows.append(CLOSE_ROW)
        mid = self.ui.reply(chat_id, thread_id, "\U0001f4c2 Choose project:", buttons=rows)
        if not thread_id and mid and chat_id:
            def _expire():
                with self.state.lock:
                    self.state.pending_project_picks.pop(pick_id, None)
            self.ui.delete_after(mid, chat_id, _PICKER_TTL, before_delete=_expire)

    def cmd_sessions(self, chat_id, thread_id=None):
        all_sessions = [s for s in self.mgr._sessions.values()
                        if s.topic_id]
        all_sessions.sort(key=lambda s: (not s.alive, -_session_last_active(s)))
        if not all_sessions:
            if not thread_id:
                self.ui.ephemeral(chat_id, "No sessions.", seconds=5)
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
                url = self.ui.topic_url(s.topic_id) if s.topic_id else None
                if url:
                    buttons.append({"text": btn_label, "url": url})
                else:
                    buttons.append({"text": btn_label,
                                    "callback_data": "noop"})
        rows = [buttons[j:j+3] for j in range(0, len(buttons), 3)]
        rows.append(CLOSE_ROW)
        text = "\U0001f4cb <b>Sessions</b>\n\n" + "\n\n".join(blocks)
        if not thread_id:
            mid = tg.send(text, chat_id, buttons=rows)
            self.ui.delete_after(mid, chat_id, _PICKER_TTL)
        else:
            tg.send(text, chat_id, thread_id=thread_id, buttons=rows)

    def _discover_resumable_sessions(self, limit=10):
        bot_sids = (self.mgr._known_bot_sids |
                    {s.claude_session_id for s in self.mgr._sessions.values()
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

    def _do_resume(self, claude_session_id: str, chat_id, thread_id=None):
        fid = get_forum_chat_id()
        if not fid:
            return
        existing = self.mgr.by_claude_session_id(claude_session_id)
        if existing and existing.alive and existing.is_bot_spawned:
            url = self.ui.topic_url(existing.topic_id) if existing.topic_id else None
            if url:
                self.ui.reply(chat_id, thread_id,
                              f"ℹ️ Already active: <b>{tg.esc(existing.name)}</b>",
                              buttons=[[{"text": "Open", "url": url}]])
            else:
                self.ui.reply(chat_id, thread_id,
                              f"ℹ️ Already active: <b>{tg.esc(existing.name)}</b>")
            return
        if existing:
            self.mgr.detach_terminal(existing.sid)
        cwd = _resolve_session_cwd(claude_session_id)
        if not cwd or not os.path.isdir(cwd):
            self.ui.reply(chat_id, thread_id,
                          f"❌ Can't find cwd for session <code>{tg.esc(claude_session_id[:12])}…</code>")
            return
        name = os.path.basename(cwd.rstrip("/"))
        label = make_topic_label(self.state, name)
        try:
            topic_id = create_tracked_topic(self.state, fid, label)
        except Exception as e:
            self.ui.ephemeral(fid, f"❌ Failed to create topic: {tg.esc(str(e))}", seconds=7)
            return
        if not topic_id:
            self.ui.ephemeral(fid, "❌ Failed to create topic.", seconds=7)
            return
        s = self.mgr.resume(claude_session_id, topic_id, name, cwd)
        s.topic_label = label
        self.lifecycle.attach_controls(s)
        url = self.ui.topic_url(topic_id)
        if url:
            self.ui.ephemeral(fid, f"▶️ {name}",
                              buttons=[[{"text": "Open", "url": url}]],
                              seconds=5)

    def _build_resume_picker(self, sessions, pick_id, max_items=None):
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
        rows.append(CLOSE_ROW)
        text = "▶️ <b>Resume</b>\n\n" + "\n\n".join(blocks)
        return text, rows

    def cmd_resume(self, args: str, chat_id, thread_id=None):
        fid = get_forum_chat_id()
        if not fid:
            self.ui.send_general("❌ Run /setup in a forum group first.")
            return
        claude_session_id = args.strip()
        if claude_session_id:
            self._do_resume(claude_session_id, chat_id, thread_id)
            return
        sessions = self._discover_resumable_sessions()
        if not sessions:
            # In General the reply must self-clean (General stays clean —
            # only the pinned dashboard persists); in a topic it can stay.
            if not thread_id and chat_id:
                self.ui.ephemeral(chat_id, "No resumable sessions found.",
                                  seconds=7)
            else:
                self.ui.reply(chat_id, thread_id,
                              "No resumable sessions found.")
            return
        pick_id = str(time.time_ns())[-10:]
        with self.state.lock:
            self.state.pending_resume_picks[pick_id] = sessions
        text, rows = self._build_resume_picker(sessions, pick_id,
                                               max_items=_RESUME_RECENT_LIMIT)
        mid = self.ui.reply(chat_id, thread_id, text, buttons=rows)
        if not thread_id and mid and chat_id:
            def _expire():
                with self.state.lock:
                    self.state.pending_resume_picks.pop(pick_id, None)
            self.ui.delete_after(mid, chat_id, _PICKER_TTL, before_delete=_expire)

    def cmd_history(self, session, chat_id, thread_id, args):
        if not session:
            if not thread_id:
                self.ui.ephemeral(chat_id, "Use in a session topic", seconds=5)
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

    def cmd_mode(self, session, chat_id, thread_id, args):
        if not session:
            if thread_id:
                tg.send("Use in a session topic", chat_id, thread_id=thread_id)
            else:
                fid = get_forum_chat_id()
                if fid:
                    self.ui.ephemeral(fid, "Use in a session topic", seconds=5)
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
        if not self.mgr.set_mode(session.sid, name):
            valid = ", ".join(MODE_PRESETS.keys())
            tg.send(f"Unknown mode: <code>{tg.esc(name)}</code>\nAvailable: {valid}",
                    chat_id, thread_id=thread_id)
            return
        preset = MODE_PRESETS[name]
        tg.send(f"\U0001f3af Mode: <b>{tg.esc(name)}</b> — {tg.esc(preset['label'])}",
                chat_id, thread_id=thread_id)

    def cmd_display(self, chat_id, thread_id, args):
        if not thread_id:
            fid = get_forum_chat_id()
            if fid:
                self.ui.ephemeral(fid, "Use in a session topic", seconds=5)
            return
        mode = args.strip().lower()
        if mode not in ("mobile", "desktop"):
            with self.state.lock:
                current = self.state.topic_display_mode.get(thread_id, self._default_display)
            mode = "desktop" if current == "mobile" else "mobile"
        with self.state.lock:
            self.state.topic_display_mode[thread_id] = mode
        icon = "\U0001f4f1" if mode == "mobile" else "\U0001f5a5"
        session = self.mgr.by_topic(thread_id)
        if session:
            self.render_topic_controls(session)
        tg.send(f"{icon} Display: <b>{mode}</b>", chat_id, thread_id=thread_id)

    def cmd_stop(self, session, chat_id, thread_id):
        if not session:
            if not thread_id:
                self.ui.ephemeral(chat_id, "Use in a session topic", seconds=5)
            else:
                tg.send("Use in a session topic", chat_id, thread_id=thread_id)
            return
        with self.state.lock:
            turn = self.state.turns.pop(session.sid, None)
        if turn:
            self.turnctl.end_turn(turn)
        self.mgr.stop(session.sid)
        audit.log("session_stop", "user stop", sid=session.sid)
        fid = get_forum_chat_id()
        if fid and session.topic_id:
            stop_label = session.name
            tg.edit_forum_topic(fid, session.topic_id, stop_label,
                                icon_custom_emoji_id=self._icon_stopped)
            with self.state.lock:
                self.state.topic_labels[session.topic_id] = stop_label
            session.topic_label = stop_label
            tg.close_forum_topic(fid, session.topic_id)
        self.render_topic_controls(session)
        tg.send("⏹ Stopped", chat_id, thread_id=thread_id)

    def cmd_interrupt(self, session, chat_id, thread_id):
        if not session:
            if not thread_id:
                self.ui.ephemeral(chat_id, "Use in a session topic", seconds=5)
            else:
                tg.send("Use in a session topic", chat_id, thread_id=thread_id)
            return
        if not session.is_bot_spawned:
            tg.send("ℹ️ Terminal — Ctrl-C in terminal",
                    chat_id, thread_id=thread_id)
            return
        self._do_interrupt(session, chat_id, thread_id)

    def _do_interrupt(self, session, chat_id, thread_id):
        if not self.mgr.interrupt(session.sid):
            self.ui.ephemeral(chat_id, "Nothing to interrupt.",
                              thread_id=thread_id, seconds=5)
            return
        edited = False
        with self.state.lock:
            turn = self.state.turns.get(session.sid)
        if turn:
            turn.interrupted = True
            fid = get_forum_chat_id()
            if fid and turn.status_msg_id:
                try:
                    tg.edit(turn.status_msg_id, "⏹ Interrupted", fid)
                    turn._last_status_text = "⏹ Interrupted"
                    edited = True
                except Exception as e:
                    print(f"[interrupt] status edit failed: {e}",
                          file=sys.stderr, flush=True)
        self.lifecycle.cancel_session_perms(session.sid, "interrupted")
        if not edited:
            # No live status to repaint (or edit failed) — fall back to ephemeral.
            self.ui.ephemeral(chat_id, "⏹ Turn interrupted.",
                              thread_id=thread_id, seconds=5)

    def cmd_restart(self, chat_id, thread_id):
        if not thread_id:
            fid = get_forum_chat_id()
            if fid:
                self.ui.ephemeral(fid, "Use in a session topic", seconds=5)
            return
        session = self.mgr.by_topic(thread_id)
        if not session:
            tg.send("No session here", chat_id, thread_id=thread_id)
            return
        if session.alive:
            tg.send("Already running", chat_id, thread_id=thread_id)
            return
        if self.mgr.restart(session.sid):
            fid = get_forum_chat_id()
            if fid:
                tg.reopen_forum_topic(fid, session.topic_id)
                tg.edit_forum_topic(fid, session.topic_id, session.name,
                                    icon_custom_emoji_id=self._icon_active)
                with self.state.lock:
                    self.state.topic_labels[session.topic_id] = session.name
                session.topic_label = session.name
            self.render_topic_controls(session)
            tg.send("▶️ Restarted", chat_id,
                    thread_id=thread_id)
        else:
            tg.send("❌ Failed to restart", chat_id, thread_id=thread_id)

    def cmd_usage(self, session, chat_id, thread_id):
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
                self.ui.ephemeral(chat_id, text, seconds=7)
            else:
                tg.send(text, chat_id, thread_id=thread_id)

        mid = tg.send("⏳ Fetching account usage...", chat_id, thread_id=thread_id)

        in_general = not thread_id

        def _do_fetch():
            try:
                result = self.dashboard.fetch_account_usage()
                if result:
                    text = f"<b>Account</b>\n{tg.esc(result)}"
                    if in_general:
                        self.ui.ephemeral(chat_id, text, seconds=7)
                    else:
                        tg.send(text, chat_id, thread_id=thread_id)
                else:
                    if in_general:
                        self.ui.ephemeral(chat_id, "⚠️ Could not fetch account usage.", seconds=7)
                    else:
                        tg.send("⚠️ Could not fetch account usage.", chat_id,
                                thread_id=thread_id)
            except Exception as e:
                if in_general:
                    self.ui.ephemeral(chat_id, f"⚠️ Error: {tg.esc(str(e))}", seconds=7)
                else:
                    tg.send(f"⚠️ Error: {tg.esc(str(e))}", chat_id,
                            thread_id=thread_id)
            finally:
                if mid:
                    tg.delete(mid, chat_id)

        threading.Thread(target=_do_fetch, daemon=True).start()

    def cmd_audit(self, args, chat_id, thread_id=None):
        n = 20
        if args.strip().isdigit():
            n = min(int(args.strip()), 100)
        entries = audit.tail(n)
        if not entries:
            self.ui.ephemeral(chat_id, "No audit events.", seconds=5,
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
        self.ui.ephemeral(chat_id, text, seconds=30, thread_id=thread_id)

    def cmd_test_perm(self, chat_id, thread_id=None):
        """Simulate a permission request to test the hook flow end-to-end."""
        def _do_test():
            payload = json.dumps({
                "hook_event_name": "PermissionRequest",
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
                        self.ui.ephemeral(chat_id, f"Test OK! Decision: {tg.esc(dec)}",
                                          seconds=7)
            except Exception as e:
                msg = f"Test failed: {tg.esc(str(e))}"
                if thread_id:
                    tg.send(msg, chat_id, thread_id=thread_id)
                else:
                    self.ui.ephemeral(chat_id, msg, seconds=7)

        threading.Thread(target=_do_test, daemon=True).start()
        if thread_id:
            tg.send("Test permission sent — click Allow/Deny.",
                    chat_id, thread_id=thread_id)
        else:
            self.ui.ephemeral(chat_id, "Test permission sent — click Allow/Deny.",
                              seconds=10)

    def cmd_update(self, chat_id, thread_id=None):
        mid = self.ui.reply(chat_id, thread_id, "⏳ Checking for updates...")

        def _do_check():
            result = _check_update()
            if mid:
                tg.delete(mid, chat_id)
            if not result:
                if not thread_id:
                    self.ui.ephemeral(chat_id, "✅ Already up to date.", seconds=5)
                else:
                    tg.send("✅ Already up to date.", chat_id,
                            thread_id=thread_id)
                return

            current, latest, changelog = result
            modified = _has_local_changes()

            text = f"<b>Update available</b>\n{tg.esc(current)} → <b>{tg.esc(latest)}</b>"
            if changelog:
                body = changelog.strip()
                if len(body) > 1500:
                    body = body[:1500].rstrip() + "\n…"
                text += f"\n\n{tg.esc(body)}"
            if modified:
                text += f"\n\n⚠️ {len(modified)} locally modified file(s):"
                for f in modified[:5]:
                    text += f"\n  <code>{tg.esc(f)}</code>"
                if len(modified) > 5:
                    text += f"\n  … and {len(modified) - 5} more"

            buttons = [[
                {"text": "⬆️ Update now", "callback_data": "upd:go"},
                {"text": "✗ Cancel", "callback_data": "upd:no"},
            ]]
            self.ui.reply(chat_id, thread_id, text, buttons=buttons)

        threading.Thread(target=_do_check, daemon=True).start()

    def cmd_help(self, chat_id, thread_id=None):
        """Open the interactive reference (menu of category buttons)."""
        if thread_id:
            tg.send(_HELP_INTRO, chat_id, thread_id=thread_id,
                    buttons=_help_menu_rows())
            return
        # General: keep exactly one help message, dismissed via Close. It
        # carries no delete timer, so the General sweep leaves it alone.
        old = get_help_msg_id()
        if old:
            tg.delete(old, chat_id)
        # persist=True: onboarding reference, dismissed by Close — opts out of
        # the General auto-reap backstop.
        mid = tg.send(_HELP_INTRO, chat_id, buttons=_help_menu_rows(),
                      persist=True)
        set_help_msg_id(mid)

    def render_help_menu(self, msg_id, chat_id):
        tg.edit(msg_id, _HELP_INTRO, chat_id, buttons=_help_menu_rows())

    def render_help_section(self, msg_id, chat_id, key):
        body = _HELP_BODY.get(key)
        if not body:
            return
        tg.edit(msg_id, body, chat_id, buttons=_help_section_rows())

    def close_help(self, msg_id, chat_id):
        tg.delete(msg_id, chat_id)
        if get_help_msg_id() == msg_id:
            set_help_msg_id(None)

    # ── per-topic control panel (pinned opening message) ─────────────

    def render_topic_controls(self, session):
        """Repaint the pinned control panel after a state change.

        Stop/restart flips the button set (alive ↔ Restart); display/mode
        only changes a label. Text stays the cwd banner so the pin reads
        the same. `editMessageText` drops the keyboard unless reply_markup
        is re-passed, so the rows go on every edit (tg-edit-buttons).
        """
        if not session or not session.controls_msg_id:
            return
        fid = get_forum_chat_id()
        if not fid:
            return
        with self.state.lock:
            display = self.state.topic_display_mode.get(
                session.topic_id, self._default_display)
        rows = topic_control_rows(session.alive, display)
        try:
            tg.edit(session.controls_msg_id,
                    f"▶️ <code>{tg.esc(session.cwd)}</code>",
                    fid, buttons=rows)
        except Exception as e:
            print(f"[controls] repaint failed: {e}", file=sys.stderr, flush=True)

    def show_mode_picker(self, session, chat_id):
        """Expand the control panel into a mode-preset picker in place."""
        if not session or not session.controls_msg_id:
            return
        rows = []
        for key, preset in MODE_PRESETS.items():
            marker = "• " if key == session.mode else ""
            rows.append([{"text": f"{marker}{key} — {preset['label']}",
                          "callback_data": f"m:mode:{key}"}])
        rows.append([{"text": "◀ Back", "callback_data": "m:controls"}])
        tg.edit(session.controls_msg_id, "🎯 <b>Choose mode:</b>",
                chat_id, buttons=rows)

    def cmd_menu(self, chat_id, thread_id=None, session=None):
        rows = [
            [{"text": "\U0001f195 New session", "callback_data": "m:new"},
             {"text": "\U0001f4cb Sessions", "callback_data": "m:sessions"}],
        ]
        if session:
            with self.state.lock:
                mode = self.state.topic_display_mode.get(
                    session.topic_id, self._default_display)
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
        rows.append(CLOSE_ROW)
        mid = tg.send("\U0001f3ae <b>Quick actions</b>", chat_id,
                      thread_id=thread_id, buttons=rows)
        if not thread_id and mid:
            self.ui.delete_after(mid, chat_id, _PICKER_TTL)
