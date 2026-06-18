"""The /settings runtime menu (#102).

Owner-level, bot-wide knobs changed at runtime and persisted to .env. Presets
only — no free-text capture. The live values live on ``runtime.rt`` (shared by
reference with every reader); a change reassigns the rt attribute AND writes
.env so it survives a restart. A Whisper change first downloads the model
(stt_install) in the background.

Split out of bot.py. A component with the BotUI helper, the TurnController and
the Commands handler injected at construction — the last two only so a display
default change can push the new value into the live copies those components
captured at construction. Telegram, audit, config and the STT stack are
imported directly.
"""
import threading

import audit
import stt
import stt_install
import telegram as tg
from botui import CLOSE_ROW
from config import get_default_mode, set_default_mode, set_env
from runtime import rt
from sessions import MODE_PRESETS, valid_mode

_SETTINGS_TTL_S = 120
_WARN_MB_PRESETS = [100, 250, 500, 1000, 2000]
_TTL_PRESETS = [6 * 3600, 24 * 3600, 48 * 3600, 7 * 86400, 14 * 86400]
_STT_PRESETS = [60, 120, 180, 300, 600]  # voice/video transcription timeout


def _fmt_ttl(seconds: int) -> str:
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    return f"{seconds // 3600}h"


class SettingsMenu:
    def __init__(self, ui, turnctl, commands):
        self.ui = ui
        self.turnctl = turnctl
        self.commands = commands

    def _root_rows(self):
        return [
            [{"text": "🎙 Whisper model", "callback_data": "st:m"}],
            [{"text": "📦 Storage alert", "callback_data": "st:w"}],
            [{"text": "🗑 Cleanup after", "callback_data": "st:t"}],
            [{"text": "📱 Display default", "callback_data": "st:d"}],
            [{"text": "🎚 Default mode", "callback_data": "st:dm"}],
            [{"text": "🕒 Transcription timeout", "callback_data": "st:st"}],
            [{"text": "⬆️ Auto-update", "callback_data": "st:au"}],
            CLOSE_ROW,
        ]

    def _text(self):
        au = "on" if rt.auto_update else "off"
        return (
            "⚙️ <b>Settings</b>\n\n"
            f"🎙 Whisper model: <b>{tg.esc(stt.model_name())}</b>\n"
            f"📦 Storage alert: <b>{rt.upload_warn_bytes // (1024 * 1024)} MB</b>\n"
            f"🗑 Cleanup after: <b>{_fmt_ttl(rt.upload_ttl_s)}</b>\n"
            f"📱 Display default: <b>{tg.esc(rt.default_display)}</b>\n"
            f"🎚 Default mode: <b>{tg.esc(get_default_mode())}</b>\n"
            f"🕒 Transcription timeout: <b>{stt._TIMEOUT}s</b>\n"
            f"⬆️ Auto-update: <b>{au}</b> ({tg.esc(rt.auto_update_policy)})"
        )

    def menu(self, chat_id, thread_id=None):
        mid = tg.send(self._text(), chat_id, thread_id=thread_id,
                      buttons=self._root_rows())
        if not thread_id and mid:
            self.ui.delete_after(mid, chat_id, _SETTINGS_TTL_S)

    def _show(self, cb_msg, cb_chat, which):
        """Expand a root item into its preset picker, in place."""
        if which == "m":
            cur = stt.model_name()
            rows = [[{"text": f"{'• ' if k == cur else ''}{k} — {size}",
                      "callback_data": f"st:m:{k}"}]
                    for k, size in stt_install.MODELS.items()]
            rows.append([{"text": "◀ Back", "callback_data": "st:root"}])
            tg.edit(cb_msg, "🎙 <b>Whisper model</b>\nChanging downloads the "
                    "model if needed (runs in background).", cb_chat,
                    buttons=rows)
        elif which == "w":
            cur = rt.upload_warn_bytes // (1024 * 1024)
            rows = [[{"text": f"{'• ' if mb == cur else ''}{mb} MB",
                      "callback_data": f"st:w:{mb}"}] for mb in _WARN_MB_PRESETS]
            rows.append([{"text": "◀ Back", "callback_data": "st:root"}])
            tg.edit(cb_msg, "📦 <b>Storage alert</b>\nDM the owner once a day "
                    "when the uploads folder grows past this size.",
                    cb_chat, buttons=rows)
        elif which == "t":
            cur = rt.upload_ttl_s
            rows = [[{"text": f"{'• ' if s == cur else ''}{_fmt_ttl(s)}",
                      "callback_data": f"st:t:{s}"}] for s in _TTL_PRESETS]
            rows.append([{"text": "◀ Back", "callback_data": "st:root"}])
            tg.edit(cb_msg, "🗑 <b>Cleanup after</b>\nDelete uploaded media older "
                    "than this (kept if a session still references it).",
                    cb_chat, buttons=rows)
        elif which == "d":
            rows = [[{"text": f"{'• ' if v == rt.default_display else ''}{v}",
                      "callback_data": f"st:d:{v}"}]
                    for v in ("mobile", "desktop")]
            rows.append([{"text": "◀ Back", "callback_data": "st:root"}])
            tg.edit(cb_msg, "📱 <b>Display default</b>\nLayout new topics start "
                    "in. /display overrides it per topic.", cb_chat,
                    buttons=rows)
        elif which == "dm":
            cur = get_default_mode()
            rows = [[{"text": f"{'• ' if k == cur else ''}{k} — {p['label']}",
                      "callback_data": f"st:dm:{k}"}]
                    for k, p in MODE_PRESETS.items()]
            rows.append([{"text": "◀ Back", "callback_data": "st:root"}])
            tg.edit(cb_msg, "🎚 <b>Default mode</b>\nWhich response style new "
                    "sessions start in.", cb_chat, buttons=rows)
        elif which == "st":
            cur = stt._TIMEOUT
            rows = [[{"text": f"{'• ' if s == cur else ''}{s}s",
                      "callback_data": f"st:st:{s}"}] for s in _STT_PRESETS]
            rows.append([{"text": "◀ Back", "callback_data": "st:root"}])
            tg.edit(cb_msg, "🕒 <b>Transcription timeout</b>\nHow long to wait "
                    "for voice/video transcription before giving up.",
                    cb_chat, buttons=rows)
        elif which == "au":
            on = rt.auto_update
            rows = [
                [{"text": f"{'• ' if on else ''}On",
                  "callback_data": "st:au:on"},
                 {"text": f"{'• ' if not on else ''}Off",
                  "callback_data": "st:au:off"}],
                [{"text": f"{'• ' if rt.auto_update_policy == 'replace' else ''}"
                  "policy: replace", "callback_data": "st:au:replace"}],
                [{"text": f"{'• ' if rt.auto_update_policy == 'merge' else ''}"
                  "policy: merge", "callback_data": "st:au:merge"}],
                [{"text": "◀ Back", "callback_data": "st:root"}],
            ]
            tg.edit(cb_msg, "⬆️ <b>Auto-update</b>\nCheck hourly and update. On "
                    "local changes: replace (back up + overwrite) or merge "
                    "(wait, /update to review).", cb_chat, buttons=rows)

    def _set_warn(self, cb_msg, cb_chat, mb):
        rt.upload_warn_bytes = mb * 1024 * 1024
        set_env("UPLOAD_WARN_MB", str(mb))
        audit.log("settings", f"upload_warn_mb={mb}")
        tg.edit(cb_msg, self._text(), cb_chat, buttons=self._root_rows())

    def _set_ttl(self, cb_msg, cb_chat, seconds):
        rt.set_upload_ttl_s(seconds)
        set_env("UPLOAD_TTL_S", str(seconds))
        audit.log("settings", f"upload_ttl_s={seconds}")
        tg.edit(cb_msg, self._text(), cb_chat, buttons=self._root_rows())

    def _set_display(self, cb_msg, cb_chat, value):
        if value not in ("mobile", "desktop"):
            return
        rt.default_display = value
        # Update the live copies the components captured at construction, so
        # new topics pick up the change without a restart.
        self.turnctl._default_display = value
        self.commands._default_display = value
        set_env("DEFAULT_DISPLAY", value)
        audit.log("settings", f"default_display={value}")
        tg.edit(cb_msg, self._text(), cb_chat, buttons=self._root_rows())

    def _set_default_mode(self, cb_msg, cb_chat, mode):
        if not valid_mode(mode):
            return
        set_default_mode(mode)
        audit.log("settings", f"default_mode={mode}")
        tg.edit(cb_msg, self._text(), cb_chat, buttons=self._root_rows())

    def _set_stt(self, cb_msg, cb_chat, seconds):
        if seconds not in _STT_PRESETS:
            return
        stt._TIMEOUT = seconds
        set_env("STT_TIMEOUT", str(seconds))
        audit.log("settings", f"stt_timeout={seconds}")
        tg.edit(cb_msg, self._text(), cb_chat, buttons=self._root_rows())

    def _set_autoupdate(self, cb_msg, cb_chat, value):
        if value in ("on", "off"):
            rt.auto_update = (value == "on")
            set_env("AUTO_UPDATE", "true" if rt.auto_update else "false")
            audit.log("settings", f"auto_update={value}")
        elif value in ("replace", "merge"):
            rt.auto_update_policy = value
            set_env("AUTO_UPDATE_POLICY", value)
            audit.log("settings", f"auto_update_policy={value}")
        else:
            return
        # Re-render the picker so the • marker moves to the new selection.
        self._show(cb_msg, cb_chat, "au")

    def _set_model(self, cb_msg, cb_chat, model):
        if model not in stt_install.MODELS:
            return
        if model == stt.model_name() and stt.pkg_present("faster_whisper"):
            tg.edit(cb_msg, self._text(), cb_chat, buttons=self._root_rows())
            return
        if stt_install.busy():
            self.ui.ephemeral(cb_chat, "⏳ Another install is already running",
                              seconds=6)
            return
        tg.edit(cb_msg, f"⏳ Downloading Whisper <b>{tg.esc(model)}</b> — "
                "this can take a few minutes…", cb_chat)
        threading.Thread(target=self.run_model_install,
                         args=(cb_msg, cb_chat, model),
                         daemon=True, name="bot-bg-install").start()

    def run_model_install(self, cb_msg, cb_chat, model):
        # install_whisper persists WHISPER_MODEL (.env + env) on success, so
        # stt.model_name() reflects the switch once this returns ok.
        ok = stt_install.install_whisper(model)
        audit.log("settings",
                  f"whisper_model={model} {'ok' if ok else 'FAILED'}")
        if ok:
            body = self._text()
        else:
            body = f"❌ Failed to install Whisper {tg.esc(model)} — see bot.log."
        # The download can run for minutes — longer than the /settings
        # message's own TTL — so the menu message may already be gone. Try to
        # edit it back; only if that fails (message deleted) send a fresh
        # result notice, so the outcome is never silently lost.
        if not tg.edit(cb_msg, body, cb_chat, buttons=self._root_rows()):
            result = (f"✅ Whisper {tg.esc(model)} ready" if ok else body)
            self.ui.ephemeral(cb_chat, result, seconds=30)

    def clicked(self, cb_msg, cb_chat, rest):
        if rest == "root":
            tg.edit(cb_msg, self._text(), cb_chat, buttons=self._root_rows())
        elif rest in ("m", "w", "t", "d", "dm", "st", "au"):
            self._show(cb_msg, cb_chat, rest)
        elif rest.startswith("m:"):
            self._set_model(cb_msg, cb_chat, rest[2:])
        elif rest.startswith("w:"):
            try:
                self._set_warn(cb_msg, cb_chat, int(rest[2:]))
            except ValueError:
                pass
        elif rest.startswith("t:"):
            try:
                self._set_ttl(cb_msg, cb_chat, int(rest[2:]))
            except ValueError:
                pass
        elif rest.startswith("dm:"):
            self._set_default_mode(cb_msg, cb_chat, rest[3:])
        elif rest.startswith("st:"):
            try:
                self._set_stt(cb_msg, cb_chat, int(rest[3:]))
            except ValueError:
                pass
        elif rest.startswith("d:"):
            self._set_display(cb_msg, cb_chat, rest[2:])
        elif rest.startswith("au:"):
            self._set_autoupdate(cb_msg, cb_chat, rest[3:])
