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


def test_pre_turn_session_visible_and_persisted(bot, tmp_path):
    """A /new'd session must show up in /sessions and persist even before
    its first turn (when claude_session_id is still unassigned)."""
    cwd = tmp_path / "fresh"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    sess = next(iter(bot.mod.mgr._sessions.values()))
    assert sess.claude_session_id is None
    assert sess.topic_id

    import json
    import os
    persist_path = os.environ["BOT_SESSIONS_FILE"]
    assert os.path.exists(persist_path)
    records = json.load(open(persist_path))
    rec = next((r for r in records if r["sid"] == sess.sid), None)
    assert rec is not None, \
        f"pre-turn session missing from persist: {records}"
    assert rec.get("topic_label"), \
        f"topic_label not persisted for fresh session: {rec}"

    bot.tg.reset()
    bot.tg.inject_update(text_update(
        "/sessions",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    msgs = [m for m in bot.tg.calls_of("sendMessage")
            if "Sessions" in m.get("text", "")]
    assert msgs, "no /sessions reply"
    assert "fresh" in msgs[-1]["text"], \
        f"pre-turn session not in /sessions: {msgs[-1]['text']}"


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


# ── 10b. topic_alive probe (production path) → invalidate ──────────

def test_topic_alive_probe_invalidates_dead(bot, tmp_path):
    """Regression: when a topic is gone, the production silent probe
    (telegram.topic_alive via editForumTopic) must return False so the
    healthcheck can invalidate the session. Without this, the bot keeps
    orphaned sessions whose topic the user has already deleted.
    """
    import telegram as tg_mod
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    sess = next(iter(bot.mod.mgr._sessions.values()))
    bot.mod.mgr.link_claude_id("claude-probed", sess)

    # Sanity: live topic probes True.
    assert tg_mod.topic_alive(bot.forum_chat_id, sess.topic_id,
                              name="anything") is True

    # Mark topic as dead → editForumTopic probe must surface as False.
    bot.tg.dead_topics.add(sess.topic_id)
    assert tg_mod.topic_alive(bot.forum_chat_id, sess.topic_id,
                              name="anything") is False

    # And the bot's invalidate path drops every routing map.
    bot.mod._invalidate_and_stop(sess, "topic deleted")
    assert sess.alive is False
    assert bot.mod.mgr.by_topic(sess.topic_id) is None
    assert bot.mod.mgr.by_cwd(str(cwd)) is None
    assert bot.mod.mgr.by_claude_session_id("claude-probed") is None


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


# ── 13. permission done → ephemeral 1s (edit + schedule delete) ────

def test_permission_done_ephemeral(bot, tmp_path):
    """After Allow/Deny, the perm message is edited to ✅/❌ and
    scheduled for deletion (1s ephemeral instead of permanent)."""
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    sess = next(iter(bot.mod.mgr._sessions.values()))
    bot.mod.mgr.link_claude_id("claude-perm-eph", sess)
    bot.tg.reset()

    import threading
    req_id = "req-eph"
    bot.mod.bridge._pending[req_id] = threading.Event()
    bot.mod.on_hook_permission(req_id, {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "session_id": "claude-perm-eph",
    })
    perm_msg = next(
        m for m in bot.tg.calls_of("sendMessage")
        if "reply_markup" in m
    )
    flat = [b for row in perm_msg["reply_markup"]["inline_keyboard"]
            for b in row]
    allow_btn = next(b for b in flat if "Allow" in b["text"])

    bot.tg.inject_update(callback_update(
        allow_btn["callback_data"],
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    edits = bot.tg.calls_of("editMessageText")
    assert any(e["text"] == "✓ Allowed" for e in edits), \
        f"expected edit to '✓ Allowed', got: {[e['text'] for e in edits]}"


# ── 14. compact button replaces ✅ finish line ─────────────────────

def test_compact_button_no_checkmark(bot, tmp_path):
    """Turn completion shows Compact button, NOT the old ✅ stats line."""
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    bot.tg.reset()

    bot.claude.script([
        {"type": "system", "session_id": "claude-compact"},
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "ls"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "listed files"},
        ]}},
        {"type": "result", "session_id": "claude-compact",
         "usage": {"input_tokens": 10, "output_tokens": 5}},
    ])
    bot.tg.inject_update(text_update(
        "list", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.tg.wait_for_call("sendMessage", message_thread_id=100, timeout=3)

    # Compact button is now added via editMessageReplyMarkup on last msg.
    edits = bot.tg.calls_of("editMessageReplyMarkup")
    compact_edits = [
        e for e in edits
        if "reply_markup" in e
        and any("Compact" in b["text"]
                for row in e["reply_markup"]["inline_keyboard"]
                for b in row)
    ]
    assert compact_edits, "no Compact button via editMessageReplyMarkup"
    # No separate "·" or "✅" anchor message.
    anchor_msgs = [
        m for m in bot.tg.calls_of("sendMessage")
        if m.get("message_thread_id") == 100
        and m.get("text") in ("·", "✅")
    ]
    assert not anchor_msgs, f"unexpected anchor message: {anchor_msgs}"


# ── 15. close button deletes message ──────────────────────────────

def test_close_button_deletes_message(bot, tmp_path):
    bot.tg.inject_update(callback_update(
        "close",
        owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id,
        message_id=999,
    ))
    _drain_updates(bot)
    assert 999 in bot.tg.deleted_messages


# ── 16. dashboard build ───────────────────────────────────────────

def test_dashboard_build(bot, tmp_path):
    """Dashboard text includes version and session count."""
    cwd = tmp_path / "demo"
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    text = bot.mod._build_dashboard()
    assert "ClaudeLaude" in text
    assert "1 active" in text


# ── security: callback OWNER_ID check ─────────────────────────────

def test_callback_from_stranger_is_ignored(bot):
    """Callback queries from non-owner users must be silently dropped."""
    stranger_id = 9999
    bot.tg.inject_update(callback_update(
        "m:new",
        owner_id=stranger_id,
        forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    assert bot.tg.find_call("createForumTopic") is None


# ── security: kill switch ──────────────────────────────────────────

def test_kill_switch_blocks_messages(bot, tmp_path):
    """When .kill exists, bot ignores all messages."""
    import config
    config.activate_kill()
    try:
        cwd = tmp_path / "demo"
        cwd.mkdir(exist_ok=True)
        bot.tg.inject_update(text_update(
            f"/new {cwd}",
            owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
        ))
        _drain_updates(bot)

        assert bot.tg.find_call("createForumTopic") is None
    finally:
        config.deactivate_kill()


def test_unlock_word_restores_after_kill(bot, tmp_path, monkeypatch):
    """Unlock word in General deactivates kill switch."""
    import config
    monkeypatch.setattr("config.UNLOCK_WORD", "s3cret")
    monkeypatch.setattr("bot.UNLOCK_WORD", "s3cret")
    config.activate_kill()
    try:
        bot.tg.inject_update(text_update(
            "s3cret",
            owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
        ))
        _drain_updates(bot)
        assert not config.is_killed()
    finally:
        config.deactivate_kill()


def test_unlock_word_rejected_in_topic(bot, tmp_path, monkeypatch):
    """Unlock word sent in a topic (not General) must be ignored."""
    import config
    monkeypatch.setattr("config.UNLOCK_WORD", "s3cret")
    monkeypatch.setattr("bot.UNLOCK_WORD", "s3cret")
    config.activate_kill()
    try:
        bot.tg.inject_update(text_update(
            "s3cret",
            owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
            thread_id=42,
        ))
        _drain_updates(bot)
        assert config.is_killed()
    finally:
        config.deactivate_kill()


def test_unlock_word_injection_resistance(bot, tmp_path, monkeypatch):
    """Partial matches and substrings must not unlock."""
    import config
    monkeypatch.setattr("config.UNLOCK_WORD", "s3cret")
    monkeypatch.setattr("bot.UNLOCK_WORD", "s3cret")
    config.activate_kill()
    try:
        for attempt in ["s3cre", "s3crett", "S3CRET", " s3cret extra"]:
            bot.tg.inject_update(text_update(
                attempt,
                owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
            ))
            _drain_updates(bot)
            assert config.is_killed(), f"unlocked with {attempt!r}"
    finally:
        config.deactivate_kill()


# ── security: audit log ───────────────────────────────────────────

def test_audit_log_writes_events(bot, tmp_path):
    """audit.log() writes JSON lines to .audit.log."""
    import audit
    audit.log("test_event", "some detail", sid="abc123")
    time.sleep(0.1)  # wait for background writer
    entries = audit.tail(5)
    assert any(e.get("event") == "test_event" for e in entries)


# ── terminal watcher: cleanup on session progress ─────────────────

def test_terminal_watcher_cleans_notification(bot, tmp_path, monkeypatch):
    """Notification in terminal topic is deleted when JSONL grows."""
    import json as _json

    projects_dir = tmp_path / "claude_projects" / "proj"
    projects_dir.mkdir(parents=True)
    monkeypatch.setattr(bot.mod, "CLAUDE_PROJECTS_DIR",
                        str(projects_dir.parent))

    csid = "term-session-001"
    jsonl = projects_dir / f"{csid}.jsonl"
    jsonl.write_text(_json.dumps({"type": "user", "cwd": "/tmp"}) + "\n")

    # Simulate: terminal session registered, notification sent to topic
    bot.mod.mgr.register_terminal(csid, 100, cwd="/tmp")
    mid = bot.mod.send_to_topic(100, "\U0001f514 test notification")
    assert mid is not None
    bot.mod._track_terminal_msg(csid, mid, bot.forum_chat_id, "notification")

    # JSONL grows → watcher should clean up
    with open(jsonl, "a") as f:
        f.write(_json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"}]}}) + "\n")

    bot.mod._cleanup_terminal_pending(csid)
    assert mid in bot.tg.deleted_messages


def test_terminal_watcher_cleans_permission(bot, tmp_path, monkeypatch):
    """Permission buttons in terminal topic are resolved when JSONL grows."""
    import json as _json

    projects_dir = tmp_path / "claude_projects" / "proj"
    projects_dir.mkdir(parents=True)
    monkeypatch.setattr(bot.mod, "CLAUDE_PROJECTS_DIR",
                        str(projects_dir.parent))

    csid = "term-session-002"
    jsonl = projects_dir / f"{csid}.jsonl"
    jsonl.write_text(_json.dumps({"type": "user", "cwd": "/tmp"}) + "\n")

    session = bot.mod.mgr.register_terminal(csid, 100, cwd="/tmp")

    # Simulate a permission message
    short_id = "abcdef123456"
    mid = bot.mod.send_to_topic(100, "Bash\nls -la")
    assert mid is not None
    with bot.mod.state.lock:
        bot.mod.state.perm_key_map[short_id] = f"full-req-{short_id}"
        bot.mod.state.pending_permissions[short_id] = (
            mid, bot.forum_chat_id, session.sid)
    bot.mod._track_terminal_msg(csid, mid, bot.forum_chat_id,
                                f"perm:{short_id}")

    # Grow JSONL
    with open(jsonl, "a") as f:
        f.write(_json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ok"}]}}) + "\n")

    bot.mod._cleanup_terminal_pending(csid)

    # Permission should be resolved
    assert bot.tg.messages[mid]["text"] == "✓ Resolved in terminal"
    with bot.mod.state.lock:
        assert short_id not in bot.mod.state.pending_permissions
        assert short_id not in bot.mod.state.perm_key_map


def test_terminal_watcher_offset_init(bot, tmp_path, monkeypatch):
    """First track initializes offset to current file size (no false cleanup)."""
    import json as _json

    projects_dir = tmp_path / "claude_projects" / "proj"
    projects_dir.mkdir(parents=True)
    monkeypatch.setattr(bot.mod, "CLAUDE_PROJECTS_DIR",
                        str(projects_dir.parent))

    csid = "term-session-003"
    jsonl = projects_dir / f"{csid}.jsonl"
    jsonl.write_text(_json.dumps({"type": "user", "cwd": "/tmp"}) + "\n")

    bot.mod.mgr.register_terminal(csid, 100, cwd="/tmp")
    mid = bot.mod.send_to_topic(100, "\U0001f514 hello")
    assert mid is not None

    # Track records current offset
    bot.mod._track_terminal_msg(csid, mid, bot.forum_chat_id, "notification")
    initial_offset = bot.mod._watcher_offsets[csid]
    assert initial_offset > 0

    # Watcher poll: no growth → no cleanup
    import os
    size = os.path.getsize(str(jsonl))
    assert size <= initial_offset
    # pending_terminal_msgs still has the entry
    with bot.mod.state.lock:
        assert csid in bot.mod.state.pending_terminal_msgs


# ── session-quality: context injection + /mode ──────────────────────

def _start_bot_session(bot, tmp_path, name="demo"):
    cwd = tmp_path / name
    cwd.mkdir(exist_ok=True)
    bot.tg.inject_update(text_update(
        f"/new {cwd}",
        owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)
    bot.tg.reset()
    return cwd


def _append_system_prompt(cmd: list[str]) -> str:
    """Extract --append-system-prompt value from a claude spawn cmd, or ''."""
    for i, tok in enumerate(cmd):
        if tok == "--append-system-prompt" and i + 1 < len(cmd):
            return cmd[i + 1]
    return ""


def _permission_mode(cmd: list[str]) -> str:
    for i, tok in enumerate(cmd):
        if tok == "--permission-mode" and i + 1 < len(cmd):
            return cmd[i + 1]
    return ""


def test_session_context_is_appended(bot, tmp_path):
    cwd = _start_bot_session(bot, tmp_path)
    bot.tg.inject_update(text_update(
        "say hi", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.claude.wait_for_spawns(1)

    cmd = bot.claude.last_spawn()["cmd"]
    appended = _append_system_prompt(cmd)
    assert "ClaudeLaude bot session" in appended
    assert "topic_id: 100" in appended
    assert str(cwd) in appended
    assert "mode: default" in appended
    assert _permission_mode(cmd) == "auto"


def test_mode_plan_switches_permission(bot, tmp_path):
    _start_bot_session(bot, tmp_path)
    bot.tg.inject_update(text_update(
        "/mode plan", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.tg.inject_update(text_update(
        "investigate", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.claude.wait_for_spawns(1)

    cmd = bot.claude.last_spawn()["cmd"]
    assert _permission_mode(cmd) == "plan"
    assert "mode: plan" in _append_system_prompt(cmd)


def test_mode_terse_injects_style(bot, tmp_path):
    _start_bot_session(bot, tmp_path)
    bot.tg.inject_update(text_update(
        "/mode terse", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.tg.inject_update(text_update(
        "go", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.claude.wait_for_spawns(1)

    appended = _append_system_prompt(bot.claude.last_spawn()["cmd"])
    assert "Response style: terse" in appended
    # Style addendum + context both present
    assert "ClaudeLaude bot session" in appended


def test_mode_unknown_rejected_and_persisted_default(bot, tmp_path):
    _start_bot_session(bot, tmp_path)
    bot.tg.reset()
    bot.tg.inject_update(text_update(
        "/mode bogus", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)

    replies = [m["text"] for m in bot.tg.calls_of("sendMessage")
               if m.get("message_thread_id") == 100]
    assert any("Unknown mode" in t for t in replies), replies

    # Session.mode untouched
    sid = bot.mod.mgr._topic_map[100]
    assert bot.mod.mgr._sessions[sid].mode == "default"


def test_mode_persists_across_restore(bot, tmp_path, monkeypatch):
    import json
    _start_bot_session(bot, tmp_path)
    bot.tg.inject_update(text_update(
        "/mode verbose", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)

    sid = bot.mod.mgr._topic_map[100]
    assert bot.mod.mgr._sessions[sid].mode == "verbose"

    # Persistence file written
    import sessions as sess_mod
    with open(sess_mod._PERSIST_PATH) as f:
        records = json.load(f)
    rec = next(r for r in records if r["sid"] == sid)
    assert rec["mode"] == "verbose"


# ── hook DoS guard ──────────────────────────────────────────────────

def test_hook_resolver_refuses_empty_payload(bot, tmp_path):
    """An empty hook body must NOT spawn a forum topic."""
    before = len(bot.tg.calls_of("createForumTopic"))
    result = bot.mod._resolve_hook_session("", {})
    after = len(bot.tg.calls_of("createForumTopic"))
    assert result is None
    assert after == before, "empty hook payload caused topic creation (DoS)"


def test_hook_resolver_refuses_no_sid_no_cwd(bot, tmp_path):
    """Payload with hook_event_name but no session_id/cwd is still rejected."""
    before = len(bot.tg.calls_of("createForumTopic"))
    result = bot.mod._resolve_hook_session(
        "", {"hook_event_name": "Notification", "message": "ping"}
    )
    after = len(bot.tg.calls_of("createForumTopic"))
    assert result is None
    assert after == before


# ── /interrupt UX ───────────────────────────────────────────────────

def test_interrupt_repaints_status_message(bot, tmp_path):
    """A live turn-status message should be edited to "⏹ Interrupted"."""
    _start_bot_session(bot, tmp_path)
    sid = next(iter(bot.mod.mgr._sessions))
    session = bot.mod.mgr._sessions[sid]
    # Plant a live turn with an existing status message.
    turn = bot.mod._get_turn(session)
    turn.status_msg_id = 9999
    # Patch interrupt to no-op success.
    bot.mod.mgr.interrupt = lambda _sid: True
    bot.tg.reset()
    bot.mod._do_interrupt(session, bot.forum_chat_id, session.topic_id)
    edits = [m for m in bot.tg.calls_of("editMessageText")
             if m.get("message_id") == 9999]
    assert any("Interrupted" in e.get("text", "") for e in edits), \
        f"no status edit to 'Interrupted': {edits}"
    assert turn.interrupted is True


# ── stickers as input ──────────────────────────────────────────────

def test_sticker_routed_to_claude_as_text(bot, tmp_path):
    """Sending a sticker in an active bot session feeds Claude a textual
    descriptor including emoji and pack name."""
    _start_bot_session(bot, tmp_path)
    captured: list[str] = []
    original = bot.mod.mgr.send_user_message
    def _cap(_sid, text):
        captured.append(text)
        return True
    bot.mod.mgr.send_user_message = _cap
    try:
        bot.tg.inject_update({
            "update_id": 9001,
            "message": {
                "message_id": 4242,
                "from": {"id": bot.owner_id},
                "chat": {"id": bot.forum_chat_id, "type": "supergroup"},
                "message_thread_id": 100,
                "date": 0,
                "sticker": {
                    "file_id": "X", "file_unique_id": "Y",
                    "width": 512, "height": 512, "is_animated": False,
                    "is_video": False, "type": "regular",
                    "emoji": "🚀", "set_name": "RocketPack",
                },
            },
        })
        _drain_updates(bot)
    finally:
        bot.mod.mgr.send_user_message = original
    assert captured, "send_user_message never called for sticker"
    assert "🚀" in captured[0]
    assert "RocketPack" in captured[0]


# ── reactions on user messages ──────────────────────────────────────

def test_user_text_gets_eyes_reaction(bot, tmp_path):
    """Sending plain text in an active bot session triggers setMessageReaction
    with 👀 on the user's message."""
    _start_bot_session(bot, tmp_path)
    bot.tg.reset()
    bot.tg.inject_update(text_update(
        "say hi", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    reactions = bot.tg.calls_of("setMessageReaction")
    assert reactions, f"no setMessageReaction call: {bot.tg.calls}"
    last = reactions[-1]
    import json
    payload = json.loads(last.get("reaction", "[]"))
    assert any(r.get("emoji") == "👀" for r in payload), \
        f"expected 👀 reaction, got {payload}"


# ── status timer hourglass rotates per tick (Batch A #2) ──────────────

def test_status_hourglass_rotates(bot):
    """⏳ and ⌛ alternate each 3-second tick when no tool ops have run."""
    from bot import _format_status, TurnState
    now = time.time()
    turn = TurnState()
    # Elapsed 0s → ⏳
    turn.started_at = now
    assert _format_status(turn).startswith("⏳"), _format_status(turn)
    # Elapsed 3s → ⌛
    turn.started_at = now - 3
    assert _format_status(turn).startswith("⌛"), _format_status(turn)
    # Elapsed 6s → ⏳
    turn.started_at = now - 6
    assert _format_status(turn).startswith("⏳"), _format_status(turn)
    # Elapsed 9s → ⌛
    turn.started_at = now - 9
    assert _format_status(turn).startswith("⌛"), _format_status(turn)
    # When a tool op is recorded, the ⚙️ status replaces the hourglass.
    turn.started_at = now
    turn.tool_ops.append("$ ls")
    assert _format_status(turn).startswith("⚙"), _format_status(turn)


# ── tool_use parsing: Claude Code 2.1.143 nested-content format ──────

def test_tool_use_inside_assistant_message(bot, tmp_path):
    """Claude Code 2.1.143+ emits tool_use as a content block inside the
    assistant message. We must surface it as on_tool_use, not drop it."""
    _start_bot_session(bot, tmp_path)
    bot.tg.reset()
    bot.claude.script([
        {"type": "system", "session_id": "nested-tool"},
        # Real 2.1.143 shape: assistant message whose content list mixes
        # text and tool_use blocks.
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "ls /tmp", "description": "list"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "x",
             "content": "fake output"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"},
        ]}},
        {"type": "result", "session_id": "nested-tool",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    bot.tg.inject_update(text_update(
        "list", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.tg.wait_for_call("sendMessage", message_thread_id=100, timeout=3)

    # The status message must show the Bash op, proving on_tool_use ran.
    status_texts = [
        m["text"] for m in bot.tg.calls_of("editMessageText")
    ] + [
        m["text"] for m in bot.tg.calls_of("sendMessage")
    ]
    assert any("ls /tmp" in t and "⚙" in t for t in status_texts), \
        f"tool_use inside assistant message was not surfaced: {status_texts}"


# ── reaction lifecycle 👀→🔥→⚡→👍 (Batch A #5) ────────────────────────

def _reaction_emojis(bot) -> list[str]:
    import json as _json
    out: list[str] = []
    for r in bot.tg.calls_of("setMessageReaction"):
        payload = _json.loads(r.get("reaction", "[]"))
        out.extend(p.get("emoji") for p in payload)
    return out


def test_reaction_lifecycle_text_only(bot, tmp_path):
    """Text-only turn: 👀 (receive) → 🔥 (streaming) → 👍 (done)."""
    _start_bot_session(bot, tmp_path)
    bot.tg.reset()
    bot.claude.script([
        {"type": "system", "session_id": "lc-text"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi"},
        ]}},
        {"type": "result", "session_id": "lc-text",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    bot.tg.inject_update(text_update(
        "hello", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.tg.wait_for_call("sendMessage", message_thread_id=100, timeout=3)
    bot.tg.wait_for_call("setMessageReaction", count=3, timeout=3)

    emojis = _reaction_emojis(bot)
    assert "👀" in emojis, emojis
    assert "🔥" in emojis, emojis
    assert "👍" in emojis, emojis
    assert emojis.index("👀") < emojis.index("🔥") < emojis.index("👍"), emojis


def test_reaction_lifecycle_with_tool_use(bot, tmp_path):
    """Tool-using turn: 👀 → ⚡ (tool) → 👍 (done). 🔥 is skipped because
    ⚡ takes precedence over the streaming flag."""
    _start_bot_session(bot, tmp_path)
    bot.tg.reset()
    bot.claude.script([
        {"type": "system", "session_id": "lc-tool"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "listed"},
        ]}},
        {"type": "result", "session_id": "lc-tool",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    bot.tg.inject_update(text_update(
        "go", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.tg.wait_for_call("sendMessage", message_thread_id=100, timeout=3)
    bot.tg.wait_for_call("setMessageReaction", count=3, timeout=3)

    emojis = _reaction_emojis(bot)
    assert "👀" in emojis, emojis
    assert "⚡" in emojis, emojis
    assert "👍" in emojis, emojis
    # 🔥 must NOT appear once ⚡ has taken over.
    assert "🔥" not in emojis, emojis
    assert emojis.index("👀") < emojis.index("⚡") < emojis.index("👍"), emojis


# ── contextual sendChatAction (Batch A #4) ────────────────────────────

def test_chat_action_upload_document_for_read(bot, tmp_path):
    """Read tool → sendChatAction(action="upload_document")."""
    _start_bot_session(bot, tmp_path)
    bot.tg.reset()
    bot.claude.script([
        {"type": "system", "session_id": "ca-read"},
        {"type": "tool_use", "name": "Read",
         "input": {"file_path": "/tmp/foo"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ok"},
        ]}},
        {"type": "result", "session_id": "ca-read",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    bot.tg.inject_update(text_update(
        "read foo", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.tg.wait_for_call("sendMessage", message_thread_id=100, timeout=3)
    actions = [c.get("action") for c in bot.tg.calls_of("sendChatAction")]
    assert "upload_document" in actions, \
        f"Read should map to upload_document; got {actions}"


def test_chat_action_find_location_for_websearch(bot, tmp_path):
    """WebSearch tool → sendChatAction(action="find_location")."""
    _start_bot_session(bot, tmp_path)
    bot.tg.reset()
    bot.claude.script([
        {"type": "system", "session_id": "ca-web"},
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "x"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ok"},
        ]}},
        {"type": "result", "session_id": "ca-web",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    bot.tg.inject_update(text_update(
        "search", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=100,
    ))
    _drain_updates(bot)
    bot.tg.wait_for_call("sendMessage", message_thread_id=100, timeout=3)
    actions = [c.get("action") for c in bot.tg.calls_of("sendChatAction")]
    assert "find_location" in actions, \
        f"WebSearch should map to find_location; got {actions}"


def test_chat_action_mapper_unit(bot):
    """Direct unit test of the tool→action mapping.

    Pulls _chat_action_for_tool through the `bot` fixture so config.py
    picks up the test BOT_TOKEN env-stub instead of trying to read .env.
    """
    fn = bot.mod._chat_action_for_tool
    assert fn(None) == "typing"
    assert fn("Bash") == "typing"
    assert fn("Read") == "upload_document"
    assert fn("Edit") == "upload_document"
    assert fn("Write") == "upload_document"
    assert fn("Glob") == "upload_document"
    assert fn("WebFetch") == "find_location"
    assert fn("WebSearch") == "find_location"
    assert fn("UnknownTool") == "typing"


# ── sendMediaGroup for multi-image output (Batch A #15) ──────────────

def test_multi_image_uses_send_media_group(bot, tmp_path, monkeypatch):
    """When a turn finishes with 2+ pending images, the bot should call
    telegram.send_media_group once (album) instead of sendPhoto N times."""
    _start_bot_session(bot, tmp_path)
    sid = next(iter(bot.mod.mgr._sessions))
    sess = bot.mod.mgr._sessions[sid]
    bot.mod.mgr.link_claude_id("mg-sess", sess)

    # Stage 3 real-on-disk image files; on_result checks os.path.isfile.
    paths = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header
        paths.append(str(p))
    sess.pending_images.extend(paths)

    # Stub send_media_group / send_photo so we observe call shape.
    import telegram as tg_mod
    group_calls: list[tuple] = []
    photo_calls: list[tuple] = []
    monkeypatch.setattr(tg_mod, "send_media_group",
                        lambda chat_id, paths, thread_id=None:
                        group_calls.append((chat_id, list(paths), thread_id)) or [])
    monkeypatch.setattr(tg_mod, "send_photo",
                        lambda chat_id, p, caption="", thread_id=None:
                        photo_calls.append((chat_id, p, thread_id)) or None)

    bot.mod.on_result(sess, "", "")

    assert len(group_calls) == 1, group_calls
    chat_id, sent_paths, thread = group_calls[0]
    assert chat_id == bot.forum_chat_id
    assert sent_paths == paths
    assert thread == sess.topic_id
    assert photo_calls == [], photo_calls
    assert sess.pending_images == []


def test_single_image_uses_send_photo(bot, tmp_path, monkeypatch):
    """One image → keep using sendPhoto (no album needed)."""
    _start_bot_session(bot, tmp_path)
    sess = next(iter(bot.mod.mgr._sessions.values()))
    p = tmp_path / "only.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    sess.pending_images.append(str(p))

    import telegram as tg_mod
    group_calls: list = []
    photo_calls: list = []
    monkeypatch.setattr(tg_mod, "send_media_group",
                        lambda chat_id, paths, thread_id=None:
                        group_calls.append(1) or [])
    monkeypatch.setattr(tg_mod, "send_photo",
                        lambda chat_id, p, caption="", thread_id=None:
                        photo_calls.append(p) or None)

    bot.mod.on_result(sess, "", "")
    assert group_calls == [], group_calls
    assert photo_calls == [str(p)], photo_calls


# ── copyMessages backfill on /fork (Batch A #14) ─────────────────────

def test_fork_backfills_recent_messages(bot, tmp_path):
    """Forking a session should copy the parent topic's last N messages
    into the fresh fork topic via copyMessages."""
    _start_bot_session(bot, tmp_path)
    parent = next(iter(bot.mod.mgr._sessions.values()))
    bot.mod.mgr.link_claude_id("parent-claude", parent)

    # Drive a full turn so user msg + assistant reply land in the topic
    # and accumulate in state.recent_msgs.
    bot.claude.script([
        {"type": "system", "session_id": "parent-claude"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "reply A"},
        ]}},
        {"type": "result", "session_id": "parent-claude",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    bot.tg.inject_update(text_update(
        "first prompt", owner_id=bot.owner_id,
        forum_chat_id=bot.forum_chat_id, thread_id=parent.topic_id,
    ))
    _drain_updates(bot)
    bot.tg.wait_for_call("sendMessage", message_thread_id=parent.topic_id,
                         timeout=3)

    # Sanity: rolling buffer captured something.
    with bot.mod.state.lock:
        recent = list(bot.mod.state.recent_msgs.get(parent.topic_id, []))
    assert recent, "recent_msgs buffer was not populated"

    bot.tg.reset()
    # Click the fork callback for this session.
    bot.tg.inject_update(callback_update(
        f"fork:{parent.sid}",
        owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
    ))
    _drain_updates(bot)

    copy_calls = bot.tg.calls_of("copyMessages")
    assert copy_calls, f"copyMessages was not called; calls: {bot.tg.calls}"
    last = copy_calls[-1]
    assert last["from_chat_id"] == bot.forum_chat_id
    assert last["chat_id"] == bot.forum_chat_id
    # New fork topic id (whatever was allocated by createForumTopic).
    new_topic = last["message_thread_id"]
    assert new_topic != parent.topic_id
    # Backfilled IDs are a subset of the recorded buffer.
    sent = list(last["message_ids"])
    assert sent, last
    assert all(mid in recent for mid in sent), (sent, recent)


# ── my_chat_member + admin sanity (Batch A #16) ──────────────────────

def test_allowed_updates_includes_my_chat_member(bot):
    """telegram.poll() should subscribe to my_chat_member explicitly so
    Telegram delivers our own membership changes."""
    import telegram as tg_mod
    tg_mod.poll(offset=0, timeout=0)
    last = bot.tg.calls_of("getUpdates")[-1]
    assert "my_chat_member" in last.get("allowed_updates", []), \
        f"my_chat_member missing from allowed_updates: {last}"


def test_handle_my_chat_member_audits(bot):
    """A my_chat_member update is logged to the audit trail."""
    import audit
    bot.mod._handle_my_chat_member({
        "chat": {"id": -9001, "title": "Test group", "type": "supergroup"},
        "from": {"id": bot.owner_id},
        "date": 0,
        "old_chat_member": {"status": "member",
                            "user": {"id": 1, "is_bot": True}},
        "new_chat_member": {"status": "administrator",
                            "user": {"id": 1, "is_bot": True}},
    })
    time.sleep(0.1)  # background audit writer
    entries = audit.tail(10)
    assert any(e.get("event") == "my_chat_member"
               and "member -> administrator" in e.get("detail", "")
               for e in entries), entries


def test_admin_sanity_logs_warning_for_non_admin(bot, capsys):
    """When getChatMember reports owner as plain member, sanity check
    emits a warning instead of crashing."""
    import telegram as tg_mod
    original_req = tg_mod._req

    def fake_req(method, params=None):
        if method == "getChatMember":
            return {"ok": True, "result": {"status": "member",
                                            "user": {"id": bot.owner_id}}}
        return original_req(method, params)

    tg_mod._req = fake_req
    try:
        bot.mod._admin_sanity_check()
    finally:
        tg_mod._req = original_req

    err = capsys.readouterr().err
    assert "owner status" in err and "member" in err, err


def test_setup_sh_brands_bot_profile():
    """setup.sh should call setMyName/setMyShortDescription/setMyDescription
    so a fresh install lands with a proper TG-side profile."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "setup.sh"
    text = src.read_text()
    assert "/setMyName" in text, "setMyName missing from setup.sh"
    assert "/setMyShortDescription" in text, \
        "setMyShortDescription missing from setup.sh"
    assert "/setMyDescription" in text, "setMyDescription missing from setup.sh"
    assert "/setMyProfilePhoto" in text, \
        "setMyProfilePhoto missing from setup.sh"


def test_admin_sanity_silent_for_admin(bot, capsys):
    """When owner is admin, sanity check is silent."""
    import telegram as tg_mod
    original_req = tg_mod._req

    def fake_req(method, params=None):
        if method == "getChatMember":
            return {"ok": True, "result": {"status": "administrator"}}
        return original_req(method, params)

    tg_mod._req = fake_req
    try:
        bot.mod._admin_sanity_check()
    finally:
        tg_mod._req = original_req
    err = capsys.readouterr().err
    assert "WARN" not in err, err


def test_record_topic_msg_trims_buffer(bot):
    """Rolling buffer caps at _FORK_BACKFILL entries."""
    from bot import _record_topic_msg, _FORK_BACKFILL, state
    tid = 7777
    for i in range(_FORK_BACKFILL * 3):
        _record_topic_msg(tid, 1000 + i)
    with state.lock:
        buf = list(state.recent_msgs.get(tid, []))
    assert len(buf) == _FORK_BACKFILL
    # Tail kept, head dropped.
    assert buf[-1] == 1000 + (_FORK_BACKFILL * 3 - 1)
    assert buf[0] == 1000 + (_FORK_BACKFILL * 3 - _FORK_BACKFILL)


# ── Terminal mirror (#51 + #56) ─────────────────────────────────────

def _make_fake_jsonl(tmp_path, csid: str, cwd: str):
    """Create the JSONL path Claude Code would write to for given csid/cwd."""
    import pathlib
    encoded = cwd.replace("/", "-")
    proj_dir = pathlib.Path.home() / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True, exist_ok=True)
    return proj_dir / f"{csid}.jsonl"


def test_mirror_register_creates_topic_and_starts_follower(bot, tmp_path):
    """POST /hook/open_in_bot via on_open_in_bot creates a forum topic
    and registers the mirror. JSONL follower picks up appended events."""
    import json as _json
    import time as _time
    csid = "mirror-test-1"
    cwd = str(tmp_path / "mirror_project_1")
    (tmp_path / "mirror_project_1").mkdir()
    jp = _make_fake_jsonl(tmp_path, csid, cwd)
    try:
        result = bot.mod.on_open_in_bot(csid, cwd, None)
        assert "topic_url" in result, result
        topic_calls = bot.tg.calls_of("createForumTopic")
        assert len(topic_calls) == 1, topic_calls
        # Mirror is in registry
        m = bot.mod.mirror_mgr.by_csid(csid)
        assert m is not None
        assert m.cwd == cwd

        # Append an assistant event; follower should pick it up.
        with open(jp, "w") as f:
            f.write(_json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "hello from terminal"},
                ]},
            }) + "\n")

        # Give the follower up to 2.5s to read + project.
        deadline = _time.time() + 2.5
        found = None
        while _time.time() < deadline:
            for params in bot.tg.calls_of("sendMessage"):
                if (params.get("message_thread_id") == m.topic_id
                        and "hello from terminal" in params.get("text", "")):
                    found = params
                    break
            if found:
                break
            _time.sleep(0.05)
        assert found, ("expected assistant text projected into topic; "
                       f"got calls: {bot.tg.calls_of('sendMessage')[-5:]}")
    finally:
        bot.mod.mirror_mgr.unregister(csid)
        try:
            jp.unlink()
            jp.parent.rmdir()
        except OSError:
            pass


def test_mirror_register_idempotent(bot, tmp_path):
    """Second call for the same csid returns the existing topic_url."""
    csid = "mirror-test-2"
    cwd = str(tmp_path / "mirror_project_2")
    (tmp_path / "mirror_project_2").mkdir()
    try:
        r1 = bot.mod.on_open_in_bot(csid, cwd, None)
        r2 = bot.mod.on_open_in_bot(csid, cwd, None)
        assert r1.get("topic_url") == r2.get("topic_url")
        assert r2.get("existing") is True
        assert len(bot.tg.calls_of("createForumTopic")) == 1
    finally:
        bot.mod.mirror_mgr.unregister(csid)


def test_mirror_response_input_bridge_flag(bot, tmp_path):
    """Bot's open_in_bot response carries `input_bridge: bool` so the
    slash command can branch on what the bot actually saw rather than
    on its own (sometimes-empty) socket-env check."""
    csid_a = "mirror-bridge-on"
    csid_b = "mirror-bridge-off"
    cwd_a = str(tmp_path / "bridge_on")
    cwd_b = str(tmp_path / "bridge_off")
    (tmp_path / "bridge_on").mkdir()
    (tmp_path / "bridge_off").mkdir()
    sock = str(tmp_path / "fake.sock")
    try:
        on_resp = bot.mod.on_open_in_bot(csid_a, cwd_a, sock)
        off_resp = bot.mod.on_open_in_bot(csid_b, cwd_b, None)
        assert on_resp.get("input_bridge") is True, on_resp
        assert off_resp.get("input_bridge") is False, off_resp
        # On the existing-mirror return path the flag must still
        # reflect actual dtach binding state.
        on_resp_again = bot.mod.on_open_in_bot(csid_a, cwd_a, sock)
        assert on_resp_again.get("input_bridge") is True, on_resp_again
    finally:
        bot.mod.mirror_mgr.unregister(csid_a)
        bot.mod.mirror_mgr.unregister(csid_b)


def test_mirror_input_bridge_pushes_to_dtach(bot, tmp_path, monkeypatch):
    """Text typed in a mirror topic with a dtach socket set should be
    pushed via push_to_dtach. Output-only mirrors must refuse with an
    ephemeral."""
    csid = "mirror-test-3"
    cwd = str(tmp_path / "mirror_project_3")
    (tmp_path / "mirror_project_3").mkdir()
    sock = str(tmp_path / "dtach.sock")

    # Patch push_to_dtach so we don't shell out to a real dtach.
    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bot.mod, "push_to_dtach",
        lambda s, text, **kw: (pushes.append((s, text)) or True),
    )

    try:
        bot.mod.on_open_in_bot(csid, cwd, sock)
        m = bot.mod.mirror_mgr.by_csid(csid)
        assert m and m.dtach_socket == sock

        bot.tg.inject_update(text_update(
            "ls -la", owner_id=bot.owner_id,
            forum_chat_id=bot.forum_chat_id,
            thread_id=m.topic_id,
        ))
        _drain_updates(bot)
        assert pushes == [(sock, "ls -la")], pushes
    finally:
        bot.mod.mirror_mgr.unregister(csid)


def test_mirror_input_bridge_output_only_rejects(bot, tmp_path, monkeypatch):
    """Mirror without dtach_socket should not call push_to_dtach; it
    should surface an output-only notice."""
    csid = "mirror-test-4"
    cwd = str(tmp_path / "mirror_project_4")
    (tmp_path / "mirror_project_4").mkdir()

    pushes: list[tuple] = []
    monkeypatch.setattr(
        bot.mod, "push_to_dtach",
        lambda *a, **kw: (pushes.append(a) or True),
    )

    try:
        bot.mod.on_open_in_bot(csid, cwd, None)
        m = bot.mod.mirror_mgr.by_csid(csid)
        assert m and m.dtach_socket is None

        bot.tg.inject_update(text_update(
            "echo hi", owner_id=bot.owner_id,
            forum_chat_id=bot.forum_chat_id,
            thread_id=m.topic_id,
        ))
        _drain_updates(bot)
        assert pushes == [], "push_to_dtach should not be called for output-only mirror"
        # An ephemeral notice should mention "Output-only"
        notices = [
            p.get("text", "") for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
            and "Output-only" in p.get("text", "")
        ]
        assert notices, "expected an Output-only ephemeral"
    finally:
        bot.mod.mirror_mgr.unregister(csid)


def test_mirror_persist_and_restore(bot_env, tmp_path, monkeypatch):
    """Mirror records survive a fresh TerminalMirrorManager instance."""
    import importlib
    import terminal_mirror as tm
    importlib.reload(tm)
    # Restore-time socket liveness check would drop the fake path
    # (no real dtach process backing it); stub it to always-alive
    # for this scenario.
    monkeypatch.setattr(tm, "dtach_socket_alive", lambda *a, **kw: True)
    csid = "mirror-persist-1"
    cwd = str(tmp_path / "persist_project")
    sock = str(tmp_path / "persist.sock")
    (tmp_path / "persist_project").mkdir()
    mgr1 = tm.TerminalMirrorManager(lambda *a, **kw: None)
    mgr1.register(csid, cwd, 555, dtach_socket=sock)
    mgr2 = tm.TerminalMirrorManager(lambda *a, **kw: None)
    restored = mgr2.by_csid(csid)
    assert restored is not None
    assert restored.topic_id == 555
    assert restored.dtach_socket == sock


def test_mirror_register_backfills_only_tail(bot, tmp_path):
    """≤ _BACKFILL_ASK_THRESHOLD logical events at /bot-mirror time →
    silent full backfill: every existing logical event projects into
    the topic, AND any event appended after registration also lands
    (follower picks up where backfill left off, in chronological
    order)."""
    import json as _json
    import time as _time

    csid = "mirror-test-backfill-silent-full"
    cwd = str(tmp_path / "mirror_project_backfill")
    (tmp_path / "mirror_project_backfill").mkdir()
    jp = _make_fake_jsonl(tmp_path, csid, cwd)
    try:
        # Pre-populate with 20 events — under the 30-event prompt
        # threshold, so backfill runs silently and projects all.
        with open(jp, "w") as f:
            for i in range(20):
                f.write(_json.dumps({
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "text",
                        "text": f"evt-{i:02d}",
                    }]},
                }) + "\n")

        bot.mod.on_open_in_bot(csid, cwd, None)
        m = bot.mod.mirror_mgr.by_csid(csid)
        assert m is not None

        # Append a brand-new event after registration; follower waits
        # on backfill_done and then projects it after the backfill batch.
        with open(jp, "a") as f:
            f.write(_json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text",
                                         "text": "POST-FRESH"}]},
            }) + "\n")

        deadline = _time.time() + 4.0
        seen_fresh = False
        while _time.time() < deadline:
            for params in bot.tg.calls_of("sendMessage"):
                if (params.get("message_thread_id") == m.topic_id
                        and "POST-FRESH" in params.get("text", "")):
                    seen_fresh = True
                    break
            if seen_fresh:
                break
            _time.sleep(0.05)
        assert seen_fresh, "fresh post-register event must be projected"

        topic_texts = [
            p.get("text", "")
            for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
        ]
        # All 20 pre-registration events should appear (silent full
        # backfill — no prompt under the threshold). evt-00 marks the
        # very start of history.
        for label in ("evt-00", "evt-10", "evt-19"):
            assert any(label in t for t in topic_texts), (
                f"{label} should be projected by silent full backfill; "
                f"got tail: {topic_texts[-5:]}")
    finally:
        bot.mod.mirror_mgr.unregister(csid)
        try:
            jp.unlink()
            jp.parent.rmdir()
        except OSError:
            pass


def test_mirror_suppresses_tg_echo(bot, tmp_path, monkeypatch):
    """When the owner types into the mirror topic, the text rides
    push_to_dtach into claude's stdin → claude logs it as a `user`
    event → the follower must NOT project it back as a blockquote
    (duplicate)."""
    import json as _json
    import time as _time
    csid = "mirror-test-echo-suppress"
    cwd = str(tmp_path / "mirror_project_echo")
    (tmp_path / "mirror_project_echo").mkdir()
    jp = _make_fake_jsonl(tmp_path, csid, cwd)
    sock = str(tmp_path / "echo.sock")

    monkeypatch.setattr(
        bot.mod, "push_to_dtach",
        lambda s, text, **kw: True,
    )

    try:
        bot.mod.on_open_in_bot(csid, cwd, sock)
        m = bot.mod.mirror_mgr.by_csid(csid)
        assert m is not None

        # Owner types in the mirror topic — input bridge fires and
        # notes the injection.
        bot.tg.inject_update(text_update(
            "ping from owner", owner_id=bot.owner_id,
            forum_chat_id=bot.forum_chat_id,
            thread_id=m.topic_id,
        ))
        _drain_updates(bot)

        # Claude logs the same text as a `user` event in JSONL. This
        # one must be SUPPRESSED (echo).
        with open(jp, "a") as f:
            f.write(_json.dumps({
                "type": "user",
                "message": {"content": "ping from owner"},
            }) + "\n")
            # An unrelated user event from another channel — must
            # still be projected as a blockquote.
            f.write(_json.dumps({
                "type": "user",
                "message": {"content": "typed-into-claude-directly"},
            }) + "\n")

        deadline = _time.time() + 2.5
        direct_hit = False
        while _time.time() < deadline:
            for params in bot.tg.calls_of("sendMessage"):
                if (params.get("message_thread_id") == m.topic_id
                        and "typed-into-claude-directly"
                            in params.get("text", "")):
                    direct_hit = True
                    break
            if direct_hit:
                break
            _time.sleep(0.05)
        assert direct_hit, "non-injected user event must be projected"

        echo_hits = [
            p for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
            and "ping from owner" in p.get("text", "")
            and "blockquote" in p.get("text", "")
        ]
        assert not echo_hits, (
            f"echo of TG-injected text must NOT be projected; "
            f"got: {echo_hits}")
    finally:
        bot.mod.mirror_mgr.unregister(csid)
        try:
            jp.unlink()
            jp.parent.rmdir()
        except OSError:
            pass


def test_mirror_drops_slash_command_url_echo(bot, tmp_path):
    """The /bot-mirror slash command instructs Claude to print
    `mirror: <topic_url>` plus a `tip:` / `output-only` line. That
    assistant turn must NOT be projected — the owner already saw the
    URL via the HTTP response."""
    import json as _json
    import time as _time
    csid = "mirror-test-slashcmd-echo"
    cwd = str(tmp_path / "mirror_project_slashcmd")
    (tmp_path / "mirror_project_slashcmd").mkdir()
    jp = _make_fake_jsonl(tmp_path, csid, cwd)
    try:
        bot.mod.on_open_in_bot(csid, cwd, None)
        m = bot.mod.mirror_mgr.by_csid(csid)

        with open(jp, "a") as f:
            f.write(_json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "text",
                    "text": (f"mirror: https://t.me/c/123/{m.topic_id}\n"
                             f"output-only (claude is not inside dtach)"),
                }]},
            }) + "\n")
            f.write(_json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "text",
                    "text": "regular reply please project me",
                }]},
            }) + "\n")

        deadline = _time.time() + 2.5
        regular_hit = False
        while _time.time() < deadline:
            for params in bot.tg.calls_of("sendMessage"):
                if (params.get("message_thread_id") == m.topic_id
                        and "regular reply please project me"
                            in params.get("text", "")):
                    regular_hit = True
                    break
            if regular_hit:
                break
            _time.sleep(0.05)
        assert regular_hit, "regular assistant text must be projected"

        slashcmd_hits = [
            p for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
            and "mirror: https://t.me/c/" in p.get("text", "")
        ]
        assert not slashcmd_hits, (
            f"slash-command URL echo must NOT be projected; "
            f"got: {slashcmd_hits}")
    finally:
        bot.mod.mirror_mgr.unregister(csid)
        try:
            jp.unlink()
            jp.parent.rmdir()
        except OSError:
            pass


def test_mirror_restore_drops_dead_sockets(bot_env, tmp_path, monkeypatch):
    """At bot restart, persisted mirrors whose dtach socket no longer
    exists must be dropped — otherwise their followers and the
    healthcheck loop burn TG rate budget probing topics for a
    terminal that exited (rate-budget contention starves the active
    mirror, backfill events get 429'd and never reach the topic).

    Legacy tmux-shaped records (tmux_socket / tmux_pane fields) must
    also be dropped — the tmux session is no longer addressable in
    the dtach-based world.

    Regression: verified by reverting the `dtach_socket_alive` guard
    in `_restore` and watching this test fail."""
    import importlib
    import json as _json
    import terminal_mirror as tm
    importlib.reload(tm)

    # Pretend dtach is installed but only ONE socket is alive.
    def _fake_socket_alive(sock):
        return sock == "/s/alive.sock"
    monkeypatch.setattr(tm, "dtach_socket_alive", _fake_socket_alive)

    persist = tmp_path / ".mirrors.json"
    persist.write_text(_json.dumps([
        {"csid": "mirror-alive", "cwd": "/x", "topic_id": 1,
         "dtach_socket": "/s/alive.sock",
         "jsonl_path": "/t/a.jsonl", "last_offset": 0},
        {"csid": "mirror-dead", "cwd": "/x", "topic_id": 2,
         "dtach_socket": "/s/dead.sock",
         "jsonl_path": "/t/d.jsonl", "last_offset": 0},
        {"csid": "mirror-output-only", "cwd": "/x", "topic_id": 3,
         "dtach_socket": None,
         "jsonl_path": "/t/o.jsonl", "last_offset": 0},
        {"csid": "mirror-legacy-tmux", "cwd": "/x", "topic_id": 4,
         "tmux_socket": "/s", "tmux_pane": "%legacy",
         "jsonl_path": "/t/l.jsonl", "last_offset": 0},
    ]))
    monkeypatch.setattr(tm, "_PERSIST_PATH", str(persist))

    mgr = tm.TerminalMirrorManager(lambda *a, **kw: None)

    assert mgr.by_csid("mirror-alive") is not None, "alive must stay"
    assert mgr.by_csid("mirror-dead") is None, (
        "dead dtach socket must be dropped on restore")
    assert mgr.by_csid("mirror-output-only") is not None, (
        "output-only (socket=None from start) must stay — only "
        "ex-bridged-but-now-dead is dropped")
    assert mgr.by_csid("mirror-legacy-tmux") is None, (
        "legacy tmux-shaped records must be dropped on restore")

    # The persist file should reflect the drop.
    on_disk = _json.loads(persist.read_text())
    csids = {r["csid"] for r in on_disk}
    assert csids == {"mirror-alive", "mirror-output-only"}, csids


def test_mirror_drops_slash_command_body_via_is_meta(bot, tmp_path):
    """When a custom slash command has no $ARGUMENTS (e.g. /bot-mirror),
    Claude Code injects the command body as a USER event with
    `isMeta: true` at the top level — there is no `ARGUMENTS:` trailer
    to pattern-match on. Mirror projection must drop these meta events
    so the topic isn't flooded with the markdown body of bot-mirror.md
    (description, instructions, the entire bash block, etc).

    Regression: this test was added 2026-05-21 after a screenshot of
    the topic containing the literal `/bot-mirror` body. Verified by
    reverting the `isMeta` guard once — this test fails — restoring it
    — passes."""
    import json as _json
    import time as _time
    csid = "mirror-test-ismeta-body"
    cwd = str(tmp_path / "mirror_project_ismeta")
    (tmp_path / "mirror_project_ismeta").mkdir()
    jp = _make_fake_jsonl(tmp_path, csid, cwd)
    try:
        bot.mod.on_open_in_bot(csid, cwd, None)
        m = bot.mod.mirror_mgr.by_csid(csid)

        # Append the exact event shape Claude Code emits for the body
        # of a no-args slash command: type=user + isMeta=True +
        # message.content = markdown body.
        body = (
            "Mirror this terminal Claude session to a ClaudeLaude "
            "Telegram topic.\nThe bot tails the JSONL transcript...\n"
            "```bash\nPORT=\"${BOT_HOOK_PORT:-9853}\"\n...\n```\n"
            "After running the Bash call, just print the captured "
            "output as-is. SLASH_CMD_BODY_MARKER\n"
        )
        with open(jp, "a") as f:
            f.write(_json.dumps({
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": body},
            }) + "\n")
            # A real user message after the slash command — must STILL
            # be projected.
            f.write(_json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "REAL_USER_INPUT"},
            }) + "\n")

        deadline = _time.time() + 2.5
        real_hit = False
        while _time.time() < deadline:
            for params in bot.tg.calls_of("sendMessage"):
                if (params.get("message_thread_id") == m.topic_id
                        and "REAL_USER_INPUT" in params.get("text", "")):
                    real_hit = True
                    break
            if real_hit:
                break
            _time.sleep(0.05)
        assert real_hit, "real user input must be projected"

        body_hits = [
            p for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
            and "SLASH_CMD_BODY_MARKER" in p.get("text", "")
        ]
        assert not body_hits, (
            f"slash-command body (isMeta=true) must NOT be projected; "
            f"got: {body_hits}")
    finally:
        bot.mod.mirror_mgr.unregister(csid)
        try:
            jp.unlink()
            jp.parent.rmdir()
        except OSError:
            pass


def test_mirror_open_sends_welcome_message(bot, tmp_path):
    """The freshly-created mirror topic must contain a single welcome
    message — otherwise the topic looks empty (the slash-command URL
    echo and the JSONL backfill are both filtered out, by design).
    Phrasing branches on whether input bridge is available."""
    csid_on = "mirror-welcome-bridged"
    csid_off = "mirror-welcome-output-only"
    cwd_on = str(tmp_path / "welcome_bridged")
    cwd_off = str(tmp_path / "welcome_output_only")
    (tmp_path / "welcome_bridged").mkdir()
    (tmp_path / "welcome_output_only").mkdir()
    try:
        bot.mod.on_open_in_bot(
            csid_on, cwd_on, str(tmp_path / "welcome.sock"))
        m_on = bot.mod.mirror_mgr.by_csid(csid_on)
        bridged = [
            p.get("text", "")
            for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m_on.topic_id
        ]
        assert any("Mirror attached" in t for t in bridged), bridged
        assert not any("output-only" in t.lower() for t in bridged), bridged

        bot.mod.on_open_in_bot(csid_off, cwd_off, None)
        m_off = bot.mod.mirror_mgr.by_csid(csid_off)
        out_only = [
            p.get("text", "")
            for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m_off.topic_id
        ]
        assert any("output-only" in t.lower() for t in out_only), out_only
        assert not any("Mirror attached" in t for t in out_only), out_only
    finally:
        bot.mod.mirror_mgr.unregister(csid_on)
        bot.mod.mirror_mgr.unregister(csid_off)


def _write_alternating_jsonl(jp, n_pairs: int, prefix: str = "evt") -> None:
    """Write n_pairs alternating user+assistant events to a JSONL —
    yielding 2*n_pairs logical events for read_logical_events()."""
    import json as _json
    with open(jp, "w") as f:
        for i in range(n_pairs):
            f.write(_json.dumps({
                "type": "user",
                "message": {"role": "user",
                            "content": f"{prefix}-q-{i:02d}"},
            }) + "\n")
            f.write(_json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "text",
                    "text": f"{prefix}-a-{i:02d}",
                }]},
            }) + "\n")


def test_mirror_prompts_above_threshold(bot, tmp_path):
    """When pre-registration history has more than _BACKFILL_ASK_THRESHOLD
    logical events, the bot must NOT silently backfill the whole stream
    (slow). Instead it sends an inline-button prompt with full / short
    choices keyed by csid prefix, and suspends the follower's projection
    via mirror.backfill_done until the click decides."""

    csid = "mirror-test-prompt-above"
    cwd = str(tmp_path / "prompt_above")
    (tmp_path / "prompt_above").mkdir()
    jp = _make_fake_jsonl(tmp_path, csid, cwd)
    try:
        # 20 user/assistant pairs = 40 logical events — above the 30
        # threshold so the bot must prompt instead of silent full.
        _write_alternating_jsonl(jp, 20, prefix="old")

        bot.mod.on_open_in_bot(csid, cwd, None)
        m = bot.mod.mirror_mgr.by_csid(csid)
        assert m is not None
        # backfill_done is cleared until user clicks → follower suspended.
        assert not m.backfill_done.is_set(), \
            "follower must wait until backfill choice is made"

        prompts = [
            p for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
            and "reply_markup" in p
        ]
        assert prompts, "expected an inline-button prompt for choice"
        btn_rows = prompts[-1]["reply_markup"]["inline_keyboard"]
        flat = [b for row in btn_rows for b in row]
        cb_data = [b["callback_data"] for b in flat]
        assert any(cd.startswith(f"mirror_history:full:{csid[:24]}")
                   for cd in cb_data), cb_data
        assert any(cd.startswith(f"mirror_history:short:{csid[:24]}")
                   for cd in cb_data), cb_data

        # No history bubble should be projected before the click.
        projected = [
            p.get("text", "")
            for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
        ]
        assert not any("old-q-" in t or "old-a-" in t for t in projected), (
            "no history should land in the topic before the user chooses; "
            f"got: {projected[-3:]}")
    finally:
        bot.mod.mirror_mgr.unregister(csid)
        try:
            jp.unlink()
            jp.parent.rmdir()
        except OSError:
            pass


def test_mirror_history_full_click_runs_backfill(bot, tmp_path):
    """Clicking the 'Full' button runs the merged backfill for the full
    pre-registration history."""
    import time as _time

    csid = "mirror-test-click-full"
    cwd = str(tmp_path / "click_full")
    (tmp_path / "click_full").mkdir()
    jp = _make_fake_jsonl(tmp_path, csid, cwd)
    try:
        # 18 user/assistant pairs = 36 logical events, above threshold.
        _write_alternating_jsonl(jp, 18, prefix="hist")

        bot.mod.on_open_in_bot(csid, cwd, None)
        m = bot.mod.mirror_mgr.by_csid(csid)
        assert m is not None
        # The prompt was sent — find its callback_data for 'full'.
        prompts = [
            p for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
            and "reply_markup" in p
        ]
        btn_rows = prompts[-1]["reply_markup"]["inline_keyboard"]
        flat = [b for row in btn_rows for b in row]
        full_btn = next(b for b in flat
                        if b["callback_data"].startswith("mirror_history:full:"))

        bot.tg.inject_update(callback_update(
            full_btn["callback_data"],
            owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
            thread_id=m.topic_id,
        ))
        _drain_updates(bot)

        # Wait for backfill thread to project the LAST event (q-17 or a-17).
        deadline = _time.time() + 5
        while _time.time() < deadline:
            texts = [
                p.get("text", "")
                for p in bot.tg.calls_of("sendMessage")
                if p.get("message_thread_id") == m.topic_id
            ]
            if any("hist-a-17" in t for t in texts):
                break
            _time.sleep(0.05)

        texts = [
            p.get("text", "")
            for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
        ]
        # Full backfill must project the earliest, mid, and last events.
        for label in ("hist-q-00", "hist-a-08", "hist-a-17"):
            assert any(label in t for t in texts), (
                f"{label} should be in full backfill; tail={texts[-5:]}")
    finally:
        bot.mod.mirror_mgr.unregister(csid)
        try:
            jp.unlink()
            jp.parent.rmdir()
        except OSError:
            pass


def test_mirror_history_short_click_emits_summary(bot, tmp_path):
    """Clicking 'Short' runs the summary backfill — last N events
    concatenated into one TG message (sent via send_long which may
    chunk if needed)."""
    import time as _time

    csid = "mirror-test-click-short"
    cwd = str(tmp_path / "click_short")
    (tmp_path / "click_short").mkdir()
    jp = _make_fake_jsonl(tmp_path, csid, cwd)
    try:
        # 20 pairs = 40 logical events; short summary keeps last 12.
        _write_alternating_jsonl(jp, 20, prefix="S")

        bot.mod.on_open_in_bot(csid, cwd, None)
        m = bot.mod.mirror_mgr.by_csid(csid)
        before_count = len(bot.tg.calls_of("sendMessage"))
        prompts = [
            p for p in bot.tg.calls_of("sendMessage")
            if p.get("message_thread_id") == m.topic_id
            and "reply_markup" in p
        ]
        btn_rows = prompts[-1]["reply_markup"]["inline_keyboard"]
        flat = [b for row in btn_rows for b in row]
        short_btn = next(b for b in flat
                         if b["callback_data"].startswith("mirror_history:short:"))

        bot.tg.inject_update(callback_update(
            short_btn["callback_data"],
            owner_id=bot.owner_id, forum_chat_id=bot.forum_chat_id,
            thread_id=m.topic_id,
        ))
        _drain_updates(bot)

        # Wait for the summary message containing the very last event.
        deadline = _time.time() + 3
        summary_text = None
        while _time.time() < deadline:
            for p in bot.tg.calls_of("sendMessage")[before_count:]:
                if (p.get("message_thread_id") == m.topic_id
                        and "S-a-19" in p.get("text", "")):
                    summary_text = p["text"]
                    break
            if summary_text:
                break
            _time.sleep(0.05)

        assert summary_text, "summary message with last events not sent"
        # Last 12 logical events out of 40: indices 28..39 of the
        # logical sequence (alternating q,a). Pairs 14..19 fully fit
        # in the 12-event tail (= 12 logical events): q-14 a-14 q-15
        # a-15 ... q-19 a-19. So q-14 / a-19 must be in; q-13 must not.
        for label in ("S-q-14", "S-a-17", "S-a-19"):
            assert label in summary_text, (
                f"{label} missing from short summary")
        for missing in ("S-q-00", "S-a-05", "S-q-13"):
            assert missing not in summary_text, (
                f"{missing} should be excluded from short summary")
    finally:
        bot.mod.mirror_mgr.unregister(csid)
        try:
            jp.unlink()
            jp.parent.rmdir()
        except OSError:
            pass
