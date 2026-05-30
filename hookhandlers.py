"""Claude Code hook handlers: turn an incoming notification or
permission-request POST into a Telegram message in the right topic, and
clean up terminal-session notices once the transcript advances.

A component with `state`, `mgr`, the `BotUI` helper, the terminal-mirror
manager, the `SessionLifecycle` layer and the terminal icon id injected
at construction; `bridge` (HookBridge) is set afterwards because the
bridge is built *with* these handlers. `should_run` is a callable the
background `terminal_watcher` loop polls so bot.py keeps owning the
shutdown flag.

`on_hook_notification` / `on_hook_permission` are wired into the
HookBridge; `terminal_watcher` is started as a daemon thread by bot.py.
Telegram and config are imported directly — tests fake Telegram at the
`telegram._req` layer.
"""

import json
import os
import sys
import time
import uuid

import telegram as tg
from config import OWNER_ID, get_forum_chat_id
from formatting import _normalize_tool_input
from session_discovery import _session_jsonl_path
from sessions import Session


class HookHandlers:
    def __init__(self, state, mgr, ui, mirror_mgr, lifecycle, icon_terminal,
                 should_run):
        self.state = state
        self.mgr = mgr
        self.ui = ui
        self.mirror_mgr = mirror_mgr
        self.lifecycle = lifecycle
        self._icon_terminal = icon_terminal
        self._should_run = should_run
        self.bridge = None  # HookBridge, setter-injected
        self._watcher_offsets: dict[str, int] = {}

    # ── notification + permission (bridge callbacks) ─────────────────

    def on_hook_notification(self, text, claude_session_id, data=None):
        try:
            session = (self.mgr.by_claude_session_id(claude_session_id)
                       if claude_session_id else None)
            if not (session and session.topic_id):
                self._resolve_hook_session(claude_session_id, data or {})
                session = (self.mgr.by_claude_session_id(claude_session_id)
                           if claude_session_id else None)

            if session and session.topic_id:
                mid = self.ui.send_to_topic(session.topic_id,
                                            f"\U0001f514 {tg.esc(text)}")
                if mid and claude_session_id and not session.is_bot_spawned:
                    self._track_terminal_msg(claude_session_id, mid,
                                             get_forum_chat_id(), "notification")
            else:
                tg.send(f"\U0001f514 {tg.esc(text)}", OWNER_ID)
        except Exception as e:
            print(f"hook notification error: {e}", file=sys.stderr, flush=True)

    def _resolve_hook_session(self, claude_session_id, data):
        _log = lambda m: print(f"[resolve] {m}", file=sys.stderr, flush=True)
        _log(f"keys={list(data.keys())} sid={claude_session_id!r}")

        if claude_session_id:
            session = self.mgr.by_claude_session_id(claude_session_id)
            if session and session.topic_id:
                _log(f"found by claude_id → topic {session.topic_id}")
                return session

        cwd = data.get("cwd", "")
        if cwd:
            existing = self.mgr.by_cwd(cwd)
            if existing and existing.topic_id and not existing.is_bot_spawned:
                _log(f"found by cwd → topic {existing.topic_id}")
                if claude_session_id:
                    self.mgr.link_claude_id(claude_session_id, existing)
                return existing
            if existing and existing.is_bot_spawned:
                _log(f"skipping bot-spawned session for cwd={cwd}")

        # DoS guard: with neither a session_id nor a cwd we have nothing to
        # anchor a topic to — refuse rather than spawning a "terminal — HH:MM"
        # topic for every malformed POST that slipped past hooks.py.
        if not claude_session_id and not cwd:
            _log("refuse: no session_id and no cwd, not creating topic")
            return None

        fid = get_forum_chat_id()
        if not fid:
            _log("no forum chat configured")
            return None

        dirname = os.path.basename(cwd) if cwd else "terminal"
        with self.state.lock:
            self.state.topic_counter[dirname] = (
                self.state.topic_counter.get(dirname, 0) + 1)
            n = self.state.topic_counter[dirname]
        ts = time.strftime("%H:%M")
        label = (f"{dirname} #{n} — {ts}" if n > 1
                 else f"{dirname} — {ts}")
        try:
            topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0,
                                             icon_custom_emoji_id=self._icon_terminal)
            _log(f"created topic {topic_id}")
        except Exception as e:
            _log(f"create_forum_topic FAILED: {e}")
            return None
        if not topic_id:
            _log("create_forum_topic returned None")
            return None
        with self.state.lock:
            self.state.topic_labels[topic_id] = label
        if claude_session_id:
            s = self.mgr.register_terminal(claude_session_id, topic_id, cwd=cwd)
            s.topic_label = label
            self.mgr._persist()
            return s
        _log(f"registered topic {topic_id} (no claude session_id)")
        sid = uuid.uuid4().hex[:8]
        session = Session(
            sid=sid, topic_id=topic_id, cwd=cwd,
            name=dirname, is_bot_spawned=False,
            topic_label=label,
        )
        self.mgr._sessions[sid] = session
        self.mgr._topic_map[topic_id] = sid
        if cwd:
            self.mgr._cwd_map[cwd] = sid
        self.mgr._persist()
        return session

    def on_hook_permission(self, req_id, data):
        try:
            claude_session_id = (data.get("session_id")
                                 or data.get("sessionId") or "")
            # If the terminal claude that fired this hook is already
            # mirrored, route the permission Allow/Deny into the SAME
            # mirror topic instead of spawning a separate "terminal — HH:MM"
            # topic. Keeps the conversation + tool prompts in one place.
            mirror = (self.mirror_mgr.by_csid(claude_session_id)
                      if claude_session_id else None)
            session = None if mirror else self._resolve_hook_session(
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
                chat_id = get_forum_chat_id()
                msg_id = tg.send(body, chat_id, thread_id=topic_id,
                                 buttons=buttons) if chat_id else None
                # Track the in-flight permission so its tool_use can be
                # filtered from mirror projection (avoids duplicate signal
                # of "permission asked + tool ran" both projected).
                mirror.pending_perm_tool = (tool, _normalize_tool_input(tool, ti))
            else:
                topic_id = self.lifecycle.valid_topic_id(session)
                msg_id = None
                chat_id = None
                if topic_id:
                    chat_id = get_forum_chat_id()
                    msg_id = tg.send(body, chat_id, thread_id=topic_id,
                                     buttons=buttons)

                if msg_id is None and session and topic_id:
                    print(f"[resolve] stale topic {topic_id}, recreating",
                          file=sys.stderr, flush=True)
                    self.lifecycle.invalidate_and_stop(session, "topic gone")
                    session = self._resolve_hook_session(claude_session_id, data)
                    topic_id = self.lifecycle.valid_topic_id(session)
                    if topic_id:
                        chat_id = get_forum_chat_id()
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
                    self.bridge.abandon_permission(req_id)
                    return

            sid = session.sid if session else None
            with self.state.lock:
                self.state.perm_key_map[short_id] = req_id
                self.state.pending_permissions[short_id] = (msg_id, chat_id, sid)

            if (session and not session.is_bot_spawned
                    and claude_session_id and msg_id and chat_id):
                self._track_terminal_msg(claude_session_id, msg_id, chat_id,
                                         f"perm:{short_id}")

            self.bridge.set_perm_context(req_id, chat_id=chat_id, topic_id=topic_id)

        except Exception as e:
            print(f"hook permission error: {e}", file=sys.stderr, flush=True)

    # ── terminal-notice tracking + cleanup watcher ───────────────────

    def _track_terminal_msg(self, claude_session_id: str, msg_id: int,
                            chat_id: int, kind: str):
        if claude_session_id not in self._watcher_offsets:
            path = _session_jsonl_path(claude_session_id)
            try:
                self._watcher_offsets[claude_session_id] = (
                    os.path.getsize(path) if path else 0)
            except OSError:
                self._watcher_offsets[claude_session_id] = 0
            print(f"[watcher] track csid={claude_session_id[:8]}… "
                  f"offset={self._watcher_offsets[claude_session_id]} "
                  f"kind={kind} msg={msg_id}",
                  file=sys.stderr, flush=True)
        with self.state.lock:
            self.state.pending_terminal_msgs.setdefault(
                claude_session_id, []).append((msg_id, chat_id, kind))

    def _cleanup_terminal_pending(self, csid: str):
        """Remove stale messages from a terminal topic after session progressed."""
        with self.state.lock:
            msgs = self.state.pending_terminal_msgs.pop(csid, [])
        if not msgs:
            return
        for msg_id, chat_id, kind in msgs:
            if kind.startswith("perm:"):
                short_id = kind[5:]
                with self.state.lock:
                    full_id = self.state.perm_key_map.pop(short_id, None)
                    self.state.pending_permissions.pop(short_id, None)
                try:
                    tg.edit(msg_id, "✓ Resolved in terminal", chat_id)
                except Exception:
                    pass
                if full_id:
                    self.bridge.abandon_permission(full_id)
                self.ui.delete_after(msg_id, chat_id, 5)
            else:
                try:
                    tg.delete(msg_id, chat_id)
                except Exception:
                    pass

    def terminal_watcher(self):
        """Poll JSONL files of terminal sessions; clean stale messages on progress."""
        while self._should_run():
            time.sleep(5)
            with self.state.lock:
                watched = list(self.state.pending_terminal_msgs.keys())
            for csid in watched:
                path = _session_jsonl_path(csid)
                if not path:
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                prev = self._watcher_offsets.get(csid, 0)
                if size <= prev:
                    continue
                print(f"[watcher] JSONL grew csid={csid[:8]}… "
                      f"{prev}→{size}, cleaning",
                      file=sys.stderr, flush=True)
                self._watcher_offsets[csid] = size
                self._cleanup_terminal_pending(csid)
