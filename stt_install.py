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
import contextlib
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


def install_analyzer(deps: list[str]) -> bool:
    """Install a speech-analyzer's pip deps into ``.venv-stt`` (#126).

    Used by ``speech.install`` for worker-side analyzers (e.g. prosody →
    ``praat-parselmouth``). Pure pip, no model download — analyzers that pull
    a model handle that themselves (and must target a roomy drive, NOT the
    space-tight one; see the speech tooling notes). Reuses the same venv,
    lock and runner as the whisper tiers."""
    if not deps:
        return False
    with _lock:
        if not _ensure_venv():
            return False
        return _run([_STT_PY, "-m", "pip", "install", "-q", *deps])


def download_model(url: str, dest: str) -> bool:
    """Download an analyzer's model file (e.g. an ONNX SER model) into the
    side-venv's models dir (#126).

    Some worker analyzers need a model file, not just pip deps (emotion →
    wav2vec2 ONNX). Streams to ``dest + '.part'`` then renames, so an aborted
    download never looks complete. Uses ``requests`` (a bot-venv dep) and the
    same lock/timeout as the pip installs, so only one heavy install runs at a
    time and ``busy()`` can refuse a second click. The file lands on whatever
    drive ``.venv-stt`` is on — keep that the roomy one (models are large)."""
    if not url or not dest:
        return False
    if os.path.isfile(dest):
        return True
    import requests
    tmp = dest + ".part"
    with _lock:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with requests.get(url, stream=True, timeout=_TIMEOUT) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            os.replace(tmp, dest)
            return True
        except Exception as e:  # noqa: BLE001
            _log(f"download_model {url[:60]}... failed: {e}")
            with contextlib.suppress(OSError):
                os.remove(tmp)
            return False


def download_archive(url: str, member: str, dest: str) -> bool:
    """Download a model archive (.zip or .tar.*) and extract one member to
    ``dest`` (#126).

    Same contract as ``download_model`` but for analyzers whose only published
    source is an archive (speaker → audeering's age-gender ZIP on Zenodo;
    diarization → sherpa-onnx's segmentation tar.bz2). Streams the archive to a
    temp file, extracts ``member`` (a path inside it) to ``dest + '.part'`` then
    renames, and always removes the archive — so an aborted download never looks
    complete. Reuses the module lock/timeout like the other installs."""
    if not url or not dest:
        return False
    if os.path.isfile(dest):
        return True
    import tarfile
    import zipfile

    import requests
    apath = dest + ".arc.part"
    tmp = dest + ".part"
    with _lock:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with requests.get(url, stream=True, timeout=_TIMEOUT) as r:
                r.raise_for_status()
                with open(apath, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if zipfile.is_zipfile(apath):
                with zipfile.ZipFile(apath) as z:
                    src = z.open(member)
            else:                                   # .tar / .tar.bz2 / .tar.gz
                src = tarfile.open(apath).extractfile(member)
            if src is None:
                raise KeyError(f"member not found: {member}")
            with src, open(tmp, "wb") as out:
                while True:
                    buf = src.read(1 << 20)
                    if not buf:
                        break
                    out.write(buf)
            os.replace(tmp, dest)
            return True
        except Exception as e:  # noqa: BLE001
            _log(f"download_archive {url[:60]}... failed: {e}")
            return False
        finally:
            for p in (apath, tmp):
                with contextlib.suppress(OSError):
                    os.remove(p)


def install_ser(ser_venv: str, deps: list[str], repo: str,
                marker_dir: str) -> bool:
    """Build the SEPARATE torch venv for the 'accurate' emotion model and fetch
    the model into the HF cache (#126). Heavy (~4 GB) — kept out of .venv-stt so
    the light path stays torch-free. Writes a ``.ready`` marker in ``marker_dir``
    on success. CPU torch (small wheel) via the pytorch CPU index; everything
    else from PyPI. Reuses the module lock + timeout, so only one heavy install
    runs at a time and ``busy()`` can refuse a second click."""
    ser_py = os.path.join(ser_venv, "bin", "python")
    with _lock:
        if not os.path.isfile(ser_py):
            if not _run([sys.executable, "-m", "venv", ser_venv]):
                return False
            _run([ser_py, "-m", "pip", "install", "-q", "--upgrade", "pip"])
        torch_pkgs = [d for d in deps if d.split("==")[0] in ("torch", "torchaudio")]
        other = [d for d in deps if d not in torch_pkgs]
        if torch_pkgs and not _run([ser_py, "-m", "pip", "install", "-q",
                                    *torch_pkgs, "--index-url",
                                    "https://download.pytorch.org/whl/cpu"]):
            return False
        if other and not _run([ser_py, "-m", "pip", "install", "-q", *other]):
            return False
        # Trigger the model download AND verify it loads (executes the repo's
        # custom code via trust_remote_code — the owner opted into this model).
        code = ("from transformers import AutoModelForAudioClassification,"
                " AutoProcessor;"
                f"AutoProcessor.from_pretrained('{repo}', trust_remote_code=True);"
                f"AutoModelForAudioClassification.from_pretrained('{repo}',"
                " trust_remote_code=True, low_cpu_mem_usage=False)")
        if not _run([ser_py, "-c", code]):
            return False
        try:
            os.makedirs(marker_dir, exist_ok=True)
            open(os.path.join(marker_dir, ".ready"), "w").close()
        except OSError as e:
            _log(f"marker write failed: {e}")
            return False
        return True
