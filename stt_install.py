"""Runtime installer for the optional STT stack (`.venv-stt`).

setup.sh offers the same install during onboarding; this module covers
users who skipped it there and later sent a voice/video message. Two tiers:

  - decoder: ``av pillow numpy``           (~250MB) — frame extraction only
  - whisper: ``faster-whisper pillow``      + model — full transcription
              (PyAV ships as a faster-whisper dependency, so the whisper
              tier implies the decoder tier)

Calls block for minutes (pip + model download) — callers run them on a
daemon thread. A module lock serializes installs; ``busy()`` lets the UI
refuse a second click instead of queueing.
"""
import os
import subprocess
import sys
import threading

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_STT_VENV = os.environ.get("STT_VENV", os.path.join(_BOT_DIR, ".venv-stt"))
_STT_PY = os.path.join(_STT_VENV, "bin", "python")
_ENV_FILE = os.path.join(_BOT_DIR, ".env")
_TIMEOUT = int(os.environ.get("STT_INSTALL_TIMEOUT", "1800"))

# Model → approximate download size, shown on buttons. Keep in sync with
# the size table in setup.sh.
MODELS = {"base": "~145MB", "small": "~460MB", "medium": "~1.5GB"}

_lock = threading.Lock()


def busy() -> bool:
    return _lock.locked()


def _log(msg: str):
    print(f"[stt_install] {msg}", file=sys.stderr, flush=True)


def _run(args: list[str]) -> bool:
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        _log(f"{args[:3]}... failed: {e}")
        return False
    if r.returncode != 0:
        _log(f"{args[:3]}... rc={r.returncode}: {r.stderr[-300:]}")
        return False
    return True


def _ensure_venv() -> bool:
    if os.path.isfile(_STT_PY):
        return True
    # sys.executable is the bot's own venv python; `-m venv` from inside a
    # venv still creates a fresh one off the same base interpreter.
    if not _run([sys.executable, "-m", "venv", _STT_VENV]):
        return False
    return _run([_STT_PY, "-m", "pip", "install", "-q", "--upgrade", "pip"])


def _persist_model(model: str):
    """Write WHISPER_MODEL to .env (so it survives restarts) and to the
    live environment (so stt.model_name() sees it now)."""
    os.environ["WHISPER_MODEL"] = model
    try:
        lines = []
        if os.path.isfile(_ENV_FILE):
            with open(_ENV_FILE) as f:
                lines = [ln for ln in f.read().splitlines()
                         if not ln.startswith("WHISPER_MODEL=")]
        lines.append(f"WHISPER_MODEL={model}")
        with open(_ENV_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        _log(f".env write failed: {e}")  # non-fatal: env var still set


def install_decoder() -> bool:
    """Frames-only tier: PyAV + Pillow + numpy, no whisper, no model."""
    with _lock:
        if not _ensure_venv():
            return False
        return _run([_STT_PY, "-m", "pip", "install", "-q",
                     "av", "pillow", "numpy"])


def install_whisper(model: str) -> bool:
    """Full tier: faster-whisper (+ bundled PyAV) + model prefetch."""
    if model not in MODELS:
        _log(f"unknown model '{model}'")
        return False
    with _lock:
        if not _ensure_venv():
            return False
        if not _run([_STT_PY, "-m", "pip", "install", "-q",
                     "faster-whisper", "pillow"]):
            return False
        if not _run([_STT_PY, "-c",
                     "from faster_whisper import WhisperModel; "
                     f"WhisperModel('{model}', device='cpu', "
                     "compute_type='int8')"]):
            return False
        _persist_model(model)
        return True
