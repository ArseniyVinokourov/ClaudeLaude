"""Manage Claude Code CLI sessions as subprocesses.

Bot-spawned sessions run `claude -p ... --output-format stream-json` for each
user message, using `--resume` to maintain conversation context.

Terminal sessions (detected via hooks) are passive — the bot only tracks their
existence for routing notifications.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from collections.abc import Callable

_CLAUDE_BIN = (shutil.which("claude")
               or os.path.expanduser("~/.local/bin/claude"))


@dataclass
class HistoryEntry:
    ts: float
    kind: str  # user | assistant | tool | system | result
    text: str


@dataclass
class Session:
    sid: str
    topic_id: int | None
    cwd: str
    name: str
    alive: bool = True
    started: float = field(default_factory=time.time)
    is_bot_spawned: bool = True
    history: list[HistoryEntry] = field(default_factory=list)
    claude_session_id: str | None = None
    stopped_at: float | None = None
    _worker_generation: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read: int = 0
    total_cache_create: int = 0
    total_cost_usd: float = 0.0
    topic_label: str = ""
    turn_input_tokens: int = 0
    turn_output_tokens: int = 0
    pending_images: list[str] = field(default_factory=list)
    _queue: queue.Queue = field(default_factory=queue.Queue)
    _proc: subprocess.Popen | None = field(default=None, repr=False)


_PERSIST_PATH = os.environ.get(
    "BOT_SESSIONS_FILE",
    os.path.join(os.path.dirname(__file__), ".sessions.json"),
)
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


class SessionManager:
    def __init__(self,
                 on_assistant_message: Callable,
                 on_result: Callable,
                 on_tool_use: Callable,
                 on_thinking: Callable,
                 on_session_stop: Callable | None = None):
        self._on_assistant = on_assistant_message
        self._on_result = on_result
        self._on_tool_use = on_tool_use
        self._on_thinking = on_thinking
        self._on_session_stop = on_session_stop
        self._sessions: dict[str, Session] = {}
        self._topic_map: dict[int, str] = {}
        self._cwd_map: dict[str, str] = {}
        self._claude_id_map: dict[str, str] = {}
        self._persist_lock = threading.Lock()
        self._restore()

    def create(self, cwd: str, name: str, topic_id: int) -> Session:
        sid = uuid.uuid4().hex[:8]
        session = Session(sid=sid, topic_id=topic_id, cwd=cwd, name=name)
        self._sessions[sid] = session
        self._topic_map[topic_id] = sid
        self._cwd_map[cwd] = sid
        threading.Thread(
            target=self._worker, args=(session, 0), daemon=True,
        ).start()
        self._persist()
        return session

    def stop(self, sid: str, reason: str = "stopped"):
        session = self._sessions.get(sid)
        if not session:
            return
        was_alive = session.alive
        session.alive = False
        session.stopped_at = time.time()
        session._queue.put(None)
        if session._proc and session._proc.poll() is None:
            session._proc.terminate()
        self._persist()
        if was_alive and self._on_session_stop:
            try:
                self._on_session_stop(session, reason)
            except Exception as e:
                print(f"[session {sid}] on_stop error: {e}",
                      file=sys.stderr, flush=True)

    def interrupt(self, sid: str) -> bool:
        """Terminate the in-flight turn's claude subprocess.

        Session and worker stay alive — the worker proceeds to the next
        queued message. Returns True if a turn was actually running.
        """
        session = self._sessions.get(sid)
        if not session or not session.alive:
            return False
        proc = session._proc
        if not proc or proc.poll() is not None:
            return False
        try:
            proc.terminate()
        except Exception:
            return False
        return True

    def restart(self, sid: str) -> bool:
        session = self._sessions.get(sid)
        if not session or session.alive:
            return False
        session.alive = True
        session.stopped_at = None
        session._worker_generation += 1
        session._queue = queue.Queue()
        if session.is_bot_spawned:
            threading.Thread(
                target=self._worker,
                args=(session, session._worker_generation),
                daemon=True,
            ).start()
        self._persist()
        return True

    def fork(self, parent: Session, topic_id: int, name: str) -> Session | None:
        if not parent.claude_session_id:
            return None
        sid = uuid.uuid4().hex[:8]
        session = Session(
            sid=sid, topic_id=topic_id, cwd=parent.cwd, name=name,
            claude_session_id=parent.claude_session_id,
        )
        session._fork = True
        self._sessions[sid] = session
        self._topic_map[topic_id] = sid
        threading.Thread(
            target=self._worker, args=(session, 0), daemon=True,
        ).start()
        self._persist()
        return session

    def resume(self, claude_session_id: str, topic_id: int,
               name: str, cwd: str = "") -> Session:
        sid = uuid.uuid4().hex[:8]
        session = Session(
            sid=sid, topic_id=topic_id, cwd=cwd, name=name,
            claude_session_id=claude_session_id,
        )
        self._sessions[sid] = session
        self._topic_map[topic_id] = sid
        self._claude_id_map[claude_session_id] = sid
        if cwd:
            self._cwd_map[cwd] = sid
        threading.Thread(
            target=self._worker, args=(session, 0), daemon=True,
        ).start()
        self._persist()
        return session

    def list_sessions(self) -> list[Session]:
        return [s for s in self._sessions.values() if s.alive]

    def by_topic(self, topic_id: int) -> Session | None:
        sid = self._topic_map.get(topic_id)
        return self._sessions.get(sid) if sid else None

    def by_cwd(self, cwd: str) -> Session | None:
        sid = self._cwd_map.get(cwd)
        return self._sessions.get(sid) if sid else None

    def by_claude_session_id(self, claude_session_id: str) -> Session | None:
        sid = self._claude_id_map.get(claude_session_id)
        return self._sessions.get(sid) if sid else None

    def link_claude_id(self, claude_session_id: str, session: Session):
        session.claude_session_id = claude_session_id
        self._claude_id_map[claude_session_id] = session.sid
        self._persist()

    def detach_terminal(self, sid: str):
        s = self._sessions.pop(sid, None)
        if not s:
            return
        if s.topic_id and self._topic_map.get(s.topic_id) == sid:
            del self._topic_map[s.topic_id]
        if s.cwd and self._cwd_map.get(s.cwd) == sid:
            del self._cwd_map[s.cwd]
        if (s.claude_session_id
                and self._claude_id_map.get(s.claude_session_id) == sid):
            del self._claude_id_map[s.claude_session_id]
        self._persist()

    def register_terminal(self, claude_session_id: str, topic_id: int,
                          cwd: str = "") -> Session:
        existing_sid = self._claude_id_map.get(claude_session_id)
        if existing_sid and existing_sid in self._sessions:
            session = self._sessions[existing_sid]
            session.topic_id = topic_id
            return session
        topic_sid = self._topic_map.get(topic_id)
        if topic_sid and topic_sid in self._sessions:
            session = self._sessions[topic_sid]
            self.link_claude_id(claude_session_id, session)
            return session
        sid = uuid.uuid4().hex[:8]
        session = Session(
            sid=sid, topic_id=topic_id, cwd=cwd,
            name=os.path.basename(cwd) if cwd else "terminal",
            is_bot_spawned=False,
        )
        session.claude_session_id = claude_session_id
        self._sessions[sid] = session
        self._topic_map[topic_id] = sid
        self._claude_id_map[claude_session_id] = sid
        if cwd:
            self._cwd_map[cwd] = sid
        self._persist()
        return session

    def send_user_message(self, sid: str, text: str) -> bool:
        session = self._sessions.get(sid)
        if not session or not session.alive or not session.is_bot_spawned:
            return False
        session.history.append(HistoryEntry(time.time(), "user", text))
        session._queue.put(text)
        return True

    # ── persistence ─────────────────────────────────────────────────

    def _persist(self):
        with self._persist_lock:
            self._gc()
            records = []
            for s in self._sessions.values():
                if not s.claude_session_id or not s.topic_id:
                    continue
                history_tail = [
                    {"ts": h.ts, "kind": h.kind, "text": h.text}
                    for h in s.history[-50:]
                ]
                records.append({
                    "sid": s.sid,
                    "topic_id": s.topic_id,
                    "cwd": s.cwd,
                    "name": s.name,
                    "topic_label": s.topic_label,
                    "claude_session_id": s.claude_session_id,
                    "is_bot_spawned": s.is_bot_spawned,
                    "alive": s.alive,
                    "stopped_at": s.stopped_at,
                    "total_input_tokens": s.total_input_tokens,
                    "total_output_tokens": s.total_output_tokens,
                    "total_cache_read": s.total_cache_read,
                    "total_cache_create": s.total_cache_create,
                    "total_cost_usd": s.total_cost_usd,
                    "history": history_tail,
                })
            try:
                with open(_PERSIST_PATH, "w") as f:
                    json.dump(records, f, indent=2)
            except Exception as e:
                print(f"[persist] save error: {e}", file=sys.stderr, flush=True)

    def _gc(self):
        expired = [
            sid for sid, s in self._sessions.items()
            if s.stopped_at and time.time() - s.stopped_at > 86400
        ]
        for sid in expired:
            s = self._sessions.pop(sid)
            if s.topic_id and self._topic_map.get(s.topic_id) == sid:
                del self._topic_map[s.topic_id]
            if s.cwd and self._cwd_map.get(s.cwd) == sid:
                del self._cwd_map[s.cwd]
            if s.claude_session_id and self._claude_id_map.get(s.claude_session_id) == sid:
                del self._claude_id_map[s.claude_session_id]

    def _restore(self):
        try:
            with open(_PERSIST_PATH) as f:
                records = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for r in records:
            sid = r["sid"]
            topic_id = r["topic_id"]
            alive = r.get("alive", True)
            history = [
                HistoryEntry(ts=h["ts"], kind=h["kind"], text=h["text"])
                for h in r.get("history", [])
            ]
            session = Session(
                sid=sid,
                topic_id=topic_id,
                cwd=r.get("cwd", ""),
                name=r.get("name", "restored"),
                is_bot_spawned=r.get("is_bot_spawned", True),
                claude_session_id=r.get("claude_session_id"),
                alive=alive,
                stopped_at=r.get("stopped_at"),
                total_input_tokens=r.get("total_input_tokens", 0),
                total_output_tokens=r.get("total_output_tokens", 0),
                total_cache_read=r.get("total_cache_read", 0),
                total_cache_create=r.get("total_cache_create", 0),
                total_cost_usd=r.get("total_cost_usd", 0.0),
                topic_label=r.get("topic_label", ""),
                history=history,
            )
            self._sessions[sid] = session
            self._topic_map[topic_id] = sid
            if session.claude_session_id:
                self._claude_id_map[session.claude_session_id] = sid
            if session.cwd:
                self._cwd_map[session.cwd] = sid
            if session.is_bot_spawned and alive:
                threading.Thread(
                    target=self._worker,
                    args=(session, session._worker_generation),
                    daemon=True,
                ).start()
        if records:
            print(f"[persist] restored {len(records)} session(s)", flush=True)

    # ── internal ────────────────────────────────────────────────────

    def _worker(self, session: Session, gen: int):
        while session.alive and session._worker_generation == gen:
            try:
                text = session._queue.get(timeout=2)
            except queue.Empty:
                continue
            if text is None:
                break
            if session._worker_generation != gen:
                session._queue.put(text)
                break
            try:
                self._run_claude(session, text, gen)
            except Exception as e:
                if session._worker_generation != gen:
                    break
                print(f"[session {session.sid}] error: {e}",
                      file=sys.stderr, flush=True)
                session.history.append(
                    HistoryEntry(time.time(), "system", f"error: {e}"))

    def _run_claude(self, session: Session, text: str, gen: int):
        cmd = [_CLAUDE_BIN, "-p", text, "--output-format", "stream-json",
               "--verbose", "--permission-mode", "auto"]
        if session.claude_session_id:
            cmd.extend(["--resume", session.claude_session_id])
        if getattr(session, '_fork', False):
            cmd.append("--fork-session")
            session._fork = False

        session.turn_input_tokens = 0
        session.turn_output_tokens = 0
        session.pending_images.clear()

        if session._worker_generation != gen:
            return
        self._on_thinking(session)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=session.cwd,
                env={**os.environ},
            )
        except Exception:
            self._on_result(session, "", "")
            raise

        session._proc = proc
        assistant_chunks: list[str] = []

        try:
            for raw_line in proc.stdout:
                if session._worker_generation != gen:
                    proc.terminate()
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    self._dispatch(session, event, assistant_chunks, gen)
                except Exception as e:
                    print(f"[session {session.sid}] dispatch error: {e}",
                          file=sys.stderr, flush=True)
        finally:
            proc.wait()
            session._proc = None

            if session._worker_generation == gen:
                full_text = "".join(
                    c for c in assistant_chunks if isinstance(c, str)
                )
                if full_text.strip():
                    session.history.append(
                        HistoryEntry(time.time(), "assistant", full_text))
                self._on_result(session, full_text, "")
            else:
                self._on_result(session, "", "")

    def _dispatch(self, session: Session, event: dict,
                  assistant_chunks: list[str], gen: int):
        if session._worker_generation != gen:
            return
        etype = event.get("type", "")

        if etype in ("system", "init"):
            sid = event.get("session_id") or event.get("sessionId")
            if sid and sid != session.claude_session_id:
                old = session.claude_session_id
                if old and self._claude_id_map.get(old) == session.sid:
                    del self._claude_id_map[old]
                session.claude_session_id = sid
                self._claude_id_map[sid] = session.sid
                self._persist()

        elif etype == "assistant":
            msg = event.get("message")
            if isinstance(msg, dict):
                content = msg.get("content", [])
                text = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ) if isinstance(content, list) else ""
            elif isinstance(msg, str):
                text = msg
            else:
                text = event.get("text") or ""
            if text:
                assistant_chunks.append(text)
                self._on_assistant(session, text)

        elif etype == "tool_use":
            tool = event.get("name") or event.get("tool") or "?"
            inp = event.get("input") or event.get("tool_input") or {}
            session.history.append(
                HistoryEntry(time.time(), "tool",
                             f"{tool}: {json.dumps(inp, ensure_ascii=False)[:200]}")
            )
            if tool == "Write":
                path = inp.get("file_path", "")
                if any(path.lower().endswith(ext) for ext in _IMAGE_EXTS):
                    session.pending_images.append(path)
            self._on_tool_use(session, tool, inp)

        elif etype == "result":
            sid = event.get("session_id") or event.get("sessionId")
            if sid and sid != session.claude_session_id:
                old = session.claude_session_id
                if old and self._claude_id_map.get(old) == session.sid:
                    del self._claude_id_map[old]
                session.claude_session_id = sid
                self._claude_id_map[sid] = session.sid

            usage = event.get("usage") or {}
            if usage:
                inp_t = usage.get("input_tokens") or usage.get("inputTokens") or 0
                out_t = usage.get("output_tokens") or usage.get("outputTokens") or 0
                cache_r = (usage.get("cache_read_input_tokens")
                           or usage.get("cacheReadInputTokens") or 0)
                cache_c = (usage.get("cache_creation_input_tokens")
                           or usage.get("cacheCreationInputTokens") or 0)
                session.turn_input_tokens = inp_t
                session.turn_output_tokens = out_t
                session.total_input_tokens += inp_t
                session.total_output_tokens += out_t
                session.total_cache_read += cache_r
                session.total_cache_create += cache_c

            cost = event.get("cost_usd") or event.get("costUsd") or 0
            if cost:
                session.total_cost_usd += cost

            if usage or cost:
                self._persist()
