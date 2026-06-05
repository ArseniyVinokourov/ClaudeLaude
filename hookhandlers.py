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

import telegram as tg
from config import (OWNER_ID, get_forum_chat_id, get_terminal_topic_id,
                    set_terminal_topic_id)
from formatting import _normalize_tool_input
from session_discovery import _session_jsonl_path


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
            data = data or {}
            session = (self.mgr.by_claude_session_id(claude_session_id)
                       if claude_session_id else None)
            if not session:
                session = self._resolve_hook_session(claude_session_id, data)

            if session and session.is_bot_spawned and session.topic_id:
                self.ui.send_to_topic(session.topic_id, f"\U0001f514 {tg.esc(text)}")
                return

            # Terminal / unknown session → shared aggregator topic, tagged
            # with the project so several terminals don't blur together.
            proj = self._proj_label(data)
            mid, tid = self._send_terminal(
                f"\U0001f514 <b>[{tg.esc(proj)}]</b> {tg.esc(text)}")
            if mid and claude_session_id:
                self._track_terminal_msg(claude_session_id, mid,
                                         get_forum_chat_id(), "notification")
            elif mid is None:
                tg.send(f"\U0001f514 {tg.esc(text)}", OWNER_ID)
        except Exception as e:
            print(f"hook notification error: {e}", file=sys.stderr, flush=True)

    def _proj_label(self, data) -> str:
        cwd = data.get("cwd", "")
        return os.path.basename(cwd.rstrip("/")) if cwd else "terminal"

    def _terminal_topic(self) -> int | None:
        """Persistent aggregator topic for terminal-session events.

        Created lazily on the first terminal notification/permission and
        reused forever (id persisted in .state.json). Replaces the old
        per-cwd "terminal — HH:MM" topics that cluttered the forum.
        """
        tid = get_terminal_topic_id()
        if tid:
            return tid
        fid = get_forum_chat_id()
        if not fid:
            return None
        try:
            tid = tg.create_forum_topic(
                fid, "Terminals", icon_color=0x6FB9F0,
                icon_custom_emoji_id=self._icon_terminal)
        except Exception as e:
            print(f"[terminal] create topic failed: {e}",
                  file=sys.stderr, flush=True)
            return None
        if not tid:
            return None
        set_terminal_topic_id(tid)
        with self.state.lock:
            self.state.topic_labels[tid] = "Terminals"
        return tid

    def handle_topic_dead(self, thread_id) -> bool:
        """React to the Terminals aggregator topic being deleted (#89).

        Forget its id — the next terminal event recreates the topic via
        _send_terminal. Pending Allow/Deny prompts died with the topic, so
        abandon them: the hook closes without a decision and each terminal
        claude falls back to its local interactive prompt (constraint #12).
        Returns True if thread_id was the aggregator.
        """
        if not thread_id or thread_id != get_terminal_topic_id():
            return False
        print("[topic_dead] Terminals aggregator deleted — resetting",
              file=sys.stderr, flush=True)
        set_terminal_topic_id(None)
        with self.state.lock:
            watched = list(self.state.pending_terminal_msgs.keys())
        for csid in watched:
            self._cleanup_terminal_pending(csid)
        return True

    def _send_terminal(self, text, buttons=None):
        """Send into the aggregator topic; recreate it once if it was deleted.

        Returns (msg_id, topic_id) — msg_id is None if the forum or topic
        could not be reached even after one recreate attempt.
        """
        fid = get_forum_chat_id()
        tid = self._terminal_topic()
        if not fid or not tid:
            return None, None
        mid = tg.send(text, fid, thread_id=tid, buttons=buttons)
        if mid is None:
            set_terminal_topic_id(None)
            tid = self._terminal_topic()
            if tid:
                mid = tg.send(text, fid, thread_id=tid, buttons=buttons)
        return mid, tid

    def _resolve_hook_session(self, claude_session_id, data):
        """Find an existing *bot-spawned* session for this hook, or None.

        Terminal sessions no longer get their own topic — they route to the
        shared aggregator — so this never creates anything. A terminal hook
        that happens to share a cwd with a bot session must not steal it
        (project_bot_cwd_routing_bug.md), so bot sessions matched by cwd are
        skipped here.
        """
        _log = lambda m: print(f"[resolve] {m}", file=sys.stderr, flush=True)
        if claude_session_id:
            session = self.mgr.by_claude_session_id(claude_session_id)
            if session and session.topic_id:
                _log(f"found by claude_id → topic {session.topic_id}")
                return session

        cwd = data.get("cwd", "")
        if cwd:
            existing = self.mgr.by_cwd(cwd)
            if existing and existing.topic_id and not existing.is_bot_spawned:
                if claude_session_id:
                    self.mgr.link_claude_id(claude_session_id, existing)
                return existing
            if existing and existing.is_bot_spawned:
                _log(f"skipping bot-spawned session for cwd={cwd}")
        return None

    def on_hook_permission(self, req_id, data):
        try:
            claude_session_id = (data.get("session_id")
                                 or data.get("sessionId") or "")
            # A live bot-spawned session governs its own permissions:
            # --permission-mode auto + the stdin control protocol
            # (--permission-prompt-tool stdio) already decide every tool. The
            # settings.json PreToolUse hook still fires (it no longer skips
            # print mode), but routing it to an Allow/Deny prompt would
            # duplicate the auto-allow and spam the topic — so silently allow.
            own = (self.mgr.by_claude_session_id(claude_session_id)
                   if claude_session_id else None)
            if own and own.is_bot_spawned and own.alive:
                # AskUserQuestion is answered over the control protocol (inline
                # buttons → updatedInput.answers), NOT allow/deny. A hook allow
                # here would tell Claude to proceed before the owner taps, so
                # the answer arrives too late. Abandon the hook and let the
                # control protocol drive it.
                if data.get("tool_name") == "AskUserQuestion":
                    self.bridge.abandon_permission(req_id)
                    return
                self.bridge.resolve_permission(req_id, "allow")
                return
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

            terminal_csid = None
            if mirror:
                topic_id = mirror.topic_id
                chat_id = get_forum_chat_id()
                msg_id = tg.send(body, chat_id, thread_id=topic_id,
                                 buttons=buttons) if chat_id else None
                # Track the in-flight permission so its tool_use can be
                # filtered from mirror projection (avoids duplicate signal
                # of "permission asked + tool ran" both projected).
                mirror.pending_perm_tool = (tool, _normalize_tool_input(tool, ti))
            elif session and session.is_bot_spawned \
                    and self.lifecycle.valid_topic_id(session):
                topic_id = self.lifecycle.valid_topic_id(session)
                chat_id = get_forum_chat_id()
                msg_id = tg.send(body, chat_id, thread_id=topic_id,
                                 buttons=buttons)
                if msg_id is None:
                    # Bot session's topic was deleted — let it die; the DM
                    # fallback below still lets the owner decide.
                    print(f"[resolve] stale topic {topic_id}, invalidating",
                          file=sys.stderr, flush=True)
                    self.lifecycle.invalidate_and_stop(session, "topic gone")
                    session = None
                    topic_id = None
            else:
                # Terminal / unknown session → aggregator topic, tagged with
                # the project. The watcher cleans the Allow/Deny line once the
                # terminal claude advances (same as before, keyed by csid).
                proj = self._proj_label(data)
                msg_id, topic_id = self._send_terminal(
                    f"\U0001f6e0 <b>[{tg.esc(proj)}]</b>\n{body}", buttons)
                chat_id = get_forum_chat_id()
                terminal_csid = claude_session_id or None

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

            if terminal_csid and msg_id and chat_id:
                self._track_terminal_msg(terminal_csid, msg_id, chat_id,
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
