"""General-topic dashboard pin + account-usage cache.

A component with its dependencies injected at construction (bot state,
the terminal-mirror manager, a help-validation callback, and the claude
binary path). The Telegram client and config accessors are imported
directly — the test harness fakes Telegram at the `telegram._req` layer,
so a direct import still goes through the fake.

bot.py owns the background thread; it calls `tick()` once per cycle.
"""

import os
import re
import subprocess
import sys
import time

import telegram as tg
from config import (PENDING_DELETE_RETRY_BACKOFF_S, add_pending_delete,
                    defer_pending_delete, get_dashboard_id,
                    get_forum_chat_id, get_pending_deletes,
                    get_pinned_help_id, is_killed, remove_pending_delete,
                    set_dashboard_id, set_pinned_help_id)
from version import get_version

_USAGE_CACHE_TTL = 300
# Periodic sweep touches an entry only this long past its due time — gives
# the live delete_after timer first shot before the sweep retries.
_OVERDUE_GRACE_S = 30.0

# editMessageText errors that mean the dashboard message is GONE upstream
# (deleted by hand, expired) — the only case where we recreate it. Every
# other failure (429, network, HTML parse) leaves the message in place, so
# recreating on it would just spawn a duplicate (that is how stale dashboards
# piled up). Matched case-insensitively against the API error body.
_EDIT_GONE_SIGNALS = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
)

_MENU_ROWS = [
    [{"text": "\U0001f195 New session", "callback_data": "m:new"},
     {"text": "\U0001f4cb Sessions", "callback_data": "m:sessions"}],
    [{"text": "▶️ Resume", "callback_data": "m:resume"},
     {"text": "❓ Help", "callback_data": "m:help"}],
    [{"text": "\U0001f44b Start here", "callback_data": "tr:open"}],
]


