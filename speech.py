"""Speech analysis layer (#126) — optional, modular "how it was said" signals.

The transcript text still goes to Claude unchanged. When any analyzer is
enabled (``SPEECH_ANALYZERS`` in .env, toggled from /settings), the full
analysis is written to a JSON file in ``rt.upload_dir`` and referenced to Claude
as an ``[Attached file: ...]`` — so the turn text stays clean and Claude reads
the detail on demand.

Design: a small registry of analyzers. Each declares an id, a human title for
the settings menu, whether extra packages must be installed, an availability
check, and where it runs (``where``):

  - ``inprocess`` — runs here in the bot via ``run(result, audio_path)`` on the
    transcript (e.g. ``timing``: pure-python tempo/pauses from word timestamps,
    no dependency, no model).
  - ``worker`` — needs the heavy side-venv (audio decode / a model), so the
    compute lives in ``speech_worker`` and runs inside ``.venv-stt`` DURING
    transcription; its section arrives pre-computed in ``result`` under the
    analyzer id (e.g. ``prosody``: voice tone/pitch via parselmouth). The bot
    only declares it here and reads the section back.

The runner merges whatever is enabled AND available; a section that is missing
(in-process analyzer raised, or worker analyzer produced nothing) is simply
absent, so one failure never blocks the turn.
"""
import glob
import json
import os
import sys

# A gap between consecutive words at/above this counts as a deliberate pause.
_PAUSE_MIN_S = 0.4


def _flatten_words(segments):
    words = []
    for seg in segments or []:
        for w in seg.get("words") or []:
            if w.get("start") is not None and w.get("end") is not None:
                words.append(w)
    return words


# ── analyzer: timing (Phase 1, pure python, no deps) ──────────────────
def _timing_available():
    return True


def _timing_run(result, audio_path):
    """Tempo / pauses / rhythm from word timestamps (segment-level fallback)."""
    segments = result.get("segments") or []
    words = _flatten_words(segments)
    anchors = words if len(words) >= 2 else segments
    if len(anchors) < 1:
        return {}
    span = anchors[-1]["end"] - anchors[0]["start"]
    pauses = []
    for i in range(1, len(anchors)):
        gap = round(anchors[i]["start"] - anchors[i - 1]["end"], 2)
        if gap >= _PAUSE_MIN_S:
            pauses.append({"after_sec": round(anchors[i - 1]["end"], 2),
                           "len_sec": gap})
    wc = len(words)
    wpm = round(wc / span * 60) if span > 0 and wc else None
    return {
        "duration_sec": round(span, 2) if span > 0 else None,
        "word_count": wc,
        "speech_rate_wpm": wpm,
        "pause_count": len(pauses),
        "longest_pause_sec": max((p["len_sec"] for p in pauses), default=0.0),
        "total_pause_sec": round(sum(p["len_sec"] for p in pauses), 2),
        "pauses": pauses,
    }


# ── analyzer: prosody (Phase 3, worker-side — parselmouth in .venv-stt) ─
# Voice tone/pitch/strain from the waveform. The compute lives in
# `speech_worker._prosody` (heavy venv, lazy parselmouth); the bot only knows
# it exists, whether it's installed, and reads its section back out of the
# transcription result. So there is no `run` here — `where: "worker"`.
def _prosody_available():
    """True if parselmouth is installed in the STT side-venv. parselmouth ships
    as a compiled extension (parselmouth.cpython-*.so), not a package dir, so
    match the glob rather than stt.pkg_present (which checks for a dir)."""
    import stt
    return bool(glob.glob(os.path.join(
        stt._STT_VENV, "lib", "python*", "site-packages", "parselmouth*")))


# ── analyzer: emotion (Phase 3, worker-side — wav2vec2 ONNX in .venv-stt) ─
# Categorical emotion from the waveform (tone of feeling). The compute lives in
# `speech_worker._emotion` (onnxruntime + numpy, no torch; both already ship
# with .venv-stt as faster-whisper deps), so the analyzer needs no pip packages
# — only the ONNX model file, downloaded on enable. `where: "worker"`.
#
# Per-language model: each entry is the int8 ONNX to fetch into
# `.venv-stt/models/emotion/<lang>/`. EN ships first (de-risked: int8 95MB,
# 6-class, standard wav2vec2 numpy preprocessing). RU lands later (its model
# needs self-quantization + an input-format check). Keep the destination
# layout in sync with `speech_worker._emotion_model_path`.
_EMOTION_MODELS = {
    "en": {
        "file": "model_int8.onnx",
        "size_mb": 95,
        "url": ("https://huggingface.co/onnx-community/"
                "wav2vec2-base-Speech_Emotion_Recognition-ONNX/"
                "resolve/main/onnx/model_int8.onnx"),
    },
}


def _model_dir(aid, lang):
    """Where an analyzer's per-language model file lives inside the side-venv.
    Mirrored by ``speech_worker`` (which resolves the same path under the
    .venv-stt interpreter), so any change here must change there too."""
    import stt
    return os.path.join(stt._STT_VENV, "models", aid, lang)


def speech_lang():
    """Active analysis language (``SPEECH_LANG``, default en). Read live so a
    /settings change takes effect without a restart. Currently only emotion
    is language-specific; EN is the only model that ships today."""
    return os.environ.get("SPEECH_LANG", "en")


