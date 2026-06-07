"""Terminal-mirror projection: render a terminal claude session's JSONL
stream into its Telegram topic, handle the /bot-mirror open request, and
back-fill prior history.

A component with `state`, a `topic_url` builder, and the terminal icon id
injected at construction; the TerminalMirrorManager is set afterwards
(setter injection) because the manager is built *with* this component's
`on_mirror_event` callback. The manager itself stays a composition-root
singleton in bot.py (it is used across hooks/dispatch/lifecycle); this
component only projects.

`on_mirror_event` is wired into the manager; `on_open_in_bot` into the
HookBridge. `welcome_text`, `welcome_buttons`, `start_backfill_thread`
and `pop_pending_backfill` are called from bot.py dispatch. Telegram,
config and formatting helpers are imported directly — the test harness
fakes Telegram at the `telegram._req` layer.
"""

import os
import sys
import threading
import time

import telegram as tg
from config import get_forum_chat_id
from formatting import (_compact_tool_msg, _is_mirror_noisy_tool,
                        _normalize_tool_input)
from terminal_mirror import read_logical_events, reap_if_abandoned

_MIRROR_MODE_CYCLE = ("default", "acceptEdits", "plan", "auto")

# When the JSONL already contains more than this many logical events
# at /bot-mirror time, the bot asks the owner via inline buttons
# whether to backfill the full history (slow, 1 msg/sec) or a brief
# summary (single TG message with last few events). Below the
# threshold, full backfill runs silently.
_BACKFILL_ASK_THRESHOLD = 30
_BACKFILL_SHORT_TAIL = 12


