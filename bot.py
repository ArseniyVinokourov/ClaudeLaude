#!/usr/bin/env python3
"""ClaudeLaude Telegram bot — Forum Topics UI for Claude Code.

Each person runs their own instance of this bot on their machine.
See README.md or run setup.sh for first-time configuration.
"""
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

from config import (OWNER_ID, AUTO_UPDATE, AUTO_UPDATE_POLICY, BOT_DIR,
                    UNLOCK_WORD, add_pending_delete,
                    get_default_mode, set_default_mode,
                    get_forum_chat_id, get_tour_shown, set_tour_shown,
                    get_uploads_warned_at, migrate_state,
                    set_uploads_warned_at, set_env,
                    is_killed, activate_kill, deactivate_kill)
import audit
import frames
import stt
import stt_install
import telegram as tg
import tour
from botui import BotUI, CLOSE_ROW
from commands import Commands
from dashboard import Dashboard
from hookhandlers import HookHandlers
from lifecycle import SessionLifecycle
from mirrorbridge import MirrorProjector
from turncontroller import TurnController, TurnState
from sessions import MODE_PRESETS, SessionManager, valid_mode
from questions import QuestionAsker
from updater import (
    _check_update, _conflicted_files, _has_local_changes, _restart_bot,
    _run_update,
)
from hooks import HookBridge
from terminal_mirror import (
    TerminalMirrorManager, push_to_dtach, dtach_socket_alive,
    reap_if_abandoned,
)
from session_discovery import _resolve_session_cwd, _session_jsonl_path

# ── state ────────────────────────────────────────────────────────────

_CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
DEFAULT_DISPLAY = os.environ.get("DEFAULT_DISPLAY", "mobile")
_UPLOAD_DIR = "/tmp/bot_uploads"

_ICON_ACTIVE = "5417915203100613993"   # 💬
_ICON_TERMINAL = "5350554349074391003" # 💻
_ICON_STOPPED = ""                     # removes custom emoji → color dot

# How many recent messages to copy from the parent topic into a fork.
# 5 is enough to see the last user prompt + assistant answer; more clutters
# the fresh fork. copyMessages is capped at 100 by the API anyway.
_FORK_BACKFILL = 5

# Telegram free-tier reaction set is small; these are picked for clarity.
_REACT_RECEIVED = "👀"   # bot got the message
_REACT_STREAMING = "🔥"  # claude is producing text
_REACT_TOOL = "⚡"        # claude reached for a tool
_REACT_DONE = "👍"
_REACT_INTERRUPTED = "🤷"
_REACT_ERROR = "😨"


class BotState:
    def __init__(self):
        self.lock = threading.Lock()
        # short_id -> (msg_id, chat_id, sid_or_None)
        self.pending_permissions: dict[str, tuple[int, int, str | None]] = {}
        self.perm_key_map: dict[str, str] = {}
        self.pending_project_picks: dict[str, list[str]] = {}
        self.pending_resume_picks: dict[str, list[tuple]] = {}
        # AskUserQuestion: qid -> entry dict; sid -> qid (for stop/interrupt).
        self.pending_questions: dict[str, dict] = {}
        self.question_by_sid: dict[str, str] = {}
        self.topic_display_mode: dict[int, str] = {}
        self.turns: dict[str, TurnState] = {}
        self.topic_counter: dict[str, int] = {}
        self.renamed_topics: set[int] = set()
        self.saved_turns: dict[str, tuple[list[int], list[str], list[str]]] = {}
        self.topic_labels: dict[int, str] = {}
        # terminal watcher: claude_session_id → [(msg_id, chat_id, kind)]
        # kind: "perm:<short_id>" | "notification"
        self.pending_terminal_msgs: dict[str, list[tuple[int, int, str]]] = {}
        # Rolling window of recent message_ids per session topic, used to
        # backfill context when a fork is created. Trimmed to _FORK_BACKFILL.
        self.recent_msgs: dict[int, list[int]] = {}
        # Runtime STT install offers (#86): pick_id → parked media message
        # (kind, file_id, caption, sid, chat/msg/thread ids, offer_mid).
        self.pending_media_installs: dict[str, dict] = {}


state = BotState()
bot_running = True
ui = BotUI()

# General auto-reap backstop: any non-persistent message sent to General gets a
# delete scheduled this far out — longer than any legitimate self-managed TTL
# (pickers are 60s), so it only ever clears messages that would otherwise have
# no timer at all. Keeps General structurally clean (only the pinned dashboard
# and opted-out onboarding/alerts persist).
_GENERAL_BACKSTOP_TTL = 180
tg.configure_general_guard(
    lambda mid: ui.delete_after(mid, get_forum_chat_id(), _GENERAL_BACKSTOP_TTL))
turnctl = TurnController(
    state, ui, _CLAUDE_BIN,
    default_display=DEFAULT_DISPLAY,
    icon_stopped=_ICON_STOPPED,
    fork_backfill=_FORK_BACKFILL,
)
lifecycle = SessionLifecycle(state, ui, turnctl)


def forum():
    return get_forum_chat_id()


# ── security helpers ────────────────────────────────────────────────

_unkill_attempts: list[float] = []
_UNKILL_MAX_ATTEMPTS = 3
_UNKILL_COOLDOWN = 300  # 5 minutes


def _try_unkill(text: str, chat_id: int, msg_id: int | None,
                thread_id: int | None) -> bool:
    """Accept unlock word in General only. Constant-time compare, no regex."""
    if not UNLOCK_WORD or not is_killed():
        return False
    fid = forum()
    if not fid or chat_id != fid or thread_id:
        return False
    now = time.time()
    # Prune old attempts
    _unkill_attempts[:] = [t for t in _unkill_attempts
                           if now - t < _UNKILL_COOLDOWN]
    if len(_unkill_attempts) >= _UNKILL_MAX_ATTEMPTS:
        audit.log("kill_switch", "unlock rate-limited")
        remaining = int(_UNKILL_COOLDOWN - (now - _unkill_attempts[0]))
        ui.ephemeral(chat_id, f"Rate limited. Try again in {remaining}s.",
                   seconds=10)
        if msg_id:
            tg.delete(msg_id, chat_id)
        return True
    import hmac
    clean = text.strip()
    if not hmac.compare_digest(clean.encode(), UNLOCK_WORD.encode()):
        _unkill_attempts.append(now)
        audit.log("kill_switch", "unlock failed attempt")
        left = _UNKILL_MAX_ATTEMPTS - len(_unkill_attempts)
        if left > 0:
            ui.ephemeral(chat_id, f"Wrong. {left} attempt(s) left.", seconds=5)
        if msg_id:
            tg.delete(msg_id, chat_id)
        return True
    _unkill_attempts.clear()
    deactivate_kill()
    audit.log("kill_switch", "deactivated via unlock word")
    if msg_id:
        tg.delete(msg_id, chat_id)
    ui.ephemeral(chat_id, "\U0001f513 Bot unlocked.", seconds=5)
    dashboard.sync()
    return True


def _do_kill():
    activate_kill()
    audit.log("kill_switch", "activated")
    for s in mgr.list_sessions():
        if s.alive:
            mgr.stop(s.sid, reason="kill switch")
    fid = forum()
    if fid:
        dashboard.sync()
        if UNLOCK_WORD:
            ui.ephemeral(fid, "\U0001f512 Bot killed. All sessions stopped.\n"
                       "Send unlock word in General to restore.",
                       seconds=15)
        else:
            ui.ephemeral(fid, "\U0001f512 Bot killed. All sessions stopped.\n"
                       "Delete .kill file on the machine to restore.",
                       seconds=15)


def _auto_update_loop():
    """Background thread: check for updates at startup + every hour.

    Always runs; honours the live AUTO_UPDATE flag each iteration so toggling
    it from /settings takes effect without a restart (when off, idles ~60s and
    rechecks)."""
    time.sleep(10)
    while bot_running:
        if not AUTO_UPDATE:
            for _ in range(60):
                if not bot_running:
                    break
                time.sleep(1)
            continue
        try:
            result = _check_update()
            if result:
                current, latest, _changelog = result
                modified = _has_local_changes()
                if not modified:
                    print(f"[auto-update] {current} → {latest}, no local changes, updating",
                          file=sys.stderr, flush=True)
                    rc, output = _run_update(non_interactive=True)
                    if rc == 0:
                        _restart_bot()
                    else:
                        print(f"[auto-update] failed (rc={rc}): {output[:200]}",
                              file=sys.stderr, flush=True)
                elif AUTO_UPDATE_POLICY == "replace":
                    print(f"[auto-update] {current} → {latest}, {len(modified)} modified, policy=replace",
                          file=sys.stderr, flush=True)
                    rc, output = _run_update(non_interactive=True, policy="replace")
                    if rc == 0:
                        _restart_bot()
                    else:
                        print(f"[auto-update] failed (rc={rc}): {output[:200]}",
                              file=sys.stderr, flush=True)
                else:
                    fid = forum()
                    if fid:
                        ui.ephemeral(fid,
                                   f"⬆️ Update available: {tg.esc(current)} → <b>{tg.esc(latest)}</b>\n"
                                   f"{len(modified)} locally modified file(s). Use /update to review.",
                                   seconds=30)
        except Exception as e:
            print(f"[auto-update] error: {e}", file=sys.stderr, flush=True)
        for _ in range(3600):
            if not bot_running:
                return
            time.sleep(1)


def _spawn_merge_resolve_session():
    """Open a guided bot session in BOT_DIR to resolve an update merge conflict.

    The working tree is already at the new release tag with conflict markers in
    the clashing files (update.sh left it resolvable). The session helps the
    owner decide what to keep; finishing it runs the `finalize` strategy and
    restarts. Edit/Write are blocked in bot sessions, so the prompt steers
    Claude to git/sed/python for the edits."""
    fid = forum()
    files = _conflicted_files()
    s = lifecycle.spawn_session(BOT_DIR, name="Update merge")
    if not s:
        if fid:
            ui.ephemeral(fid, "❌ Couldn't open a resolve session. "
                         "Run <code>bash update.sh --strategy=replace</code> "
                         "to take the new version.", seconds=30)
        return
    flist = "\n".join(f"  • {f}" for f in files) or "  (see `git status`)"
    prompt = (
        "This topic is a self-update merge-conflict workspace for the "
        f"ClaudeLaude bot itself, at `{BOT_DIR}`.\n\n"
        "The bot tried to update to a new release, but local edits to tracked "
        "files clash with it. The repo is already checked out at the new "
        "release; these files have git conflict markers "
        "(`<<<<<<<` / `=======` / `>>>>>>>`):\n"
        f"{flist}\n\n"
        "Help me decide, per file, what to keep from my version vs the new "
        "one, then resolve the markers. Note: Edit/Write are blocked here — "
        "use Bash with `git`, `sed`, or a `python3` heredoc to edit files. "
        "When `git status` shows no unmerged paths, tell me to tap "
        "“✅ Done — finalize update” below."
    )
    mgr.send_user_message(s.sid, prompt)
    buttons = [
        [{"text": "✅ Done — finalize update", "callback_data": "upd:finalize"}],
        [{"text": "⬆️ Give up, take new version", "callback_data": "upd:replace"}],
    ]
    ui.send_to_topic(s.topic_id,
                     "When conflicts are resolved (no unmerged paths in "
                     "<code>git status</code>), finalize:", buttons=buttons)


