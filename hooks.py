"""HTTP server that receives hook events from Claude Code.

Claude Code hooks POST JSON to localhost:HOOK_PORT.  For notifications the
server responds immediately.  For permission requests it blocks until the
Telegram user clicks Allow / Deny (or the timeout expires — default: deny).
"""
import json
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Callable

from config import HOOK_PORT


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[hooks {ts}] {msg}", file=sys.stderr, flush=True)


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class HookBridge:
    def __init__(self,
                 on_notification: Callable,
                 on_permission: Callable,
                 on_perm_warning: Callable | None = None):
        self.on_notification = on_notification
        self.on_permission = on_permission
        self.on_perm_warning = on_perm_warning
        self._pending: dict[str, threading.Event] = {}
        self._decisions: dict[str, str] = {}
        self._perm_context: dict[str, dict] = {}
        self._timers: dict[str, threading.Timer] = {}
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
        t = self._timers.pop(req_id, None)
        if t:
            t.cancel()
        event = self._pending.get(req_id)
        if event:
            event.set()
            _log(f"event set for {req_id}")
        else:
            _log(f"WARNING: no pending event for {req_id}")

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

                if path == "/hook/notification":
                    self._handle_notification(body)
                elif path == "/hook/permission":
                    self._handle_permission(body)
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

                # Warning timer: fires 30s before the 120s timeout
                def _warn():
                    if req_id in bridge._pending:
                        ctx = bridge._perm_context.get(req_id, {})
                        if bridge.on_perm_warning:
                            try:
                                bridge.on_perm_warning(req_id, ctx)
                            except Exception as e:
                                _log(f"perm warning error: {e}")

                timer = threading.Timer(260, _warn)
                bridge._timers[req_id] = timer
                timer.start()

                _log(f"waiting for decision req_id={req_id} ...")
                got_signal = event.wait(timeout=290)

                # Cleanup
                t = bridge._timers.pop(req_id, None)
                if t:
                    t.cancel()
                decision = bridge._decisions.pop(req_id, "deny")
                bridge._pending.pop(req_id, None)
                bridge._perm_context.pop(req_id, None)

                if got_signal:
                    _log(f"decision received: {decision}")
                else:
                    _log(f"TIMEOUT, auto-deny for req_id={req_id}")

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
