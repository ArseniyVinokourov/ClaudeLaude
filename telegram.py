"""Thin Telegram Bot API wrapper — module-level functions."""
import json
import os
import re
import sys
import time

import requests

from config import BOT_TOKEN

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
_session = requests.Session()

MAX_TEXT = 4096


def _log(msg):
    print(f"[tg] {msg}", file=sys.stderr, flush=True)


def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _req(method: str, params: dict | None = None) -> dict:
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
            _log(f"rate limited, retry after {retry_after}s")
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
         buttons: list | None = None, markdown: bool = False) -> int | None:
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
              markdown: bool = False) -> list[int]:
    if markdown:
        text = md_to_html(text)
    ids: list[int] = []
    while text:
        chunk = text[:MAX_TEXT]
        if len(text) > MAX_TEXT:
            cut = chunk.rfind("\n")
            if cut > MAX_TEXT // 2:
                chunk = chunk[:cut]
        mid = send(chunk, chat_id, thread_id=thread_id)
        if mid:
            ids.append(mid)
        text = text[len(chunk):]
    return ids


def delete(msg_id: int, chat_id: int):
    try:
        _req("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
    except Exception as e:
        _log(f"delete error: {e}")


def pin(msg_id: int, chat_id: int, silent: bool = True):
    try:
        _req("pinChatMessage", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "disable_notification": silent,
        })
    except Exception as e:
        _log(f"pin error: {e}")


def unpin(msg_id: int, chat_id: int):
    try:
        _req("unpinChatMessage", {
            "chat_id": chat_id,
            "message_id": msg_id,
        })
    except Exception as e:
        _log(f"unpin error: {e}")


def edit(msg_id: int, text: str, chat_id: int, buttons: list | None = None):
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
    except Exception as e:
        _log(f"edit error: {e}")


# ── media ───────────────────────────────────────────────────────────

def send_photo(chat_id: int, photo_path: str, caption: str = "",
               thread_id: int | None = None) -> int | None:
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


def send_document(chat_id: int, doc_path: str, caption: str = "",
                  thread_id: int | None = None) -> int | None:
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
    params: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
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


def set_message_reaction(chat_id: int, msg_id: int, emoji: str | None):
    """Set or clear a reaction on a message.

    `emoji=None` (or empty) clears any reaction the bot set previously.
    Telegram restricts unverified bots to its built-in reaction set —
    use one of: 👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🥱 🥴 😍 ❤‍🔥 🌚 💯
    🤣 ⚡ 🍌 🏆 💔 🤨 😐 🤡 🤓 👻 👨‍💻 👀 🙈 😇 😨 🤝 ✍ 🤗 🫡 💅 🗿 🆒 💘
    🙉 😘 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡 — anything else returns 400.
    """
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
                       ) -> int | None:
    """Create a forum topic. Caller chooses the leading emoji — defaults
    to 💬 (bot session). Pass an explicit ID (e.g. terminal 💻) to
    differentiate session types in the topic list."""
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
                     icon_custom_emoji_id: str | None = None):
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


def close_forum_topic(chat_id: int, topic_id: int):
    try:
        _req("closeForumTopic", {
            "chat_id": chat_id,
            "message_thread_id": topic_id,
        })
    except Exception as e:
        _log(f"close_forum_topic error: {e}")


def reopen_forum_topic(chat_id: int, topic_id: int):
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