_KNOWN_COMMANDS = {
    "/setup", "/new", "/sessions", "/resume", "/history", "/stop",
    "/interrupt", "/restart", "/usage", "/display", "/mode", "/test_perm",
    "/update", "/settings", "/stop_bot", "/help", "/start", "/menu",
    "/tour", "/kill", "/audit",
}

_HELP_DOCUMENTED_COMMANDS = {
    "/new", "/sessions", "/resume", "/stop", "/interrupt", "/restart",
    "/history", "/usage", "/display", "/mode", "/update", "/settings",
    "/menu", "/help", "/tour", "/stop_bot",
}


_HIDDEN_COMMANDS = {"/setup", "/test_perm", "/start", "/kill", "/audit"}


# The BotFather command menu (setMyCommands). Hoisted to a module constant so
# it is test-assertable; applied across scopes in main().
_BOT_COMMANDS = [
    {"command": "tour", "description": "Guided walkthrough"},
    {"command": "new", "description": "New Claude session"},
    {"command": "resume", "description": "Resume a session"},
    {"command": "sessions", "description": "Active sessions"},
    {"command": "menu", "description": "Quick actions"},
    {"command": "mode", "description": "Response style"},
    {"command": "stop", "description": "Stop session"},
    {"command": "restart", "description": "Restart stopped session"},
    {"command": "history", "description": "Last N events"},
    {"command": "usage", "description": "Token usage"},
    {"command": "display", "description": "Toggle mobile/desktop"},
    {"command": "settings", "description": "Bot settings"},
    {"command": "update", "description": "Check for bot updates"},
    {"command": "help", "description": "Show help"},
]


def _validate_help():
    """Warn if any non-hidden command is missing from help text."""
    should_document = _KNOWN_COMMANDS - _HIDDEN_COMMANDS
    missing = should_document - _HELP_DOCUMENTED_COMMANDS
    extra = _HELP_DOCUMENTED_COMMANDS - _KNOWN_COMMANDS
    if missing:
        print(f"[WARN] commands not in help: {missing}",
              file=sys.stderr, flush=True)
    if extra:
        print(f"[WARN] help references unknown commands: {extra}",
              file=sys.stderr, flush=True)


# ── main loop ────────────────────────────────────────────────────────


# Bring persisted state up to the current schema before anything reads it —
# SessionManager loads .sessions.json the moment it's constructed below.
migrate_state()

asker = QuestionAsker(state)
mgr = SessionManager(
    on_assistant_message=turnctl.on_assistant,
    on_result=turnctl.on_result,
    on_tool_use=turnctl.on_tool_use,
    on_thinking=turnctl.on_thinking,
    on_session_stop=lifecycle.on_session_stop,
    on_session_context=turnctl.session_context,
    on_ask_question=asker.ask,
)
turnctl.mgr = mgr
lifecycle.mgr = mgr


mirror = MirrorProjector(state, ui.topic_url, _ICON_TERMINAL)
mirror_mgr = TerminalMirrorManager(on_event=mirror.on_mirror_event)
mirror.mgr = mirror_mgr
dashboard = Dashboard(state, mirror_mgr, _validate_help, _CLAUDE_BIN)
hooks = HookHandlers(state, mgr, ui, mirror_mgr, lifecycle, _ICON_TERMINAL,
                     should_run=lambda: bot_running)


bridge = HookBridge(
    on_notification=hooks.on_hook_notification,
    on_permission=hooks.on_hook_permission,
    on_open_in_bot=mirror.on_open_in_bot,
    on_terminal_closed=mirror.on_terminal_closed,
)
lifecycle.bridge = bridge
hooks.bridge = bridge
commands = Commands(
    state, mgr, ui, dashboard, turnctl, lifecycle,
    default_display=DEFAULT_DISPLAY,
    icon_stopped=_ICON_STOPPED,
    icon_active=_ICON_ACTIVE,
)


def _dashboard_loop():
    """Background: update dashboard pin every 60s, refresh usage every 5m,
    sweep stale media uploads every hour (first pass ~5s after start, which
    doubles as the startup sweep)."""
    time.sleep(5)
    while bot_running:
        dashboard.tick()
        if time.time() - _uploads_last_sweep > _UPLOAD_SWEEP_INTERVAL_S:
            try:
                _sweep_uploads()
            except Exception as e:
                print(f"[uploads] sweep error: {e}",
                      file=sys.stderr, flush=True)
        time.sleep(60)


# ── upload retention (#87) ──────────────────────────────────────────
# Downloaded media and extracted frames in _UPLOAD_DIR were never deleted
# (it's /tmp, so only a WSL restart cleaned it). Sweep: anything older than
# _UPLOAD_TTL_S goes — unless its path still appears in the JSONL transcript
# of a session the bot knows about (Claude may re-read a path it was given,
# and a stopped session can be /resume'd). Size guard: past _UPLOAD_WARN_MB
# the owner gets a DM, at most one a day. Claude itself is told the files
# are temporary (_TEMP_NOTE) so anything long-lived gets copied out.

_UPLOAD_TTL_S = int(os.environ.get("UPLOAD_TTL_S", str(48 * 3600)))
_UPLOAD_SWEEP_INTERVAL_S = 3600
_UPLOAD_WARN_BYTES = int(os.environ.get("UPLOAD_WARN_MB", "500")) * 1024 * 1024
_UPLOAD_WARN_INTERVAL_S = 24 * 3600
_uploads_last_sweep = 0.0

# Appended to every turn that hands Claude an _UPLOAD_DIR path. Rebuilt
# when /settings changes the TTL so the stated retention stays accurate.
def _build_temp_note() -> str:
    return (f"(note: attached files under {_UPLOAD_DIR} are temporary — "
            f"auto-deleted after ~{_UPLOAD_TTL_S // 3600}h; copy anything "
            f"needed long-term into the project)")


_TEMP_NOTE = _build_temp_note()


def _known_session_jsonls() -> list[str]:
    """Transcript files of every session the bot knows about — alive AND
    stopped (a stopped session can be /resume'd and re-read an upload path
    it was given). Protection lapses only when the session ages out of the
    manager (GC, ~24h after stop) or its JSONL disappears."""
    paths = []
    for s in mgr.list_sessions():
        if s.claude_session_id:
            p = _session_jsonl_path(s.claude_session_id)
            if p:
                paths.append(p)
    for m in mirror_mgr.list():
        jp = getattr(m, "jsonl_path", None)
        if jp and os.path.isfile(jp):
            paths.append(jp)
    return paths


def _sweep_uploads():
    global _uploads_last_sweep
    _uploads_last_sweep = time.time()
    try:
        entries = os.listdir(_UPLOAD_DIR)
    except OSError:
        return
    now = time.time()
    due = []
    for name in entries:
        path = os.path.join(_UPLOAD_DIR, name)
        try:
            if now - os.path.getmtime(path) > _UPLOAD_TTL_S:
                due.append(path)
        except OSError:
            continue
    if due:
        # Transcripts are read only when something is actually due. A
        # substring check is enough: every upload name embeds a timestamp,
        # and a frames_<ts> dir shows up via its frame file paths.
        blob_parts = []
        for p in _known_session_jsonls():
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    blob_parts.append(f.read())
            except OSError:
                continue
        blob = "\n".join(blob_parts)
        removed = 0
        for path in due:
            if path in blob:
                continue  # an alive session still references it
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                removed += 1
            except OSError:
                pass
        if removed:
            print(f"[uploads] swept {removed} stale media files",
                  file=sys.stderr, flush=True)
    _warn_uploads_size()


def _warn_uploads_size():
    total = 0
    for root, _dirs, files in os.walk(_UPLOAD_DIR):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    if total < _UPLOAD_WARN_BYTES:
        return
    if time.time() - get_uploads_warned_at() < _UPLOAD_WARN_INTERVAL_S:
        return
    set_uploads_warned_at(time.time())
    mb = total // (1024 * 1024)
    tg.send(f"📦 Media uploads folder reached {mb} MB ({_UPLOAD_DIR}).\n"
            f"Files older than {_UPLOAD_TTL_S // 3600}h are cleaned "
            f"automatically; the rest is recent media still referenced "
            f"by sessions.", OWNER_ID)


def _on_topic_dead(chat_id: int, thread_id: int) -> None:
    """Callback fired by telegram.py when a write returned TOPIC_ID_INVALID.

    This replaces the old per-30s active probe: the only way the bot
    learns a topic is gone is by actually trying to send there. The send
    fails, telegram.py calls us here, we stop the session/mirror behind
    the dead topic.
    """
    # Terminals aggregator? (resets its id + abandons pending perms)
    if hooks.handle_topic_dead(thread_id):
        return
    # Bot session?
    sess = None
    for s in mgr.list_sessions():
        if s.topic_id == thread_id:
            sess = s
            break
    if sess and sess.alive:
        print(f"[topic_dead] session {sess.sid[:8]} — invalidating",
              file=sys.stderr, flush=True)
        try:
            lifecycle.invalidate_and_stop(sess, "topic deleted")
        except Exception as e:
            print(f"[topic_dead] stop error: {e}",
                  file=sys.stderr, flush=True)
        return
    # Mirror?
    for mirror in mirror_mgr.list():
        if mirror.topic_id == thread_id and mirror.alive:
            print(f"[topic_dead] mirror {mirror.csid[:8]} — unregistering",
                  file=sys.stderr, flush=True)
            try:
                mirror_mgr.unregister(mirror.csid)
            except Exception as e:
                print(f"[topic_dead] unregister error: {e}",
                      file=sys.stderr, flush=True)
            return


_chat_dead_notice_ts = 0.0


def _on_chat_dead(chat_id: int) -> None:
    """Callback fired by telegram.py when a forum-group write failed with a
    group-level error (group deleted / bot kicked). The bot can't fix this
    itself — DM the owner a recovery path, throttled to once an hour so the
    60s dashboard tick doesn't turn it into spam. State is kept: if the bot
    is simply re-added to the group, everything resumes untouched.
    """
    global _chat_dead_notice_ts
    now = time.time()
    if now - _chat_dead_notice_ts < 3600:
        return
    _chat_dead_notice_ts = now
    print(f"[chat_dead] forum group {chat_id} unreachable — DMing owner",
          file=sys.stderr, flush=True)
    tg.send("⚠️ Forum group unreachable — deleted, or the bot was removed.\n"
            "If the group still exists: add the bot back as admin.\n"
            "Otherwise: create a new group, enable Topics, add the bot "
            "as admin, and send /setup there.", OWNER_ID)


