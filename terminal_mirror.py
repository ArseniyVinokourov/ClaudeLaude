"""Terminal session mirror — bridge a terminal Claude session to a TG topic.

Two channels:
- Output: tail the session's JSONL transcript at
  ~/.claude/projects/<encoded-cwd>/<csid>.jsonl, project new events
  (user/assistant/tool_use) into the bot's mirror topic.
- Input: pipe bytes from a TG message into the wrapped claude's stdin
  via `dtach -p <socket>`, exactly as if the owner had typed them at
  the keyboard.

The bot does not own the terminal process — it observes via the JSONL
and (optionally) injects bytes through the dtach socket. Lifecycle
stays external; healthcheck handles disappearance.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable

_PERSIST_PATH = os.environ.get(
    "BOT_MIRRORS_FILE",
    os.path.join(os.path.dirname(__file__), ".mirrors.json"),
)


_DTACH_BIN = shutil.which("dtach")


def push_to_dtach(socket: str | None, text: str,
                  timeout: float = 3.0,
                  with_enter: bool = True) -> bool:
    """Inject `text` (optionally followed by a carriage return) into the
    dtach socket.

    The bytes land on the wrapped program's stdin exactly as if typed
    at the keyboard. Claude's TUI runs in raw mode, so the Enter key
    is `\\r` (0x0D), not `\\n` (0x0A) — sending `\\n` leaves the text
    in the input box without submitting it. This matches what tmux's
    `send-keys ... Enter` produced in the previous bridge.

    `with_enter=False` is for control keys like Shift+Tab (`\\e[Z`)
    where the trailing `\\r` would also be sent and submit a blank
    line into Claude's input.

    Returns False on missing dtach, missing/stale socket, timeout, or
    non-zero exit.
    """
    if not socket or not _DTACH_BIN:
        return False
    if not dtach_socket_alive(socket):
        return False
    payload = text + ("\r" if with_enter else "")
    try:
        p = subprocess.run(
            [_DTACH_BIN, "-p", socket],
            input=payload.encode("utf-8", errors="replace"),
            timeout=timeout, capture_output=True,
        )
        return p.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def dtach_socket_alive(socket: str | None) -> bool:
    """Probe whether the dtach socket file exists and is a unix socket.

    dtach auto-cleans the socket file when its child exits, so the
    presence of a valid socket inode is a strong signal that the
    wrapped program is still running. Missing path, wrong file type,
    or stat error → not alive.
    """
    if not socket:
        return False
    try:
        st = os.stat(socket)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode)


def _tail_offset(path: str, max_events: int = 12) -> int:
    """Return the byte offset at which the last `max_events` JSONL
    lines begin. Used to start the JSONL follower at the tail of an
    existing transcript without spamming the topic with the full
    history.

    Event-count (not byte-count) tail: when /bot-mirror is invoked
    Claude has just appended a giant isMeta line containing the
    markdown body of bot-mirror.md (~3-10 KB). A byte-window tail
    lands INSIDE that line and the readline() skip discards every
    real conversation event that came before — leaving the topic
    blank. Counting lines from EOF backwards guarantees the offset
    lands at a clean event boundary.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return 0
    if not data:
        return 0
    # Split on b"\n" — trailing empty element from final newline is OK
    # to ignore; what we want is the byte offset of the first non-empty
    # line within the last max_events lines.
    parts = data.split(b"\n")
    # Drop trailing empty (from final newline).
    if parts and parts[-1] == b"":
        parts.pop()
    if len(parts) <= max_events:
        return 0
    skip = parts[:-max_events]
    # Each skipped line is len(line) + 1 (for its newline).
    return sum(len(line) + 1 for line in skip)


