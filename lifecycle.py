"""Session lifecycle: create a topic-backed session, tear one down when
its topic dies, and cancel any permission prompts left dangling.

A component with `state`, the `BotUI` helper, and the `TurnController`
injected at construction. `mgr` (SessionManager) and `bridge`
(HookBridge) are set afterwards (setter injection) because both are
built *with* this component — `mgr` takes `on_session_stop`, and
`bridge` is wired after `mgr`. Commands, hook handlers, topic-dead
detection and dispatch all call into this layer, so it sits below them.
"""

import os
import sys
import time

import audit
import telegram as tg
from config import get_forum_chat_id


class SessionLifecycle:
    def __init__(self, state, ui, turnctl):
        self.state = state
        self.ui = ui
        self.turnctl = turnctl
        self.mgr = None     # SessionManager, setter-injected
        self.bridge = None  # HookBridge, setter-injected

    def spawn_session(self, cwd, name=None):
        fid = get_forum_chat_id()
        if not fid:
            self.ui.send_general("❌ Run /setup in a forum group first.")
            return
        if not name:
            name = os.path.basename(cwd.rstrip("/"))
        with self.state.lock:
            self.state.topic_counter[name] = (
                self.state.topic_counter.get(name, 0) + 1)
            n = self.state.topic_counter[name]
        ts = time.strftime("%H:%M")
        label = (f"{name} #{n} — {ts}" if n > 1
                 else f"{name} — {ts}")
        try:
            topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0)
        except Exception as e:
            self.ui.ephemeral(fid, f"❌ Failed to create topic: {tg.esc(str(e))}",
                              seconds=7)
            return
        if not topic_id:
            self.ui.ephemeral(fid, "❌ Failed to create topic. Check bot admin rights.",
                              seconds=7)
            return
        with self.state.lock:
            self.state.topic_labels[topic_id] = label
        s = self.mgr.create(cwd=cwd, name=name, topic_id=topic_id)
        s.topic_label = label
        self.mgr._persist()
        audit.log("session_start", cwd, sid=s.sid)
        self.ui.send_to_topic(topic_id, f"▶️ <code>{tg.esc(cwd)}</code>")
        url = self.ui.topic_url(topic_id)
        if url:
            self.ui.ephemeral(fid, f"▶ {name}",
                              buttons=[[{"text": "Open", "url": url}]],
                              seconds=5)

    def _invalidate_session(self, session):
        """Remove a session with a stale/deleted topic."""
        sid = session.sid
        mgr = self.mgr
        if session.topic_id and mgr._topic_map.get(session.topic_id) == sid:
            del mgr._topic_map[session.topic_id]
        if session.cwd and mgr._cwd_map.get(session.cwd) == sid:
            del mgr._cwd_map[session.cwd]
        if (session.claude_session_id
                and mgr._claude_id_map.get(session.claude_session_id) == sid):
            del mgr._claude_id_map[session.claude_session_id]
        mgr._sessions.pop(sid, None)
        mgr._persist()

    def cancel_session_perms(self, sid: str, reason: str):
        """Cancel pending permission requests tied to a session.

        Edits the Allow/Deny message to "✗ Cancelled — {reason}", schedules
        deletion, and unblocks the hook handler with deny so it can return.
        """
        with self.state.lock:
            victims = [
                (short_id, msg_id, chat_id)
                for short_id, (msg_id, chat_id, p_sid)
                in list(self.state.pending_permissions.items())
                if p_sid == sid
            ]
            for short_id, _, _ in victims:
                self.state.pending_permissions.pop(short_id, None)
        for short_id, msg_id, chat_id in victims:
            full_id = None
            with self.state.lock:
                full_id = self.state.perm_key_map.pop(short_id, None)
            try:
                tg.edit(msg_id, f"✗ Cancelled — {tg.esc(reason)}", chat_id)
            except Exception as e:
                print(f"[cancel_perm] edit failed: {e}",
                      file=sys.stderr, flush=True)
            self.ui.delete_after(msg_id, chat_id, 5)
            if full_id:
                self.bridge.abandon_permission(full_id)

    def invalidate_and_stop(self, session, reason: str):
        """Topic gone → clean up turn, stop session, drop maps.

        Order matters: mgr.stop() fires the on_session_stop callback (which
        cancels pending perms) only while the session is still in the
        manager's tables.  _invalidate_session() removes the entry, so it has
        to come last.
        """
        sid = session.sid
        print(f"[lifecycle] invalidate_and_stop sid={sid} reason={reason}",
              file=sys.stderr, flush=True)
        with self.state.lock:
            turn = self.state.turns.pop(sid, None)
        if turn:
            self.turnctl.end_turn(turn)
        self.mgr.stop(sid, reason=reason)
        self._invalidate_session(session)

    def valid_topic_id(self, session) -> int | None:
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

    def on_session_stop(self, session, reason: str):
        self.cancel_session_perms(session.sid, reason)
