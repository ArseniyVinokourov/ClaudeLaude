"""Speech-to-text bridge.

The heavy faster-whisper model runs in a SEPARATE venv (`.venv-stt`) as a
subprocess, so the bot's main process keeps only its light dependency set
(requests + python-dotenv). This module is the thin bot-side wrapper: it
locates the side venv, shells out to `transcribe.py`, and parses the JSON.

Used for Telegram voice messages (#83) and the audio track of videos (#84).
"""
import glob
import json
import os
import subprocess
import sys

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_STT_VENV = os.environ.get("STT_VENV", os.path.join(_BOT_DIR, ".venv-stt"))
_STT_PY = os.path.join(_STT_VENV, "bin", "python")
_TRANSCRIBE = os.path.join(_BOT_DIR, "transcribe.py")
_TIMEOUT = int(os.environ.get("STT_TIMEOUT", "180"))


def model_name() -> str:
    """Read at call time — a runtime install (stt_install) may set it."""
    return os.environ.get("WHISPER_MODEL", "small")


def pkg_present(name: str) -> bool:
    """True if a package dir exists in the side-venv's site-packages.
    A directory check instead of a trial import: importing faster_whisper
    spins up ctranslate2 and takes seconds."""
    return bool(glob.glob(os.path.join(
        _STT_VENV, "lib", "python*", "site-packages", name)))


def available() -> bool:
    """True if the STT side-venv, worker script AND faster-whisper are
    present. The venv can exist decoder-only (frames without transcription),
    so the package check is load-bearing."""
    return (os.path.isfile(_STT_PY) and os.path.isfile(_TRANSCRIBE)
            and pkg_present("faster_whisper"))


def transcribe(audio_path: str,
               worker_analyzers: list[str] | None = None) -> dict | None:
    """Transcribe an audio file. Returns the worker's JSON dict
    ({"text", "segments", "language"}) or None on any failure.

    ``worker_analyzers`` (#126) are extra speech analyzers that must run in
    the side-venv (e.g. ``prosody``); each adds its section to the dict under
    its id. The bot computes which to pass via ``speech.active_worker_analyzers``.
    """
    if not available() or not os.path.isfile(audio_path):
        return None
    cmd = [_STT_PY, _TRANSCRIBE, audio_path, model_name()]
    if worker_analyzers:
        cmd.append("--analyzers=" + ",".join(worker_analyzers))
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT,
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
