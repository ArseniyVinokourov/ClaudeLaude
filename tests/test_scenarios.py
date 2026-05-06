"""Foundation regression scenarios for the bot.

Each test drives the bot via FakeTelegram (no real HTTP) and FakeClaude
(no real subprocess), then asserts on the recorded API calls.

Helpers in `tests.fakes` provide:
  * fake_tg.inject_update / wait_for_call / find_call
  * fake_claude.script(events) — pre-load stream-json for the next spawn
"""
from __future__ import annotations

import time

from tests.fakes import callback_update, text_update


def _drain_updates(bot):
    """Pull queued updates and dispatch via bot._handle_update."""
    import telegram as tg
    for u in tg._req("getUpdates", {}).get("result", []):
        bot.mod._handle_update(u)


# ── 1. /new creates a forum topic and greets ────────────────────────

def test_new_creates_topic_and_greets(bot, tmp_path):
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    create = bot.tg.find_call("createForumTopic")
    assert create is not None, "createForumTopic was not called"
    assert str(cwd).endswith(create["name"].split(" ")[1].rstrip("…")) \
        or cwd.name in create["name"]

    bot.tg.wait_for_call("sendMessage", message_thread_id=100)
    greeting = next(
        p for p in bot.tg.calls_of("sendMessage")
        if p.get("message_thread_id") == 100
    )
    assert "Session started" in greeting["text"]
    assert str(cwd) in greeting["text"]


# ── 2. user message in session topic → claude → assistant reply ─────

def test_user_message_streams_claude_reply(bot, tmp_path):
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)

    # Start session.
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    bot.tg.reset()

    # Script claude's stream-json output for the next spawn.
    bot.claude.script([
        {"type": "system", "session_id": "claude-sess-1"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello world"},
        ]}},
        {"type": "result", "session_id": "claude-sess-1",
         "usage": {"input_tokens": 5, "output_tokens": 2}},
    ])

    bot.tg.inject_update(text_update(
        "say hi",
        owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id,
        thread_id=100,
    ))
    _drain_updates(bot)

    # Assistant reply lands in the right topic.
    msgs = bot.tg.wait_for_call("sendMessage", message_thread_id=100, timeout=3)
    bodies = [m["text"] for m in msgs]
    assert any("hello world" in t for t in bodies), bodies

    # Claude was spawned with -p "say hi" in the right cwd.
    spawn = bot.claude.last_spawn()
    cmd = spawn["cmd"]
    assert "-p" in cmd and "say hi" in cmd
    assert "stream-json" in cmd and "--verbose" in cmd
    assert spawn["cwd"] == str(cwd)

    # The system event linked the claude session id — a follow-up turn
    # should pass --resume claude-sess-1.
    bot.claude.script([
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "again"},
        ]}},
        {"type": "result", "session_id": "claude-sess-1",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    bot.tg.inject_update(text_update(
        "and again",
        owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id,
        thread_id=100,
    ))
    _drain_updates(bot)
    bot.claude.wait_for_spawns(2)
    cmd2 = bot.claude.spawns[-1]["cmd"]
    assert "--resume" in cmd2 and "claude-sess-1" in cmd2


# ── 3. tool_use updates status indicator ────────────────────────────

def test_tool_use_updates_status(bot, tmp_path):
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)

    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    bot.tg.reset()

    bot.claude.script([
        {"type": "system", "session_id": "claude-sess-1"},
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "ls -la"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"},
        ]}},
        {"type": "result", "session_id": "claude-sess-1",
         "usage": {"input_tokens": 5, "output_tokens": 2}},
    ])

    bot.tg.inject_update(text_update(
        "list files",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
        thread_id=100,
    ))
    _drain_updates(bot)

    bot.tg.wait_for_call("sendMessage", message_thread_id=100, timeout=3)

    # Bash is non-noisy so should show up as ⚙️ status with the command.
    status_texts = [
        m["text"] for m in bot.tg.calls_of("editMessageText")
    ] + [
        m["text"] for m in bot.tg.calls_of("sendMessage")
    ]
    assert any("ls -la" in t and "⚙" in t for t in status_texts), status_texts


# ── 4. result event with usage updates token totals ─────────────────

