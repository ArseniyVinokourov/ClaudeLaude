"""In-process replacements for Telegram API and Claude subprocess.

FakeTelegram intercepts every Bot API call (sendMessage, editMessageText,
createForumTopic, getUpdates, etc.) and records params for later assertion.
It serves canned updates from `inject_update`.

FakeClaude replaces subprocess.Popen for the claude CLI. Each spawn returns
a process whose stdout is a pre-scripted sequence of stream-json events.
"""
from __future__ import annotations

import io
import json
import threading
import time


class FakeTelegram:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._next_msg_id = 1000
        self._next_topic_id = 100
        self._update_queue: list[dict] = []
        self._lock = threading.Lock()
        # message_id -> {"chat_id", "thread_id", "text"} for delete-tracking
        self.messages: dict[int, dict] = {}
        self.deleted_messages: set[int] = set()
        self.pinned_messages: list[int] = []
        self.unpinned_all: int = 0
        # If True, sendMessage with a thread_id that is in `dead_topics`
        # raises 400 (mimics deleted-topic behavior).
        self.dead_topics: set[int] = set()

    # ── core request entry-point (replaces telegram._req) ───────────

    def req(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        with self._lock:
            self.calls.append((method, dict(params)))

        if method == "getUpdates":
            with self._lock:
                ups = self._update_queue[:]
                self._update_queue.clear()
            return {"ok": True, "result": ups}

        if method in ("sendMessage", "editForumTopic"):
            tid = params.get("message_thread_id")
            if tid is not None and tid in self.dead_topics:
                # Mimic Telegram's "message thread not found" 400.
                # _req raises HTTPError on 4xx; tests use the topic_alive
                # probe which catches Exception, so this is consistent.
                # Production silent probe uses editForumTopic (see
                # telegram.topic_alive), so the same fault must surface there.
                import requests
                resp = _FakeResponse(400, b'{"ok":false,"description":"thread not found"}')
                raise requests.HTTPError("400", response=resp)
        if method == "sendMessage":
            tid = params.get("message_thread_id")
            mid = self._next_msg_id
            self._next_msg_id += 1
            self.messages[mid] = {
                "chat_id": params.get("chat_id"),
                "thread_id": tid,
                "text": params.get("text", ""),
            }
            return {"ok": True, "result": {"message_id": mid}}

        if method == "createForumTopic":
            tid = self._next_topic_id
            self._next_topic_id += 1
            return {"ok": True, "result": {"message_thread_id": tid, "name": params.get("name", "")}}

        if method == "deleteMessage":
            self.deleted_messages.add(params.get("message_id"))
            return {"ok": True, "result": True}

        if method == "pinChatMessage":
            self.pinned_messages.append(params.get("message_id"))
            return {"ok": True, "result": True}

        if method == "unpinAllForumTopicMessages":
            self.unpinned_all += 1
            return {"ok": True, "result": True}

        if method == "editMessageText":
            mid = params.get("message_id")
            if mid in self.messages:
                self.messages[mid]["text"] = params.get("text", "")
            return {"ok": True, "result": True}

        return {"ok": True, "result": {}}

    # ── update injection (drives _handle_update via tests) ──────────

    def inject_update(self, update: dict):
        with self._lock:
            self._update_queue.append(update)

    # ── query helpers ───────────────────────────────────────────────

    def calls_of(self, method: str) -> list[dict]:
        with self._lock:
            return [dict(p) for m, p in self.calls if m == method]

    def reset(self):
        with self._lock:
            self.calls.clear()

    def find_call(self, method: str, **filters) -> dict | None:
        for params in self.calls_of(method):
            if all(params.get(k) == v for k, v in filters.items()):
                return params
        return None

    def wait_for_call(self, method: str, *, timeout: float = 3.0,
                      count: int = 1, **filters) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            matches = [
                p for p in self.calls_of(method)
                if all(p.get(k) == v for k, v in filters.items())
            ]
            if len(matches) >= count:
                return matches
            time.sleep(0.01)
        raise AssertionError(
            f"timed out waiting for {count}x {method} (filters={filters}); "
            f"last calls: {self.calls[-10:]}"
        )

    def messages_in_topic(self, thread_id: int) -> list[dict]:
        return [
            m for m in self.messages.values()
            if m.get("thread_id") == thread_id
        ]


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self._body = body
        self.text = body.decode()

    def raise_for_status(self):
        import requests
        raise requests.HTTPError(self.text, response=self)


# ── Claude subprocess fake ──────────────────────────────────────────

class _FakeStdin:
    """Captures everything the engine writes to claude's stdin."""

    def __init__(self):
        self.data: list[str] = []

    def write(self, s):
        self.data.append(s)

    def flush(self):
        pass

    def close(self):
        pass


class FakeClaudeProcess:
    """Stand-in for subprocess.Popen of the claude CLI.

    `events` is a list of stream-json dicts; we serialize them as one
    JSON-line each on stdout, matching the real CLI's --output-format
    stream-json --verbose output.
    """

    def __init__(self, events: list[dict]):
        # Bidirectional stream-json: the engine spawns with text=True and reads
        # str lines; the prompt + control responses are written to stdin.
        text = "".join(json.dumps(e) + "\n" for e in events)
        self.stdout = io.StringIO(text)
        self.stderr = io.StringIO("")
        self.stdin = _FakeStdin()
        self._returncode: int | None = None

    def user_turns(self) -> list[dict]:
        """The {"type":"user",...} messages the engine wrote to stdin."""
        out = []
        for line in "".join(self.stdin.data).splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") == "user":
                out.append(obj)
        return out

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def terminate(self):
        if self._returncode is None:
            self._returncode = -15

    def kill(self):
        if self._returncode is None:
            self._returncode = -9

    @property
    def returncode(self):
        return self._returncode


class FakeClaudeFactory:
    """Replacement for sessions.subprocess.Popen.

    Tests `script(events)` ahead of time. Each call to the factory pops the
    next scripted response. Spawn args (cmd, cwd) are recorded so tests can
    assert on `--resume` / `--fork-session` flags.
    """

    def __init__(self):
        self.scripts: list[list[dict]] = []
        self.spawns: list[dict] = []
        self._lock = threading.Lock()
        # Default events emitted on a turn when no script queued.
        self.default_events = [
            {"type": "system", "session_id": "fake-claude-session"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "ok"},
            ]}},
            {"type": "result", "session_id": "fake-claude-session",
             "usage": {"input_tokens": 10, "output_tokens": 1}},
        ]

    def script(self, events: list[dict]):
        """Queue a sequence of stream-json events for the next spawn."""
        with self._lock:
            self.scripts.append(events)

    def __call__(self, cmd, **kwargs):
        with self._lock:
            events = self.scripts.pop(0) if self.scripts else list(self.default_events)
            proc = FakeClaudeProcess(events)
            self.spawns.append({"cmd": list(cmd), "cwd": kwargs.get("cwd"),
                                "proc": proc})
        return proc

    def last_spawn(self) -> dict:
        with self._lock:
            return self.spawns[-1] if self.spawns else {}

    def wait_for_spawns(self, count: int, *, timeout: float = 3.0) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if len(self.spawns) >= count:
                    return list(self.spawns)
            time.sleep(0.01)
        with self._lock:
            raise AssertionError(
                f"timed out waiting for {count} spawns; got {len(self.spawns)}"
            )


# ── canned update builders ──────────────────────────────────────────

def text_update(text: str, *, owner_id: int, forum_chat_id: int,
                thread_id: int | None = None, update_id: int | None = None) -> dict:
    msg: dict = {
        "message_id": int(time.time() * 1000) % 1_000_000,
        "from": {"id": owner_id},
        "chat": {"id": forum_chat_id, "type": "supergroup"},
        "date": int(time.time()),
        "text": text,
    }
    if thread_id is not None:
        msg["message_thread_id"] = thread_id
    return {
        "update_id": update_id if update_id is not None else int(time.time() * 1000),
        "message": msg,
    }


def callback_update(data: str, *, owner_id: int, forum_chat_id: int,
                    message_id: int = 1,
                    thread_id: int | None = None) -> dict:
    return {
        "update_id": int(time.time() * 1000),
        "callback_query": {
            "id": str(int(time.time() * 1000)),
            "from": {"id": owner_id},
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": forum_chat_id, "type": "supergroup"},
                **({"message_thread_id": thread_id} if thread_id else {}),
            },
        },
    }