def read_logical_events(path: str, limit: int | None = None) -> list[dict]:
    """Read JSONL and return a list of "logical" events.

    A logical event is what the user would see as one bubble in TG:
      {"kind": "user", "text": "...", "byte_end": <offset>}
      {"kind": "assistant", "text": "...", "tools": [str,...], "byte_end": <offset>}

    Filters: isMeta=True events dropped, slash-command wrapper-tag
    events dropped, events with no visible content dropped.

    Merging: consecutive assistant events (text + tool_use blocks)
    accumulate into ONE logical event until the next user event flushes
    them. Claude often emits 2-4 assistant events per turn (text chunk,
    tool, more text); the user perceives them as one reply.

    `byte_end` is the offset right after the LAST raw event line that
    contributed to this logical event — used by the caller to set the
    JSONL follower's resume point.

    If `limit` is given, returns at most the last `limit` logical
    events (suffix slice).
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    if not data:
        return []

    events: list[dict] = []
    cur_assistant: dict | None = None
    pos = 0
    # When Claude Code resumes a session that was interrupted mid-turn
    # (e.g. our /bot-mirror swap SIGTERMs claude after writing the
    # sentinel), the new claude injects a synthetic isMeta user prompt
    # like "Continue from where you left off." and the model usually
    # replies with a one-line stub ("No response requested.", "OK." …).
    # The user has no business seeing either in the mirror topic. We
    # already drop the isMeta user via the filter below; this flag
    # also drops the synthetic assistant reply that follows.
    drop_next_assistant = False
    for raw_line in data.split(b"\n"):
        line_len = len(raw_line) + 1  # +1 for the newline split removed
        line_end = pos + line_len
        pos = line_end
        if not raw_line.strip():
            continue
        try:
            e = json.loads(raw_line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if e.get("isMeta"):
            # Flag a recovery-style isMeta — the next assistant message
            # is the synthetic stub reply and should be dropped too.
            msg = e.get("message") or {}
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if "Continue from where you left off" in text:
                drop_next_assistant = True
            continue
        etype = e.get("type")
        msg = e.get("message") or {}
        content = msg.get("content")

        if etype == "user":
            # Flush any in-progress assistant.
            if cur_assistant is not None:
                events.append(cur_assistant)
                cur_assistant = None
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text", ""))
                text = "".join(parts)
            text = text.strip()
            if not text:
                continue
            if ("<command-message>" in text
                    or "<command-name>" in text
                    or "<command-args>" in text):
                continue
            events.append({"kind": "user", "text": text,
                           "byte_end": line_end})
            continue

        if etype == "assistant":
            if drop_next_assistant:
                # Synthetic recovery reply after an interrupted-turn
                # resume — invisible in the mirror.
                drop_next_assistant = False
                continue
            if not isinstance(content, list):
                continue
            text_parts = []
            tool_lines = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    text_parts.append(b.get("text", ""))
                elif btype == "tool_use":
                    tool_lines.append({
                        "name": b.get("name") or "?",
                        "input": b.get("input") or {},
                    })
            text_chunk = "".join(text_parts)
            if not text_chunk and not tool_lines:
                continue
            if cur_assistant is None:
                cur_assistant = {"kind": "assistant",
                                 "text": text_chunk,
                                 "tools": tool_lines,
                                 "byte_end": line_end}
            else:
                if text_chunk:
                    # Separate with newline so chunks read as distinct
                    # paragraphs when merged.
                    sep = "\n" if cur_assistant["text"] else ""
                    cur_assistant["text"] += sep + text_chunk
                cur_assistant["tools"].extend(tool_lines)
                cur_assistant["byte_end"] = line_end
            continue
        # Other event types (tool_use as top-level, tool_result, etc.)
        # are ignored — they're already represented inside the
        # assistant block above for modern Claude Code (≥2.1.x).
    if cur_assistant is not None:
        events.append(cur_assistant)

    # Drop the leading "mirror: https://t.me/c/..." assistant event
    # that Claude emits as the slash-command's own reply — it's noise
    # the owner already saw via the HTTP response.
    events = [
        e for e in events
        if not (e["kind"] == "assistant"
                and e["text"].startswith("mirror: https://t.me/c/"))
    ]

    if limit is not None and len(events) > limit:
        events = events[-limit:]
    return events


def jsonl_path_for(csid: str, cwd: str) -> str:
    """Resolve Claude Code's transcript file for a given session+cwd.

    Claude Code stores transcripts at
    ~/.claude/projects/<encoded-cwd>/<csid>.jsonl where encoded-cwd is
    the absolute cwd with '/' replaced by '-'. The leading '/'
    becomes a leading '-'.
    """
    encoded = cwd.replace("/", "-")
    return os.path.expanduser(f"~/.claude/projects/{encoded}/{csid}.jsonl")


def _backfill_done_event() -> threading.Event:
    """A pre-set Event so by default the follower projects events live
    without waiting. on_open_in_bot clears it explicitly when a backfill
    needs to land first."""
    ev = threading.Event()
    ev.set()
    return ev


@dataclass
class TerminalMirror:
    csid: str
    cwd: str
    topic_id: int
    dtach_socket: str | None
    jsonl_path: str
    last_offset: int = 0
    alive: bool = True
    # Last TG user-msg the bot relayed into the pane; cleared once
    # claude posts an assistant reply (so the bot can swap 👀 → 👍).
    pending_user_msg_id: int | None = field(default=None, repr=False)
    follower: threading.Thread | None = field(default=None, repr=False)
    # When clear, the follower thread suspends projection — used so a
    # backfill batch lands FIRST in the topic (chronological order)
    # before any concurrently-arriving live events. Default = set
    # (live-only path doesn't need to wait).
    backfill_done: threading.Event = field(default_factory=_backfill_done_event,
                                           repr=False)
    # Texts pushed into the pane via TG within the last few seconds.
    # Used to suppress the JSONL echo that would otherwise duplicate
    # the owner's own message back into the topic they sent it from.
    _recent_injected: list = field(default_factory=list, repr=False)
    _recent_lock: threading.Lock = field(default_factory=threading.Lock,
                                         repr=False)
    # Set when we just saw an isMeta "Continue from where you left
    # off." synthetic user — Claude Code injects it on --resume after
    # an interrupted turn (our /bot-mirror swap is one such case).
    # The next assistant event is the synthetic stub reply ("No
    # response requested.", "OK." …); we drop it from the topic.
    _drop_next_assistant: bool = field(default=False, repr=False)
    # Mirror filter level:
    #   "all"  — hide every tool_use (chat-only view, owner reads
    #            only their prompts and claude's text replies).
    #   "lite" — hide Write/Edit/python-heredoc Bash and tool_use
    #            that was just permission-paired; show normal Bash,
    #            other tools as-is. Default.
    filter_level: str = field(default="lite")
    # (tool_name, normalized_input) of the most recent permission
    # request the bot showed for this mirror. When the projection
    # sees a tool_use matching this pair, it skips it — Allow/Deny
    # prompt already conveyed the action; projecting the tool again
    # would duplicate the signal.
    pending_perm_tool: tuple | None = field(default=None, repr=False)
    # ID of the welcome message in the topic — bot edits this when
    # the filter level toggles so the inline button labels reflect
    # current state. None for legacy mirrors restored from
    # .mirrors.json that pre-date this field.
    welcome_msg_id: int | None = field(default=None)
    # Bot's best guess at Claude's current Shift+Tab permission mode.
    # Cycle: default → acceptEdits → plan → default. Starts at 0
    # (default — what `claude` launches with absent
    # `--permission-mode`). Drifts if the user presses Shift+Tab in
    # the terminal directly; the bot has no read-back from the TUI
    # so we trust our own count.
    mode_index: int = field(default=0)

    def note_injection(self, text: str) -> None:
        """Record that `text` was just pushed into the pane from TG."""
        with self._recent_lock:
            self._recent_injected.append((text, time.time()))
            if len(self._recent_injected) > 32:
                del self._recent_injected[0]

    def consume_recent_echo(self, text: str, ttl: float = 30.0) -> bool:
        """Return True (and pop the entry) if `text` was injected recently.
        Stale entries (> ttl seconds) are evicted on every call."""
        now = time.time()
        with self._recent_lock:
            while (self._recent_injected
                   and now - self._recent_injected[0][1] > ttl):
                self._recent_injected.pop(0)
            for i, (t, _ts) in enumerate(self._recent_injected):
                if t == text:
                    del self._recent_injected[i]
                    return True
        return False


class TerminalMirrorManager:
    """Registry + persistence + JSONL follower lifecycle.

    on_event(mirror, event) is invoked for each parsed JSONL event
    once a follower is running. The bot wires this to project the
    event into the mirror's topic.
    """

    def __init__(self, on_event: Callable[["TerminalMirror", dict], None]):
        self._on_event = on_event
        self._mirrors: dict[str, TerminalMirror] = {}
        self._topic_map: dict[int, str] = {}
        self._lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._restore()

    def register(self, csid: str, cwd: str, topic_id: int,
                 dtach_socket: str | None = None) -> TerminalMirror:
        """Idempotent — second call for the same csid returns the existing record."""
        with self._lock:
            existing = self._mirrors.get(csid)
            if existing:
                return existing
            m = TerminalMirror(
                csid=csid, cwd=cwd, topic_id=topic_id,
                dtach_socket=dtach_socket or None,
                jsonl_path=jsonl_path_for(csid, cwd),
            )
            # Default: start the follower at EOF so it only projects
            # events that ARRIVE after registration. Any backfill of
            # already-existing history is orchestrated separately by
            # the bot (see on_open_in_bot — it counts logical events,
            # then either runs a silent full backfill or asks the user
            # via inline buttons how much history to project).
            try:
                if os.path.exists(m.jsonl_path):
                    m.last_offset = os.path.getsize(m.jsonl_path)
            except OSError:
                pass
            self._mirrors[csid] = m
            self._topic_map[topic_id] = csid
        self._persist()
        return m

    def unregister(self, csid: str):
        with self._lock:
            m = self._mirrors.pop(csid, None)
            if not m:
                return
            if self._topic_map.get(m.topic_id) == csid:
                del self._topic_map[m.topic_id]
            m.alive = False
            # Release the follower in case it was suspended on a backfill
            # that will now never finish (mirror is dying).
            m.backfill_done.set()
        self._persist()

    def by_topic(self, topic_id: int) -> TerminalMirror | None:
        with self._lock:
            csid = self._topic_map.get(topic_id)
            return self._mirrors.get(csid) if csid else None

    def by_csid(self, csid: str) -> TerminalMirror | None:
        with self._lock:
            return self._mirrors.get(csid)

    def list(self) -> list[TerminalMirror]:
        with self._lock:
            return list(self._mirrors.values())

    def set_dtach_socket(self, csid: str, socket: str | None):
        with self._lock:
            m = self._mirrors.get(csid)
            if m:
                m.dtach_socket = socket or None
        self._persist()

    def set_welcome_msg_id(self, csid: str, msg_id: int):
        with self._lock:
            m = self._mirrors.get(csid)
            if m:
                m.welcome_msg_id = msg_id
        self._persist()

    def set_filter_level(self, csid: str, level: str):
        if level not in ("all", "lite"):
            return
        with self._lock:
            m = self._mirrors.get(csid)
            if m:
                m.filter_level = level
        self._persist()

    def advance_mode(self, csid: str, cycle_len: int = 4) -> int | None:
        """Increment mode_index by 1 mod cycle_len and return the new
        value. Used by the Shift+Tab callback to keep label in sync
        with our best-guess view of Claude's current permission mode.

        cycle_len defaults to 4 (default → acceptEdits → plan → auto)
        — matches the cycle when the optional `auto` mode is enabled,
        which is the documented default for current Claude Code.
        """
        with self._lock:
            m = self._mirrors.get(csid)
            if not m:
                return None
            m.mode_index = (m.mode_index + 1) % cycle_len
            new_index = m.mode_index
        self._persist()
        return new_index

    # ── follower lifecycle ──────────────────────────────────────────

    def start_follower(self, mirror: TerminalMirror):
        if mirror.follower and mirror.follower.is_alive():
            return
        t = threading.Thread(
            target=self._follow_loop, args=(mirror,), daemon=True,
            name=f"mirror-follow-{mirror.csid[:8]}",
        )
        mirror.follower = t
        t.start()

    def start_all_followers(self):
        for m in self.list():
            if m.alive:
                self.start_follower(m)

    def _follow_loop(self, mirror: TerminalMirror):
        """Tail the JSONL transcript and project each new event.

        Polls 2 Hz. Survives the file not existing yet (waits up to
        60 s for the terminal to write its first event), gracefully
        ignores partial lines (no trailing newline → roll back and
        retry), and snaps offset back to 0 if the file shrinks
        (rotation / replacement).
        """
        def log(msg):
            print(f"[mirror {mirror.csid[:8]}] {msg}",
                  file=sys.stderr, flush=True)

        # 1. Wait for the JSONL file to appear.
        deadline = time.time() + 60
        while mirror.alive and not os.path.exists(mirror.jsonl_path):
            if time.time() > deadline:
                log(f"jsonl never appeared at {mirror.jsonl_path}")
                return
            time.sleep(1)
        if not mirror.alive:
            return

        try:
            f = open(mirror.jsonl_path, "rb")
        except OSError as e:
            log(f"open failed: {e}")
            return

        try:
            # If saved offset is past EOF (file truncated / replaced),
            # restart from the beginning.
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            start = mirror.last_offset
            if start > file_size:
                log(f"file shrank ({file_size} < {start}); restarting at 0")
                start = 0
                mirror.last_offset = 0
            f.seek(start)

            idle_ticks = 0
            events_since_save = 0
            while mirror.alive:
                pos_before = f.tell()
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    idle_ticks += 1
                    # Persist offset roughly every 30 s of idle so a
                    # restart resumes near the current tail rather
                    # than at the last delivered event.
                    if idle_ticks >= 60 and events_since_save > 0:
                        self._persist()
                        events_since_save = 0
                        idle_ticks = 0
                    continue
                if not line.endswith(b"\n"):
                    # Partial line — back off, wait for the writer to
                    # finish, retry.
                    f.seek(pos_before)
                    time.sleep(0.3)
                    continue

                idle_ticks = 0
                mirror.last_offset = f.tell()
                try:
                    event = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                # Wait if a backfill batch is currently posting — keeps
                # chronological order in the topic (backfill goes first).
                mirror.backfill_done.wait()
                if not mirror.alive:
                    break
                # Filter the synthetic "resume after interrupted turn"
                # pair (isMeta user prompt + the model's stub reply
                # that follows). Mirrors read_logical_events.
                if event.get("isMeta"):
                    msg = event.get("message") or {}
                    content = msg.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = "".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    if "Continue from where you left off" in text:
                        mirror._drop_next_assistant = True
                    continue
                if (event.get("type") == "assistant"
                        and mirror._drop_next_assistant):
                    mirror._drop_next_assistant = False
                    continue
                try:
                    self._on_event(mirror, event)
                except Exception as e:
                    log(f"on_event error: {e}")
                events_since_save += 1
                # Batch offset saves: every 5 events.
                if events_since_save >= 5:
                    self._persist()
                    events_since_save = 0
        finally:
            try:
                f.close()
            except Exception:
                pass
            # Final offset save on exit.
            self._persist()

    # ── persistence ─────────────────────────────────────────────────

    def _persist(self):
        with self._persist_lock:
            records = []
            with self._lock:
                for m in self._mirrors.values():
                    records.append({
                        "csid": m.csid,
                        "cwd": m.cwd,
                        "topic_id": m.topic_id,
                        "dtach_socket": m.dtach_socket,
                        "jsonl_path": m.jsonl_path,
                        "last_offset": m.last_offset,
                        "filter_level": m.filter_level,
                        "welcome_msg_id": m.welcome_msg_id,
                        "mode_index": m.mode_index,
                    })
            try:
                with open(_PERSIST_PATH, "w") as f:
                    json.dump(records, f, indent=2)
            except Exception as e:
                print(f"[mirror persist] save error: {e}",
                      file=sys.stderr, flush=True)

    def _restore(self):
        try:
            with open(_PERSIST_PATH) as f:
                records = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        kept = 0
        dropped = 0
        for r in records:
            csid = r.get("csid")
            cwd = r.get("cwd", "")
            topic_id = r.get("topic_id")
            if not csid or not topic_id:
                dropped += 1
                continue
            # Legacy tmux-shaped records (tmux_socket / tmux_pane) are
            # not migratable — the tmux session no longer exists in
            # the dtach-based world. Drop them.
            if r.get("tmux_socket") or r.get("tmux_pane"):
                dropped += 1
                continue
            dtach_socket = r.get("dtach_socket") or None
            # A mirror persisted WITH a dtach socket whose socket file
            # no longer exists is dead — the terminal exited and dtach
            # cleaned up. Drop so we don't chase a corpse on the
            # healthcheck. Mirrors persisted as output-only
            # (dtach_socket=None from the start) survive.
            if dtach_socket and not dtach_socket_alive(dtach_socket):
                dropped += 1
                continue
            level = r.get("filter_level") or "lite"
            if level not in ("all", "lite"):
                level = "lite"
            try:
                mode_index = int(r.get("mode_index") or 0) % 4
            except (TypeError, ValueError):
                mode_index = 0
            m = TerminalMirror(
                csid=csid, cwd=cwd, topic_id=topic_id,
                dtach_socket=dtach_socket,
                jsonl_path=r.get("jsonl_path") or jsonl_path_for(csid, cwd),
                last_offset=int(r.get("last_offset") or 0),
                filter_level=level,
                welcome_msg_id=r.get("welcome_msg_id"),
                mode_index=mode_index,
            )
            self._mirrors[csid] = m
            self._topic_map[topic_id] = csid
            kept += 1
        if kept or dropped:
            print(f"[mirror persist] restored {kept} mirror(s), "
                  f"dropped {dropped} dead", flush=True)
        if dropped:
            # Rewrite the persist file so the drop sticks.
            self._persist()