# ── 429 user-facing notice (owner DM) ───────────────────────────────

_RATE_NOTICE_LOCK = threading.Lock()
_rate_notice_msg_id: int | None = None
_rate_notice_until: float = 0.0


def _on_429_notify(retry_after_s: int) -> None:
    """Callback fired by telegram.py when a group send hit 429.

    Sends a soft "hold on" message to the owner DM (separate budget),
    but only when the wait is meaningful (>=15s) and we don't already
    have a fresh notice up. The notice is auto-deleted by a background
    cleaner once the pause window passes.
    """
    if retry_after_s < 15:
        return
    global _rate_notice_msg_id, _rate_notice_until
    with _RATE_NOTICE_LOCK:
        now = time.monotonic()
        if now < _rate_notice_until and _rate_notice_msg_id:
            # Already have an active notice — extend its life.
            _rate_notice_until = max(_rate_notice_until,
                                     now + retry_after_s + 2)
            return
        # Send fresh notice.
        try:
            mid = tg.send("⏳ Перегружено, секунду", OWNER_ID)
        except Exception:
            mid = None
        _rate_notice_msg_id = mid
        _rate_notice_until = now + retry_after_s + 2

    if mid:
        def _delete_when_stale():
            while True:
                with _RATE_NOTICE_LOCK:
                    if time.monotonic() >= _rate_notice_until:
                        target = _rate_notice_msg_id
                        break
                time.sleep(1.0)
            if target:
                try:
                    tg.delete(target, OWNER_ID)
                except Exception:
                    pass
            with _RATE_NOTICE_LOCK:
                globals()["_rate_notice_msg_id"] = None

        threading.Thread(target=_delete_when_stale, daemon=True, name="bot-bg-delstale").start()


# ── mirror dtach-socket watcher ─────────────────────────────────────


def _mirror_continue_buttons(csid: str) -> list:
    """Inline button offering to continue a dead-terminal mirror as a
    regular bot session in the same topic (callback mr:<csid>)."""
    return [[{"text": "▶ Continue as bot session",
              "callback_data": f"mr:{csid}"}]]


def _reconcile_output_only_mirror_topics():
    """One-shot at startup: reap output-only mirrors whose topic was
    deleted while the bot was down.

    Output-only mirrors (terminal already closed) get no further sends, so
    the lazy TOPIC_ID_INVALID-on-send path that catches deleted topics
    never fires for them — without this they linger in .mirrors.json
    forever. The probe is silent (editForumTopic with the stored name) and
    runs at P3 so it never competes with live traffic; only a
    confirmed-deleted topic is reaped, never a transient failure. Mirrors
    with a live terminal, or legacy records with no stored label, are
    skipped."""
    fid = forum()
    if not fid:
        return
    for mirror in mirror_mgr.list():
        if not mirror.alive or dtach_socket_alive(mirror.dtach_socket):
            continue  # only mirrors that are already output-only
        if not mirror.topic_label:
            continue  # legacy record, no stored name -> can't probe silently
        try:
            if tg.topic_gone(fid, mirror.topic_id, mirror.topic_label):
                print(f"[mirror reconcile] {mirror.csid[:8]} topic "
                      f"{mirror.topic_id} gone — unregistering",
                      file=sys.stderr, flush=True)
                mirror_mgr.unregister(mirror.csid)
        except Exception as e:
            print(f"[mirror reconcile] probe error {mirror.csid[:8]}: {e}",
                  file=sys.stderr, flush=True)


def _mirror_socket_watcher():
    """Local-only watcher: flip mirrors to output-only when their dtach
    socket vanishes, and reap claudes whose terminal closed (zero
    attached clients + idle JSONL) so they don't run detached forever.
    Uses no Telegram budget — only filesystem/proc stats.

    Exception: a one-shot startup topic reconcile runs first (a P3
    probe of already-output-only mirrors), then it settles into the
    local-only loop."""
    _reconcile_output_only_mirror_topics()
    while bot_running:
        time.sleep(30)
        for mirror in mirror_mgr.list():
            if not mirror.alive or not mirror.dtach_socket:
                continue
            if not dtach_socket_alive(mirror.dtach_socket):
                ui.send_to_topic(
                    mirror.topic_id,
                    "\U0001f50c Terminal closed — mirror is now output-only",
                    buttons=_mirror_continue_buttons(mirror.csid))
                mirror_mgr.set_dtach_socket(mirror.csid, None)
                continue
            # Terminal gone but claude still alive under dtach: SIGTERM
            # it once nothing is in flight. dtach then removes the
            # socket and the branch above posts the continue notice on
            # the next tick.
            if reap_if_abandoned(mirror.dtach_socket, mirror.jsonl_path):
                print(f"[mirror] {mirror.csid[:8]} reaped detached claude "
                      f"(terminal closed, session idle)",
                      file=sys.stderr, flush=True)


def _device_monitor_loop():
    """Background: check for new TG sessions every 5 minutes."""
    import device_monitor
    if not device_monitor._available():
        return
    time.sleep(10)
    while bot_running:
        try:
            new = device_monitor.check_new_devices()
            for d in new:
                fid = forum()
                if not fid:
                    break
                ip_masked = d.get("ip", "?")
                if "." in ip_masked:
                    parts = ip_masked.split(".")
                    ip_masked = f"{parts[0]}.{parts[1]}.x.x"
                text = (
                    f"⚠️ <b>New TG session detected</b>\n"
                    f"{tg.esc(d.get('device_model', '?'))}, "
                    f"{tg.esc(d.get('platform', '?'))}\n"
                    f"{tg.esc(d.get('country', '?'))}, "
                    f"IP {tg.esc(ip_masked)}"
                )
                key = d.get("key", "")
                buttons = [
                    [{"text": "✓ Trust", "callback_data": f"dt:{key[:40]}"},
                     {"text": "\U0001f512 Kill bot", "callback_data": "dk:"}],
                ]
                audit.log("device_alert",
                          f"{d.get('device_model', '?')} "
                          f"{d.get('country', '?')} {ip_masked}")
                # Security alert — persists in General until the user acts
                # (Trust self-deletes after 5s; Kill leaves a "killed" notice).
                ui.send_general(text, buttons=buttons, persist=True)
        except Exception as e:
            print(f"[device_monitor] error: {e}",
                  file=sys.stderr, flush=True)
        time.sleep(300)


def main():
    global bot_running

    # Wire telegram.py to know our group chat id (so it can route paid
    # writes through the budget) and to call us back on 429 / dead topic.
    fid = forum()
    if fid:
        tg.set_forum_chat_id(fid)
    tg.set_on_429(_on_429_notify)
    tg.set_on_topic_dead(_on_topic_dead)
    tg.set_on_chat_dead(_on_chat_dead)

    for s in mgr.list_sessions():
        if s.topic_id and s.topic_label:
            with state.lock:
                state.topic_labels[s.topic_id] = s.topic_label
    bridge.start()
    dashboard.cleanup_general()
    mirror_mgr.start_all_followers()
    # Mirrors whose terminal died while the bot was down were dropped at
    # restore; offer to continue each one as a bot session in its topic.
    for d in mirror_mgr.dropped_on_restore:
        ui.send_to_topic(
            d["topic_id"],
            "\U0001f50c Terminal closed while the bot was down — "
            "mirror dropped",
            buttons=_mirror_continue_buttons(d["csid"]))
    mirror_mgr.dropped_on_restore.clear()
    # No active topic healthcheck — dead topics are detected lazily via
    # TOPIC_ID_INVALID on the next send (set_on_topic_dead callback).
    # The dtach-socket watcher only does local filesystem checks.
    threading.Thread(target=_mirror_socket_watcher, daemon=True).start()
    threading.Thread(target=_dashboard_loop, daemon=True).start()
    threading.Thread(target=hooks.terminal_watcher, daemon=True).start()
    threading.Thread(target=_device_monitor_loop, daemon=True).start()
    tg.set_my_commands(_BOT_COMMANDS)
    threading.Thread(target=_auto_update_loop, daemon=True).start()
    dashboard.refresh_usage()
    dashboard.sync()
    _admin_sanity_check()

    offset = None
    while bot_running:
        updates = tg.poll(offset)
        for u in updates:
            offset = u["update_id"] + 1
            try:
                _handle_update(u)
            except Exception as e:
                print(f"[update] error: {e}", file=sys.stderr, flush=True)

    for s in mgr.list_sessions():
        mgr.stop(s.sid)


def _admin_sanity_check():
    """On startup, verify the bot is an admin in the linked forum group.

    Fail-fast warning is much easier to read than the cryptic 400 from
    createForumTopic that surfaces only when the first session is opened.
    Silent no-op when no forum is linked yet (fresh install).
    """
    fid = forum()
    if not fid or not OWNER_ID:
        return
    try:
        r = tg._req("getChatMember", {"chat_id": fid, "user_id": OWNER_ID})
        status = (r.get("result", {}) or {}).get("status", "")
    except Exception as e:
        print(f"[setup] getChatMember failed: {e}",
              file=sys.stderr, flush=True)
        return
    if status not in ("creator", "administrator"):
        print(f"[setup] WARN: owner status in forum group is '{status}'. "
              "Bot expects owner to be admin so /new can manage topics.",
              file=sys.stderr, flush=True)


def _handle_my_chat_member(mcm: dict):
    """React when our own admin/member status changes in some chat.

    Useful for setup-flow visibility: the bot logs when it's added to a
    new group or promoted to admin so the owner can spot mis-configured
    state without diffing /audit. Forum auto-link only happens via
    /setup — we don't auto-claim a new group as the forum.
    """
    chat = mcm.get("chat", {}) or {}
    new_member = (mcm.get("new_chat_member") or {})
    old_member = (mcm.get("old_chat_member") or {})
    chat_id = chat.get("id")
    title = chat.get("title", "?")
    new_status = new_member.get("status", "?")
    old_status = old_member.get("status", "?")
    print(f"[my_chat_member] chat={chat_id} title={title!r} "
          f"{old_status}→{new_status}", file=sys.stderr, flush=True)
    audit.log("my_chat_member",
              f"{title} ({chat_id}): {old_status} -> {new_status}")


# ── media-group (album) assembly ────────────────────────────────────
# Telegram delivers an album (multiple photos/files sent together) as N
# SEPARATE messages that share one `media_group_id`; the caption rides on
# the first part only. The parts arrive back-to-back. We buffer them and
# flush once no new part has shown up for _MEDIA_GROUP_FLUSH_S, then hand
# Claude a single turn carrying every attachment path. Flushing is done on
# a daemon Timer (not the poll thread) so it fires even while the long-poll
# is blocked; the buffer dict is guarded by a lock since two threads touch
# it. enqueue_user_input is queue-based, so calling it off-thread is safe.
_MEDIA_GROUP_FLUSH_S = 1.5
_media_groups: dict = {}
_media_group_lock = threading.Lock()