def _emotion_available():
    """True if the active-language emotion model is downloaded. onnxruntime +
    numpy already ship with .venv-stt (faster-whisper deps), so only the model
    file gates availability."""
    lang = speech_lang()
    m = _EMOTION_MODELS.get(lang)
    return bool(m) and os.path.isfile(os.path.join(_model_dir("emotion", lang),
                                                   m["file"]))


# ── registry ───────────────────────────────────────────────────────────
# `where`: "inprocess" runs in the bot on the transcript result; "worker" runs
# in `.venv-stt` during transcription and arrives pre-computed in `result`.
# `deps`: pip packages installed into `.venv-stt` for a worker analyzer.
_REGISTRY = [
    {"id": "timing", "title": "Tempo & pauses", "needs_install": False,
     "where": "inprocess", "deps": [],
     "available": _timing_available, "run": _timing_run},
    {"id": "prosody", "title": "Voice tone & pitch", "needs_install": True,
     "where": "worker", "deps": ["praat-parselmouth"],
     "available": _prosody_available, "run": None},
    {"id": "emotion", "title": "Emotion (tone of feeling)", "needs_install": True,
     "where": "worker", "deps": [], "models": _EMOTION_MODELS,
     "available": _emotion_available, "run": None},
]


def registry():
    """Descriptors for the settings menu: id, title, needs_install, available,
    where, and (for model-backed analyzers) the active-language download size."""
    out = []
    for a in _REGISTRY:
        m = (a.get("models") or {}).get(speech_lang())
        out.append({"id": a["id"], "title": a["title"],
                    "needs_install": a["needs_install"],
                    "available": a["available"](),
                    "where": a.get("where", "inprocess"),
                    "size_mb": m["size_mb"] if m else None})
    return out


def _by_id(aid):
    return next((a for a in _REGISTRY if a["id"] == aid), None)


def active_analyzers():
    """Enabled analyzer ids from SPEECH_ANALYZERS (.env, csv). Read live so a
    /settings toggle takes effect without a restart."""
    raw = os.environ.get("SPEECH_ANALYZERS", "")
    return [x for x in (s.strip() for s in raw.split(",")) if x]


def active_worker_analyzers():
    """Enabled worker-side analyzers that are installed — the ids
    ``stt.transcribe`` must hand to the side-venv so their sections come back
    in the result. In-process analyzers (run by the bot) are excluded."""
    out = []
    for aid in active_analyzers():
        a = _by_id(aid)
        if a and a.get("where") == "worker" and a["available"]():
            out.append(aid)
    return out


def install(aid):
    """Install a worker analyzer's requirements into ``.venv-stt`` — pip deps
    and/or model files. Blocks (pip + download), so callers run it on a daemon
    thread. Returns True only if everything the analyzer needs is present.

    ``deps`` → pip (e.g. prosody → praat-parselmouth). ``models`` → a model
    file per language downloaded into ``_model_dir`` (e.g. emotion → the EN
    ONNX). An analyzer may declare either, both, or neither."""
    a = _by_id(aid)
    if not a:
        return False
    import stt_install
    if a.get("deps") and not stt_install.install_analyzer(a["deps"]):
        return False
    # Fetch only the active language's model, not the whole catalog — RU is
    # 1.27GB and there's no point pulling a language the user isn't using.
    models = a.get("models") or {}
    if models:
        lang = speech_lang()
        m = models.get(lang)
        if not m:
            return False
        dest = os.path.join(_model_dir(aid, lang), m["file"])
        if not stt_install.download_model(m["url"], dest):
            return False
    return bool(a.get("deps") or models)


def set_active(ids):
    import config
    config.set_env("SPEECH_ANALYZERS", ",".join(ids))


def toggle(aid):
    """Flip one analyzer on/off; returns the new active list."""
    ids = active_analyzers()
    if aid in ids:
        ids = [x for x in ids if x != aid]
    elif _by_id(aid):
        ids.append(aid)
    set_active(ids)
    return ids


def analyze(result, audio_path):
    """Run every enabled+available analyzer on a transcription ``result``
    ({text, segments, language}; segments may carry per-word timestamps).

    Returns {language, transcript, analyzers:[...], <id>:{...}} or None when
    nothing is enabled / available / produced — in which case the caller
    behaves exactly as it did before this feature existed.
    """
    enabled = active_analyzers()
    if not enabled:
        return None
    out, ran = {}, []
    for aid in enabled:
        a = _by_id(aid)
        if not a or not a["available"]():
            continue
        if a.get("where") == "worker":
            # Computed in .venv-stt during transcription; already in `result`
            # under its id (absent if the worker analyzer found/produced
            # nothing — then it's simply dropped here, same as a failure).
            section = result.get(aid)
        else:
            try:
                section = a["run"](result, audio_path)
            except Exception as e:  # noqa: BLE001 — never let one analyzer block the turn
                print(f"[speech] analyzer {aid} failed: {e}",
                      file=sys.stderr, flush=True)
                continue
        if section:
            out[aid] = section
            ran.append(aid)
    if not ran:
        return None
    out["analyzers"] = ran
    out["language"] = result.get("language")
    out["transcript"] = result.get("text")
    return out


def write_analysis_file(analysis, audio_path):
    """Dump the analysis JSON next to the upload; return its path or None."""
    from runtime import rt
    base = os.path.splitext(os.path.basename(audio_path))[0]
    path = os.path.join(rt.upload_dir, f"{base}_speech.json")
    try:
        os.makedirs(rt.upload_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump(analysis, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[speech] could not write {path}: {e}",
              file=sys.stderr, flush=True)
        return None
    return path