class MirrorProjector:
    def __init__(self, state, topic_url, icon_terminal):
        self.state = state
        self.mgr = None  # TerminalMirrorManager, setter-injected
        self._topic_url = topic_url
        self._icon_terminal = icon_terminal
        # Pending backfill choices keyed by csid: {"snapshot": int,
        # "button_msg_id": int|None}. Populated when on_open_in_bot sends
        # the choice prompt; consumed when the user clicks Full / Short.
        self._pending_backfill: dict = {}
        self._pending_backfill_lock = threading.Lock()

    # ── live projection (manager callback) ───────────────────────────

    def on_mirror_event(self, mirror, event):
        """Project a JSONL event from a terminal session into its mirror topic.

        Filters for content the owner cares about: user prompts (plain
        text only), assistant text blocks, and tool_use one-liners.
        Everything else (tool_result echoes, attachments, system events,
        thinking blocks) is dropped.
        """
        fid = get_forum_chat_id()
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
            # Auto-reaction 👍 on mirror echo removed for budget reasons
            # (each reaction = 1 unit). The projected assistant text itself
            # is the end-to-end delivery confirmation.
            if (text or tool_lines) and mirror.pending_user_msg_id:
                mirror.pending_user_msg_id = None
            return

        if etype == "tool_use":
            tool = event.get("name") or "?"
            inp = event.get("input") or event.get("tool_input") or {}
            if not _is_mirror_noisy_tool(tool, inp):
                tg.send(f"⚙️ {tg.esc(_compact_tool_msg(tool, inp))}",
                        fid, thread_id=mirror.topic_id)

    # ── history backfill ─────────────────────────────────────────────

    def _send_logical_event(self, fid: int, topic_id: int, ev: dict) -> None:
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

    def _backfill_full(self, mirror, fid: int, snapshot_offset: int) -> None:
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
                self._send_logical_event(fid, mirror.topic_id, ev)
            except Exception as e:
                print(f"[mirror backfill] send failed: {e}",
                      file=sys.stderr, flush=True)

    def _backfill_short_summary(self, mirror, fid: int,
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

    def start_backfill_thread(self, mirror, fid: int, mode: str,
                              snapshot_offset: int) -> None:
        """Spawn a daemon thread that runs the chosen backfill, then
        releases mirror.backfill_done so the follower can resume."""
        def run():
            try:
                if mode == "full":
                    self._backfill_full(mirror, fid, snapshot_offset)
                elif mode == "short":
                    self._backfill_short_summary(mirror, fid, snapshot_offset,
                                                 _BACKFILL_SHORT_TAIL)
            finally:
                mirror.backfill_done.set()
        threading.Thread(target=run, daemon=True,
                         name=f"mirror-backfill-{mirror.csid[:8]}").start()

    def pop_pending_backfill(self, csid_prefix):
        """Look up and remove a pending backfill choice by csid prefix.

        Returns (full_csid|None, entry|None)."""
        with self._pending_backfill_lock:
            hit = None
            for full in list(self._pending_backfill.keys()):
                if full.startswith(csid_prefix):
                    hit = full
                    break
            entry = self._pending_backfill.pop(hit, None) if hit else None
        return hit, entry

    # ── welcome message (controls) ───────────────────────────────────

    def welcome_text(self, mirror) -> str:
        if mirror.dtach_socket:
            return ("\U0001fa9e Mirror attached. Type in this topic — keystrokes "
                    "go into the terminal claude.")
        return ("\U0001f50c Mirror is output-only — start your terminal "
                "claude inside dtach to enable typing from here.")

    def _mode_name(self, mirror) -> str:
        try:
            return _MIRROR_MODE_CYCLE[mirror.mode_index % len(_MIRROR_MODE_CYCLE)]
        except Exception:
            return _MIRROR_MODE_CYCLE[0]

    def welcome_buttons(self, mirror) -> list:
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
                {"text": f"⇄ Mode: {self._mode_name(mirror)}",
                 "callback_data": f"mm:{short}"},
            ])
        return rows

    # ── /bot-mirror open request (hook callback) ─────────────────────

    def on_open_in_bot(self, csid, cwd, dtach_socket):
        """Bot-side handler for POST /hook/open_in_bot.

        Creates a mirror topic if one doesn't exist for this csid, starts
        the JSONL follower, returns {status, topic_url}. dtach_socket is
        the path to the unix socket of the wrapped claude (e.g.
        `/tmp/clmirror-<pid>.sock`) — bot writes input via
        `dtach -p <socket>`. When empty, the mirror runs output-only.
        """
        fid = get_forum_chat_id()
        if not fid:
            return {"error": "bot has no forum chat configured"}
        existing = self.mgr.by_csid(csid)
        if existing:
            url = self._topic_url(existing.topic_id)
            # If the previous follower thread died (e.g. JSONL hadn't been
            # written yet on first registration), restart it now that the
            # user is invoking the command again with the file likely
            # already in place.
            if not existing.follower or not existing.follower.is_alive():
                self.mgr.start_follower(existing)
            # Refresh the dtach binding — the user may have re-launched
            # their terminal claude, getting a new socket path.
            if dtach_socket and existing.dtach_socket != dtach_socket:
                self.mgr.set_dtach_socket(csid, dtach_socket)
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
                icon_custom_emoji_id=self._icon_terminal)
        except Exception as e:
            return {"error": f"create_forum_topic failed: {e}"}
        if not topic_id:
            return {"error": "create_forum_topic returned no id"}
        with self.state.lock:
            self.state.topic_labels[topic_id] = label
        m = self.mgr.register(csid, cwd, topic_id, dtach_socket)
        snapshot_offset = m.last_offset  # JSONL size at registration time
        # Welcome with inline controls (filter toggle + mode cycle). Stays
        # at the top of the topic; we edit its buttons in place when state
        # changes (filter toggled), so the labels always reflect reality.
        welcome_text = self.welcome_text(m)
        welcome_buttons = self.welcome_buttons(m)
        welcome_id = tg.send(welcome_text, fid, thread_id=topic_id,
                             buttons=welcome_buttons)
        if welcome_id:
            self.mgr.set_welcome_msg_id(csid, welcome_id)

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
                self.start_backfill_thread(m, fid, "full", snapshot_offset)
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
                with self._pending_backfill_lock:
                    self._pending_backfill[csid] = {
                        "snapshot": snapshot_offset,
                        "button_msg_id": msg_id,
                    }

        self.mgr.start_follower(m)
        url = self._topic_url(topic_id)
        return {"status": "ok", "topic_url": url, "existing": False,
                "input_bridge": bool(dtach_socket)}

    # ── terminal closed (hook callback) ──────────────────────────────

    def on_terminal_closed(self, csid):
        """Bot-side handler for POST /hook/terminal_closed.

        The shell wrapper's SIGHUP trap fires this the moment the
        terminal hosting a dtach-wrapped claude closes. SIGTERM the
        detached claude right away — the close was the owner's explicit
        action, no idle wait needed. dtach then removes the socket and
        the socket watcher posts the continue-as-bot-session notice on
        its next tick. The zero-attached-clients check inside
        reap_if_abandoned keeps a second attached terminal safe.
        """
        m = self.mgr.by_csid(csid) if self.mgr else None
        if not m or not m.alive or not m.dtach_socket:
            return {"status": "ignored"}
        reaped = reap_if_abandoned(m.dtach_socket, None)
        print(f"[mirror] {csid[:8]} terminal_closed hook → "
              f"{'reaped claude' if reaped else 'skipped (client attached or no pid)'}",
              file=sys.stderr, flush=True)
        return {"status": "reaped" if reaped else "skipped"}