def _buffer_media_group(gid, session, file_id, filename, caption,
                        chat_id, msg_id, thread_id):
    """Append one album part and (re)arm its flush timer."""
    with _media_group_lock:
        grp = _media_groups.get(gid)
        if grp is None:
            grp = {"session": session, "chat_id": chat_id,
                   "thread_id": thread_id, "caption": "",
                   "items": [], "first_msg_id": msg_id, "timer": None}
            _media_groups[gid] = grp
        grp["items"].append((file_id, filename))
        if caption and not grp["caption"]:
            grp["caption"] = caption
        if grp["timer"] is not None:
            grp["timer"].cancel()
        t = threading.Timer(_MEDIA_GROUP_FLUSH_S, _flush_media_group, args=(gid,))
        t.name = "bot-bg-album"
        t.daemon = True
        grp["timer"] = t
        t.start()


def _flush_media_group(gid):
    """Download every buffered part and enqueue one combined turn."""
    with _media_group_lock:
        grp = _media_groups.pop(gid, None)
    if not grp:
        return
    session = grp["session"]
    chat_id = grp["chat_id"]
    thread_id = grp["thread_id"]
    paths = []
    for i, (file_id, filename) in enumerate(grp["items"]):
        # Index the name: album parts download within the same second and
        # photos all carry filename "photo.jpg", so a bare timestamp would
        # collide and each download would overwrite the last.
        dest = os.path.join(_UPLOAD_DIR, f"{int(time.time())}_{i}_{filename}")
        if tg.download_file(file_id, dest):
            paths.append(dest)
    if not paths:
        tg.send("❌ Download failed", chat_id, thread_id=thread_id)
        return
    attach = "\n".join(f"[Attached file: {p}]" for p in paths)
    caption = grp["caption"]
    user_text = f"{caption}\n{attach}" if caption else attach
    user_text += f"\n{_TEMP_NOTE}"
    audit.log("user_message", f"[album] {len(paths)} files", sid=session.sid)
    turnctl.enqueue_user_input(session, user_text, chat_id,
                               grp["first_msg_id"], thread_id)


# ── voice transcription (#83) ───────────────────────────────────────
# A voice note (TG `voice`/`audio`, ogg/opus) is downloaded, transcribed
# locally by faster-whisper (in its own venv via stt.transcribe), and the
# text is fed to Claude as a normal turn. Run on a daemon thread so the
# single poll loop is never blocked by the seconds-long transcription.

def _handle_voice(session, file_id, caption, chat_id, msg_id, thread_id):
    dest = os.path.join(_UPLOAD_DIR, f"{int(time.time())}_voice.oga")
    if not tg.download_file(file_id, dest):
        tg.send("❌ Download failed", chat_id, thread_id=thread_id)
        return
    tg.send_chat_action(chat_id, "typing", thread_id=thread_id)
    result = stt.transcribe(dest)
    if not result or not result.get("text"):
        ui.ephemeral(chat_id, "🎙 Could not transcribe the audio",
                     thread_id=thread_id, seconds=8)
        return
    transcript = result["text"]
    body = f"[Voice message transcript]: {transcript}"
    user_text = f"{caption}\n{body}" if caption else body
    audit.log("user_message", f"[voice] {transcript[:200]}", sid=session.sid)
    turnctl.enqueue_user_input(session, user_text, chat_id, msg_id, thread_id)


# ── video transcription + frame sampling (#84) ──────────────────────
# A video / video-note → its audio is transcribed (faster-whisper reads the
# video container's audio stream directly via PyAV) AND scene-change frames
# are sampled (frames.extract, also PyAV — no system ffmpeg). Claude gets ONE
# turn: transcript with timecodes + the frames as attachments, each tagged
# with its timecode so words and visuals line up. Runs on a daemon thread.

def _mmss(seconds) -> str:
    s = int(seconds or 0)
    return f"{s // 60:02d}:{s % 60:02d}"


def _handle_video(session, file_id, caption, chat_id, msg_id, thread_id):
    ts = int(time.time())
    dest = os.path.join(_UPLOAD_DIR, f"{ts}_video.mp4")
    if not tg.download_file(file_id, dest):
        tg.send("❌ Download failed", chat_id, thread_id=thread_id)
        return
    tg.send_chat_action(chat_id, "typing", thread_id=thread_id)
    # Audio transcript (None if the video has no audio track).
    result = stt.transcribe(dest)
    transcript = (result or {}).get("text", "")
    segments = (result or {}).get("segments") or []
    # Scene-change frames.
    shots = frames.extract(dest, os.path.join(_UPLOAD_DIR, f"frames_{ts}"))

    parts = []
    if transcript:
        if segments:
            lines = "\n".join(f"[{_mmss(s['start'])}] {s['text']}"
                              for s in segments)
            parts.append(f"[Video transcript]\n{lines}")
        else:
            parts.append(f"[Video transcript]: {transcript}")
    if shots:
        flines = "\n".join(f"[Attached file: {s['path']}] (t={_mmss(s['t'])})"
                           for s in shots)
        parts.append(f"[Video frames at scene changes]\n{flines}\n{_TEMP_NOTE}")
    if not parts:
        ui.ephemeral(chat_id,
                     "🎬 Could not read the video (no audio track, no frames)",
                     thread_id=thread_id, seconds=8)
        return
    if caption:
        parts.insert(0, caption)
    # Decoder-only tier (#86): frames extracted, speech silently absent —
    # tell the user once per video so the missing transcript isn't a mystery.
    if shots and not transcript and not stt.available():
        ui.ephemeral(chat_id,
                     "🎬 Frames only — Whisper isn't installed, "
                     "speech not transcribed",
                     thread_id=thread_id, seconds=8)
    user_text = "\n\n".join(parts)
    audit.log("user_message",
              f"[video] {len(shots)} frames, transcript {len(transcript)} chars",
              sid=session.sid)
    turnctl.enqueue_user_input(session, user_text, chat_id, msg_id, thread_id)


# ── user reactions on bot messages (#77) ───────────────────────────
# A reaction the owner puts on a bot message is forwarded to Claude as a
# plain user action — Claude decides what (if anything) it means. The
# update carries no thread id, so routing goes through the recent-send
# registry in telegram.py. Removals, reactions on unknown/old messages
# and reactions outside a live bot session are dropped silently.

def _handle_reaction(mr):
    if mr.get("user", {}).get("id") != OWNER_ID:
        return
    new = mr.get("new_reaction") or []
    if not new:
        return  # reaction removed — nothing to forward
    r0 = new[-1]
    emoji = r0.get("emoji") if r0.get("type") == "emoji" else "(custom emoji)"
    chat_id = mr.get("chat", {}).get("id")
    info = tg.recent_send_info(chat_id, mr.get("message_id"))
    if not info:
        return
    thread_id, excerpt = info
    session = mgr.by_topic(thread_id) if thread_id else None
    if not (session and session.is_bot_spawned and session.alive):
        return
    if excerpt:
        text = f'[User reacted {emoji} to your message: "{excerpt}"]'
    else:
        text = f"[User reacted {emoji} to your message]"
    audit.log("user_message", text[:200], sid=session.sid)
    turnctl.enqueue_user_input(session, text, chat_id, None, thread_id)


# ── sticker media (#77) ─────────────────────────────────────────────
# Static stickers (webp) are downloaded and attached so Claude can see the
# image. Video stickers (webm) get scene frames via frames.extract — same
# PyAV path as #84, so it runs on a daemon thread. Animated tgs stickers
# are Lottie vectors PyAV can't decode; their static thumbnail is attached
# instead when available.

def _handle_video_sticker(session, file_id, descr, chat_id, msg_id, thread_id):
    ts = int(time.time())
    dest = os.path.join(_UPLOAD_DIR, f"{ts}_sticker.webm")
    if not tg.download_file(file_id, dest):
        tg.send("❌ Download failed", chat_id, thread_id=thread_id)
        return
    shots = frames.extract(dest, os.path.join(_UPLOAD_DIR, f"frames_{ts}"))
    if shots:
        flines = "\n".join(f"[Attached file: {s['path']}]" for s in shots)
        user_text = f"{descr}\n{flines}\n{_TEMP_NOTE}"
    else:
        user_text = descr
    audit.log("user_message", f"[sticker video] {len(shots)} frames",
              sid=session.sid)
    turnctl.enqueue_user_input(session, user_text, chat_id, msg_id, thread_id)


# ── runtime STT install offers (#86) ────────────────────────────────
# setup.sh makes the ~1GB transcription stack opt-in; a voice/video that
# arrives without it gets an inline install offer instead of a dead end.
# The message's file_id is parked in state.pending_media_installs and
# replayed through the normal handler once the install finishes (Telegram
# file_ids stay downloadable, so nothing is fetched until then).

def _offer_media_install(session, kind, file_id, caption,
                         chat_id, msg_id, thread_id):
    pick_id = str(time.time_ns())[-10:]
    model_rows = [
        [{"text": f"base — fast, {stt_install.MODELS['base']}",
          "callback_data": f"mi:{pick_id}:base"}],
        [{"text": f"small — better, {stt_install.MODELS['small']}",
          "callback_data": f"mi:{pick_id}:small"}],
        [{"text": f"medium — best, {stt_install.MODELS['medium']}",
          "callback_data": f"mi:{pick_id}:medium"}],
    ]
    frames_row = [{"text": "🎞 Frames only — ~250MB",
                   "callback_data": f"mi:{pick_id}:frames"}]
    cancel_row = [{"text": "✗ Not now", "callback_data": f"mi:{pick_id}:x"}]
    if kind == "voice":
        text = ("🎙 Voice transcription isn't installed.\n"
                "Whisper runs fully on this machine — pick a model:")
        rows = model_rows + [cancel_row]
    elif kind == "video":
        text = ("🎬 Video processing isn't installed.\n"
                "Whisper transcribes speech (fully local); the decoder "
                "alone extracts frames without transcription:")
        rows = model_rows + [frames_row, cancel_row]
    else:  # video sticker — decoder is all it needs
        text = ("🎞 Video stickers need the video decoder (~250MB), "
                "which isn't installed:")
        rows = [[{"text": "Install decoder — ~250MB",
                  "callback_data": f"mi:{pick_id}:frames"}], cancel_row]
    offer_mid = tg.send(text, chat_id, thread_id=thread_id, buttons=rows)
    with state.lock:
        state.pending_media_installs[pick_id] = {
            "kind": kind, "file_id": file_id, "caption": caption,
            "sid": session.sid, "chat_id": chat_id, "msg_id": msg_id,
            "thread_id": thread_id, "offer_mid": offer_mid,
        }


