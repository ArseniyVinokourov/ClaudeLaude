"""Pytest fixtures for bot scenario tests.

Each test gets a freshly-imported `bot` module wired up to FakeTelegram and
FakeClaude. State files (.state.json, .sessions.json) live in a per-test
tempdir via env-overridable paths added to config.py / sessions.py.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
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
        "audit", "device_monitor", "terminal_mirror", "runtime",
        # Component modules cache `import telegram as tg` at import time;
        # purge them too so a fresh bot import rebinds their `tg` to the
        # freshly-patched telegram module (else they hold a stale one).
        "turncontroller", "dashboard", "formatting", "session_discovery",
        "updater", "mirrorbridge", "botui", "lifecycle", "hookhandlers",
        "commands", "questions", "tour", "media", "settings",
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
    # Pin speech analysis OFF (the documented default). config.load_dotenv runs
    # at import with override=False, so any knob the fixture doesn't pin leaks
    # in from the developer's real .env — a local SPEECH_ANALYZERS=timing toggle
    # would otherwise add an analysis attachment that count-asserting media
    # tests don't expect. The dedicated speech tests override these. emotion has
    # its OWN knob (SPEECH_EMOTION_MODEL), so pin it too or a developer's
    # emotion=light .env leaks an extra worker analyzer into every test.
    monkeypatch.setenv("SPEECH_ANALYZERS", "")
    monkeypatch.setenv("SPEECH_EMOTION_MODEL", "")
    monkeypatch.setenv("BOT_STATE_FILE", str(state_file))
    monkeypatch.setenv("BOT_SESSIONS_FILE", str(sessions_file))
    monkeypatch.setenv("BOT_MIRRORS_FILE", str(mirrors_file))

    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "demo").mkdir()

    # /settings persists knobs via config.set_env, which writes os.environ
    # directly (not through monkeypatch). Snapshot the env now and restore it
    # on teardown so a knob a test changes (UPLOAD_TTL_S, DEFAULT_DISPLAY, …)
    # doesn't leak into the next test's fresh runtime.rt.
    env_snapshot = dict(os.environ)

    _purge_bot_modules()
    yield SimpleNamespace(
        tmp_path=tmp_path,
        state_file=state_file,
        sessions_file=sessions_file,
        owner_id=OWNER_ID,
        forum_chat_id=FORUM_CHAT_ID,
    )
    _purge_bot_modules()
    os.environ.clear()
    os.environ.update(env_snapshot)


@pytest.fixture
def bot(bot_env, monkeypatch: pytest.MonkeyPatch):
    # Fresh group-budget singleton per test. It's a module global whose
    # 60s sliding window does NOT decay within a ~30s suite run, so writes
    # from earlier tests would otherwise throttle/pace later ones — e.g.
    # starving the mirror-backfill test until its poll window expires
    # (sends then leak past teardown into the real API). Reset isolates it.
    import budget as _budget
    _budget.reset_for_tests()

    fake_tg = FakeTelegram()
    fake_claude = FakeClaudeFactory()

    # Patch BEFORE importing bot — bot.py instantiates SessionManager at
    # import time, which can spawn worker threads on _restore().
    import telegram as telegram_mod
    monkeypatch.setattr(telegram_mod, "_req", fake_tg.req)

    # Bypass the group-budget queue in scenario tests: run every paid write
    # directly through the fake. The budget only paces/drops sends to respect
    # real TG rate limits — against an instant fake it adds nothing but timing
    # nondeterminism (e.g. the mirror backfill's 20 sends getting paced past a
    # poll deadline). The budget itself is covered by test_budget.py.
    monkeypatch.setattr(telegram_mod, "_via_budget",
                        lambda chat_id, prio, fn, *a, **kw: fn(*a, **kw))

    import sessions as sessions_mod
    monkeypatch.setattr(sessions_mod.subprocess, "Popen", fake_claude)

    # Suppress real threads from auto-rename helper (it shells out to claude).
    bot_mod = importlib.import_module("bot")

    # /settings persists knobs via config.set_env, which writes the repo .env.
    # Redirect it to a tmp file so tests never touch the real one.
    import config as config_mod
    monkeypatch.setattr(config_mod, "_ENV_FILE",
                        str(bot_env.tmp_path / ".env"))

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

    ns = SimpleNamespace(
        mod=bot_mod,
        tg=fake_tg,
        claude=fake_claude,
        owner_id=bot_env.owner_id,
        forum_chat_id=bot_env.forum_chat_id,
        sessions_file=bot_env.sessions_file,
    )
    yield ns
    # Shut down every background thread the test spawned BEFORE monkeypatch
    # unwinds the fake telegram. A session worker mid-_run_claude or a live
    # mirror follower would otherwise keep sending after the fake reverts,
    # landing "send error: 404" on the real API — a load-sensitive flake
    # (~13% under a loaded/concurrent runner, ~0% idle). "Thread dead" isn't
    # a usable idle signal (workers block on their queue, followers poll on
    # a flag), so signal the loops to exit and join them deterministically
    # ([[gate-thread-racing-tests-on-completion-signal]]):
    #   - sessions: alive=False + a None sentinel wakes the queue.get; the
    #     worker finishes any in-flight _run_claude, then sees the flag and
    #     returns. We set the flag directly (not mgr.stop) to avoid firing
    #     the on_session_stop callback, which itself sends.
    #   - mirrors: alive=False; _follow_loop exits within its 0.5s poll.
    # Then join the named worker/follower/backfill threads (cap 5s total).
    import threading
    for s in bot_mod.mgr.list_sessions():
        s.alive = False
        try:
            s._queue.put_nowait(None)
        except Exception:
            pass
    for m in bot_mod.mirror_mgr.list():
        m.alive = False
    deadline = time.time() + 5.0
    for t in threading.enumerate():
        if t is threading.current_thread() or not t.is_alive():
            continue
        # session-worker-* / mirror-* : the long-lived loop threads (stopped
        # via the alive=False flags above). bot-bg-* : the short-lived
        # daemon threads/timers the bot spawns to send off the poll loop
        # (media handlers, runtime install, perm/ephemeral deletes, compact,
        # update). Joining them all means no bot thread can send after the
        # fake telegram is reverted ("send error: 404" flake).
        if t.name.startswith(("session-worker", "mirror-follow",
                              "mirror-backfill", "bot-bg")):
            t.join(timeout=max(0.0, deadline - time.time()))
    # Stop the budget worker thread so it can't outlive the test and land
    # stray calls in the next test's fake.
    _budget.reset_for_tests()
