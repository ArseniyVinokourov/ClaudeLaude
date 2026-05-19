"""Terminal session mirror — bridge a terminal Claude session to a TG topic.

Two channels:
- Output: tail the session's JSONL transcript at
  ~/.claude/projects/<encoded-cwd>/<csid>.jsonl, project new events
  (user/assistant/tool_use) into the bot's mirror topic.
- Input: tmux `send-keys` writes bytes from a TG message directly into
  the running terminal Claude's pane, exactly as if the owner had typed
  them at the keyboard.

The bot does not own the terminal process — it observes via the JSONL
and (optionally) types into the pane via tmux. Lifecycle stays external;
healthcheck handles disappearance.
"""
import json
import os
import shutil
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


_TMUX_BIN = shutil.which("tmux")


def push_to_tmux(socket: str | None, pane: str,
                 text: str, timeout: float = 3.0) -> bool:
    """Type `text` into a tmux pane as if at the keyboard, then submit.

    Uses `tmux send-keys -l` (literal) so the text bytes are not
    interpreted as tmux key bindings, then a follow-up `send-keys Enter`
    submits the line. Returns False on any failure (missing tmux, no
    such pane, timeout, non-zero exit).
    """
    if not pane or not _TMUX_BIN:
        return False
    base = [_TMUX_BIN]
    if socket:
        base += ["-S", socket]
    try:
        r1 = subprocess.run(
            base + ["send-keys", "-t", pane, "-l", text],
            timeout=timeout, capture_output=True,
        )
        if r1.returncode != 0:
            return False
        r2 = subprocess.run(
            base + ["send-keys", "-t", pane, "Enter"],
            timeout=timeout, capture_output=True,
        )
        return r2.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def tmux_pane_alive(socket: str | None, pane: str,
                    timeout: float = 2.0) -> bool:
    """Probe whether a given tmux pane still exists on the given server.

    `tmux list-panes -a -F '#{pane_id}'` enumerates panes across all
    sessions on the server; we grep for the target pane id. Missing
    tmux or unreachable socket returns False.
    """
    if not pane or not _TMUX_BIN:
        return False
    base = [_TMUX_BIN]
    if socket:
        base += ["-S", socket]
    try:
        r = subprocess.run(
            base + ["list-panes", "-a", "-F", "#{pane_id}"],
            timeout=timeout, capture_output=True,
        )
        if r.returncode != 0:
            return False
        ids = r.stdout.decode("utf-8", errors="replace").split()
        return pane in ids
    except (subprocess.TimeoutExpired, OSError):
        return False


def jsonl_path_for(csid: str, cwd: str) -> str:
    """Resolve Claude Code's transcript file for a given session+cwd.

    Claude Code stores transcripts at
    ~/.claude/projects/<encoded-cwd>/<csid>.jsonl where encoded-cwd is
    the absolute cwd with '/' replaced by '-'. The leading '/'
    becomes a leading '-'.
    """
    encoded = cwd.replace("/", "-")
    return os.path.expanduser(f"~/.claude/projects/{encoded}/{csid}.jsonl")


@dataclass
class TerminalMirror:
    csid: str
    cwd: str
    topic_id: int
    tmux_socket: str | None
    tmux_pane: str | None
    jsonl_path: str
    last_offset: int = 0
    alive: bool = True
    # Last TG user-msg the bot relayed into the pane; cleared once
    # claude posts an assistant reply (so the bot can swap 👀 → 👍).
    pending_user_msg_id: int | None = field(default=None, repr=False)
    follower: threading.Thread | None = field(default=None, repr=False)


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
                 tmux_socket: str | None = None,
                 tmux_pane: str | None = None) -> TerminalMirror:
        """Idempotent — second call for the same csid returns the existing record."""
        with self._lock:
            existing = self._mirrors.get(csid)
            if existing:
                return existing
            m = TerminalMirror(
                csid=csid, cwd=cwd, topic_id=topic_id,
                tmux_socket=tmux_socket or None,
                tmux_pane=tmux_pane or None,
                jsonl_path=jsonl_path_for(csid, cwd),
            )
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

    def set_tmux_target(self, csid: str,
                        socket: str | None, pane: str | None):
        with self._lock:
            m = self._mirrors.get(csid)
            if m:
                m.tmux_socket = socket or None
                m.tmux_pane = pane or None
        self._persist()

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
                        "tmux_socket": m.tmux_socket,
                        "tmux_pane": m.tmux_pane,
                        "jsonl_path": m.jsonl_path,
                        "last_offset": m.last_offset,
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
        for r in records:
            csid = r.get("csid")
            cwd = r.get("cwd", "")
            topic_id = r.get("topic_id")
            if not csid or not topic_id:
                continue
            m = TerminalMirror(
                csid=csid, cwd=cwd, topic_id=topic_id,
                tmux_socket=r.get("tmux_socket") or None,
                tmux_pane=r.get("tmux_pane") or None,
                jsonl_path=r.get("jsonl_path") or jsonl_path_for(csid, cwd),
                last_offset=int(r.get("last_offset") or 0),
            )
            self._mirrors[csid] = m
            self._topic_map[topic_id] = csid
        if records:
            print(f"[mirror persist] restored {len(records)} mirror(s)",
                  flush=True)
