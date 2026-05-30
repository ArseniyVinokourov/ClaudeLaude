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

    def send_general(self, text, buttons=None):
        fid = get_forum_chat_id()
        if fid:
            return tg.send(text, fid, buttons=buttons)
        return tg.send(text, OWNER_ID, buttons=buttons)

    def reply(self, chat_id, thread_id, text, buttons=None):
        if chat_id:
            return tg.send(text, chat_id, thread_id=thread_id, buttons=buttons)
        return self.send_general(text, buttons=buttons)

    def ephemeral(self, chat_id, text, thread_id=None, seconds=15, buttons=None):
        """Send a message that auto-deletes after `seconds`."""
        mid = tg.send(text, chat_id, thread_id=thread_id, buttons=buttons)
        if mid:
            def _cleanup():
                time.sleep(seconds)
                tg.delete(mid, chat_id)
            threading.Thread(target=_cleanup, daemon=True).start()
        return mid

    def topic_url(self, topic_id):
        fid = get_forum_chat_id()
        if not fid:
            return None
        short_id = str(fid).replace("-100", "")
        return f"https://t.me/c/{short_id}/{topic_id}"
