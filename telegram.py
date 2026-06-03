"""Thin Telegram Bot API wrapper — module-level functions."""
import json
import os
import re
import sys
import threading
import time

import requests

import budget as _budget_mod
from config import BOT_TOKEN

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
_session = requests.Session()

MAX_TEXT = 4096

# Per-chat send-rate gate — used ONLY for chats outside the group budget
# (e.g. owner DM). Inside the forum group, all paid writes flow through
# the budget worker (one serialized lane), so this gate is bypassed.
_CHAT_SEND_INTERVAL = float(os.environ.get("BOT_CHAT_SEND_INTERVAL", "1.05"))
_chat_send_ts: dict[int, float] = {}
_chat_send_lock = threading.Lock()

# Forum-group identity is set by bot.py at startup. When `chat_id` matches,
# paid writes are routed through the budget worker. Other chats run direct.
_forum_chat_id: int | None = None
_on_429_callback = None


def set_forum_chat_id(cid: int | None) -> None:
    global _forum_chat_id
    _forum_chat_id = cid


def set_on_429(callback) -> None:
    """Install a hook called whenever we hit 429 on the group.

    Signature: callback(retry_after_s: int) -> None. Used by bot.py to
    send a smoothed "rate limited, hold on" notice to the owner DM.
    """
    global _on_429_callback
    _on_429_callback = callback


_on_topic_dead_callback = None


def set_on_topic_dead(callback) -> None:
    """Install a hook called when a write fails with TOPIC_ID_INVALID.

    Signature: callback(chat_id: int, thread_id: int) -> None. Used by
    bot.py to invalidate the session attached to a deleted topic — the
    only detection path now that active healthchecks are gone.
    """
    global _on_topic_dead_callback
    _on_topic_dead_callback = callback


def _check_topic_dead(chat_id, thread_id, exc_body: str) -> None:
    """If the failure body contains TOPIC_ID_INVALID and we have a
    forum-group write, notify bot.py so it can stop the session."""
    if not thread_id or not _on_topic_dead_callback:
        return
    if chat_id != _forum_chat_id:
        return
    if "topic_id_invalid" in exc_body.lower() or "thread not found" in exc_body.lower():
        try:
            _on_topic_dead_callback(chat_id, thread_id)
        except Exception as e:
            _log(f"on_topic_dead error: {e}")


# Re-export priority constants so callers can do `tg.P0` etc.
P0 = _budget_mod.P0
P1 = _budget_mod.P1
P2 = _budget_mod.P2
P3 = _budget_mod.P3


def _via_budget(chat_id, prio: int, fn, *args, **kwargs):
    """Route a paid write through the group budget when chat is the
    forum group; otherwise run direct."""
    if (_forum_chat_id is not None and chat_id == _forum_chat_id
            and prio is not None):
        fut = _budget_mod.instance().submit(prio, fn, *args, **kwargs)
        try:
            return fut.result(timeout=180)
        except Exception:
            raise
    return fn(*args, **kwargs)


def _log(msg):
    print(f"[tg] {msg}", file=sys.stderr, flush=True)


def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _gate_chat(chat_id) -> None:
    """Block until _CHAT_SEND_INTERVAL has passed since the last
    chat-touching call for the same chat_id. No-op for the forum group
    (the budget worker handles its own serialization) and for missing
    chat_id (global API calls)."""
    if not chat_id:
        return
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return
    if cid == _forum_chat_id:
        return
    with _chat_send_lock:
        now = time.monotonic()
        last = _chat_send_ts.get(cid, 0.0)
        wait = last + _CHAT_SEND_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _chat_send_ts[cid] = now


def _req(method: str, params: dict | None = None) -> dict:
    # Chat-modifying methods carry a `chat_id` field. For chats outside
    # the forum group, throttle per chat. Inside the group, the budget
    # worker already serializes paid writes.
    if params:
        _gate_chat(params.get("chat_id"))
    chat_id = (params or {}).get("chat_id")
    is_group_chat = (chat_id == _forum_chat_id
                     and _forum_chat_id is not None)
    for attempt in range(3):
        try:
            r = _session.post(f"{API}/{method}", json=params or {}, timeout=60)
        except (requests.ConnectionError, requests.Timeout) as e:
            _log(f"network error ({method}): {e}")
            if attempt < 2:
                time.sleep(2)
                continue
            raise
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", 1))
            _log(f"rate limited ({method}), retry after {retry_after}s")
            if is_group_chat:
                # Tell the budget so paused_until is set + headroom() is
                # accurate; also notify any external 429 hook (DM notice).
                try:
                    _budget_mod.instance().report_429(retry_after)
                except Exception:
                    pass
                if _on_429_callback:
                    try:
                        _on_429_callback(retry_after)
                    except Exception:
                        pass
            time.sleep(retry_after)
            continue
        if 400 <= r.status_code < 500:
            r.raise_for_status()
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


