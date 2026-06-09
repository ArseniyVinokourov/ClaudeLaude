import json
import os
import threading
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.expanduser("~/Projects"))
HOOK_PORT = int(os.environ.get("HOOK_PORT", "9853"))
AUTO_UPDATE = os.environ.get("AUTO_UPDATE", "false").lower() in ("true", "1", "yes")
AUTO_UPDATE_POLICY = os.environ.get("AUTO_UPDATE_POLICY", "replace")
UNLOCK_WORD = os.environ.get("UNLOCK_WORD", "")
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_KILL_FILE = os.path.join(BOT_DIR, ".kill")
_ENV_FILE = os.path.join(BOT_DIR, ".env")


def set_env(key: str, value: str):
    """Upsert ``KEY=value`` in .env and update the live process env.

    The change takes effect now (os.environ) and survives a restart (.env,
    re-read by load_dotenv on next start). Best-effort on the file write —
    the in-process var is always set. Comments and blank lines are kept.
    """
    os.environ[key] = value
    try:
        lines = []
        if os.path.isfile(_ENV_FILE):
            with open(_ENV_FILE) as f:
                lines = [ln for ln in f.read().splitlines()
                         if ln.split("=", 1)[0].strip() != key]
        lines.append(f"{key}={value}")
        with open(_ENV_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass

_STATE_FILE = os.environ.get(
    "BOT_STATE_FILE",
    os.path.join(os.path.dirname(__file__), ".state.json"),
)


def _load_state() -> dict:
    if os.path.exists(_STATE_FILE):
        with open(_STATE_FILE) as f:
            return json.load(f)
    return {}


def _save_state(state: dict):
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f)


def get_forum_chat_id() -> int | None:
    return _load_state().get("forum_chat_id")


def set_forum_chat_id(chat_id: int):
    state = _load_state()
    state["forum_chat_id"] = chat_id
    _save_state(state)


def get_pinned_help_id() -> int | None:
    return _load_state().get("pinned_help_id")


def set_pinned_help_id(msg_id: int | None):
    state = _load_state()
    state["pinned_help_id"] = msg_id
    _save_state(state)


def get_dashboard_id() -> int | None:
    return _load_state().get("dashboard_id")


def set_dashboard_id(msg_id: int | None):
    state = _load_state()
    state["dashboard_id"] = msg_id
    _save_state(state)


def get_terminal_topic_id() -> int | None:
    return _load_state().get("terminal_topic_id")


def set_terminal_topic_id(topic_id: int | None):
    state = _load_state()
    state["terminal_topic_id"] = topic_id
    _save_state(state)


def get_default_mode() -> str:
    """Mode new bot sessions start in (a /mode preset name; default 'default').
    State-backed so every module reads the current value live."""
    return _load_state().get("default_mode", "default")


def set_default_mode(name: str):
    state = _load_state()
    state["default_mode"] = name
    _save_state(state)


def get_tour_shown() -> bool:
    return _load_state().get("tour_shown", False)


def set_tour_shown(v: bool = True):
    state = _load_state()
    state["tour_shown"] = v
    _save_state(state)


def get_tour_msg_id() -> int | None:
    return _load_state().get("tour_msg_id")


def set_tour_msg_id(msg_id: int | None):
    state = _load_state()
    state["tour_msg_id"] = msg_id
    _save_state(state)


def get_help_msg_id() -> int | None:
    return _load_state().get("help_msg_id")


def set_help_msg_id(msg_id: int | None):
    state = _load_state()
    state["help_msg_id"] = msg_id
    _save_state(state)


# Pending-delete registry: transient bot messages in the forum group
# (ephemerals, pickers, replaced dashboard pins) that must not outlive their
# TTL. Each entry is [msg_id, due_ts] — registered when the delete is
# scheduled, removed once it succeeds. Leftovers are swept two ways:
#   - startup (cleanup_general): everything — the timers died with the
#     process, nothing will fire for these entries;
#   - periodic (dashboard tick): only entries PAST due (+ grace) — their
#     live timer should have fired and didn't (failed delete), so retry.
#     Entries still inside their TTL belong to a running timer; touching
#     them early would kill a picker the user is about to click.
# Targeted ids only — never ranged sweeps (message ids are chat-global,
# a range would hit session topics).
_pending_delete_lock = threading.Lock()
_PENDING_DELETE_CAP = 200
# A failed retry pushes due forward so an undeletable message (e.g. past
# Telegram's bot-delete window) doesn't burn one paid call per tick forever.
PENDING_DELETE_RETRY_BACKOFF_S = 600.0


def get_pending_deletes() -> list[list]:
    """[[msg_id, due_ts], ...] — entries whose delete is scheduled/overdue."""
    return _load_state().get("pending_delete_ids", [])


def add_pending_delete(msg_id: int, due_ts: float):
    if not msg_id:
        return
    with _pending_delete_lock:
        state = _load_state()
        entries = state.get("pending_delete_ids", [])
        if all(e[0] != msg_id for e in entries):
            entries.append([msg_id, due_ts])
            state["pending_delete_ids"] = entries[-_PENDING_DELETE_CAP:]
            _save_state(state)


def remove_pending_delete(msg_id: int):
    with _pending_delete_lock:
        state = _load_state()
        entries = state.get("pending_delete_ids", [])
        kept = [e for e in entries if e[0] != msg_id]
        if len(kept) != len(entries):
            state["pending_delete_ids"] = kept
            _save_state(state)


def defer_pending_delete(msg_id: int, due_ts: float):
    """Push an entry's due forward after a failed retry."""
    with _pending_delete_lock:
        state = _load_state()
        entries = state.get("pending_delete_ids", [])
        for e in entries:
            if e[0] == msg_id:
                e[1] = due_ts
                state["pending_delete_ids"] = entries
                _save_state(state)
                return


def get_uploads_warned_at() -> float:
    """Last time the owner was DM'd about upload-folder size (#87)."""
    return _load_state().get("uploads_warned_at", 0.0)


def set_uploads_warned_at(ts: float):
    state = _load_state()
    state["uploads_warned_at"] = ts
    _save_state(state)


def is_killed() -> bool:
    return os.path.exists(_KILL_FILE)


def activate_kill():
    with open(_KILL_FILE, "w") as f:
        f.write(str(int(time.time())))


def deactivate_kill():
    try:
        os.remove(_KILL_FILE)
    except FileNotFoundError:
        pass


