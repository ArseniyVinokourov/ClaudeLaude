"""Video frame sampling bridge.

Like stt.py, this shells out to the `.venv-stt` interpreter (which has PyAV)
to run extract_frames.py, keeping the bot's main venv light. Used for video
and video-note input (#84): scene-change frames are fed to Claude alongside
the audio transcript.
"""
import json
import os
import subprocess
import sys

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_STT_VENV = os.environ.get("STT_VENV", os.path.join(_BOT_DIR, ".venv-stt"))
_STT_PY = os.path.join(_STT_VENV, "bin", "python")
_EXTRACT = os.path.join(_BOT_DIR, "extract_frames.py")
_TIMEOUT = int(os.environ.get("FRAME_TIMEOUT", "180"))


def available() -> bool:
    """True if the side-venv, worker script AND PyAV are present. The venv
    may have been created whisper-only or be mid-install, so check the
    actual decoder package (same rationale as stt.available)."""
    import stt as _stt
    return (os.path.isfile(_STT_PY) and os.path.isfile(_EXTRACT)
            and _stt.pkg_present("av"))


def extract(video_path: str, out_dir: str) -> list:
    """Return a list of {"path", "t"} scene frames (empty on any failure)."""
    if not available() or not os.path.isfile(video_path):
        return []
    try:
        r = subprocess.run(
            [_STT_PY, _EXTRACT, video_path, out_dir],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print("[frames] extraction timed out", file=sys.stderr, flush=True)
        return []
    except Exception as e:  # noqa: BLE001
        print(f"[frames] subprocess error: {e}", file=sys.stderr, flush=True)
        return []
    if r.returncode != 0:
        print(f"[frames] worker failed: {r.stderr[:300]}",
              file=sys.stderr, flush=True)
        return []
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        print(f"[frames] bad worker output: {e}", file=sys.stderr, flush=True)
        return []
    if "error" in data:
        print(f"[frames] {data['error']}", file=sys.stderr, flush=True)
        return []
    return data.get("frames", [])
