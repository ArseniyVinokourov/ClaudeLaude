"""Telegram output primitives — the thin send/reply/ephemeral helpers
shared by every controller layer (commands, hooks, lifecycle, dispatch).

Packaged as an injectable `BotUI` so the controller components depend on
one explicit collaborator instead of reaching for module globals, and so
tests can stub a single shared instance (e.g. neutralise `ephemeral`'s
auto-delete thread) at one point. No bot state — only the Telegram
client and the forum-chat id from config.
"""

import threading
import time

import telegram as tg
from config import OWNER_ID, get_forum_chat_id

# Shared inline-keyboard row used by every picker/menu (and the callback
# dispatcher that handles its `close` action).
CLOSE_ROW = [{"text": "✕ Close", "callback_data": "close"}]


class BotUI:
    def send_to_topic(self, topic_id, text, buttons=None):
        fid = get_forum_chat_id()
        if fid and topic_id:
            return tg.send(text, fid, thread_id=topic_id, buttons=buttons)
        return None

    def send_general(self, text, buttons=None, persist=False, seconds=15):
        """Write to General — the sanctioned channel for any notice that
        doesn't belong to a specific category.

        Transient by default: General must stay clean (only the pinned
        dashboard lives there permanently), so a generic message self-deletes
        after `seconds`. Pass persist=True only for security alerts, which
        must stay until the user acts on them. Falls back to the owner DM
        when no forum is configured (no auto-delete there).

        Anything that needs a different lifetime has its own category:
        ui.ephemeral (custom TTL), pickers (delete_after), the dashboard pin.
        Raw tg.send(text, forum_id) is reserved for the pin alone.
        """
        fid = get_forum_chat_id()
        if fid:
            mid = tg.send(text, fid, buttons=buttons)
            if mid and not persist:
                self.delete_after(mid, fid, seconds)
            return mid
        return tg.send(text, OWNER_ID, buttons=buttons)

    def reply(self, chat_id, thread_id, text, buttons=None):
        if chat_id:
            return tg.send(text, chat_id, thread_id=thread_id, buttons=buttons)
        return self.send_general(text, buttons=buttons)

    def ephemeral(self, chat_id, text, thread_id=None, seconds=15, buttons=None):
        """Send a message that auto-deletes after `seconds`."""
        mid = tg.send(text, chat_id, thread_id=thread_id, buttons=buttons)
        self.delete_after(mid, chat_id, seconds)
        return mid

    def delete_after(self, mid, chat_id, seconds, before_delete=None):
        """Schedule a message deletion `seconds` from now in a daemon thread.

        The single place deferred deletes go through (picker expiry, perm
        cancellation, terminal-notice cleanup, interrupted-status fade) so
        the fire-and-forget timer can be neutralised in tests at one point.
        `before_delete`, if given, runs just before the delete (used to
        expire the matching pending-pick state).
        """
        if not mid:
            return
        def _run():
            time.sleep(seconds)
            if before_delete is not None:
                before_delete()
            tg.delete(mid, chat_id)
        threading.Thread(target=_run, daemon=True).start()

    def topic_url(self, topic_id):
        fid = get_forum_chat_id()
        if not fid:
            return None
        short_id = str(fid).replace("-100", "")
        return f"https://t.me/c/{short_id}/{topic_id}"