# ── markdown → HTML ─────────────────────────────────────────────────

_FENCE_RE = re.compile(r'```\w*\n?(.*?)```', re.DOTALL)
_INLINE_CODE_RE = re.compile(r'`([^`\n]+)`')
_BOLD_ITALIC_RE = re.compile(r'\*{3}(.+?)\*{3}')
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
_ITALIC_RE = re.compile(r'(?<!\*)\*(?!\*)(\S(?:[^*<]*\S)?)\*(?!\*)')
_STRIKE_RE = re.compile(r'~~(.+?)~~')
_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
_HEADING_RE = re.compile(r'^#{1,6}\s+(.+)$', re.MULTILINE)
_TAG_RE = re.compile(r'<(/?)([bisa]|pre|code)(?:\s[^>]*)?\s*>')


def _tags_balanced(text: str) -> bool:
    stack: list[str] = []
    for m in _TAG_RE.finditer(text):
        if m.group(1):
            if not stack or stack[-1] != m.group(2):
                return False
            stack.pop()
        else:
            stack.append(m.group(2))
    return not stack


def md_to_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML."""
    blocks: list[str] = []
    codes: list[str] = []

    def _save_block(m):
        blocks.append(m.group(1))
        return f"\x00B{len(blocks)-1}\x00"

    def _save_code(m):
        codes.append(m.group(1))
        return f"\x00C{len(codes)-1}\x00"

    text = re.sub(r"<antml_thinking>.*?</antml_thinking>\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"<thinking>.*?</thinking>\s*", "", text, flags=re.DOTALL)

    text = _FENCE_RE.sub(_save_block, text)
    text = _INLINE_CODE_RE.sub(_save_code, text)

    text = esc(text)

    text = _BOLD_ITALIC_RE.sub(r'<b><i>\1</i></b>', text)
    text = _BOLD_RE.sub(r'<b>\1</b>', text)

    def _heading_sub(m):
        inner = re.sub(r'</?[bi]>', '', m.group(1))
        return f'<b>{inner}</b>'
    text = _HEADING_RE.sub(_heading_sub, text)

    text = _ITALIC_RE.sub(r'<i>\1</i>', text)
    text = _STRIKE_RE.sub(r'<s>\1</s>', text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)

    for i, block in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", f"<pre>{esc(block)}</pre>")
    for i, code in enumerate(codes):
        text = text.replace(f"\x00C{i}\x00", f"<code>{esc(code)}</code>")

    if not _tags_balanced(text):
        text = re.sub(r'<[^>]+>', '', text)

    return text


# ── send / edit / delete ────────────────────────────────────────────

def send(text: str, chat_id: int, thread_id: int | None = None,
         buttons: list | None = None, markdown: bool = False,
         prio: int = P1) -> int | None:
    return _via_budget(chat_id, prio, _send_impl,
                       text, chat_id, thread_id, buttons, markdown)


def _send_impl(text, chat_id, thread_id, buttons, markdown) -> int | None:
    if markdown:
        text = md_to_html(text)
    params: dict = {
        "chat_id": chat_id,
        "text": text[:MAX_TEXT],
        "parse_mode": "HTML",
    }
    if thread_id:
        params["message_thread_id"] = thread_id
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    try:
        r = _req("sendMessage", params)
        return r.get("result", {}).get("message_id")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            body = ""
            try:
                body = e.response.text
            except Exception:
                pass
            _log(f"send 400: {body}")
            _check_topic_dead(chat_id, thread_id, body)
            params.pop("parse_mode", None)
            params["text"] = re.sub(r'<[^>]+>', '', text)[:MAX_TEXT]
            try:
                r = _req("sendMessage", params)
                return r.get("result", {}).get("message_id")
            except Exception as e2:
                _log(f"plain fallback failed: {e2}")
        else:
            _log(f"send error: {e}")
        return None
    except Exception as e:
        _log(f"send error: {e}")
        return None


def send_long(text: str, chat_id: int, thread_id: int | None = None,
              markdown: bool = False, prio: int = P1) -> list[int]:
    if markdown:
        text = md_to_html(text)
    ids: list[int] = []
    while text:
        chunk = text[:MAX_TEXT]
        if len(text) > MAX_TEXT:
            cut = chunk.rfind("\n")
            if cut > MAX_TEXT // 2:
                chunk = chunk[:cut]
        mid = send(chunk, chat_id, thread_id=thread_id, prio=prio)
        if mid:
            ids.append(mid)
        text = text[len(chunk):]
    return ids


def delete(msg_id: int, chat_id: int, prio: int = P3):
    _via_budget(chat_id, prio, _delete_impl, msg_id, chat_id)


def _delete_impl(msg_id, chat_id):
    try:
        _req("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
    except Exception as e:
        _log(f"delete error: {e}")


def pin(msg_id: int, chat_id: int, silent: bool = True, prio: int = P2):
    _via_budget(chat_id, prio, _pin_impl, msg_id, chat_id, silent)


def _pin_impl(msg_id, chat_id, silent):
    try:
        _req("pinChatMessage", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "disable_notification": silent,
        })
    except Exception as e:
        _log(f"pin error: {e}")


def unpin(msg_id: int, chat_id: int, prio: int = P2):
    _via_budget(chat_id, prio, _unpin_impl, msg_id, chat_id)


def _unpin_impl(msg_id, chat_id):
    try:
        _req("unpinChatMessage", {
            "chat_id": chat_id,
            "message_id": msg_id,
        })
    except Exception as e:
        _log(f"unpin error: {e}")


def edit(msg_id: int, text: str, chat_id: int, buttons: list | None = None,
         prio: int = P1):
    _via_budget(chat_id, prio, _edit_impl, msg_id, text, chat_id, buttons)


def _edit_impl(msg_id, text, chat_id, buttons):
    params: dict = {
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": text[:MAX_TEXT],
        "parse_mode": "HTML",
    }
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    try:
        _req("editMessageText", params)
    except requests.HTTPError as e:
        body = ""
        if e.response is not None:
            try:
                body = e.response.text
            except Exception:
                pass
        _check_topic_dead(chat_id, None, body)
        # A no-op edit (same text + markup) is benign — Telegram rejects it
        # with "message is not modified". The status indicator re-paints with
        # identical content sometimes; don't treat that as an error.
        if "not modified" not in body.lower():
            _log(f"edit error: {e} :: {body[:200]}")
    except Exception as e:
        _log(f"edit error: {e}")


# ── media ───────────────────────────────────────────────────────────

def send_photo(chat_id: int, photo_path: str, caption: str = "",
               thread_id: int | None = None,
               prio: int = P1) -> int | None:
    return _via_budget(chat_id, prio, _send_photo_impl,
                       chat_id, photo_path, caption, thread_id)


def _send_photo_impl(chat_id, photo_path, caption, thread_id) -> int | None:
    params: dict = {"chat_id": chat_id}
    if thread_id:
        params["message_thread_id"] = thread_id
    if caption:
        params["caption"] = caption[:1024]
        params["parse_mode"] = "HTML"
    try:
        with open(photo_path, "rb") as f:
            r = _session.post(f"{API}/sendPhoto", data=params,
                              files={"photo": f}, timeout=60)
        r.raise_for_status()
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        _log(f"send_photo error: {e}")
        return None


def copy_messages(chat_id: int, from_chat_id: int, message_ids: list[int],
                  thread_id: int | None = None,
                  remove_caption: bool = False,
                  prio: int = P3) -> list[int]:
    """Silently duplicate up to 100 messages into another chat/topic.

    Unlike forwardMessage, copyMessage produces fresh messages without a
    "Forwarded from" header. Used here to backfill context into a fork
    topic. Returns the new message ids; empty on failure.
    """
    return _via_budget(chat_id, prio, _copy_messages_impl,
                       chat_id, from_chat_id, message_ids, thread_id,
                       remove_caption)


def _copy_messages_impl(chat_id, from_chat_id, message_ids, thread_id,
                        remove_caption) -> list[int]:
    if not message_ids:
        return []
    params: dict = {
        "chat_id": chat_id,
        "from_chat_id": from_chat_id,
        "message_ids": sorted(set(message_ids))[:100],
    }
    if thread_id:
        params["message_thread_id"] = thread_id
    if remove_caption:
        params["remove_caption"] = True
    try:
        r = _req("copyMessages", params)
        return [m.get("message_id") for m in r.get("result", [])
                if m.get("message_id")]
    except Exception as e:
        _log(f"copy_messages error: {e}")
        return []


def send_media_group(chat_id: int, photo_paths: list[str],
                     thread_id: int | None = None,
                     prio: int = P1) -> list[int]:
    """Send 2–10 photos as a single album. Returns list of message_ids.

    Telegram caps an album at 10 items. Callers should split if more.
    Mixed media types (photo+video) are also supported by sendMediaGroup
    but this helper handles photos only.

    Note: an album costs ~20 budget units server-side, near-flat in photo
    count (probe #3). For typical usage prefer N×send_photo (cost N).
    """
    return _via_budget(chat_id, prio, _send_media_group_impl,
                       chat_id, photo_paths, thread_id)


def _send_media_group_impl(chat_id, photo_paths, thread_id) -> list[int]:
    if not photo_paths:
        return []
    if len(photo_paths) > 10:
        photo_paths = photo_paths[:10]
    media: list[dict] = []
    files: dict = {}
    handles: list = []
    try:
        for i, path in enumerate(photo_paths):
            attach = f"photo{i}"
            media.append({"type": "photo", "media": f"attach://{attach}"})
            fh = open(path, "rb")
            handles.append(fh)
            files[attach] = fh
        data: dict = {
            "chat_id": chat_id,
            "media": json.dumps(media),
        }
        if thread_id:
            data["message_thread_id"] = thread_id
        r = _session.post(f"{API}/sendMediaGroup", data=data,
                          files=files, timeout=60)
        r.raise_for_status()
        results = r.json().get("result", []) or []
        return [m.get("message_id") for m in results if m.get("message_id")]
    except Exception as e:
        _log(f"send_media_group error: {e}")
        return []
    finally:
        for fh in handles:
            try:
                fh.close()
            except Exception:
                pass


def send_document(chat_id: int, doc_path: str, caption: str = "",
                  thread_id: int | None = None,
                  prio: int = P1) -> int | None:
    return _via_budget(chat_id, prio, _send_document_impl,
                       chat_id, doc_path, caption, thread_id)


def _send_document_impl(chat_id, doc_path, caption, thread_id) -> int | None:
    params: dict = {"chat_id": chat_id}
    if thread_id:
        params["message_thread_id"] = thread_id
    if caption:
        params["caption"] = caption[:1024]
        params["parse_mode"] = "HTML"
    try:
        with open(doc_path, "rb") as f:
            r = _session.post(f"{API}/sendDocument", data=params,
                              files={"document": f}, timeout=60)
        r.raise_for_status()
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        _log(f"send_document error: {e}")
        return None


def download_file(file_id: str, dest_path: str) -> bool:
    try:
        r = _req("getFile", {"file_id": file_id})
        file_path = r.get("result", {}).get("file_path", "")
        if not file_path:
            return False
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        resp = _session.get(url, timeout=60)
        resp.raise_for_status()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        _log(f"download_file error: {e}")
        return False


def send_chat_action(chat_id: int, action: str = "typing",
                     thread_id: int | None = None) -> bool:
    params: dict = {"chat_id": chat_id, "action": action}
    if thread_id:
        params["message_thread_id"] = thread_id
    try:
        _req("sendChatAction", params)
        return True
    except Exception:
        return False


def topic_alive(chat_id: int, thread_id: int, name: str | None = None) -> bool:
    """Probe whether a forum topic still exists.

    When `name` is provided, uses editForumTopic (truly silent — no
    message, no notification).  Returns TOPIC_ID_INVALID for deleted
    topics, ok:true for alive ones.

    Without `name`, falls back to send-and-delete (visible but brief).
    """
    if name:
        try:
            _req("editForumTopic", {
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "name": name[:128],
            })
            return True
        except Exception as e:
            body = ""
            if hasattr(e, "response") and e.response is not None:
                try:
                    body = e.response.text
                except Exception:
                    pass
            if "not_modified" in body.lower() or "not modified" in body.lower():
                return True
            return False
    try:
        r = _req("sendMessage", {
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": "⁣",
            "disable_notification": True,
        })
    except Exception:
        return False
    msg_id = r.get("result", {}).get("message_id")
    if msg_id:
        try:
            _req("deleteMessage", {
                "chat_id": chat_id,
                "message_id": msg_id,
            })
        except Exception:
            pass
    return True


# ── forum topics ────────────────────────────────────────────────────

def poll(offset: int | None = None, timeout: int = 30) -> list[dict]:
    params: dict = {"timeout": timeout,
                    "allowed_updates": ["message", "callback_query",
                                        "my_chat_member"]}
    if offset is not None:
        params["offset"] = offset
    try:
        r = _req("getUpdates", params)
        return r.get("result", [])
    except Exception as e:
        _log(f"poll error: {e}")
        return []


def answer_callback(callback_id: str):
    try:
        _req("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception as e:
        _log(f"answer_callback error: {e}")


def set_message_reaction(chat_id: int, msg_id: int, emoji: str | None,
                         prio: int = P2):
    """Set or clear a reaction on a message.

    `emoji=None` (or empty) clears any reaction the bot set previously.
    Telegram restricts unverified bots to its built-in reaction set —
    use one of: 👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🥱 🥴 😍 ❤‍🔥 🌚 💯
    🤣 ⚡ 🍌 🏆 💔 🤨 😐 🤡 🤓 👻 👨‍💻 👀 🙈 😇 😨 🤝 ✍ 🤗 🫡 💅 🗿 🆒 💘
    🙉 😘 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡 — anything else returns 400.
    """
    _via_budget(chat_id, prio, _set_reaction_impl, chat_id, msg_id, emoji)


def _set_reaction_impl(chat_id, msg_id, emoji):
    params: dict = {"chat_id": chat_id, "message_id": msg_id}
    if emoji:
        params["reaction"] = json.dumps(
            [{"type": "emoji", "emoji": emoji}])
    else:
        params["reaction"] = "[]"
    try:
        _req("setMessageReaction", params)
    except Exception as e:
        _log(f"set_message_reaction error: {e}")


_TOPIC_ICON_EMOJI_ID = "5417915203100613993"  # 💬 — default (bot session)


def create_forum_topic(chat_id: int, label: str,
                       icon_color: int = 0x6FB9F0,
                       icon_custom_emoji_id: str | None = None,
                       prio: int = P0,
                       ) -> int | None:
    """Create a forum topic. Caller chooses the leading emoji — defaults
    to 💬 (bot session). Pass an explicit ID (e.g. terminal 💻) to
    differentiate session types in the topic list.

    Submitted at P0: a session that can't get its topic created is broken
    end-to-end, so this must succeed even when budget is tight."""
    return _via_budget(chat_id, prio, _create_forum_topic_impl,
                       chat_id, label, icon_color, icon_custom_emoji_id)


def _create_forum_topic_impl(chat_id, label, icon_color,
                             icon_custom_emoji_id) -> int | None:
    params: dict = {
        "chat_id": chat_id,
        "name": label[:128],
        "icon_color": icon_color,
        "icon_custom_emoji_id": (icon_custom_emoji_id
                                  if icon_custom_emoji_id is not None
                                  else _TOPIC_ICON_EMOJI_ID),
    }
    r = _req("createForumTopic", params)
    return r.get("result", {}).get("message_thread_id")


def edit_forum_topic(chat_id: int, topic_id: int, label: str,
                     icon_custom_emoji_id: str | None = None,
                     prio: int = P3):
    _via_budget(chat_id, prio, _edit_forum_topic_impl,
                chat_id, topic_id, label, icon_custom_emoji_id)


def _edit_forum_topic_impl(chat_id, topic_id, label, icon_custom_emoji_id):
    params: dict = {
        "chat_id": chat_id,
        "message_thread_id": topic_id,
        "name": label[:128],
    }
    if icon_custom_emoji_id is not None:
        params["icon_custom_emoji_id"] = icon_custom_emoji_id
    try:
        _req("editForumTopic", params)
    except Exception as e:
        _log(f"edit_forum_topic error: {e}")


def close_forum_topic(chat_id: int, topic_id: int, prio: int = P3):
    _via_budget(chat_id, prio, _close_forum_topic_impl, chat_id, topic_id)


def _close_forum_topic_impl(chat_id, topic_id):
    try:
        _req("closeForumTopic", {
            "chat_id": chat_id,
            "message_thread_id": topic_id,
        })
    except Exception as e:
        _log(f"close_forum_topic error: {e}")


def reopen_forum_topic(chat_id: int, topic_id: int, prio: int = P3):
    _via_budget(chat_id, prio, _reopen_forum_topic_impl, chat_id, topic_id)


def _reopen_forum_topic_impl(chat_id, topic_id):
    try:
        _req("reopenForumTopic", {
            "chat_id": chat_id,
            "message_thread_id": topic_id,
        })
    except Exception as e:
        _log(f"reopen_forum_topic error: {e}")


def set_my_commands(commands: list[dict]):
    for scope in [
        {"type": "default"},
        {"type": "all_group_chats"},
        {"type": "all_chat_administrators"},
    ]:
        try:
            _req("setMyCommands", {"commands": commands, "scope": scope})
        except Exception as e:
            _log(f"set_my_commands error: {e}")