def _media_install_clicked(pick_id, choice):
    with state.lock:
        entry = state.pending_media_installs.get(pick_id)
    if not entry:
        return
    if choice == "x":
        with state.lock:
            state.pending_media_installs.pop(pick_id, None)
        if entry["offer_mid"]:
            tg.delete(entry["offer_mid"], entry["chat_id"])
        return
    if choice not in ("frames", *stt_install.MODELS):
        return
    if stt_install.busy():
        ui.ephemeral(entry["chat_id"], "⏳ Another install is already running",
                     thread_id=entry["thread_id"], seconds=6)
        return
    with state.lock:
        state.pending_media_installs.pop(pick_id, None)
    threading.Thread(target=_run_media_install, args=(entry, choice),
                     daemon=True, name="bot-bg-install").start()


def _run_media_install(entry, choice):
    chat_id, thread_id = entry["chat_id"], entry["thread_id"]
    offer_mid = entry["offer_mid"]
    if choice == "frames":
        what = "video decoder (~250MB)"
    else:
        what = f"Whisper {choice} ({stt_install.MODELS[choice]})"
    if offer_mid:
        tg.edit(offer_mid,
                f"⏳ Installing {what} — this can take a few minutes…",
                chat_id)
    ok = (stt_install.install_decoder() if choice == "frames"
          else stt_install.install_whisper(choice))
    audit.log("stt_install", f"{choice} {'ok' if ok else 'FAILED'}")
    if not ok:
        if offer_mid:
            tg.edit(offer_mid, "✗ Install failed — see bot.log", chat_id)
        return
    if offer_mid:
        tg.edit(offer_mid, f"✓ Installed {what}", chat_id)
        ui.delete_after(offer_mid, chat_id, 8)
    session = mgr._sessions.get(entry["sid"])
    if not (session and session.alive):
        return  # session died while pip ran; user can re-send the media
    handler = {"voice": _handle_voice, "video": _handle_video,
               "sticker": _handle_video_sticker}[entry["kind"]]
    handler(session, entry["file_id"], entry["caption"],
            chat_id, entry["msg_id"], thread_id)


# ── /settings runtime menu (#102) ───────────────────────────────────
# Owner-level, bot-wide knobs changed at runtime and persisted to .env.
# Presets only — no free-text capture. The live value is the bot.py global
# above (_UPLOAD_TTL_S / _UPLOAD_WARN_BYTES, plus WHISPER_MODEL in the env);
# a change reassigns the global AND writes .env so it survives a restart.
# A Whisper change first downloads the model (stt_install) in the background.

_SETTINGS_TTL_S = 120
_WARN_MB_PRESETS = [100, 250, 500, 1000, 2000]
_TTL_PRESETS = [6 * 3600, 24 * 3600, 48 * 3600, 7 * 86400, 14 * 86400]
_STT_PRESETS = [60, 120, 180, 300, 600]  # voice/video transcription timeout


def _fmt_ttl(seconds: int) -> str:
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    return f"{seconds // 3600}h"


def _settings_root_rows():
    return [
        [{"text": "🎙 Whisper model", "callback_data": "st:m"}],
        [{"text": "📦 Upload alert", "callback_data": "st:w"}],
        [{"text": "🗑 Cleanup TTL", "callback_data": "st:t"}],
        [{"text": "📱 Display default", "callback_data": "st:d"}],
        [{"text": "🎚 Default mode", "callback_data": "st:dm"}],
        [{"text": "🕒 Transcription timeout", "callback_data": "st:st"}],
        [{"text": "⬆️ Auto-update", "callback_data": "st:au"}],
        CLOSE_ROW,
    ]


def _settings_text():
    au = "on" if AUTO_UPDATE else "off"
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"🎙 Whisper model: <b>{tg.esc(stt.model_name())}</b>\n"
        f"📦 Upload alert: <b>{_UPLOAD_WARN_BYTES // (1024 * 1024)} MB</b>\n"
        f"🗑 Cleanup after: <b>{_fmt_ttl(_UPLOAD_TTL_S)}</b>\n"
        f"📱 Display default: <b>{tg.esc(DEFAULT_DISPLAY)}</b>\n"
        f"🎚 Default mode: <b>{tg.esc(get_default_mode())}</b>\n"
        f"🕒 Transcription timeout: <b>{stt._TIMEOUT}s</b>\n"
        f"⬆️ Auto-update: <b>{au}</b> ({tg.esc(AUTO_UPDATE_POLICY)})"
    )


def _settings_menu(chat_id, thread_id=None):
    mid = tg.send(_settings_text(), chat_id, thread_id=thread_id,
                  buttons=_settings_root_rows())
    if not thread_id and mid:
        ui.delete_after(mid, chat_id, _SETTINGS_TTL_S)


def _settings_show(cb_msg, cb_chat, which):
    """Expand a root item into its preset picker, in place."""
    if which == "m":
        cur = stt.model_name()
        rows = [[{"text": f"{'• ' if k == cur else ''}{k} — {size}",
                  "callback_data": f"st:m:{k}"}]
                for k, size in stt_install.MODELS.items()]
        rows.append([{"text": "↩ Back", "callback_data": "st:root"}])
        tg.edit(cb_msg, "🎙 <b>Whisper model</b>\nChanging downloads the "
                "model if needed (runs in background).", cb_chat, buttons=rows)
    elif which == "w":
        cur = _UPLOAD_WARN_BYTES // (1024 * 1024)
        rows = [[{"text": f"{'• ' if mb == cur else ''}{mb} MB",
                  "callback_data": f"st:w:{mb}"}] for mb in _WARN_MB_PRESETS]
        rows.append([{"text": "↩ Back", "callback_data": "st:root"}])
        tg.edit(cb_msg, "📦 <b>Upload-folder size alert</b>\nDM the owner "
                "once a day past this size.", cb_chat, buttons=rows)
    elif which == "t":
        cur = _UPLOAD_TTL_S
        rows = [[{"text": f"{'• ' if s == cur else ''}{_fmt_ttl(s)}",
                  "callback_data": f"st:t:{s}"}] for s in _TTL_PRESETS]
        rows.append([{"text": "↩ Back", "callback_data": "st:root"}])
        tg.edit(cb_msg, "🗑 <b>Cleanup TTL</b>\nDelete uploaded media older "
                "than this (kept if a session still references it).",
                cb_chat, buttons=rows)
    elif which == "d":
        rows = [[{"text": f"{'• ' if v == DEFAULT_DISPLAY else ''}{v}",
                  "callback_data": f"st:d:{v}"}] for v in ("mobile", "desktop")]
        rows.append([{"text": "↩ Back", "callback_data": "st:root"}])
        tg.edit(cb_msg, "📱 <b>Display default</b>\nLayout new topics start "
                "in. /display overrides it per topic.", cb_chat, buttons=rows)
    elif which == "dm":
        cur = get_default_mode()
        rows = [[{"text": f"{'• ' if k == cur else ''}{k} — {p['label']}",
                  "callback_data": f"st:dm:{k}"}]
                for k, p in MODE_PRESETS.items()]
        rows.append([{"text": "↩ Back", "callback_data": "st:root"}])
        tg.edit(cb_msg, "🎚 <b>Default mode</b>\nWhich response style new "
                "sessions start in.", cb_chat, buttons=rows)
    elif which == "st":
        cur = stt._TIMEOUT
        rows = [[{"text": f"{'• ' if s == cur else ''}{s}s",
                  "callback_data": f"st:st:{s}"}] for s in _STT_PRESETS]
        rows.append([{"text": "↩ Back", "callback_data": "st:root"}])
        tg.edit(cb_msg, "🕒 <b>Transcription timeout</b>\nHow long to wait "
                "for voice/video transcription before giving up.",
                cb_chat, buttons=rows)
    elif which == "au":
        on = AUTO_UPDATE
        rows = [
            [{"text": f"{'• ' if on else ''}On", "callback_data": "st:au:on"},
             {"text": f"{'• ' if not on else ''}Off",
              "callback_data": "st:au:off"}],
            [{"text": f"{'• ' if AUTO_UPDATE_POLICY == 'replace' else ''}"
              "policy: replace", "callback_data": "st:au:replace"}],
            [{"text": f"{'• ' if AUTO_UPDATE_POLICY == 'merge' else ''}"
              "policy: merge", "callback_data": "st:au:merge"}],
            [{"text": "↩ Back", "callback_data": "st:root"}],
        ]
        tg.edit(cb_msg, "⬆️ <b>Auto-update</b>\nCheck hourly and update. On "
                "local changes: replace (back up + overwrite) or merge "
                "(wait, /update to review).", cb_chat, buttons=rows)


def _settings_set_warn(cb_msg, cb_chat, mb):
    global _UPLOAD_WARN_BYTES
    _UPLOAD_WARN_BYTES = mb * 1024 * 1024
    set_env("UPLOAD_WARN_MB", str(mb))
    audit.log("settings", f"upload_warn_mb={mb}")
    tg.edit(cb_msg, _settings_text(), cb_chat, buttons=_settings_root_rows())


def _settings_set_ttl(cb_msg, cb_chat, seconds):
    global _UPLOAD_TTL_S, _TEMP_NOTE
    _UPLOAD_TTL_S = seconds
    _TEMP_NOTE = _build_temp_note()
    set_env("UPLOAD_TTL_S", str(seconds))
    audit.log("settings", f"upload_ttl_s={seconds}")
    tg.edit(cb_msg, _settings_text(), cb_chat, buttons=_settings_root_rows())


def _settings_set_display(cb_msg, cb_chat, value):
    if value not in ("mobile", "desktop"):
        return
    global DEFAULT_DISPLAY
    DEFAULT_DISPLAY = value
    # Update the live copies the components captured at construction, so new
    # topics pick up the change without a restart.
    turnctl._default_display = value
    commands._default_display = value
    set_env("DEFAULT_DISPLAY", value)
    audit.log("settings", f"default_display={value}")
    tg.edit(cb_msg, _settings_text(), cb_chat, buttons=_settings_root_rows())


def _settings_set_default_mode(cb_msg, cb_chat, mode):
    if not valid_mode(mode):
        return
    set_default_mode(mode)
    audit.log("settings", f"default_mode={mode}")
    tg.edit(cb_msg, _settings_text(), cb_chat, buttons=_settings_root_rows())


def _settings_set_stt(cb_msg, cb_chat, seconds):
    if seconds not in _STT_PRESETS:
        return
    stt._TIMEOUT = seconds
    set_env("STT_TIMEOUT", str(seconds))
    audit.log("settings", f"stt_timeout={seconds}")
    tg.edit(cb_msg, _settings_text(), cb_chat, buttons=_settings_root_rows())


def _settings_set_autoupdate(cb_msg, cb_chat, value):
    global AUTO_UPDATE, AUTO_UPDATE_POLICY
    if value in ("on", "off"):
        AUTO_UPDATE = (value == "on")
        set_env("AUTO_UPDATE", "true" if AUTO_UPDATE else "false")
        audit.log("settings", f"auto_update={value}")
    elif value in ("replace", "merge"):
        AUTO_UPDATE_POLICY = value
        set_env("AUTO_UPDATE_POLICY", value)
        audit.log("settings", f"auto_update_policy={value}")
    else:
        return
    # Re-render the picker so the • marker moves to the new selection.
    _settings_show(cb_msg, cb_chat, "au")