def test_usage_persists(bot, tmp_path):
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)

    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    bot.claude.script([
        {"type": "system", "session_id": "claude-sess-1"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ok"},
        ]}},
        {"type": "result", "session_id": "claude-sess-1",
         "usage": {"input_tokens": 100, "output_tokens": 25,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 5}},
    ])
    bot.tg.inject_update(text_update(
        "hi", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.tg.wait_for_call("sendMessage", message_thread_id=100, timeout=3)

    # Wait briefly for worker thread to update totals after result event.
    deadline = time.time() + 2
    sess = None
    while time.time() < deadline:
        sessions = list(bot.mod.mgr._sessions.values())
        if sessions and sessions[0].total_input_tokens > 0:
            sess = sessions[0]
            break
        time.sleep(0.01)
    assert sess is not None, "session never recorded usage"
    assert sess.total_input_tokens == 100
    assert sess.total_output_tokens == 25
    assert sess.total_cache_create == 5


# ── 5. permission hook produces Allow/Deny buttons; click → decision ─

def test_permission_flow_allow(bot, tmp_path):
    # Create a session so the hook has a route.
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    # Force-link claude_session_id so the hook routes to the topic.
    sess = next(iter(bot.mod.mgr._sessions.values()))
    bot.mod.mgr.link_claude_id("claude-sess-perm", sess)
    bot.tg.reset()

    # Simulate hook arriving via the bridge callback.
    req_id = "req-1"
    bot.mod.bridge._pending[req_id] = __import__("threading").Event()
    bot.mod.on_hook_permission(req_id, {
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
        "session_id": "claude-sess-perm",
    })

    # Bot should have sent a message with Allow/Deny inline buttons.
    perm_msgs = [
        m for m in bot.tg.calls_of("sendMessage")
        if "reply_markup" in m
    ]
    assert perm_msgs, "no permission message with buttons sent"
    buttons = perm_msgs[-1]["reply_markup"]["inline_keyboard"]
    flat = [b for row in buttons for b in row]
    allow_btn = next(b for b in flat if "Allow" in b["text"])

    # Drive the callback as if user clicked Allow.
    bot.tg.inject_update(callback_update(
        allow_btn["callback_data"],
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    # The bridge should have a decision recorded.
    deadline = time.time() + 1
    while time.time() < deadline and req_id not in bot.mod.bridge._decisions:
        time.sleep(0.01)
    assert bot.mod.bridge._decisions.get(req_id) == "allow", \
        f"expected allow, got {bot.mod.bridge._decisions}"


# ── 6. /sessions lists active session ───────────────────────────────

def test_sessions_command_lists(bot, tmp_path):
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    # Need claude_session_id to show up in /sessions list.
    sess = next(iter(bot.mod.mgr._sessions.values()))
    bot.mod.mgr.link_claude_id("claude-listed", sess)
    bot.tg.reset()

    bot.tg.inject_update(text_update(
        "/sessions",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    msgs = [m for m in bot.tg.calls_of("sendMessage") if "Sessions" in m.get("text", "")]
    assert msgs, f"no /sessions output: {bot.tg.calls_of('sendMessage')}"
    assert "demo" in msgs[-1]["text"]


# ── 7. permission Deny → bridge.decisions["deny"] ───────────────────

def test_permission_flow_deny(bot, tmp_path):
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    sess = next(iter(bot.mod.mgr._sessions.values()))
    bot.mod.mgr.link_claude_id("claude-sess-perm-d", sess)
    bot.tg.reset()

    import threading
    req_id = "req-deny"
    bot.mod.bridge._pending[req_id] = threading.Event()
    bot.mod.on_hook_permission(req_id, {
        "tool_name": "Write",
        "tool_input": {"file_path": "/etc/passwd"},
        "session_id": "claude-sess-perm-d",
    })
    perm_msg = next(
        m for m in bot.tg.calls_of("sendMessage")
        if "reply_markup" in m
    )
    flat = [b for row in perm_msg["reply_markup"]["inline_keyboard"] for b in row]
    deny_btn = next(b for b in flat if "Deny" in b["text"])

    bot.tg.inject_update(callback_update(
        deny_btn["callback_data"],
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    deadline = time.time() + 1
    while time.time() < deadline and req_id not in bot.mod.bridge._decisions:
        time.sleep(0.01)
    assert bot.mod.bridge._decisions.get(req_id) == "deny"


# ── 8. hook routing: terminal session by cwd, NOT bot session ───────

def test_hook_routing_skips_bot_session_by_cwd(bot, tmp_path):
    """Regression for project_bot_cwd_routing_bug.md: a hook from the
    terminal must NOT route into a bot-spawned session that happens to
    share its cwd. Bot session at /a + terminal hook with cwd /a should
    create a NEW (terminal) topic, not steal the bot session.
    """
    bot_cwd = tmp_path / "shared"
    bot_cwd.mkdir()

    # Spawn a bot session at the shared cwd.
    bot.tg.inject_update(text_update(
        f"/new {bot_cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    bot_session = bot.mod.mgr.by_cwd(str(bot_cwd))
    assert bot_session is not None and bot_session.is_bot_spawned

    # Terminal hook arrives with the same cwd but a fresh claude_session_id.
    resolved = bot.mod._resolve_hook_session(
        "terminal-claude-id",
        {"cwd": str(bot_cwd)},
    )
    assert resolved is not None
    assert resolved.sid != bot_session.sid, \
        "terminal hook stole the bot session — cwd routing bug regressed"
    assert resolved.is_bot_spawned is False
    # Bot session is intact.
    assert bot.mod.mgr.by_cwd(str(bot_cwd)).sid == bot_session.sid \
        or bot.mod.mgr._sessions[bot_session.sid].alive


# ── 9. /stop then /restart ──────────────────────────────────────────

def test_stop_then_restart(bot, tmp_path):
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    sess = next(iter(bot.mod.mgr._sessions.values()))
    bot.mod.mgr.link_claude_id("claude-restart", sess)
    topic_id = sess.topic_id

    # Stop.
    bot.tg.inject_update(text_update(
        "/stop",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
        thread_id=topic_id,
    ))
    _drain_updates(bot)
    assert sess.alive is False

    # Restart.
    bot.tg.inject_update(text_update(
        "/restart",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
        thread_id=topic_id,
    ))
    _drain_updates(bot)
    assert sess.alive is True

    # New worker should accept user messages again.
    bot.claude.script([
        {"type": "system", "session_id": "claude-restart"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "back"},
        ]}},
        {"type": "result", "session_id": "claude-restart",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    bot.tg.inject_update(text_update(
        "ping",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
        thread_id=topic_id,
    ))
    _drain_updates(bot)
    bot.claude.wait_for_spawns(1)


# ── 10. topic-gone cleanup primitives ───────────────────────────────

def test_invalidate_session_cleans_maps(bot, tmp_path):
    """When the bot detects a topic is gone, it must drop the session
    from all routing maps (by_topic, by_cwd, by_claude_session_id) and
    stop the worker. This is the core of the healthcheck-driven cleanup;
    higher-level wrappers (topic_alive probe, _invalidate_and_stop) are
    in the lifecycle-batch branch and tested there.
    """
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    sess = next(iter(bot.mod.mgr._sessions.values()))
    bot.mod.mgr.link_claude_id("claude-orphan", sess)

    # Sanity: routes resolve.
    assert bot.mod.mgr.by_topic(sess.topic_id) is sess
    assert bot.mod.mgr.by_cwd(str(cwd)) is sess
    assert bot.mod.mgr.by_claude_session_id("claude-orphan") is sess

    # Topic deleted → stop first, then invalidate maps. (Order matters:
    # invalidate pops from _sessions, after which stop becomes a no-op.
    # The lifecycle-batch branch consolidates this into a single helper.)
    bot.mod.mgr.stop(sess.sid)
    bot.mod._invalidate_session(sess)

    assert sess.alive is False
    assert bot.mod.mgr.by_topic(sess.topic_id) is None
    assert bot.mod.mgr.by_cwd(str(cwd)) is None
    assert bot.mod.mgr.by_claude_session_id("claude-orphan") is None


# ── 11. markdown table → mobile list ────────────────────────────────

def test_markdown_table_to_mobile_list(bot):
    src = (
        "| File | Lines |\n"
        "|------|-------|\n"
        "| a.py | 100 |\n"
        "| b.py | 200 |\n"
    )
    out = bot.mod._md_table_to_list(src)
    assert "**a.py**" in out
    assert "**b.py**" in out
    assert "Lines: 100" in out
    assert "Lines: 200" in out
    # No raw pipe table rows in the output.
    assert "|------|" not in out


# ── 12. long message splits on newline boundary ─────────────────────

def test_send_long_splits_at_newline(bot):
    import telegram as tg_mod
    # Build a body well over 4096 with a clear newline boundary.
    chunk_a = "A" * 3000
    chunk_b = "B" * 3000
    text = chunk_a + "\n" + chunk_b
    ids = tg_mod.send_long(text, bot.forum_chat_id)
    assert len(ids) == 2
    sent = bot.tg.calls_of("sendMessage")[-2:]
    bodies = [s["text"] for s in sent]
    # First chunk should end at the newline (no B leaked into chunk 1).
    assert "B" not in bodies[0]
    assert "A" not in bodies[1]
