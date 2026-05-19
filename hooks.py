"""HTTP server that receives hook events from Claude Code.

Claude Code hooks POST JSON to localhost:HOOK_PORT.  For notifications the
server responds immediately.  For permission requests it blocks until the
Telegram user clicks Allow / Deny, or until the bot itself cancels the
request (session stopped, topic deleted) by calling resolve_permission().
The bot never makes the decision on the user's behalf.
"""
import json
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from collections.abc import Callable

from config import HOOK_PORT


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[hooks {ts}] {msg}", file=sys.stderr, flush=True)


def _looks_like_hook_payload(body) -> bool:
    """Cheap shape check for Claude Code hook bodies.

    Real PreToolUse / Notification / PermissionRequest payloads always carry
    a non-empty session_id and a non-empty hook_event_name. Bodies without
    both are treated as garbage (curl probes, port scans, broken clients)
    and dropped without invoking the bot callbacks.
    """
    if not isinstance(body, dict):
        return False
    sid = body.get("session_id") or body.get("sessionId")
    event = body.get("hook_event_name") or body.get("hookEventName")
    return bool(sid and isinstance(sid, str)
                and event and isinstance(event, str))


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class HookBridge:
    def __init__(self,
                 on_notification: Callable,
                 on_permission: Callable,
                 on_open_in_bot: Callable | None = None):
        self.on_notification = on_notification
        self.on_permission = on_permission
        self.on_open_in_bot = on_open_in_bot
        self._pending: dict[str, threading.Event] = {}
        self._decisions: dict[str, str] = {}
        self._abandoned: set[str] = set()
        self._perm_context: dict[str, dict] = {}
        self._server: HTTPServer | None = None

    def start(self):
        handler = self._make_handler()
        self._server = _ThreadingHTTPServer(("127.0.0.1", HOOK_PORT), handler)
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        _log(f"listening on 127.0.0.1:{HOOK_PORT}")

    def set_perm_context(self, req_id: str, **kwargs):
        self._perm_context[req_id] = kwargs

    def resolve_permission(self, req_id: str, decision: str):
        _log(f"resolve req_id={req_id} decision={decision}")
        self._decisions[req_id] = decision
        event = self._pending.get(req_id)
        if event:
            event.set()
            _log(f"event set for {req_id}")
        else:
            _log(f"WARNING: no pending event for {req_id}")

    def abandon_permission(self, req_id: str):
        """Wake the handler thread without producing a decision.

        Used when the bot can no longer offer the user a way to answer
        (session stopped, topic deleted, bot shutting down).  The handler
        closes the connection without writing a response — claude treats
        the dropped connection however its own policy says, the bot does
        not pick allow/deny on the user's behalf.
        """
        _log(f"abandon req_id={req_id}")
        self._abandoned.add(req_id)
        event = self._pending.get(req_id)
        if event:
            event.set()

    def _make_handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}

                path = self.path.rstrip("/")

                if path == "/hook/notification" or path == "/hook/permission":
                    # DoS guard: every legitimate Claude Code hook payload
                    # carries a non-empty session_id AND hook_event_name.
                    # Reject malformed/empty bodies without invoking the bot
                    # callback (which would otherwise create a forum topic).
                    if not _looks_like_hook_payload(body):
                        _log(f"reject {path}: payload missing required fields "
                             f"(keys={list(body.keys()) if isinstance(body, dict) else type(body).__name__})")
                        self.send_response(400)
                        self.end_headers()
                        return

                if path == "/hook/notification":
                    self._handle_notification(body)
                elif path == "/hook/permission":
                    self._handle_permission(body)
                elif path == "/hook/open_in_bot":
                    self._handle_open_in_bot(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def _handle_notification(self, body):
                try:
                    text = None
                    for key in ("message", "title", "text", "body"):
                        val = body.get(key)
                        if val and isinstance(val, str):
                            text = val
                            break
                    sid = body.get("session_id") or body.get("sessionId") or ""
                    bridge.on_notification(
                        text or "notification",
                        sid,
                        body,
                    )
                except Exception as e:
                    _log(f"notification error: {e}")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def _handle_open_in_bot(self, body):
                """Register a terminal Claude session as a mirror.

                Body: {session_id, cwd, tmux_socket?, tmux_pane?}. Returns
                200 with {topic_url} on success, 503 if the bot isn't ready.
                When tmux_pane is empty, the mirror is registered as
                output-only (no input bridge).
                """
                csid = body.get("session_id") or body.get("sessionId") or ""
                cwd = body.get("cwd") or ""
                tmux_socket = (body.get("tmux_socket")
                               or body.get("tmuxSocket") or "")
                tmux_pane = (body.get("tmux_pane")
                             or body.get("tmuxPane") or "")
                if not csid or not cwd:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error":"missing session_id or cwd"}')
                    return
                if not bridge.on_open_in_bot:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"error":"open_in_bot not wired"}')
                    return
                try:
                    result = bridge.on_open_in_bot(
                        csid, cwd,
                        tmux_socket or None, tmux_pane or None,
                    )
                except Exception as e:
                    _log(f"open_in_bot error: {e}")
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(
                        json.dumps({"error": str(e)}).encode())
                    return
                payload = json.dumps(result or {"status": "ok"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _handle_permission(self, body):
                req_id = f"{time.time_ns()}"
                _log(f"permission request req_id={req_id} "
                     f"tool={body.get('tool_name', '?')}")
                _log(f"BODY keys={list(body.keys())} "
                     f"session_id={body.get('session_id', 'MISSING')}")
                event = threading.Event()
                bridge._pending[req_id] = event

                try:
                    bridge.on_permission(req_id, body)
                except Exception as e:
                    _log(f"permission callback error: {e}")

                _log(f"waiting for decision req_id={req_id} ...")
                event.wait()

                # Cleanup
                abandoned = req_id in bridge._abandoned
                bridge._abandoned.discard(req_id)
                decision = bridge._decisions.pop(req_id, None)
                bridge._pending.pop(req_id, None)
                bridge._perm_context.pop(req_id, None)

                if abandoned or decision is None:
                    _log(f"abandoned req_id={req_id} — closing without "
                         f"response, claude decides on its own")
                    self.close_connection = True
                    return

                _log(f"decision received: {decision}")
                payload = json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "decision": {
                            "behavior": decision,
                        },
                    }
                }).encode()

                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()
                    _log(f"response sent: {decision}")
                except Exception as e:
                    _log(f"ERROR sending response: {e}")

        return Handler
