"""Pytest fixtures for bot scenario tests.

Each test gets a freshly-imported `bot` module wired up to FakeTelegram and
FakeClaude. State files (.state.json, .sessions.json) live in a per-test
tempdir via env-overridable paths added to config.py / sessions.py.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make the repo root importable regardless of pytest cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.fakes import FakeTelegram, FakeClaudeFactory  # noqa: E402

OWNER_ID = 42
FORUM_CHAT_ID = 1001


def _purge_bot_modules():
    for name in [
        "bot", "telegram", "sessions", "hooks", "config", "version",
        "audit", "device_monitor", "terminal_mirror",
        # Component modules cache `import telegram as tg` at import time;
        # purge them too so a fresh bot import rebinds their `tg` to the
        # freshly-patched telegram module (else they hold a stale one).
        "turncontroller", "dashboard", "formatting", "session_discovery",
        "updater", "mirrorbridge", "botui", "lifecycle", "hookhandlers",
        "commands",
    ]:
        sys.modules.pop(name, None)


@pytest.fixture
def bot_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    sessions_file = tmp_path / "sessions.json"
    mirrors_file = tmp_path / "mirrors.json"
    state_file.write_text(json.dumps({"forum_chat_id": FORUM_CHAT_ID}))

    monkeypatch.setenv("BOT_TOKEN", "fake-token")
    monkeypatch.setenv("OWNER_ID", str(OWNER_ID))
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("HOOK_PORT", "0")
    monkeypatch.setenv("UNLOCK_WORD", "")
    monkeypatch.setenv("BOT_STATE_FILE", str(state_file))
    monkeypatch.setenv("BOT_SESSIONS_FILE", str(sessions_file))
    monkeypatch.setenv("BOT_MIRRORS_FILE", str(mirrors_file))

    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "demo").mkdir()

    _purge_bot_modules()
    yield SimpleNamespace(
        tmp_path=tmp_path,
        state_file=state_file,
        sessions_file=sessions_file,
        owner_id=OWNER_ID,
        forum_chat_id=FORUM_CHAT_ID,
    )
    _purge_bot_modules()


@pytest.fixture
def bot(bot_env, monkeypatch: pytest.MonkeyPatch):
    fake_tg = FakeTelegram()
    fake_claude = FakeClaudeFactory()

    # Patch BEFORE importing bot — bot.py instantiates SessionManager at
    # import time, which can spawn worker threads on _restore().
    import telegram as telegram_mod
    monkeypatch.setattr(telegram_mod, "_req", fake_tg.req)

    import sessions as sessions_mod
    monkeypatch.setattr(sessions_mod.subprocess, "Popen", fake_claude)

    # Suppress real threads from auto-rename helper (it shells out to claude).
    bot_mod = importlib.import_module("bot")

    # Stub auto-rename — it spawns its own claude subprocess via shell.
    monkeypatch.setattr(bot_mod.turnctl, "_auto_rename_topic",
                        lambda *a, **kw: None)

    # Stub the per-turn status timer. on_thinking starts a daemon thread that
    # loops every 3s calling tg.edit / send_chat_action until the turn ends;
    # if it outlives the test it lands stray calls in another test's fake.
    # Tests assert on the initial status send and on_tool_use updates, not on
    # the periodic refresh, so a no-op timer is behaviour-equivalent here.
    monkeypatch.setattr(bot_mod.turnctl, "_turn_timer",
                        lambda *a, **kw: None)

    # Neutralise every deferred delete. All "sleep N then deleteMessage"
    # timers (ephemeral, picker expiry, perm cancel, terminal cleanup,
    # interrupted-status fade) route through the one shared BotUI instance's
    # `delete_after`. With the real impl the daemon thread wakes after the
    # test's monkeypatch is reverted and either hits real Telegram or — worse
    # — lands a stray call in another test's fake, which is what made the
    # suite flaky under random ordering. Stub it to a no-op: the send still
    # happens (tests assert on that), only the timer is dropped.
    monkeypatch.setattr(bot_mod.ui, "delete_after",
                        lambda *a, **kw: None)

    return SimpleNamespace(
        mod=bot_mod,
        tg=fake_tg,
        claude=fake_claude,
        owner_id=bot_env.owner_id,
        forum_chat_id=bot_env.forum_chat_id,
        sessions_file=bot_env.sessions_file,
    )
