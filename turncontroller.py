"""Turn lifecycle: project Claude's output stream into a topic and render
the live per-turn status (message + timer + reactions).

One cohesive responsibility — how a turn's progress is reflected to the
owner — so the stream callbacks and the status UI live together and share
`TurnState` privately. A component with its dependencies injected at
construction (bot state, the claude binary path, and a few config
literals); `mgr` is set afterwards (setter injection) because the
SessionManager is built *with* this component's callbacks, so it cannot
exist yet at construction time.

The four `on_*` methods plus `session_context` are wired into
SessionManager. `build_summary`, `send_fork_summary`, `end_turn` and
`enqueue_user_input` are called from bot.py dispatch/commands. The
Telegram client, config accessors and formatting helpers are imported
directly — the test harness fakes Telegram at the `telegram._req` layer.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import audit
import telegram as tg
from config import get_forum_chat_id
from formatting import (_chat_action_for_tool, _compact_tool_msg,
                        _is_noisy_tool, _md_table_to_list)

_NOISE_TEXTS = {
    "claude is waiting for your input",
}
_MAX_SAVED_TURNS = 50

# Claude asks the bot to deliver a file by emitting `[Send file: <path>]` in
# its reply (mirrors the inbound `[Attached file: ...]` convention). The bot
# extracts the paths, removes the marker from the shown text, and sends the
# files to the topic at turn end.
_SEND_FILE_RE = re.compile(r"\[Send file:\s*([^\]]+?)\s*\]")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _extract_send_files(text, cwd):
    """Pull `[Send file: ...]` paths out of `text`. Returns (clean_text,
    paths) — paths resolved against `cwd` when relative."""
    paths = []
    for m in _SEND_FILE_RE.finditer(text):
        p = m.group(1).strip()
        if cwd and not os.path.isabs(p):
            p = os.path.join(cwd, p)
        paths.append(p)
    clean = _SEND_FILE_RE.sub("", text).strip() if paths else text
    return clean, paths
_REACT_STREAMING = "🔥"
_REACT_TOOL = "⚡"


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


class TurnController:
    def __init__(self, state, ui, claude_bin, *, default_display,
                 icon_stopped, fork_backfill):
        self.state = state
        self.ui = ui
        self.mgr = None  # setter-injected after SessionManager is built
        self._claude_bin = claude_bin
        self._default_display = default_display
        self._icon_stopped = icon_stopped
        self._fork_backfill = fork_backfill

    # ── turn tracking ────────────────────────────────────────────────

    def _get_turn(self, session) -> TurnState:
        with self.state.lock:
            if session.sid not in self.state.turns:
                self.state.turns[session.sid] = TurnState()
            return self.state.turns[session.sid]

    def enqueue_user_input(self, session, text: str, chat_id: int,
                           msg_id: int | None, thread_id: int | None):
        """Send a user message into Claude and wire up the 👀→👍 reaction.

        Used for plain text, sticker descriptors, and file-attached prompts.
        Reacting up-front gives the user immediate feedback that the bot saw
        the message even before Claude starts streaming.
        """
        ok = self.mgr.send_user_message(session.sid, text)
        if not ok:
            tg.send("⚠️ Session died", chat_id, thread_id=thread_id)
            return
        if msg_id and chat_id:
            # Auto-reaction 👀 removed — each reaction costs a budget unit
            # and the "⏳ Думаю…" status + typing indicator already signal
            # "got it, working on it" for free.
            turn = self._get_turn(session)
            turn.user_msg_ids.append(msg_id)
            self._record_topic_msg(session.topic_id, msg_id)

    def _react_user_msg(self, msg_id: int, chat_id: int, emoji: str | None):
        """Set/clear a reaction on a user message. Silent on failure."""
        try:
            tg.set_message_reaction(chat_id, msg_id, emoji)
        except Exception as e:
            print(f"[react] {emoji!r} on {msg_id} failed: {e}",
                  file=sys.stderr, flush=True)

    def _record_topic_msg(self, topic_id: int | None, msg_id: int | None):
        """Push a message id into the per-topic rolling window used for fork
        backfill. Trims to fork_backfill most-recent ids. Silently no-ops on
        missing inputs so callers don't need to branch."""
        if not topic_id or not msg_id:
            return
        with self.state.lock:
            buf = self.state.recent_msgs.setdefault(topic_id, [])
            buf.append(int(msg_id))
            if len(buf) > self._fork_backfill:
                del buf[:-self._fork_backfill]

    def _react_progress(self, turn: TurnState, emoji: str):
        """Auto-reactions disabled — every reaction costs 1 budget unit and
        a turn-lifecycle 👀→🔥/⚡→👍 burned 2-3 units per user message. The
        `typing…` indicator carries the same "I'm working" signal for free.

        Kept as a no-op so call sites don't all need to be deleted; manual
        reactions still go through tg.set_message_reaction directly."""
        return

    def _react_for_turn(self, turn: TurnState, kind: str = "completed"):
        """Auto-reactions disabled (see _react_progress). This still clears
        the per-turn tracking state so the next turn starts clean."""
        turn.user_msg_ids.clear()
        turn.streamed = False
        turn.tooled = False
        turn.last_tool_name = None

    def _format_status(self, turn: TurnState) -> str:
        """Status text shown while Claude works on a turn.

        Deliberately time-free: every tick that produced a different number
        used one budget unit. Now the text only changes on tool transitions
        (≤ a few per turn), and Telegram's free "typing…" indicator carries
        the liveness signal.
        """
        n = len(turn.tool_ops)
        if n == 0:
            return "⏳ Думаю…"
        last = turn.tool_ops[-1]
        if n > 1:
            return f"⚙️ <code>{tg.esc(last)}</code> ({n})"
        return f"⚙️ <code>{tg.esc(last)}</code>"

    def _interrupt_button(self, session):
        return [[{"text": "⏹ Interrupt", "callback_data": f"int:{session.sid}"}]]

    def _update_status(self, session, turn: TurnState):
        fid = get_forum_chat_id()
        if not fid or not session.topic_id:
            return
        text = self._format_status(turn)
        if text == turn._last_status_text:
            return
        btn = self._interrupt_button(session)
        if turn.status_msg_id:
            tg.edit(turn.status_msg_id, text, fid, buttons=btn)
        else:
            mid = tg.send(text, fid, thread_id=session.topic_id, buttons=btn)
            if mid:
                turn.status_msg_id = mid
        turn._last_status_text = text

    def _finish_status(self, session, turn: TurnState):
        """Remove status message (timer thread keeps running).

        P1, not the default P3: this is part of the live turn UX (the
        "⏳ Думаю…" indicator must vanish the moment the reply lands), so the
        throughput budget must not drop it the way it can a housekeeping
        delete.
        """
        fid = get_forum_chat_id()
        if not fid:
            return
        if turn.status_msg_id:
            tg.delete(turn.status_msg_id, fid, prio=tg.P1)
            turn.status_msg_id = None

    def end_turn(self, turn: TurnState):
        """Stop timer thread and clean up status message.

        On a normal turn end the status message is deleted right away. On an
        interrupted turn it sticks as "⏹ Interrupted" for ~3s so the user sees
        the final state before it disappears.
        """
        turn.stop_event.set()
        if turn._timer_thread:
            turn._timer_thread.join(timeout=5)
            turn._timer_thread = None
        fid = get_forum_chat_id()
        if fid and turn.status_msg_id:
            mid = turn.status_msg_id
            if turn.interrupted:
                try:
                    tg.edit(mid, "⏹ Interrupted", fid)
                except Exception:
                    pass
                self.ui.delete_after(mid, fid, 3)
            else:
                tg.delete(mid, fid, prio=tg.P1)
            turn.status_msg_id = None
        self._react_for_turn(turn, "completed")

    def _turn_timer(self, session, turn: TurnState):
        """Background thread: update status timer."""
        fid = get_forum_chat_id()
        while not turn.stop_event.wait(3):
            mid = turn.status_msg_id
            if mid and fid:
                text = self._format_status(turn)
                if text != turn._last_status_text:
                    tg.edit(mid, text, fid,
                            buttons=self._interrupt_button(session))
                    turn._last_status_text = text
            # Telegram hides the chat-action indicator after ~5s; refresh it
            # on every tick (3s) so the verb stays visible for the full turn.
            if fid and session.topic_id:
                tg.send_chat_action(
                    fid,
                    action=_chat_action_for_tool(turn.last_tool_name),
                    thread_id=session.topic_id)

    # ── compact / expand ─────────────────────────────────────────────

    def build_summary(self, texts: list[str], ops: list[str]) -> str:
        combined = "\n".join(t.strip() for t in texts if t.strip())
        combined = re.sub(r'<[^>]+>', '', combined)
        combined = re.sub(r'[*_~`#]', '', combined)
        combined = re.sub(r'\n{2,}', '\n', combined).strip()
        try:
            r = subprocess.run(
                [self._claude_bin, '-p',
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

    # ── session context for claude --append-system-prompt ────────────

    def session_context(self, session) -> str:
        """Return the per-turn context appended to Claude's system prompt.

        Tells Claude it's running inside the ClaudeLaude Telegram bot, plus
        the topic/display-mode/mode it currently lives in and the bot
        commands the user can hit from Telegram.
        """
        display = "mobile"
        if session.topic_id:
            with self.state.lock:
                display = self.state.topic_display_mode.get(
                    session.topic_id, self._default_display)
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
            "- To send a file back to this topic (a document, log, image, "
            "etc.), put a line `[Send file: /abs/path]` in your reply. The "
            "bot delivers the file and removes that line from what's shown.",
        ]
        return "\n".join(lines)

    # ── callbacks from SessionManager ────────────────────────────────

    def on_assistant(self, session, text):
        if not session.topic_id:
            return
        if text.strip().lower() in _NOISE_TEXTS:
            return
        # Pull out any `[Send file: ...]` markers: queue the files for delivery
        # and strip the markers so the user sees a clean reply.
        text, files = _extract_send_files(text, session.cwd)
        if files:
            session.pending_files.extend(files)
        if not text:
            return
        turn = self._get_turn(session)
        # Upgrade reaction to 🔥 on first real assistant text, but not over ⚡
        # if a tool was already used in this turn.
        if not turn.streamed and not turn.tooled:
            self._react_progress(turn, _REACT_STREAMING)
            turn.streamed = True
        with self.state.lock:
            mode = self.state.topic_display_mode.get(
                session.topic_id, self._default_display)
        if mode == "mobile":
            text = _md_table_to_list(text)
        fid = get_forum_chat_id()
        if fid:
            # Drop the "⏳ Думаю…"/⚙️ status right before the reply lands so
            # the working indicator never sits next to the finished answer.
            # If more work follows (a tool after this text), on_tool_use
            # recreates the status; end_turn is the final fallback.
            self._finish_status(session, turn)
            ids = tg.send_long(text, fid, thread_id=session.topic_id,
                               markdown=True)
            turn.msg_ids.extend(ids)
            turn.msg_texts.append(text)
            for mid in ids:
                self._record_topic_msg(session.topic_id, mid)

    def on_result(self, session, result_text, summary):
        if not session.topic_id:
            return
        with self.state.lock:
            turn = self.state.turns.pop(session.sid, TurnState())
        self.end_turn(turn)
        fid = get_forum_chat_id()
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
        already = set(imgs)
        session.pending_images.clear()

        # Deliver files Claude asked to send (`[Send file: ...]`): images as
        # photos, everything else as documents. Skip ones already sent above.
        for p in session.pending_files:
            if p in already or not os.path.isfile(p):
                continue
            already.add(p)
            if p.lower().endswith(_IMAGE_EXTS):
                tg.send_photo(fid, p, thread_id=session.topic_id)
            else:
                tg.send_document(fid, p, thread_id=session.topic_id)
        session.pending_files.clear()

        if turn.msg_ids:
            with self.state.lock:
                if len(self.state.saved_turns) >= _MAX_SAVED_TURNS:
                    oldest = next(iter(self.state.saved_turns))
                    self.state.saved_turns.pop(oldest)
                compact_id = str(time.time_ns())[-10:]
                self.state.saved_turns[compact_id] = (
                    turn.msg_ids[:], turn.msg_texts[:], turn.tool_ops[:])
            btn = [[{"text": "\U0001f5dc Compact",
                     "callback_data": f"c:{compact_id}"}]]
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
                                icon_custom_emoji_id=self._icon_stopped)
            with self.state.lock:
                self.state.topic_labels[session.topic_id] = stop_label
            session.topic_label = stop_label
        elif (session.topic_id not in self.state.renamed_topics
              and result_text.strip()):
            # The first user message is often a greeting or one-word request
            # that yields a poor title; the second carries the real intent.
            # Rename provisionally after turn 1 (so single-turn sessions still
            # get named), then re-rename from the second message and lock it.
            user_turns = sum(1 for h in session.history if h.kind == "user")
            if user_turns >= 2:
                with self.state.lock:
                    self.state.renamed_topics.add(session.topic_id)
            self._auto_rename_topic(session, result_text, fid, user_turns)

    def _auto_rename_topic(self, session, result_text, fid, turn_no=1):
        # Name from the theme of the conversation, not a single message: the
        # first message is often a greeting and the second carries the intent,
        # so feed both. Turn 1 → msg 1 only (provisional, so single-turn
        # sessions still get named); turn 2 → msgs 1+2 (real theme, then locks).
        user_msgs = [h.text.strip() for h in session.history
                     if h.kind == "user" and h.text.strip()][:2]
        if not user_msgs:
            return
        user_msg = user_msgs[-1]  # for the keyword fallback below

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
            context = "\n".join(
                f'User message {i + 1}: {m[:300]}'
                for i, m in enumerate(user_msgs))
            if result_text:
                context += f'\nAssistant response (start): {result_text[:200]}'
            try:
                r = subprocess.run(
                    [self._claude_bin, '-p',
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
                with self.state.lock:
                    # A later turn's name wins: don't let a slow turn-1 rename
                    # (claude shell-out can take up to 20s) clobber the turn-2
                    # name that carries the real context.
                    if turn_no < getattr(session, "_rename_turn", 0):
                        return
                    session._rename_turn = turn_no
                    self.state.topic_labels[session.topic_id] = label[:128]
                tg.edit_forum_topic(fid, session.topic_id, label[:128])
                session.topic_label = label[:128]
                session.name = title
                self.mgr._persist()

        threading.Thread(target=_do_rename, daemon=True).start()

    def send_fork_summary(self, parent, topic_id):
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
                    [self._claude_bin, '-p',
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
                fid = get_forum_chat_id()
                if fid and topic_id:
                    tg.send(f"📋 <b>Parent session summary:</b>\n{tg.esc(summary)}",
                            fid, thread_id=topic_id)

        threading.Thread(target=_do, daemon=True).start()

    def on_tool_use(self, session, tool, inp):
        if not session.topic_id:
            return
        audit.log("tool_use", f"{tool}: {json.dumps(inp, ensure_ascii=False)}"
                  if isinstance(inp, dict) else f"{tool}: {inp}",
                  sid=session.sid)
        turn = self._get_turn(session)
        turn.last_tool_name = tool
        # Push a fresh contextual chat-action so the indicator under the chat
        # title matches what Claude is doing right now.
        fid = get_forum_chat_id()
        if fid:
            tg.send_chat_action(fid, action=_chat_action_for_tool(tool),
                                thread_id=session.topic_id)
        # Upgrade reaction to ⚡ on first tool use; ⚡ overrides 🔥.
        if not turn.tooled:
            self._react_progress(turn, _REACT_TOOL)
            turn.tooled = True
        if not _is_noisy_tool(tool, inp):
            compact = _compact_tool_msg(tool, inp)
            turn.tool_ops.append(compact)
            self._update_status(session, turn)

    def on_thinking(self, session):
        if not session.topic_id:
            return
        fid = get_forum_chat_id()
        if not fid:
            return
        turn = self._get_turn(session)
        if turn._timer_thread is None:
            mid = tg.send("⏳ Думаю…", fid, thread_id=session.topic_id,
                          buttons=self._interrupt_button(session))
            turn.status_msg_id = mid
            tg.send_chat_action(fid, thread_id=session.topic_id)
            t = threading.Thread(
                target=self._turn_timer, args=(session, turn), daemon=True)
            turn._timer_thread = t
            t.start()
