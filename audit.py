"""Append-only audit log for security events.

Writes are non-blocking: log() enqueues and returns immediately.
A background thread flushes to disk.
"""
import json
import os
import queue
import threading
import time

_LOG_PATH = os.path.join(os.path.dirname(__file__), ".audit.log")
_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
_DETAIL_LIMIT = 200
_queue: queue.Queue[str] = queue.Queue()


def _writer():
    while True:
        entries = [_queue.get()]
        while not _queue.empty():
            try:
                entries.append(_queue.get_nowait())
            except queue.Empty:
                break
        try:
            if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > _MAX_SIZE:
                backup = _LOG_PATH + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(_LOG_PATH, backup)
            with open(_LOG_PATH, "a") as f:
                f.write("\n".join(entries) + "\n")
        except Exception:
            pass


threading.Thread(target=_writer, daemon=True).start()


def log(event: str, detail: str = "", sid: str | None = None):
    entry = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event": event,
        "detail": detail[:_DETAIL_LIMIT],
        "sid": sid,
    }, ensure_ascii=False)
    _queue.put(entry)


def tail(n: int = 20) -> list[dict]:
    if not os.path.exists(_LOG_PATH):
        return []
    try:
        with open(_LOG_PATH) as f:
            lines = f.readlines()
        result = []
        for line in lines[-n:]:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return result
    except Exception:
        return []
