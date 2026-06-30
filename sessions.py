"""Manage Claude Code CLI sessions as subprocesses.

Bot-spawned sessions run `claude -p ... --output-format stream-json` for each
user message, using `--resume` to maintain conversation context.

Terminal sessions (detected via hooks) are passive — the bot only tracks their
existence for routing notifications.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from collections.abc import Callable

import stickers
from config import CLAUDE_BIN as _CLAUDE_BIN
from formatting import IMAGE_EXTS as _IMAGE_EXTS


MODE_PRESETS: dict[str, dict] = {
    "default": {
        "style": "",
        "permission_mode": "auto",
        "label": "normal behavior",
    },
    "terse": {
        "style": (
            "Response style: terse. Answer ONLY the literal question — "
            "nothing more. Do NOT add sections the user did not ask for "
            "(no 'Implementation:', no 'Applications:', no 'How it "
            "works:' unless the question explicitly requests them). Do "
            "NOT use `## ` or `### ` markdown headers anywhere in your "
            "reply. Stay in one prose block. If the question is 'what "
            "is X', answer in 1-3 sentences without expanding. If the "
            "question is 'how do I X', give the single canonical "
            "command or procedure — do NOT enumerate flag variants, "
            "options, or alternative commands. No preambles, no recap "
            "of the question, no trailing summary. Lists only when "
            "items are genuinely distinct entities."
        ),
        "permission_mode": "auto",
        "label": "short answers",
    },
    "verbose": {
        "style": (
            "Response style: verbose. Explain context, alternatives, and "
            "tradeoffs. Walk through reasoning before conclusions. Cite "
            "specific files and lines when relevant."
        ),
        "permission_mode": "auto",
        "label": "full reasoning and context",
    },
    "beginner": {
        "style": (
            "Response style: beginner-friendly. Write as if explaining "
            "to a curious 8th-grader with no programming background.\n\n"
            "DEFINITION RULE: define every technical term in plain "
            "words on its FIRST appearance within this reply (em-dash, "
            "parenthesis, or 'means...'). After that first definition, "
            "you may use the bare term elsewhere in the SAME reply.\n\n"
            "On every NEW user message where the term appears again, "
            "define it again on first use in that new reply — even if "
            "you defined it in a previous reply. Each user message is "
            "a fresh teaching moment.\n\n"
            "The only exception: if the user has explicitly told you "
            "in this conversation that they already understand a "
            "specific term ('я понял что такое X', 'I get O(1) now', "
            "etc.), you may use that term without re-defining it from "
            "that point onward.\n\n"
            "When a plain-word alternative exists, prefer it over the "
            "technical term. Always include at least one concrete "
            "example (numbers, code, or everyday analogy) for "
            "'what is X' / 'how does X work' questions."
        ),
        "permission_mode": "auto",
        "label": "explains as it goes",
    },
    "plan": {
        "style": (
            "Permission mode: plan (read-only). You cannot edit files "
            "or run mutating commands.\n\n"
            "PLAN-FILE RULE: when the user asks for actionable work "
            "(refactor, fix, implement, create, etc.), produce a "
            "numbered plan AND write/update a plan file at "
            "`~/.claude/plans/<slug>.md`. For pure informational "
            "questions ('what is X', 'how does Y work'), answer "
            "directly — no plan file required.\n\n"
            "APPROVAL RULE: never start implementation work without "
            "the user's explicit go-ahead. After presenting a plan, "
            "end your reply with a clear approval request — 'Approve "
            "this plan?', 'Хочешь чтобы я это выполнил?', or similar. "
            "Do not assume that the user wants execution just because "
            "you presented a plan. Switching to a different mode by "
            "the user is approval; presenting a plan in plan mode is "
            "not.\n\n"
            "Investigate, propose, and wait for the user."
        ),
        "permission_mode": "plan",
        "label": "read-only research mode",
    },
    "burn": {
        "style": (
            "Burn mode: do not economize tokens or time. Push through to a "
            "complete, verified result — don't stop at 'probably' or 'should "
            "work'. Use the Agent tool aggressively, including parallel "
            "Agent calls, for research and independent verification of any "
            "non-trivial claim. Cite verifications (file:line, command "
            "output) rather than asserting from memory."
        ),
        "permission_mode": "auto",
        "model": "claude-opus-4-7[1m]",
        "effort": "max",
        "max_budget_usd": 5.0,
        "label": "Opus 1M + max effort + parallel agents",
    },
}


def valid_mode(name: str) -> bool:
    return name in MODE_PRESETS


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
    mode: str = "default"
    controls_msg_id: int | None = None
    turn_input_tokens: int = 0
    turn_output_tokens: int = 0
    pending_images: list[str] = field(default_factory=list)
    pending_files: list[str] = field(default_factory=list)
    _queue: queue.Queue = field(default_factory=queue.Queue)
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    # Holds the in-flight AskUserQuestion entry while a turn waits on the
    # owner; stop()/interrupt() cancel it. Set by QuestionAsker.ask.
    _pending_q: dict | None = field(default=None, repr=False)


_PERSIST_PATH = os.environ.get(
    "BOT_SESSIONS_FILE",
    os.path.join(os.path.dirname(__file__), ".sessions.json"),
)


class SessionManager:
    def __init__(self,
                 on_assistant_message: Callable,
                 on_result: Callable,
                 on_tool_use: Callable,
                 on_thinking: Callable,
                 on_session_stop: Callable | None = None,
                 on_session_context: Callable[["Session"], str] | None = None,
                 on_ask_question: Callable | None = None):
        self._on_assistant = on_assistant_message
        self._on_result = on_result
        self._on_tool_use = on_tool_use
        self._on_thinking = on_thinking
        self._on_session_stop = on_session_stop
        self._on_session_context = on_session_context
        # Called from the worker thread when Claude invokes AskUserQuestion:
        # (session, questions:list) -> {question_text: answer} | None.
        self._on_ask_question = on_ask_question
        self._sessions: dict[str, Session] = {}
        self._topic_map: dict[int, str] = {}
        self._cwd_map: dict[str, str] = {}
        self._claude_id_map: dict[str, str] = {}
        self._known_bot_sids: set[str] = set()
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
            name=f"session-worker-{sid[:8]}",
        ).start()
        self._persist()
        return session

    def set_mode(self, sid: str, mode: str) -> bool:
        if not valid_mode(mode):
            return False
        session = self._sessions.get(sid)
        if not session:
            return False
        session.mode = mode
        self._persist()
        return True

    def stop(self, sid: str, reason: str = "stopped"):
        session = self._sessions.get(sid)
        if not session:
            return
        was_alive = session.alive
        session.alive = False
        session.stopped_at = time.time()
        session._queue.put(None)
        self._cancel_pending_question(session, reason)
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
        self._cancel_pending_question(session, "interrupted")
        try:
            proc.terminate()
        except Exception:
            return False
        return True

    @staticmethod
    def _cancel_pending_question(session, reason: str):
        """Unblock a turn that is waiting on AskUserQuestion (stop/interrupt)."""
        pq = session._pending_q
        if pq and not pq["event"].is_set():
            pq["cancelled"] = True
            pq["reason"] = reason
            pq["event"].set()

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
                daemon=True, name=f"session-worker-{sid[:8]}",
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
            name=f"session-worker-{sid[:8]}",
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
            name=f"session-worker-{sid[:8]}",
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
                if not s.topic_id:
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
                    "mode": s.mode,
                    "controls_msg_id": s.controls_msg_id,
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
                mode=r.get("mode", "default") if valid_mode(r.get("mode", "default")) else "default",
                controls_msg_id=r.get("controls_msg_id"),
                history=history,
            )
            self._sessions[sid] = session
            self._topic_map[topic_id] = sid
            if session.claude_session_id:
                self._claude_id_map[session.claude_session_id] = sid
                if session.is_bot_spawned:
                    self._known_bot_sids.add(session.claude_session_id)
            if session.cwd:
                self._cwd_map[session.cwd] = sid
            if session.is_bot_spawned and alive:
                threading.Thread(
                    target=self._worker,
                    args=(session, session._worker_generation),
                    daemon=True, name=f"session-worker-{sid[:8]}",
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
        from config import is_killed
        if is_killed():
            return
        preset = MODE_PRESETS.get(session.mode, MODE_PRESETS["default"])
        if session.mode != "default":
            text_for_claude = f"[mode: {session.mode}] {text}"
        else:
            text_for_claude = text
        # Bidirectional stream-json: the prompt is sent on stdin (not -p) so
        # the CLI can route AskUserQuestion (and any permission prompt) back to
        # us over the control protocol. `--permission-prompt-tool stdio` is the
        # (undocumented) flag that enables that routing; without it
        # AskUserQuestion auto-errors. `--permission-mode auto` still
        # auto-allows ordinary tools, so only AskUserQuestion reaches us.
        cmd = [_CLAUDE_BIN, "--output-format", "stream-json", "--verbose",
               "--input-format", "stream-json",
               "--permission-prompt-tool", "stdio",
               "--permission-mode", preset["permission_mode"]]
        if preset.get("model"):
            cmd.extend(["--model", preset["model"]])
        if preset.get("effort"):
            cmd.extend(["--effort", preset["effort"]])
        if preset.get("max_budget_usd"):
            cmd.extend(["--max-budget-usd", str(preset["max_budget_usd"])])
        append_parts: list[str] = []
        if self._on_session_context is not None:
            try:
                ctx = self._on_session_context(session)
                if ctx:
                    append_parts.append(ctx)
            except Exception as e:
                print(f"[session {session.sid}] context callback error: {e}",
                      file=sys.stderr, flush=True)
        if preset["style"]:
            append_parts.append(preset["style"])
        # Bot sessions only: catalog of sendable stickers + how to use them.
        # No-op (empty suffix) when the feature is off or the catalog is empty.
        if session.is_bot_spawned:
            sticker_suffix = stickers.session_suffix()
            if sticker_suffix:
                append_parts.append(sticker_suffix)
        if append_parts:
            cmd.extend(["--append-system-prompt", "\n\n".join(append_parts)])
        if session.claude_session_id:
            cmd.extend(["--resume", session.claude_session_id])
        if getattr(session, '_fork', False):
            cmd.append("--fork-session")
            session._fork = False

        session.turn_input_tokens = 0
        session.turn_output_tokens = 0
        session.pending_images.clear()
        session.pending_files.clear()

        if session._worker_generation != gen:
            return
        self._on_thinking(session)

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=session.cwd,
                env={**os.environ},
                text=True,
                bufsize=1,
            )
        except Exception:
            self._on_result(session, "", "")
            raise

        session._proc = proc
        assistant_chunks: list[str] = []

        # Control-protocol handshake, then the user turn — both on stdin.
        self._send_line(proc, {"type": "control_request",
                               "request_id": "req_init",
                               "request": {"subtype": "initialize",
                                           "hooks": None}})
        self._send_line(proc, {"type": "user", "message": {
            "role": "user", "content": text_for_claude}})

        try:
            for raw_line in proc.stdout:
                if session._worker_generation != gen:
                    proc.terminate()
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "control_response":
                    continue  # init ack — nothing to do
                if etype == "control_request":
                    self._handle_control(session, proc, event)
                    continue
                try:
                    self._dispatch(session, event, assistant_chunks, gen)
                except Exception as e:
                    print(f"[session {session.sid}] dispatch error: {e}",
                          file=sys.stderr, flush=True)
                if etype == "result":
                    break  # turn done; close stdin so the CLI exits
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
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

    @staticmethod
    def _send_line(proc, obj):
        """Write one JSON line to claude's stdin (control msg or user turn)."""
        try:
            if proc.stdin:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
        except Exception:
            pass  # proc may have died (interrupt/stop) — read loop will end

    def _handle_control(self, session: Session, proc, event: dict):
        """Answer a `can_use_tool` control_request from the CLI.

        Ordinary tools are auto-allowed (preserving --permission-mode auto).
        AskUserQuestion is routed to the owner via `_on_ask_question`; the
        chosen answers ride back inside `updatedInput.answers`.
        """
        req = event.get("request") or {}
        if req.get("subtype") != "can_use_tool":
            return
        rid = event.get("request_id")
        tool = req.get("tool_name")
        inp = req.get("input") or {}

        if tool == "AskUserQuestion" and self._on_ask_question:
            answers = None
            try:
                answers = self._on_ask_question(session, inp.get("questions") or [])
            except Exception as e:
                print(f"[session {session.sid}] ask_question error: {e}",
                      file=sys.stderr, flush=True)
            if answers:
                updated = dict(inp)
                updated["answers"] = answers
                self._send_line(proc, {"type": "control_response", "response": {
                    "subtype": "success", "request_id": rid,
                    "response": {"behavior": "allow", "updatedInput": updated}}})
            else:
                self._send_line(proc, {"type": "control_response", "response": {
                    "subtype": "success", "request_id": rid,
                    "response": {"behavior": "deny", "message": "No answer",
                                 "interrupt": True}}})
            return

        self._send_line(proc, {"type": "control_response", "response": {
            "subtype": "success", "request_id": rid,
            "response": {"behavior": "allow", "updatedInput": inp}}})

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
                if session.is_bot_spawned:
                    self._known_bot_sids.add(sid)
                self._persist()

        elif etype == "assistant":
            # Claude Code 2.1.143+ packs tool_use as a content block inside
            # the assistant message instead of emitting it as a separate
            # top-level event. We extract both: text blocks → on_assistant,
            # tool_use blocks → on_tool_use. The old top-level shape is
            # still handled below for forward-compatibility.
            msg = event.get("message")
            text = ""
            if isinstance(msg, dict):
                content = msg.get("content", [])
                if isinstance(content, list):
                    text = "".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") != "tool_use":
                            continue
                        tool = b.get("name") or "?"
                        inp = b.get("input") or {}
                        session.history.append(
                            HistoryEntry(
                                time.time(), "tool",
                                f"{tool}: "
                                f"{json.dumps(inp, ensure_ascii=False)[:200]}")
                        )
                        if tool == "Write":
                            path = inp.get("file_path", "")
                            if any(path.lower().endswith(ext)
                                   for ext in _IMAGE_EXTS):
                                session.pending_images.append(path)
                        self._on_tool_use(session, tool, inp)
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
