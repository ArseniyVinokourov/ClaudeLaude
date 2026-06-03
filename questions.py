"""Render Claude's AskUserQuestion picker as Telegram inline buttons.

When a bot session calls the built-in AskUserQuestion tool, `SessionManager`
invokes `QuestionAsker.ask` (from the worker thread). We post the question
into the session topic with one button per option, block until the owner taps
(or the session is stopped/interrupted), and return the chosen answers so the
session engine can hand them back to Claude over the control protocol.

Answer shape returned: ``{question_text: chosen_label}`` for single-select,
``{question_text: "Label A, Label B"}`` for multi-select (comma-joined — the
documented encoding; verify live against the CLI before fully trusting it).
"""
import threading
import uuid

import telegram as tg
from config import get_forum_chat_id

_BTN_MAX = 28  # mobile button label cap (see feedback_tg_button_limits)


def _clip(s: str, n: int = _BTN_MAX) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


class QuestionAsker:
    """Owns the pending-question registry and the blocking ask() flow.

    Mirrors the permission Allow/Deny pattern: a `threading.Event` per pending
    question, taps resolve it via `handle_callback`, stop/interrupt cancel it
    through the session's `_pending_q` handle (set by the engine).
    """

    def __init__(self, state):
        self.state = state  # BotState: .lock, .pending_questions, .question_by_sid

    # ── engine-facing (worker thread) ──────────────────────────────
    def ask(self, session, questions):
        fid = get_forum_chat_id()
        if not fid or not session.topic_id or not questions:
            return None
        qid = uuid.uuid4().hex[:8]
        entry = {
            "qid": qid,
            "event": threading.Event(),
            "questions": questions,
            "answers": {},
            "selected": set(),   # option indices chosen for the current multi-Q
            "cur": 0,
            "msg_id": None,
            "chat_id": fid,
            "thread_id": session.topic_id,
            "sid": session.sid,
            "cancelled": False,
            "reason": "",
        }
        with self.state.lock:
            self.state.pending_questions[qid] = entry
            self.state.question_by_sid[session.sid] = qid
        session._pending_q = entry  # so stop()/interrupt() can cancel us

        text, buttons = self._render(entry)
        # text is already HTML (<b>…</b>); send as-is (HTML parse_mode), not
        # through the markdown converter which would escape the tags.
        entry["msg_id"] = tg.send(text, fid, thread_id=session.topic_id,
                                  buttons=buttons)

        try:
            while not entry["event"].wait(0.5):
                if not session.alive or entry["cancelled"]:
                    break
            answered = entry["event"].is_set() and not entry["cancelled"]
            if answered:
                return entry["answers"] or None
            # cancelled or session died → mark the message and bail
            reason = entry["reason"] or ("stopped" if not session.alive else "cancelled")
            if entry["msg_id"]:
                try:
                    tg.edit(entry["msg_id"], f"✗ Question cancelled — {tg.esc(reason)}",
                            fid)
                except Exception:
                    pass
            return None
        finally:
            session._pending_q = None
            with self.state.lock:
                self.state.pending_questions.pop(qid, None)
                if self.state.question_by_sid.get(session.sid) == qid:
                    self.state.question_by_sid.pop(session.sid, None)

    # ── callback-facing (main thread) ──────────────────────────────
    def handle_callback(self, data):
        # data: "aq:<qid>:<qi>:<oi|done>"
        parts = data.split(":")
        if len(parts) != 4:
            return
        _, qid, qi_s, tok = parts
        with self.state.lock:
            entry = self.state.pending_questions.get(qid)
        if not entry or not qi_s.isdigit():
            return
        qi = int(qi_s)
        if qi != entry["cur"] or qi >= len(entry["questions"]):
            return  # stale tap on an already-advanced question
        q = entry["questions"][qi]
        opts = q.get("options") or []
        multi = bool(q.get("multiSelect"))

        if tok == "done":
            if not multi:
                return
            labels = [opts[i]["label"] for i in sorted(entry["selected"])
                      if 0 <= i < len(opts)]
            entry["answers"][q["question"]] = ", ".join(labels)
            self._advance(entry)
            return

        if not tok.isdigit():
            return
        oi = int(tok)
        if not (0 <= oi < len(opts)):
            return
        if multi:
            entry["selected"] ^= {oi}  # toggle
            text, buttons = self._render(entry)
            if entry["msg_id"]:
                tg.edit(entry["msg_id"], text, entry["chat_id"], buttons=buttons)
        else:
            entry["answers"][q["question"]] = opts[oi]["label"]
            self._advance(entry)

    # ── internals ──────────────────────────────────────────────────
    def _advance(self, entry):
        entry["cur"] += 1
        entry["selected"] = set()
        if entry["cur"] < len(entry["questions"]):
            text, buttons = self._render(entry)
            if entry["msg_id"]:
                tg.edit(entry["msg_id"], text, entry["chat_id"], buttons=buttons)
        else:
            # all answered → freeze the message into a summary, unblock ask()
            lines = ["✓ <b>Answered</b>"]
            for qq, ans in entry["answers"].items():
                lines.append(f"• {tg.esc(qq)}: <b>{tg.esc(ans)}</b>")
            if entry["msg_id"]:
                try:
                    tg.edit(entry["msg_id"], "\n".join(lines), entry["chat_id"])
                except Exception:
                    pass
            entry["event"].set()

    def _render(self, entry):
        qi = entry["cur"]
        q = entry["questions"][qi]
        total = len(entry["questions"])
        multi = bool(q.get("multiSelect"))
        opts = q.get("options") or []

        head = f"❓ <b>{tg.esc(q.get('header') or 'Question')}</b>"
        if total > 1:
            head += f"  ({qi + 1}/{total})"
        body = [head, tg.esc(q.get("question") or "")]
        if multi:
            body.append("<i>Select any, then tap Done.</i>")

        qid = entry["qid"]
        rows = []
        for i, o in enumerate(opts):
            label = _clip(o.get("label") or f"Option {i + 1}")
            if multi and i in entry["selected"]:
                label = f"✓ {label}"
            rows.append([{"text": label,
                          "callback_data": f"aq:{qid}:{qi}:{i}"}])
        if multi:
            rows.append([{"text": "✅ Done",
                          "callback_data": f"aq:{qid}:{qi}:done"}])
        return "\n".join(p for p in body if p), rows
