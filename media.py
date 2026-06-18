"""Inbound media handling — albums, voice/video transcription, stickers,
reactions, and the runtime STT install offers.

Split out of bot.py. A component with bot state, the BotUI helper, the
SessionManager and the TurnController injected at construction; the Telegram
client, audit log, STT stack (stt / stt_install) and frame sampler are
imported directly. Every handler turns inbound media into one Claude turn via
``turnctl.enqueue_user_input`` (queue-based, safe to call off the poll thread).
The transcription/frame handlers are launched on daemon threads by bot.py's
dispatch so the single long-poll is never blocked.
"""
import os
import threading
import time

import audit
import frames
import stt
import stt_install
import telegram as tg
from config import OWNER_ID
from runtime import rt

# Album parts arrive as separate updates sharing a media_group_id; buffer them
# and flush once no new part has shown up for this long, as one combined turn.
_MEDIA_GROUP_FLUSH_S = 1.5


class MediaHandlers:
    def __init__(self, state, ui, mgr, turnctl):
        self.state = state
        self.ui = ui
        self.mgr = mgr
        self.turnctl = turnctl
        # Album buffer, guarded by a lock since the poll thread and the flush
        # Timer both touch it.
        self._media_groups: dict = {}
        self._media_group_lock = threading.Lock()

    # ── albums ──────────────────────────────────────────────────────
    def buffer_media_group(self, gid, session, file_id, filename, caption,
                           chat_id, msg_id, thread_id):
        """Append one album part and (re)arm its flush timer."""
        with self._media_group_lock:
            grp = self._media_groups.get(gid)
            if grp is None:
                grp = {"session": session, "chat_id": chat_id,
                       "thread_id": thread_id, "caption": "",
                       "items": [], "first_msg_id": msg_id, "timer": None}
                self._media_groups[gid] = grp
            grp["items"].append((file_id, filename))
            if caption and not grp["caption"]:
                grp["caption"] = caption
            if grp["timer"] is not None:
                grp["timer"].cancel()
            t = threading.Timer(_MEDIA_GROUP_FLUSH_S, self.flush_media_group,
                                args=(gid,))
            t.name = "bot-bg-album"
            t.daemon = True
            grp["timer"] = t
            t.start()

    def flush_media_group(self, gid):
        """Download every buffered part and enqueue one combined turn."""
        with self._media_group_lock:
            grp = self._media_groups.pop(gid, None)
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
            dest = os.path.join(rt.upload_dir,
                                f"{int(time.time())}_{i}_{filename}")
            if tg.download_file(file_id, dest):
                paths.append(dest)
        if not paths:
            tg.send("❌ Download failed", chat_id, thread_id=thread_id)
            return
        attach = "\n".join(f"[Attached file: {p}]" for p in paths)
        caption = grp["caption"]
        user_text = f"{caption}\n{attach}" if caption else attach
        user_text += f"\n{rt.temp_note}"
        audit.log("user_message", f"[album] {len(paths)} files", sid=session.sid)
        self.turnctl.enqueue_user_input(session, user_text, chat_id,
                                        grp["first_msg_id"], thread_id)

    # ── voice transcription (#83) ───────────────────────────────────
    # A voice note (ogg/opus) is downloaded, transcribed locally by
    # faster-whisper (its own venv via stt.transcribe), and fed to Claude as a
    # normal turn. Run on a daemon thread so the poll loop is never blocked.
    def handle_voice(self, session, file_id, caption, chat_id, msg_id,
                     thread_id):
        dest = os.path.join(rt.upload_dir, f"{int(time.time())}_voice.oga")
        if not tg.download_file(file_id, dest):
            tg.send("❌ Download failed", chat_id, thread_id=thread_id)
            return
        tg.send_chat_action(chat_id, "typing", thread_id=thread_id)
        result = stt.transcribe(dest)
        if not result or not result.get("text"):
            self.ui.ephemeral(chat_id, "🎙 Could not transcribe the audio",
                              thread_id=thread_id, seconds=8)
            return
        transcript = result["text"]
        body = f"[Voice message transcript]: {transcript}"
        user_text = f"{caption}\n{body}" if caption else body
        audit.log("user_message", f"[voice] {transcript[:200]}", sid=session.sid)
        self.turnctl.enqueue_user_input(session, user_text, chat_id, msg_id,
                                        thread_id)

    # ── video transcription + frame sampling (#84) ──────────────────
    # Audio is transcribed (faster-whisper reads the container's audio via
    # PyAV) AND scene-change frames are sampled (frames.extract, also PyAV — no
    # system ffmpeg). Claude gets ONE turn: transcript with timecodes + the
    # frames as attachments, each tagged with its timecode. Daemon thread.
    @staticmethod
    def _mmss(seconds) -> str:
        s = int(seconds or 0)
        return f"{s // 60:02d}:{s % 60:02d}"

    def handle_video(self, session, file_id, caption, chat_id, msg_id,
                     thread_id):
        ts = int(time.time())
        dest = os.path.join(rt.upload_dir, f"{ts}_video.mp4")
        if not tg.download_file(file_id, dest):
            tg.send("❌ Download failed", chat_id, thread_id=thread_id)
            return
        tg.send_chat_action(chat_id, "typing", thread_id=thread_id)
        # Audio transcript (None if the video has no audio track).
        result = stt.transcribe(dest)
        transcript = (result or {}).get("text", "")
        segments = (result or {}).get("segments") or []
        # Scene-change frames.
        shots = frames.extract(dest, os.path.join(rt.upload_dir,
                                                  f"frames_{ts}"))

        parts = []
        if transcript:
            if segments:
                lines = "\n".join(f"[{self._mmss(s['start'])}] {s['text']}"
                                  for s in segments)
                parts.append(f"[Video transcript]\n{lines}")
            else:
                parts.append(f"[Video transcript]: {transcript}")
        if shots:
            flines = "\n".join(
                f"[Attached file: {s['path']}] (t={self._mmss(s['t'])})"
                for s in shots)
            parts.append(
                f"[Video frames at scene changes]\n{flines}\n{rt.temp_note}")
        if not parts:
            self.ui.ephemeral(
                chat_id,
                "🎬 Could not read the video (no audio track, no frames)",
                thread_id=thread_id, seconds=8)
            return
        if caption:
            parts.insert(0, caption)
        # Decoder-only tier (#86): frames extracted, speech silently absent —
        # tell the user once per video so the missing transcript isn't a
        # mystery.
        if shots and not transcript and not stt.available():
            self.ui.ephemeral(chat_id,
                              "🎬 Frames only — Whisper isn't installed, "
                              "speech not transcribed",
                              thread_id=thread_id, seconds=8)
        user_text = "\n\n".join(parts)
        audit.log(
            "user_message",
            f"[video] {len(shots)} frames, transcript {len(transcript)} chars",
            sid=session.sid)
        self.turnctl.enqueue_user_input(session, user_text, chat_id, msg_id,
                                        thread_id)

    # ── user reactions on bot messages (#77) ────────────────────────
    # A reaction the owner puts on a bot message is forwarded to Claude as a
    # plain user action. The update carries no thread id, so routing goes
    # through the recent-send registry in telegram.py. Removals, reactions on
    # unknown/old messages and reactions outside a live bot session drop
    # silently.
    def handle_reaction(self, mr):
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
        session = self.mgr.by_topic(thread_id) if thread_id else None
        if not (session and session.is_bot_spawned and session.alive):
            return
        if excerpt:
            text = f'[User reacted {emoji} to your message: "{excerpt}"]'
        else:
            text = f"[User reacted {emoji} to your message]"
        audit.log("user_message", text[:200], sid=session.sid)
        self.turnctl.enqueue_user_input(session, text, chat_id, None, thread_id)

    # ── sticker media (#77) ─────────────────────────────────────────
    # Video stickers (webm) get scene frames via frames.extract — same PyAV
    # path as #84, so it runs on a daemon thread.
    def handle_video_sticker(self, session, file_id, descr, chat_id, msg_id,
                             thread_id):
        ts = int(time.time())
        dest = os.path.join(rt.upload_dir, f"{ts}_sticker.webm")
        if not tg.download_file(file_id, dest):
            tg.send("❌ Download failed", chat_id, thread_id=thread_id)
            return
        shots = frames.extract(dest, os.path.join(rt.upload_dir,
                                                  f"frames_{ts}"))
        if shots:
            flines = "\n".join(f"[Attached file: {s['path']}]" for s in shots)
            user_text = f"{descr}\n{flines}\n{rt.temp_note}"
        else:
            user_text = descr
        audit.log("user_message", f"[sticker video] {len(shots)} frames",
                  sid=session.sid)
        self.turnctl.enqueue_user_input(session, user_text, chat_id, msg_id,
                                        thread_id)

    # ── runtime STT install offers (#86) ────────────────────────────
    # setup.sh makes the ~1GB transcription stack opt-in; media that arrives
    # without it gets an inline install offer instead of a dead end. The
    # file_id is parked in state.pending_media_installs and replayed through
    # the normal handler once the install finishes (Telegram file_ids stay
    # downloadable, so nothing is fetched until then).
    def offer_media_install(self, session, kind, file_id, caption,
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
        with self.state.lock:
            self.state.pending_media_installs[pick_id] = {
                "kind": kind, "file_id": file_id, "caption": caption,
                "sid": session.sid, "chat_id": chat_id, "msg_id": msg_id,
                "thread_id": thread_id, "offer_mid": offer_mid,
            }

    def media_install_clicked(self, pick_id, choice):
        with self.state.lock:
            entry = self.state.pending_media_installs.get(pick_id)
        if not entry:
            return
        if choice == "x":
            with self.state.lock:
                self.state.pending_media_installs.pop(pick_id, None)
            if entry["offer_mid"]:
                tg.delete(entry["offer_mid"], entry["chat_id"])
            return
        if choice not in ("frames", *stt_install.MODELS):
            return
        if stt_install.busy():
            self.ui.ephemeral(entry["chat_id"],
                              "⏳ Another install is already running",
                              thread_id=entry["thread_id"], seconds=6)
            return
        with self.state.lock:
            self.state.pending_media_installs.pop(pick_id, None)
        threading.Thread(target=self._run_media_install, args=(entry, choice),
                         daemon=True, name="bot-bg-install").start()

    def _run_media_install(self, entry, choice):
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
            self.ui.delete_after(offer_mid, chat_id, 8)
        session = self.mgr._sessions.get(entry["sid"])
        if not (session and session.alive):
            return  # session died while pip ran; user can re-send the media
        handler = {"voice": self.handle_voice, "video": self.handle_video,
                   "sticker": self.handle_video_sticker}[entry["kind"]]
        handler(session, entry["file_id"], entry["caption"],
                chat_id, entry["msg_id"], thread_id)