def _settings_set_model(cb_msg, cb_chat, model):
    if model not in stt_install.MODELS:
        return
    if model == stt.model_name() and stt.pkg_present("faster_whisper"):
        tg.edit(cb_msg, _settings_text(), cb_chat,
                buttons=_settings_root_rows())
        return
    if stt_install.busy():
        ui.ephemeral(cb_chat, "⏳ Another install is already running",
                     seconds=6)
        return
    tg.edit(cb_msg, f"⏳ Downloading Whisper <b>{tg.esc(model)}</b> — "
            "this can take a few minutes…", cb_chat)
    threading.Thread(target=_run_settings_model_install,
                     args=(cb_msg, cb_chat, model),
                     daemon=True, name="bot-bg-install").start()


def _run_settings_model_install(cb_msg, cb_chat, model):
    # install_whisper persists WHISPER_MODEL (.env + env) on success, so
    # stt.model_name() reflects the switch once this returns ok.
    ok = stt_install.install_whisper(model)
    audit.log("settings", f"whisper_model={model} {'ok' if ok else 'FAILED'}")
    if ok:
        body = _settings_text()
    else:
        body = f"❌ Failed to install Whisper {tg.esc(model)} — see bot.log."
    # The download can run for minutes — longer than the /settings message's
    # own TTL — so the menu message may already be gone. Try to edit it back;
    # only if that fails (message deleted) send a fresh result notice, so the
    # outcome is never silently lost.
    if not tg.edit(cb_msg, body, cb_chat, buttons=_settings_root_rows()):
        result = (f"✅ Whisper {tg.esc(model)} ready" if ok else body)
        ui.ephemeral(cb_chat, result, seconds=30)


def _settings_clicked(cb_msg, cb_chat, rest):
    if rest == "root":
        tg.edit(cb_msg, _settings_text(), cb_chat,
                buttons=_settings_root_rows())
    elif rest in ("m", "w", "t", "d", "dm", "st", "au"):
        _settings_show(cb_msg, cb_chat, rest)
    elif rest.startswith("m:"):
        _settings_set_model(cb_msg, cb_chat, rest[2:])
    elif rest.startswith("w:"):
        try:
            _settings_set_warn(cb_msg, cb_chat, int(rest[2:]))
        except ValueError:
            pass
    elif rest.startswith("t:"):
        try:
            _settings_set_ttl(cb_msg, cb_chat, int(rest[2:]))
        except ValueError:
            pass
    elif rest.startswith("dm:"):
        _settings_set_default_mode(cb_msg, cb_chat, rest[3:])
    elif rest.startswith("st:"):
        try:
            _settings_set_stt(cb_msg, cb_chat, int(rest[3:]))
        except ValueError:
            pass
    elif rest.startswith("d:"):
        _settings_set_display(cb_msg, cb_chat, rest[2:])
    elif rest.startswith("au:"):
        _settings_set_autoupdate(cb_msg, cb_chat, rest[3:])


def _reject_no_session(chat_id, thread_id, what):
    """Notice for media/stickers dropped without an active bot session.

    In a session topic it persists; in General it self-cleans so the chat
    stays tidy (General must stay clean — only the pinned dashboard stays).
    """
    text = f"Send {what} in an active session"
    if thread_id:
        tg.send(text, chat_id, thread_id=thread_id)
    else:
        ui.ephemeral(chat_id, text, seconds=6)


def _handle_update(u):
    global bot_running

    cb = u.get("callback_query")
    if cb:
        tg.answer_callback(cb["id"])
        if cb.get("from", {}).get("id") != OWNER_ID:
            return
        _handle_callback(cb, cb.get("data", ""))
        return

    mcm = u.get("my_chat_member")
    if mcm:
        _handle_my_chat_member(mcm)
        return

    mr = u.get("message_reaction")
    if mr:
        _handle_reaction(mr)
        return

    msg = u.get("message", {})
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    from_user = msg.get("from", {}).get("id")

    text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()
    print(f"[msg] chat={chat_id} from={from_user} "
          f"text={text[:60] or caption[:60]}",
          file=sys.stderr, flush=True)

    # Auto-delete pin service messages from bot in General
    if msg.get("pinned_message"):
        fid = forum()
        if fid and chat_id == fid:
            mid = msg.get("message_id")
            if mid and not tg.delete(mid, chat_id):
                add_pending_delete(mid, time.time())  # due now — sweep retries
        return

    if from_user != OWNER_ID:
        return

    # Kill switch — ignore everything except unlock word in General
    if is_killed():
        if text:
            _try_unkill(text, chat_id, msg.get("message_id"),
                        msg.get("message_thread_id"))
        return

    thread_id = msg.get("message_thread_id")
    msg_id = msg.get("message_id")
    session = mgr.by_topic(thread_id) if thread_id else None
    mirror = mirror_mgr.by_topic(thread_id) if thread_id and not session else None

    fid = forum()
    if not thread_id and fid and chat_id == fid and msg_id:
        if not tg.delete(msg_id, chat_id):
            add_pending_delete(msg_id, time.time())  # due now — sweep retries

    # ── Terminal-mirror topic: forward text into the terminal claude
    # via the dtach socket, or politely reject if input isn't bridged.
    if mirror:
        photos = msg.get("photo")
        document = msg.get("document")
        sticker = msg.get("sticker")
        if photos or document or sticker:
            ui.ephemeral(chat_id,
                       "\U0001f501 Mirror does not bridge files/stickers — type text",
                       thread_id=thread_id, seconds=5)
            return
        if not text:
            return
        if not mirror.dtach_socket:
            ui.ephemeral(chat_id,
                       "\U0001f501 Output-only mirror — terminal input is not bridged "
                       "(start your terminal claude inside dtach to enable it)",
                       thread_id=thread_id, seconds=8)
            return
        # Auto-reaction 👀 on mirror input removed for budget reasons.
        ok = push_to_dtach(mirror.dtach_socket, text)
        if not ok:
            # Delivery failed → terminal is most likely gone. Persist the
            # notice (not ephemeral) so the continue-button stays clickable;
            # the user may have typed before the socket watcher fired.
            ui.send_to_topic(thread_id,
                             "❌ Could not deliver to terminal "
                             "(dtach socket missing or unresponsive)",
                             buttons=_mirror_continue_buttons(mirror.csid))
            mirror_mgr.set_dtach_socket(mirror.csid, None)
        else:
            # Track so the follower can clear pending state when claude
            # actually replies (end-to-end delivery confirmation).
            mirror.pending_user_msg_id = msg_id
            # Mark the text so the follower's user-event projection
            # suppresses the echo back into this same topic.
            mirror.note_injection(text)
        audit.log("mirror_input", text[:200], sid=mirror.csid)
        return

    # Handle photo/document attachments
    photos = msg.get("photo")
    document = msg.get("document")
    if photos or document:
        if not (session and session.is_bot_spawned and session.alive):
            _reject_no_session(chat_id, thread_id, "files")
            return
        file_id = photos[-1]["file_id"] if photos else document["file_id"]
        filename = ("photo.jpg" if photos
                    else document.get("file_name", "file"))
        # Album? Buffer this part and let the flush timer combine them into
        # a single turn instead of one turn per image.
        media_group_id = msg.get("media_group_id")
        if media_group_id:
            _buffer_media_group(media_group_id, session, file_id, filename,
                                caption, chat_id, msg_id, thread_id)
            return
        dest = os.path.join(_UPLOAD_DIR,
                            f"{int(time.time())}_{filename}")
        if tg.download_file(file_id, dest):
            user_text = (f"{caption}\n[Attached file: {dest}]" if caption
                         else f"[Attached file: {dest}]")
            user_text += f"\n{_TEMP_NOTE}"
            audit.log("user_message", f"[file] {filename}",
                      sid=session.sid)
            turnctl.enqueue_user_input(session, user_text, chat_id, msg_id, thread_id)
        else:
            tg.send("❌ Download failed", chat_id,
                    thread_id=thread_id)
        return

    # Handle stickers: emoji + pack name as text, plus the image itself —
    # static webp directly, video webm as scene frames, animated tgs via
    # its static thumbnail (Lottie vectors can't be decoded) (#77).
    sticker = msg.get("sticker")
    if sticker:
        if not (session and session.is_bot_spawned and session.alive):
            _reject_no_session(chat_id, thread_id, "stickers")
            return
        emoji = sticker.get("emoji") or ""
        set_name = sticker.get("set_name") or ""
        if set_name and emoji:
            ident = f" {emoji} (from pack \"{set_name}\")"
        elif set_name:
            ident = f" (from pack \"{set_name}\")"
        elif emoji:
            ident = f" {emoji}"
        else:
            ident = ""
        descr = (
            f"[The owner sent you a sticker{ident} — a sticker is a "
            "deliberate emotional/reaction cue, not just a dropped image. "
            "Read its expression, pose, any text on it, and the emoji it "
            "maps to as part of what the owner is communicating. When the "
            "sticker has BOTH a drawing and text, give the image and the "
            "text equal weight — do not fixate on the words and overlook "
            "the picture (or the reverse); both carry the meaning.]"
        )
        if sticker.get("is_video"):
            if not frames.available():
                _offer_media_install(session, "sticker", sticker["file_id"],
                                     descr, chat_id, msg_id, thread_id)
                return
            threading.Thread(
                target=_handle_video_sticker,
                args=(session, sticker["file_id"], descr,
                      chat_id, msg_id, thread_id),
                daemon=True, name="bot-bg-sticker",
            ).start()
            return
        # Static sticker → the file itself (webp); animated (tgs) → its
        # static thumbnail (jpeg).
        if sticker.get("is_animated"):
            image_id = (sticker.get("thumbnail") or {}).get("file_id")
            ext = "jpg"
        else:
            image_id = sticker["file_id"]
            ext = "webp"
        audit.log("user_message",
                  f"[sticker]{ident}".strip(), sid=session.sid)
        if image_id:
            dest = os.path.join(_UPLOAD_DIR,
                                f"{int(time.time())}_sticker.{ext}")
            if tg.download_file(image_id, dest):
                descr += f"\n[Attached file: {dest}]\n{_TEMP_NOTE}"
        turnctl.enqueue_user_input(session, descr, chat_id, msg_id, thread_id)
        return

    # Voice / audio messages → transcribe (off the poll thread).
    voice = msg.get("voice") or msg.get("audio")
    if voice:
        if not (session and session.is_bot_spawned and session.alive):
            _reject_no_session(chat_id, thread_id, "voice")
            return
        if not stt.available():
            _offer_media_install(session, "voice", voice["file_id"],
                                 caption, chat_id, msg_id, thread_id)
            return
        threading.Thread(
            target=_handle_voice,
            args=(session, voice["file_id"], caption, chat_id, msg_id, thread_id),
            daemon=True, name="bot-bg-voice",
        ).start()
        return

    # Video / video-note → transcribe audio + sample scene frames.
    video = msg.get("video") or msg.get("video_note")
    if video:
        if not (session and session.is_bot_spawned and session.alive):
            _reject_no_session(chat_id, thread_id, "video")
            return
        if not (frames.available() or stt.available()):
            _offer_media_install(session, "video", video["file_id"],
                                 caption, chat_id, msg_id, thread_id)
            return
        threading.Thread(
            target=_handle_video,
            args=(session, video["file_id"], caption, chat_id, msg_id, thread_id),
            daemon=True, name="bot-bg-video",
        ).start()
        return

    if not text:
        return

    if text.startswith("/"):
        cmd, _, args = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        audit.log("command", f"{cmd} {args}".strip(),
                  sid=session.sid if session else None)
        _handle_command(cmd, args, chat_id, thread_id, session)
    else:
        audit.log("user_message", text,
                  sid=session.sid if session else None)
        if session and session.is_bot_spawned:
            turnctl.enqueue_user_input(session, text, chat_id, msg_id, thread_id)
        elif session:
            tg.send("Terminal session — use terminal",
                    chat_id, thread_id=thread_id)
        elif thread_id:
            tg.send("No active session. Use /restart or /new",
                    chat_id, thread_id=thread_id)
        elif chat_id == OWNER_ID:
            tg.send("Use in a session topic, or /new",
                    chat_id)
        else:
            fid = forum()
            if fid and chat_id == fid:
                ui.ephemeral(chat_id, "Use in a session topic, or /new", seconds=5)


