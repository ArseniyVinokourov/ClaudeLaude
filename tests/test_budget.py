"""Tests for the group write budget (sliding-window token bucket).

Each test uses its own GroupBudget instance with shortened timing constants
to avoid sleeping for real seconds in CI.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import budget as budget_mod  # noqa: E402


@pytest.fixture
def fast_budget(monkeypatch):
    """Build a budget with millisecond-scale timings.

    Pacing is disabled (returns 0) so tests don't sleep on the long
    P0-only branch (6s) — the production behaviour is covered by the
    ladder logic in _allowed_prio_locked, which we test directly.
    """
    monkeypatch.setattr(budget_mod.GroupBudget, "WINDOW_S", 1.0)
    monkeypatch.setattr(budget_mod.GroupBudget, "BURST_WINDOW_S", 0.2)
    monkeypatch.setattr(budget_mod.GroupBudget, "RECOVERY_S", 5.0)
    monkeypatch.setattr(budget_mod.GroupBudget,
                        "_pace_wait_locked", lambda self: 0.0)
    b = budget_mod.GroupBudget(name="test")
    yield b
    b.stop()


def test_simple_submit_returns_result(fast_budget):
    fut = fast_budget.submit(budget_mod.P1, lambda x: x * 2, 21)
    assert fut.result(timeout=2) == 42


def test_exception_propagates(fast_budget):
    def boom():
        raise ValueError("nope")
    fut = fast_budget.submit(budget_mod.P1, boom)
    with pytest.raises(ValueError, match="nope"):
        fut.result(timeout=2)


def test_writes_recorded_in_window(fast_budget):
    for _ in range(5):
        fast_budget.submit(budget_mod.P3, lambda: None).result(timeout=2)
    snap = fast_budget.snapshot()
    assert snap["writes_last_60s"] == 5


def test_window_decay(fast_budget):
    for _ in range(3):
        fast_budget.submit(budget_mod.P3, lambda: None).result(timeout=2)
    assert fast_budget.snapshot()["writes_last_60s"] == 3
    # WINDOW_S patched to 1s
    time.sleep(1.2)
    assert fast_budget.snapshot()["writes_last_60s"] == 0


def test_p0_jumps_ahead_of_p3(fast_budget):
    """When budget is plentiful but P3 was queued first, P0 must still
    run first. (Worker scans P0 lane before lower lanes on every pick.)"""
    order: list[str] = []

    def slow():
        time.sleep(0.05)
        order.append("p3-running")

    # Saturate worker so a queue forms.
    holder = fast_budget.submit(budget_mod.P3, slow)
    # Queue both; submitted in P3-then-P0 order but P0 must run first.
    f3 = fast_budget.submit(budget_mod.P3, lambda: order.append("p3"))
    f0 = fast_budget.submit(budget_mod.P0, lambda: order.append("p0"))
    holder.result(timeout=2)
    f0.result(timeout=2)
    f3.result(timeout=2)
    assert order.index("p0") < order.index("p3")


def test_429_pauses_subsequent_calls(fast_budget):
    # Manually flag a pause.
    fast_budget.report_429(retry_after_s=0.4)
    t0 = time.monotonic()
    fut = fast_budget.submit(budget_mod.P0, lambda: None)
    fut.result(timeout=2)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.35


def test_429_via_exception_requeues(fast_budget):
    """If a wrapped call raises Telegram429, the budget re-queues it,
    pauses, then retries it. The future resolves with the eventual result."""
    state = {"attempts": 0}

    def flaky():
        state["attempts"] += 1
        if state["attempts"] == 1:
            raise budget_mod.Telegram429(retry_after=0.3)
        return "ok"

    fut = fast_budget.submit(budget_mod.P1, flaky)
    assert fut.result(timeout=3) == "ok"
    assert state["attempts"] == 2


def test_p0_only_when_near_ceiling(fast_budget, monkeypatch):
    """When the 60s counter is at or above P0_ONLY_AT, only P0 may
    advance; lower priorities wait."""
    # Pre-populate window with timestamps to put us at P0_ONLY_AT (18).
    now = time.monotonic()
    fast_budget._writes.extend([now] * fast_budget.P0_ONLY_AT)

    started: list[str] = []
    p0 = fast_budget.submit(budget_mod.P0, lambda: started.append("p0"))
    p2 = fast_budget.submit(budget_mod.P2, lambda: started.append("p2"))

    # P0 must complete promptly; P2 must NOT complete while window full.
    p0.result(timeout=2)
    time.sleep(0.2)
    assert "p0" in started
    assert "p2" not in started

    # Let the window decay past P2_OK_UNTIL.
    time.sleep(1.0)
    p2.result(timeout=2)
    assert "p2" in started


def test_ceiling_lowered_after_429(fast_budget):
    fast_budget.report_429(retry_after_s=0.05)
    # Wait past the pause.
    time.sleep(0.15)
    snap = fast_budget.snapshot()
    # Ceiling should be at or below HARD_CEILING, near POST_429_CEILING.
    assert snap["ceiling"] <= fast_budget.HARD_CEILING
    assert snap["ceiling"] >= fast_budget.POST_429_CEILING


def test_snapshot_shape(fast_budget):
    fast_budget.submit(budget_mod.P1, lambda: None).result(timeout=2)
    snap = fast_budget.snapshot()
    assert set(snap.keys()) == {"writes_last_60s", "ceiling", "paused_for_s", "queued"}
    assert len(snap["queued"]) == 4


def test_nested_submit_from_worker_does_not_deadlock(fast_budget):
    """A job that issues another paid write from inside the worker thread
    (e.g. the topic-dead callback doing cleanup edits during a failed send)
    must not block on its own thread. The re-entrancy guard runs nested
    calls directly (found live, #89)."""
    bud = fast_budget
    nested_ran = []

    def nested_write():
        # What telegram._via_budget does with the guard: detect we're on
        # the worker and run directly instead of submit()+result().
        if bud.is_worker_thread():
            nested_ran.append(True)
            return "direct"
        return bud.submit(budget_mod.P3, lambda: "queued").result(timeout=5)

    def outer_job():
        return nested_write()

    fut = bud.submit(budget_mod.P1, outer_job)
    assert fut.result(timeout=5) == "direct"
    assert nested_ran, "nested write must run directly on the worker thread"


def test_via_budget_nested_write_from_callback_no_deadlock(monkeypatch):
    """Integration: a send whose failure triggers the topic-dead callback,
    which itself does a paid edit. Without the re-entrancy guard in
    telegram._via_budget the budget worker waits on itself for 180s
    (found live, #89). Must complete in seconds."""
    import os
    import threading

    os.environ.setdefault("BOT_TOKEN", "fake-token")
    os.environ.setdefault("OWNER_ID", "42")
    budget_mod.reset_for_tests()
    import telegram as tg

    calls = []

    def fake_req(method, params=None):
        calls.append((method, params or {}))
        if method == "sendMessage":
            import requests

            class _R:
                status_code = 400
                text = '{"ok":false,"description":"Bad Request: message thread not found"}'
            raise requests.HTTPError("400", response=_R())
        return {"ok": True, "result": True}

    monkeypatch.setattr(tg, "_req", fake_req)
    tg.set_forum_chat_id(777)

    def on_dead(chat_id, thread_id):
        # What bot.py's handler does: paid cleanup writes from inside
        # the failing send's worker context.
        tg.edit(123, "cleanup", chat_id)

    tg.set_on_topic_dead(on_dead)

    result = {}

    def run():
        result["mid"] = tg.send("hello", 777, thread_id=99)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=10)
    try:
        assert not t.is_alive(), "send deadlocked on nested paid write"
        assert result["mid"] is None  # the send itself failed (dead topic)
        assert any(m == "editMessageText" for m, _ in calls), \
            "nested cleanup edit must have executed"
    finally:
        tg.set_on_topic_dead(None)
        tg.set_forum_chat_id(None)
        budget_mod.reset_for_tests()
