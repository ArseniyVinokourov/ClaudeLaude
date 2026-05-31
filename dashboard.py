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
from config import (get_dashboard_id, get_forum_chat_id, get_pinned_help_id,
                    is_killed, set_dashboard_id, set_pinned_help_id)
from version import get_version

_USAGE_CACHE_TTL = 300

_MENU_ROWS = [
    [{"text": "\U0001f195 New session", "callback_data": "m:new"},
     {"text": "\U0001f4cb Sessions", "callback_data": "m:sessions"}],
    [{"text": "▶️ Resume", "callback_data": "m:resume"},
     {"text": "❓ Help", "callback_data": "m:help"}],
]


class Dashboard:
    def __init__(self, state, mirror_mgr, validate_help, claude_bin):
        self.state = state
        self.mirror_mgr = mirror_mgr
        self._validate_help = validate_help
        self._claude_bin = claude_bin
        self._usage_cache: str | None = None
        self._usage_cache_ts: float = 0

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

    def cleanup_general(self):
        """Delete stale messages in General (before the pinned help)."""
        fid = get_forum_chat_id()
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

    def sync(self):
        """Send or update the pinned dashboard message in General."""
        self._validate_help()
        fid = get_forum_chat_id()
        if not fid:
            return
        text = self.build()
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
            # Deleting the previous dashboard message also drops its pin, so
            # the fresh pin below becomes General's only pin. Do NOT use
            # unpinAllChatMessages here — it is chat-wide and would wipe every
            # session topic's pinned control panel too (#85).
            tg.delete(old_id, fid)
        msg_id = tg.send(text, fid, buttons=_MENU_ROWS)
        if msg_id:
            tg.pin(msg_id, fid)
            set_dashboard_id(msg_id)

    def tick(self):
        """One background cycle: refresh usage if stale, then push the pin."""
        if time.time() - self._usage_cache_ts > _USAGE_CACHE_TTL:
            self.refresh_usage()
        try:
            self.sync()
        except Exception as e:
            print(f"[dashboard] update error: {e}",
                  file=sys.stderr, flush=True)
