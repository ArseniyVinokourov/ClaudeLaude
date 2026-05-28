"""Group-chat write budget — sliding-window token bucket with priority queue.

The forum supergroup has a server-side rate limit of ~20 writes/min, shared
across all topics (one chat). Edits, sends, deletes, reactions all count.
sendChatAction and read-only calls are free and bypass this module.

Model:
  - Sliding 60s window of past write timestamps.
  - HARD_CEILING (20): absolute server limit — never exceed.
  - P0_ONLY_AT (18): at >=18 writes in the last 60s, only P0 (security/
    permission) may proceed. Reserve for time-critical traffic.
  - P1_OK_UNTIL (14): below 14, P0+P1 (live stream) flow normally.
  - P2_OK_UNTIL (10): below 10, P0+P1+P2 (notices/reactions) also flow.
  - Below P2_OK_UNTIL: all priorities go.
  - BURST_CAP (7): no more than 7 writes within any BURST_WINDOW_S (7s).
  - On 429: pause the whole bucket for retry_after, then resume with a
    temporarily-lowered ceiling that recovers over a few minutes.

Priorities (lower number = higher priority):
  P0 — security alerts, permission Allow/Deny prompts
  P1 — live assistant stream (text, edits, end-of-turn)
  P2 — ephemeral notices, on-demand reactions
  P3 — housekeeping (dashboard refresh, message cleanup)

API:
  budget.submit(prio, fn, *args, **kwargs) -> Future
    Enqueue a paid group-chat call. Returns a Future the caller can wait on.
  budget.report_429(retry_after_s)
    Tell the budget the server replied 429; pauses everything.
  budget.headroom() -> int
    Free slots in the 60s window (for diagnostics).
  budget.snapshot() -> dict
    State summary for /status.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque
from concurrent.futures import Future
from typing import Callable

# Priority lanes — lower = more urgent.
P0 = 0
P1 = 1
P2 = 2
P3 = 3
_N_PRIO = 4


class GroupBudget:
    WINDOW_S = 60.0
    HARD_CEILING = 20
    P0_ONLY_AT = 18
    P1_OK_UNTIL = 14
    P2_OK_UNTIL = 10
    BURST_CAP = 7
    BURST_WINDOW_S = 7.0
    # Adaptive ceiling after a 429: drop to this for RECOVERY_S seconds,
    # then gradually return to HARD_CEILING.
    POST_429_CEILING = 17
    RECOVERY_S = 300.0

    def __init__(self, name: str = "group"):
        self.name = name
        self._writes: deque = deque()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queues: list[list] = [[] for _ in range(_N_PRIO)]
        self._paused_until: float = 0.0
        self._last_429_at: float = 0.0
        self._stop = False
        self._worker = threading.Thread(
            target=self._run, name=f"budget-{name}", daemon=True)
        self._worker.start()

    # ── public API ───────────────────────────────────────────────────

    def submit(self, prio: int, fn: Callable, *args, **kwargs) -> Future:
        if prio < 0 or prio >= _N_PRIO:
            raise ValueError(f"bad priority {prio}")
        fut: Future = Future()
        with self._cv:
            self._queues[prio].append((fut, fn, args, kwargs))
            self._cv.notify()
        return fut

    def report_429(self, retry_after_s: float) -> None:
        with self._cv:
            now = time.monotonic()
            self._paused_until = max(self._paused_until,
                                     now + max(1.0, retry_after_s))
            self._last_429_at = now
            self._cv.notify_all()

    def headroom(self) -> int:
        with self._lock:
            self._decay_locked()
            return max(0, self._ceiling_locked() - len(self._writes))

    def snapshot(self) -> dict:
        with self._lock:
            self._decay_locked()
            now = time.monotonic()
            return {
                "writes_last_60s": len(self._writes),
                "ceiling": self._ceiling_locked(),
                "paused_for_s": max(0.0, self._paused_until - now),
                "queued": [len(q) for q in self._queues],
            }

    def stop(self) -> None:
        self._stop = True
        with self._cv:
            self._cv.notify_all()

    # ── internals ────────────────────────────────────────────────────

    def _decay_locked(self) -> None:
        cutoff = time.monotonic() - self.WINDOW_S
        while self._writes and self._writes[0] < cutoff:
            self._writes.popleft()

    def _ceiling_locked(self) -> int:
        """Return the current effective ceiling (lowered after a 429)."""
        if self._last_429_at == 0.0:
            return self.HARD_CEILING
        age = time.monotonic() - self._last_429_at
        if age >= self.RECOVERY_S:
            return self.HARD_CEILING
        # Linear recovery from POST_429_CEILING toward HARD_CEILING.
        frac = age / self.RECOVERY_S
        return int(self.POST_429_CEILING +
                   (self.HARD_CEILING - self.POST_429_CEILING) * frac)

    def _allowed_prio_locked(self) -> int:
        n = len(self._writes)
        ceiling = self._ceiling_locked()
        if n >= ceiling:
            return -1
        if n >= self.P0_ONLY_AT:
            return P0
        if n >= self.P1_OK_UNTIL:
            return P1
        if n >= self.P2_OK_UNTIL:
            return P2
        return P3

    def _burst_count_locked(self) -> int:
        cutoff = time.monotonic() - self.BURST_WINDOW_S
        return sum(1 for t in self._writes if t >= cutoff)

    def _pace_wait_locked(self) -> float:
        """Seconds to wait before next call, given current load."""
        n = len(self._writes)
        # Burst guard: ≤ BURST_CAP in BURST_WINDOW_S.
        if self._burst_count_locked() >= self.BURST_CAP:
            return 1.0
        if n < self.P2_OK_UNTIL:
            return 0.0  # plenty of room — fire fast
        if n < self.P1_OK_UNTIL:
            return 2.0
        if n < self.P0_ONLY_AT:
            return 4.0
        return 6.0  # only P0 left, give P0 some breathing room

    def _pick_locked(self, allowed: int):
        for p in range(0, _N_PRIO):
            if p > allowed:
                return None
            if self._queues[p]:
                return p
        return None

    def _run(self) -> None:
        while not self._stop:
            with self._cv:
                # Wait for work.
                while not any(self._queues) and not self._stop:
                    self._cv.wait(timeout=1.0)
                if self._stop:
                    return

                # Pause until retry_after expires.
                now = time.monotonic()
                if now < self._paused_until:
                    self._cv.wait(timeout=self._paused_until - now)
                    continue

                self._decay_locked()
                allowed = self._allowed_prio_locked()
                if allowed < 0:
                    # Over ceiling — wait briefly, then re-check.
                    self._cv.wait(timeout=1.0)
                    continue
                pick = self._pick_locked(allowed)
                if pick is None:
                    # Highest-prio queue under the allowed bar is empty;
                    # everything queued is below the bar. Wait for budget
                    # to refill (cheapest: short sleep).
                    self._cv.wait(timeout=1.0)
                    continue

                # Pacing: avoid bursting too hard even with budget.
                pace = self._pace_wait_locked()
                if pace > 0:
                    woken = self._cv.wait(timeout=pace)
                    # Re-check after pacing in case state changed.
                    if not self._queues[pick]:
                        continue

                fut, fn, args, kwargs = self._queues[pick].pop(0)

            # Execute outside lock.
            try:
                result = fn(*args, **kwargs)
                fut.set_result(result)
                ok = True
            except Telegram429 as e:
                # The wrapper raised — re-queue this call at head of its
                # lane, then pause.
                with self._cv:
                    self._queues[pick].insert(0, (fut, fn, args, kwargs))
                self.report_429(e.retry_after)
                ok = False
            except Exception as e:
                fut.set_exception(e)
                ok = True

            if ok:
                with self._lock:
                    self._writes.append(time.monotonic())


class Telegram429(Exception):
    """Raised by the request layer on HTTP 429 so the budget can pause."""

    def __init__(self, retry_after: float):
        super().__init__(f"429 retry_after={retry_after}")
        self.retry_after = retry_after


# Module-level singleton (initialized lazily by telegram.py).
_instance: GroupBudget | None = None
_instance_lock = threading.Lock()


def instance() -> GroupBudget:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = GroupBudget()
    return _instance


def reset_for_tests() -> None:
    """Tear down the singleton (used by pytest fixtures)."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.stop()
            _instance = None
