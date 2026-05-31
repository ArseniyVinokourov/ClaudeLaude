"""Speech-to-text bridge.

The heavy faster-whisper model runs in a SEPARATE venv (`.venv-stt`) as a
subprocess, so the bot's main process keeps only its light dependency set
(requests + python-dotenv). This module is the thin bot-side wrapper: it
locates the side venv, shells out to `transcribe.py`, and parses the JSON.

Used for Telegram voice messages (#83) and the audio track of videos (#84).
"""
import json
import os
import subprocess
import sys

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_STT_VENV = os.environ.get("STT_VENV", os.path.join(_BOT_DIR, ".venv-stt"))
_STT_PY = os.path.join(_STT_VENV, "bin", "python")
_TRANSCRIBE = os.path.join(_BOT_DIR, "transcribe.py")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
_TIMEOUT = int(os.environ.get("STT_TIMEOUT", "180"))


def available() -> bool:
    """True if the STT side-venv and worker script are present."""
    return os.path.isfile(_STT_PY) and os.path.isfile(_TRANSCRIBE)


def transcribe(audio_path: str) -> dict | None:
    """Transcribe an audio file. Returns the worker's JSON dict
    ({"text", "segments", "language"}) or None on any failure."""
    if not available() or not os.path.isfile(audio_path):
        return None
    try:
        r = subprocess.run(
            [_STT_PY, _TRANSCRIBE, audio_path, WHISPER_MODEL],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print("[stt] transcribe timed out", file=sys.stderr, flush=True)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[stt] subprocess error: {e}", file=sys.stderr, flush=True)
        return None
    if r.returncode != 0:
        print(f"[stt] worker failed: {r.stderr[:300]}",
              file=sys.stderr, flush=True)
        return None
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        print(f"[stt] bad worker output: {e}", file=sys.stderr, flush=True)
        return None
    if "error" in data:
        print(f"[stt] {data['error']}", file=sys.stderr, flush=True)
        return None
    return data