class Dashboard:
    def __init__(self, state, mirror_mgr, validate_help, claude_bin):
        self.state = state
        self.mirror_mgr = mirror_mgr
        self._validate_help = validate_help
        self._claude_bin = claude_bin
        self._usage_cache: str | None = None
        self._usage_cache_ts: float = 0
        # Force a full pin-stack collapse on the first sync after startup,
        # so leftover dashboards pinned beneath ours (e.g. by a crashed run
        # or a separate-state test build) are cleared even when they are not
        # on top of the stack. Cleared after the first reconcile.
        self._force_pin_reconcile: bool = True

    def fetch_account_usage(self) -> str | None:
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
                [self._claude_bin],
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

    def refresh_usage(self):
        try:
            raw = self.fetch_account_usage()
            if raw:
                lines = raw.strip().splitlines()
                short = []
                for line in lines:
                    m = re.search(r'(\d+)%', line)
                    label = "Week" if "week" in line.lower() else "Session"
                    if m:
                        short.append(f"{label}: {m.group(1)}%")
                self._usage_cache = " · ".join(short) if short else None
            else:
                self._usage_cache = None
            self._usage_cache_ts = time.time()
        except Exception as e:
            print(f"[dashboard] usage fetch error: {e}",
                  file=sys.stderr, flush=True)

    def build(self) -> str:
        """Pinned status in General. Designed to need ZERO Telegram-side
        healthchecks: every line is computed from local state.

        Layout:
          <b>ClaudeLaude</b> vX.Y.Z
          🔔 N waiting     (only when N>0 — permissions pending Allow/Deny)
          🔗 N mirror      (only when N>0 — active terminal mirrors)
          <usage cache>    (free, refreshed in-process)
          🔒 KILLED        (only when locked)
        """
        ver = get_version().split("+")[0]
        parts = [f"<b>ClaudeLaude</b> v{tg.esc(ver)}"]
        with self.state.lock:
            n_waiting = len(self.state.pending_permissions)
        n_mirror = sum(1 for m in self.mirror_mgr.list() if m.alive)
        if n_waiting > 0:
            parts.append(f"\U0001f514 {n_waiting} waiting")
        if n_mirror > 0:
            parts.append(f"\U0001f517 {n_mirror} mirror")
        if self._usage_cache:
            parts.append(self._usage_cache)
        if is_killed():
            parts.append("\U0001f512 <b>KILLED</b>")
        return "\n".join(parts)

    def cleanup_general(self, overdue_only: bool = False):
        """Sweep leftover transient messages tracked in the pending-delete
        registry (bot's own ephemerals, pickers, replaced dashboard pins).

        Every scheduled forum-group delete is registered in .state.json
        (BotUI.delete_after / dashboard replacement); normally its timer
        removes it. This sweep catches what survives, in two modes:
          - startup (overdue_only=False): everything — the timers died
            with the process, nothing will fire for these entries;
          - periodic tick (overdue_only=True): only entries past due +
            grace, i.e. whose live timer fired and failed. Entries inside
            their TTL belong to a running timer — deleting them early
            would kill a picker the user is about to click.
        No ranged sweeps: message ids are chat-global, a range would hit
        session topics (feedback_no_aggressive_sweep). A delete that still
        fails (e.g. message older than the bot-delete window) gets its due
        pushed back so it doesn't burn one paid call per tick forever.
        """
        fid = get_forum_chat_id()
        if not fid:
            return
        now = time.time()
        keep = {get_dashboard_id(), get_pinned_help_id()}
        count = 0
        for msg_id, due_ts in get_pending_deletes():
            if msg_id in keep:
                remove_pending_delete(msg_id)
                continue
            if overdue_only and now < due_ts + _OVERDUE_GRACE_S:
                continue
            if tg.delete(msg_id, fid):
                remove_pending_delete(msg_id)
                count += 1
            else:
                defer_pending_delete(
                    msg_id, now + PENDING_DELETE_RETRY_BACKOFF_S)
        if count:
            print(f"[cleanup] deleted {count} stale transient messages",
                  file=sys.stderr, flush=True)

    def sync(self):
        """Keep General showing exactly one pinned dashboard.

        Steady state: edit the tracked dashboard in place. If it is gone
        upstream, spawn a fresh one. Either way, reconcile General's pin
        stack against ground truth so any second dashboard (left by a crash,
        a restart, or a separate-state test build) is deleted — at any moment
        there is one dashboard, and only one.
        """
        self._validate_help()
        fid = get_forum_chat_id()
        if not fid:
            return
        text = self.build()
        cur = get_dashboard_id() or get_pinned_help_id()

        if cur:
            status = self._edit_dashboard(fid, cur, text)
            if status == "ok":
                if not get_dashboard_id():
                    set_dashboard_id(cur)
                    set_pinned_help_id(None)
                # Only reconcile the pin stack once, on the first sync after
                # startup — that collapses any duplicate left pinned while the
                # bot was down. In steady state the dashboard is edited in
                # place and stays the sole pin, so polling getChat every tick
                # would only churn (its chat-level pointer lags behind the
                # General-topic pins the user actually sees).
                if self._force_pin_reconcile:
                    self._reconcile_pin(fid, cur, force=True)
                return
            if status == "transient":
                # Message still exists (429 / network / parse error). Do NOT
                # recreate — that is exactly how duplicate dashboards piled
                # up. Leave the tracked id alone and retry next tick.
                return
            # status == "gone": the tracked message can't be edited (deleted,
            # or too old to edit). Drop the id and remove the old message so
            # it never lingers as a second dashboard; if the delete fails it
            # stays registered for the startup sweep (todo #98). Then spawn a
            # replacement.
            set_dashboard_id(None)
            set_pinned_help_id(None)
            add_pending_delete(cur, time.time())  # due now
            if tg.delete(cur, fid):
                remove_pending_delete(cur)

        self._spawn_dashboard(fid, text)

    def _edit_dashboard(self, fid, msg_id, text) -> str:
        """Try to edit the dashboard in place. Returns 'ok', 'gone', or
        'transient'."""
        try:
            tg._req("editMessageText", {
                "chat_id": fid,
                "message_id": msg_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": _MENU_ROWS},
            })
            return "ok"
        except Exception as e:
            body = ""
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    body = resp.text
                except Exception:
                    pass
            low = body.lower()
            if "not modified" in low:
                return "ok"
            if any(s in low for s in _EDIT_GONE_SIGNALS):
                print(f"[dashboard] message gone, recreating: {body[:120]}",
                      file=sys.stderr, flush=True)
                return "gone"
            print(f"[dashboard] edit failed (transient): {e} {body[:120]}",
                  file=sys.stderr, flush=True)
            return "transient"

    def _spawn_dashboard(self, fid, text):
        """Send a fresh dashboard as General's only pin."""
        msg_id = tg.send(text, fid, buttons=_MENU_ROWS, persist=True)
        if not msg_id:
            return
        set_dashboard_id(msg_id)
        set_pinned_help_id(None)
        # New dashboard becomes General's single pin: collapse the whole
        # General pin stack first (scoped to General — does NOT touch any
        # session topic's control panel, unlike chat-wide
        # unpinAllChatMessages, #85), then pin just this one, then delete any
        # other dashboard that was sitting pinned on top.
        self._reconcile_pin(fid, msg_id, force=True)

    def _reconcile_pin(self, fid, keep_id, force=False):
        """Ensure `keep_id` is the only pinned dashboard in General.

        Ground truth is getChat.pinned_message (telegram.pinned_message_id),
        which is scoped to General and yields None for session-topic pins, so
        a session topic's control panel is never touched here. When something
        other than `keep_id` is on top of the stack, it is a leftover
        dashboard: delete it (its id is known from getChat), collapse the
        stack, and re-pin ours.
        """
        force = force or self._force_pin_reconcile
        top = tg.pinned_message_id(fid)
        if not force and (top is None or top == keep_id):
            return
        if top is not None and top != keep_id:
            add_pending_delete(top, time.time())  # due now
            if tg.delete(top, fid):
                remove_pending_delete(top)
        tg.unpin_all_general(fid)
        tg.pin(keep_id, fid)
        self._force_pin_reconcile = False

    def tick(self):
        """One background cycle: refresh usage if stale, push the pin,
        retry overdue transient deletes (so General heals without a
        restart when a timer's delete failed)."""
        if time.time() - self._usage_cache_ts > _USAGE_CACHE_TTL:
            self.refresh_usage()
        try:
            self.sync()
        except Exception as e:
            print(f"[dashboard] update error: {e}",
                  file=sys.stderr, flush=True)
        try:
            self.cleanup_general(overdue_only=True)
        except Exception as e:
            print(f"[cleanup] sweep error: {e}",
                  file=sys.stderr, flush=True)
