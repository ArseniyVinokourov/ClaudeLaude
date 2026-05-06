import json
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.expanduser("~/Projects"))
HOOK_PORT = int(os.environ.get("HOOK_PORT", "9853"))

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
