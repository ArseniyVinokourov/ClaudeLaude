"""Terminal session mirror — bridge a terminal Claude session to a TG topic.

Two channels:
- Output: tail the session's JSONL transcript at
  ~/.claude/projects/<encoded-cwd>/<csid>.jsonl, project new events
  (user/assistant/tool_use) into the bot's mirror topic.
- Input: a dtach Unix-socket client writes bytes from a TG message
  into the terminal session's stdin.

The bot does not own the terminal process — it observes via the
JSONL and (optionally) types into its PTY via dtach. Lifecycle stays
external; healthcheck handles disappearance.
"""
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from collections.abc import Callable

_PERSIST_PATH = os.environ.get(
    "BOT_MIRRORS_FILE",
    os.path.join(os.path.dirname(__file__), ".mirrors.json"),
)


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
    dtach_sock: str | None
    jsonl_path: str
    last_offset: int = 0
    alive: bool = True
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
                 dtach_sock: str | None = None) -> TerminalMirror:
        """Idempotent — second call for the same csid returns the existing record."""
        with self._lock:
            existing = self._mirrors.get(csid)
            if existing:
                return existing
            m = TerminalMirror(
                csid=csid, cwd=cwd, topic_id=topic_id,
                dtach_sock=dtach_sock or None,
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

    def set_dtach_sock(self, csid: str, sock: str | None):
        with self._lock:
            m = self._mirrors.get(csid)
            if m:
                m.dtach_sock = sock or None
        self._persist()

    # ── follower lifecycle (loop body added in step 2) ──────────────

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
        # Implemented in step 2 (JSONL follower).
        pass

    def _save_offset(self, mirror: TerminalMirror):
        """Persist last_offset without rewriting unrelated state."""
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
                        "dtach_sock": m.dtach_sock,
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
                dtach_sock=r.get("dtach_sock") or None,
                jsonl_path=r.get("jsonl_path") or jsonl_path_for(csid, cwd),
                last_offset=int(r.get("last_offset") or 0),
            )
            self._mirrors[csid] = m
            self._topic_map[topic_id] = csid
        if records:
            print(f"[mirror persist] restored {len(records)} mirror(s)",
                  flush=True)