def _handle_command(cmd, args, chat_id, thread_id, session):
    global bot_running
    if cmd == "/setup":
        commands.cmd_setup(chat_id)
        # First successful link → drop the owner into the guided tour (once).
        fid = get_forum_chat_id()
        if fid and not get_tour_shown():
            tour.open_tour(fid)
            set_tour_shown(True)
    elif cmd == "/tour":
        tour.open_tour(get_forum_chat_id())
    elif cmd == "/new":
        commands.cmd_new(args, chat_id=chat_id, thread_id=thread_id)
    elif cmd == "/sessions":
        commands.cmd_sessions(chat_id, thread_id)
    elif cmd == "/resume":
        commands.cmd_resume(args, chat_id, thread_id)
    elif cmd == "/history":
        commands.cmd_history(session, chat_id, thread_id, args)
    elif cmd == "/stop":
        commands.cmd_stop(session, chat_id, thread_id)
    elif cmd == "/interrupt":
        commands.cmd_interrupt(session, chat_id, thread_id)
    elif cmd == "/restart":
        commands.cmd_restart(chat_id, thread_id)
    elif cmd == "/usage":
        commands.cmd_usage(session, chat_id, thread_id)
    elif cmd == "/display":
        commands.cmd_display(chat_id, thread_id, args)
    elif cmd == "/mode":
        commands.cmd_mode(session, chat_id, thread_id, args)
    elif cmd == "/update":
        commands.cmd_update(chat_id, thread_id)
    elif cmd == "/test_perm":
        commands.cmd_test_perm(chat_id, thread_id)
    elif cmd == "/kill":
        _do_kill()
    elif cmd == "/audit":
        commands.cmd_audit(args, chat_id, thread_id)
    elif cmd == "/stop_bot":
        fid = forum()
        if fid:
            ui.ephemeral(fid, "\U0001f44b Shutting down.", seconds=5)
        bot_running = False
    elif cmd in ("/help", "/start"):
        commands.cmd_help(chat_id, thread_id)
    elif cmd == "/menu":
        commands.cmd_menu(chat_id, thread_id, session)
    elif cmd == "/settings":
        _settings_menu(chat_id, thread_id)
    else:
        if session and session.is_bot_spawned:
            ok = mgr.send_user_message(session.sid,
                                       f"{cmd} {args}".strip())
            if not ok:
                tg.send("⚠️ Session died", chat_id,
                        thread_id=thread_id)
        elif session:
            tg.send("ℹ️ Terminal session — use terminal",
                    chat_id, thread_id=thread_id)


