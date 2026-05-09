import json
import os
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