def _handle_callback(cb, data):
    cb_chat = cb.get("message", {}).get("chat", {}).get("id")
    cb_msg = cb.get("message", {}).get("message_id")
    cb_thread = cb.get("message", {}).get("message_thread_id")

    if data.startswith("int:"):
        sid = data[4:]
        session = mgr._sessions.get(sid)
        if session:
            commands._do_interrupt(session, cb_chat, cb_thread)
        return

    if data.startswith("aq:"):
        asker.handle_callback(data)
        return

    if data.startswith("mi:"):
        parts = data.split(":")
        if len(parts) == 3:
            _media_install_clicked(parts[1], parts[2])
        return

    if data.startswith("st:"):
        if cb_msg and cb_chat:
            _settings_clicked(cb_msg, cb_chat, data[3:])
        return

    if data.startswith("tr:"):
        rest = data[3:]
        if rest.startswith("nav:") and cb_msg and cb_chat:
            idx_s = rest[4:]
            if idx_s.isdigit():
                tour.paint(cb_msg, cb_chat, int(idx_s))
        elif rest == "open":
            tour.open_tour(get_forum_chat_id())
        elif rest == "close" and cb_msg and cb_chat:
            tour.close_tour(cb_msg, cb_chat)
        elif rest == "go:new":
            commands.cmd_new("", chat_id=get_forum_chat_id(), thread_id=None)
        elif rest == "go:help":
            commands.cmd_help(get_forum_chat_id(), None)
        return

    if data.startswith("hr:") and cb_msg and cb_chat:
        rest = data[3:]
        if rest == "menu":
            commands.render_help_menu(cb_msg, cb_chat)
        elif rest == "close":
            commands.close_help(cb_msg, cb_chat)
        elif rest.startswith("cat:"):
            commands.render_help_section(cb_msg, cb_chat, rest[4:])
        return

    if data.startswith("p:"):
        parts = data.split(":")
        if len(parts) == 3:
            short_id = parts[1]
            decision = "allow" if parts[2] == "a" else "deny"
            with state.lock:
                full_id = state.perm_key_map.pop(short_id, short_id)
                entry = state.pending_permissions.pop(short_id, None)
            bridge.resolve_permission(full_id, decision)
            audit.log("permission_decision", f"{decision} {short_id}")
            if entry:
                msg_id, perm_chat, _ = entry
                mark = "✓ Allowed" if decision == "allow" else "✗ Denied"
                tg.edit(msg_id, mark, perm_chat)
                def _del_perm(mid=msg_id, cid=perm_chat):
                    time.sleep(1)
                    tg.delete(mid, cid)
                threading.Thread(target=_del_perm, daemon=True, name="bot-bg-delperm").start()

    elif data.startswith("na:"):
        pick_id = data.split(":")[1]
        with state.lock:
            projects = state.pending_project_picks.get(pick_id)
        if projects:
            rows = []
            for i, p in enumerate(projects):
                label = os.path.basename(p.rstrip("/"))
                rows.append([{"text": label,
                              "callback_data": f"n:{pick_id}:{i}"}])
            rows.append(CLOSE_ROW)
            if cb_chat and cb_msg:
                tg.edit(cb_msg, "\U0001f4c2 Choose project:",
                        cb_chat, buttons=rows)

    elif data.startswith("n:"):
        parts = data.split(":")
        if len(parts) == 3:
            pick_id, idx_s = parts[1], parts[2]
            with state.lock:
                projects = state.pending_project_picks.pop(pick_id, None)
            if projects and idx_s.isdigit():
                idx = int(idx_s)
                if 0 <= idx < len(projects):
                    if cb_chat and cb_msg:
                        tg.delete(cb_msg, cb_chat)
                    lifecycle.spawn_session(projects[idx])

    elif data.startswith("ra:"):
        pick_id = data.split(":")[1]
        with state.lock:
            sessions_list = state.pending_resume_picks.get(pick_id)
        if sessions_list and cb_chat and cb_msg:
            text, rows = commands._build_resume_picker(sessions_list, pick_id,
                                              max_items=None)
            tg.edit(cb_msg, text, cb_chat, buttons=rows)

    elif data.startswith("r:"):
        parts = data.split(":")
        if len(parts) == 3:
            pick_id, idx_s = parts[1], parts[2]
            with state.lock:
                sessions_list = state.pending_resume_picks.pop(pick_id, None)
            if sessions_list and idx_s.isdigit():
                idx = int(idx_s)
                if 0 <= idx < len(sessions_list):
                    if cb_chat and cb_msg:
                        tg.delete(cb_msg, cb_chat)
                    sid, cwd, _, _ = sessions_list[idx]
                    commands._do_resume(sid, cb_chat, cb_thread)

    elif data.startswith("c:"):
        compact_id = data[2:]
        with state.lock:
            saved = state.saved_turns.get(compact_id)
        if saved and cb_chat:
            msg_ids, texts, ops = saved
            for mid in msg_ids:
                if mid != cb_msg:
                    tg.delete(mid, cb_chat)
            if cb_msg:
                tg.edit(cb_msg, "⏳ Summarizing…", cb_chat)

            def _do_compact():
                summary = turnctl.build_summary(texts, ops)
                btn = [[{"text": "\U0001f4c2 Expand",
                          "callback_data": f"uc:{compact_id}"}]]
                if cb_msg:
                    tg.edit(cb_msg, summary, cb_chat, buttons=btn)

            threading.Thread(target=_do_compact, daemon=True, name="bot-bg-compact").start()

    elif data.startswith("uc:"):
        compact_id = data[3:]
        with state.lock:
            saved = state.saved_turns.get(compact_id)
        if saved and cb_chat:
            _, texts, ops = saved
            if cb_msg:
                tg.delete(cb_msg, cb_chat)
            new_ids = []
            for t in texts:
                new_ids.extend(
                    tg.send_long(t, cb_chat, thread_id=cb_thread,
                                 markdown=True))
            btn = [[{"text": "\U0001f5dc Compact",
                      "callback_data": f"c:{compact_id}"}]]
            if new_ids:
                try:
                    tg._req("editMessageReplyMarkup", {
                        "chat_id": cb_chat,
                        "message_id": new_ids[-1],
                        "reply_markup": {"inline_keyboard": btn},
                    })
                except Exception:
                    pass
            with state.lock:
                state.saved_turns[compact_id] = (new_ids, texts, ops)

    elif data.startswith("fork:"):
        parent_sid = data[5:]
        parent = mgr._sessions.get(parent_sid)
        if parent and parent.claude_session_id:
            if cb_chat and cb_msg:
                tg.delete(cb_msg, cb_chat)
            fid = forum()
            if fid:
                ts = time.strftime("%H:%M")
                label = f"{parent.name} fork — {ts}"
                topic_id = tg.create_forum_topic(fid, label, icon_color=0x6FB9F0)
                if topic_id:
                    with state.lock:
                        state.topic_labels[topic_id] = label
                    session = mgr.fork(parent, topic_id, parent.name)
                    if session:
                        session.topic_label = label
                        lifecycle.attach_controls(
                            session,
                            text=f"\U0001f500 Fork of <b>{tg.esc(parent.name)}</b>")
                        # Copy the last few messages from the parent topic
                        # so the fork doesn't open empty — the user sees
                        # the point they branched from.
                        if parent.topic_id:
                            with state.lock:
                                recent = list(state.recent_msgs.get(
                                    parent.topic_id, []))
                            if recent:
                                tg.copy_messages(
                                    fid, fid, recent[-_FORK_BACKFILL:],
                                    thread_id=topic_id)
                        turnctl.send_fork_summary(parent, topic_id)
                        url = ui.topic_url(topic_id)
                        if url:
                            ui.ephemeral(fid, "\U0001f500",
                                       buttons=[[{"text": "Open fork", "url": url}]],
                                       seconds=5)

    elif data == "close":
        if cb_chat and cb_msg:
            tg.delete(cb_msg, cb_chat)
        return

    elif data.startswith("upd:"):
        action = data[4:]

        def _apply_then_restart(strategy=None):
            """Run an apply strategy; restart on success, surface conflict /
            error otherwise. rc contract: 0=ok, 2=up-to-date, 3=conflict."""
            rc, output = _run_update(non_interactive=True, strategy=strategy)
            if rc == 0:
                if cb_chat:
                    tg.send("✅ Updated. Restarting...", cb_chat,
                            thread_id=cb_thread)
                time.sleep(1)
                _restart_bot()
            elif rc == 2:
                if cb_chat:
                    tg.send("✅ Already up to date.", cb_chat, thread_id=cb_thread)
            elif rc == 3:
                _offer_conflict_choice()
            else:
                if cb_chat:
                    tg.send(f"❌ Update failed:\n<code>{tg.esc(output[:500])}</code>",
                            cb_chat, thread_id=cb_thread)

        def _offer_conflict_choice():
            """Local edits clash with the update — let the owner pick: take the
            new version (backup + overwrite) or resolve it in a guided session."""
            files = _conflicted_files()
            n = len(files)
            text = (f"⚠️ <b>Merge conflict</b> — your local edits clash with "
                    f"the update in {n} file(s).\n\nKeep your changes by "
                    f"resolving them, or take the new version (your edits are "
                    f"backed up either way).")
            buttons = [
                [{"text": "🧩 Resolve in a session", "callback_data": "upd:resolve"}],
                [{"text": "⬆️ Take new version", "callback_data": "upd:replace"}],
            ]
            if cb_chat:
                tg.send(text, cb_chat, thread_id=cb_thread, buttons=buttons)

        if action == "go":
            if cb_msg and cb_chat:
                tg.edit(cb_msg, "⬆️ Updating...", cb_chat)
            # Always 'auto' here, never the legacy AUTO_UPDATE_POLICY: a manual
            # /update must try a real merge and offer the conflict choice, not
            # silently replace local edits.
            threading.Thread(target=lambda: _apply_then_restart(strategy="auto"),
                             daemon=True, name="bot-bg-update").start()
        elif action == "replace":
            if cb_msg and cb_chat:
                tg.edit(cb_msg, "⬆️ Taking new version...", cb_chat)
            threading.Thread(target=lambda: _apply_then_restart(strategy="replace"),
                             daemon=True, name="bot-bg-update").start()
        elif action == "finalize":
            if cb_msg and cb_chat:
                tg.edit(cb_msg, "⬆️ Finalizing...", cb_chat)
            threading.Thread(target=lambda: _apply_then_restart(strategy="finalize"),
                             daemon=True, name="bot-bg-update").start()
        elif action == "resolve":
            if cb_msg and cb_chat:
                tg.edit(cb_msg, "🧩 Opening a resolve session…", cb_chat)
            threading.Thread(target=_spawn_merge_resolve_session, daemon=True,
                             name="bot-bg-update").start()
        elif action == "no":
            if cb_msg and cb_chat:
                tg.delete(cb_msg, cb_chat)

    elif data.startswith("dt:"):
        import device_monitor
        key = data[3:]
        device_monitor.trust_device(key)
        audit.log("device_trust", key)
        if cb_msg and cb_chat:
            tg.edit(cb_msg, "✓ Device trusted", cb_chat)
            def _del_dt(mid=cb_msg, cid=cb_chat):
                time.sleep(5)
                tg.delete(mid, cid)
            threading.Thread(target=_del_dt, daemon=True, name="bot-bg-deldt").start()

    elif data == "dk:":
        _do_kill()
        if cb_msg and cb_chat:
            tg.edit(cb_msg, "\U0001f512 Bot killed", cb_chat)

    elif data.startswith("mf:"):
        # mf:<csid_prefix>:<level>  — toggle filter level on a mirror.
        parts = data.split(":", 2)
        if len(parts) == 3:
            prefix, level = parts[1], parts[2]
            if level in ("all", "lite"):
                hit = None
                for full in [m.csid for m in mirror_mgr.list()]:
                    if full.startswith(prefix):
                        hit = full
                        break
                if hit:
                    mirror_mgr.set_filter_level(hit, level)
                    m = mirror_mgr.by_csid(hit)
                    if m and m.welcome_msg_id and cb_chat:
                        try:
                            tg.edit(m.welcome_msg_id,
                                    mirror.welcome_text(m), cb_chat,
                                    buttons=mirror.welcome_buttons(m))
                        except Exception as e:
                            print(f"[mirror] welcome edit failed: {e}",
                                  file=sys.stderr, flush=True)

    elif data.startswith("mm:"):
        # mm:<csid_prefix>  — push Shift+Tab into the dtach socket and
        # advance our local "current mode" index so the button label
        # reflects what Claude's TUI shows after the keystroke.
        prefix = data.split(":", 1)[1]
        hit = None
        for full in [m.csid for m in mirror_mgr.list()]:
            if full.startswith(prefix):
                hit = full
                break
        m = mirror_mgr.by_csid(hit) if hit else None
        if m and m.dtach_socket:
            ok = push_to_dtach(m.dtach_socket, "\x1b[Z", with_enter=False)
            if ok:
                mirror_mgr.advance_mode(hit)
                m_now = mirror_mgr.by_csid(hit)
                if m_now and m_now.welcome_msg_id and cb_chat:
                    try:
                        tg.edit(m_now.welcome_msg_id,
                                mirror.welcome_text(m_now), cb_chat,
                                buttons=mirror.welcome_buttons(m_now))
                    except Exception as e:
                        print(f"[mirror] welcome edit failed: {e}",
                              file=sys.stderr, flush=True)
            elif cb_chat:
                tg.send("⚠️ couldn't push Shift+Tab — dtach socket gone?",
                        cb_chat, thread_id=cb_thread)

    elif data.startswith("mr:"):
        # mr:<csid> — continue a dead-terminal mirror as a bot session
        # in the same topic: drop the mirror, `claude --resume <csid>`.
        csid = data[3:]
        if not (cb_thread and cb_chat):
            return
        existing = mgr.by_topic(cb_thread)
        if existing and existing.alive:
            if cb_msg:
                tg.edit(cb_msg, "ℹ️ Already a bot session — just type here",
                        cb_chat)
            return
        m = mirror_mgr.by_csid(csid)
        cwd = (m.cwd if m else None) or _resolve_session_cwd(csid)
        if not cwd or not os.path.isdir(cwd):
            if cb_msg:
                tg.edit(cb_msg,
                        "❌ Can't resolve the session's cwd — try /resume",
                        cb_chat)
            return
        # Unregister the mirror FIRST — on_message routes mirror-topic
        # input into the dead dtach socket until the topic stops
        # resolving as a mirror.
        if m:
            mirror_mgr.unregister(csid)
        old = mgr.by_claude_session_id(csid)
        if old and not old.is_bot_spawned:
            mgr.detach_terminal(old.sid)
        name = os.path.basename(cwd.rstrip("/")) or "session"
        s = mgr.resume(csid, cb_thread, name, cwd)
        with state.lock:
            label = state.topic_labels.get(cb_thread) or name
        s.topic_label = label
        lifecycle.attach_controls(s)
        fid = forum()
        if fid:
            tg.edit_forum_topic(fid, cb_thread, label,
                                icon_custom_emoji_id=_ICON_ACTIVE)
        audit.log("mirror_to_session", f"{csid[:12]} → topic {cb_thread}")
        if cb_msg:
            tg.edit(cb_msg, "▶ Continued as bot session — just type here",
                    cb_chat)

    elif data.startswith("mirror_history:"):
        # mirror_history:<mode>:<csid_prefix>
        parts = data.split(":", 2)
        if len(parts) == 3:
            mode, csid_prefix = parts[1], parts[2]
            hit_csid, entry = mirror.pop_pending_backfill(csid_prefix)
            m = mirror_mgr.by_csid(hit_csid) if hit_csid else None
            if entry and m and cb_chat and cb_msg:
                # Drop the prompt; the chosen mode's content takes its place.
                try:
                    tg.delete(cb_msg, cb_chat)
                except Exception:
                    pass
                mirror.start_backfill_thread(
                    m, cb_chat, mode, entry["snapshot"])
            elif cb_msg and cb_chat:
                tg.edit(cb_msg,
                        "История больше недоступна (сессия пересоздана).",
                        cb_chat)

    elif data.startswith("m:"):
        action = data[2:]
        session = mgr.by_topic(cb_thread) if cb_thread else None
        # Don't delete the source message — it may be the dashboard pin
        if action == "mode" and session:
            commands.show_mode_picker(session, cb_chat)
        elif action == "controls" and session:
            commands.render_topic_controls(session)
        elif action.startswith("mode:") and session:
            mgr.set_mode(session.sid, action.split(":", 1)[1])
            commands.render_topic_controls(session)
        elif action == "new":
            commands.cmd_new("", chat_id=cb_chat, thread_id=cb_thread)
        elif action == "sessions":
            commands.cmd_sessions(cb_chat, cb_thread)
        elif action == "resume":
            commands.cmd_resume("", cb_chat, cb_thread)
        elif action == "display" and cb_thread:
            commands.cmd_display(cb_chat, cb_thread, "")
        elif action == "stop" and session:
            commands.cmd_stop(session, cb_chat, cb_thread)
        elif action == "restart":
            commands.cmd_restart(cb_chat, cb_thread)
        elif action == "usage" and session:
            commands.cmd_usage(session, cb_chat, cb_thread)
        elif action == "history" and session:
            commands.cmd_history(session, cb_chat, cb_thread, "")
        elif action == "help":
            commands.cmd_help(cb_chat, cb_thread)


if __name__ == "__main__":
    main()
