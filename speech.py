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
import re
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


# ── analyzer: fluency (Phase 3, in-process, no deps) ──────────────────
# Fillers / hesitations / immediate repeats from the transcript ALONE — no
# audio decode, no model (Tier A), so it runs here in the bot like `timing`.
# The lexicon is deliberately CONSERVATIVE: only high-precision hesitation
# markers (EN + RU), so the signal under-counts rather than flagging ordinary
# words ("like", "well", "so", "вот", "значит") as fillers. Output is a hint
# for Claude (counts + where), not a verdict on the speaker.
_FLUENCY_FILLERS = {
    # EN non-lexical hesitations
    "um", "umm", "ummm", "uh", "uhh", "uhhh", "uhm", "uhmm",
    "er", "err", "erm", "ermm", "hmm", "hmmm", "mhm", "mm", "mmm",
    # RU non-lexical hesitations + the dominant RU particle fillers
    "э", "ээ", "эээ", "эм", "эмм", "эммм", "мм", "ммм",
    "аа", "ааа", "ну", "нуу", "нууу", "типа",
}
# Multiword fillers, matched on the normalized token-bigram stream. Kept small
# and high-precision (leading-position discourse markers, rarely literal).
_FLUENCY_MULTIWORD = {
    "you know",                 # EN
    "как бы", "это самое",      # RU
}


def _norm(word):
    """Lowercase + strip everything but letters/digits, so 'Um,' → 'um' and
    'э-э' → 'ээ'. `\\w` matches Cyrillic too (str patterns are Unicode)."""
    return re.sub(r"[^\w]", "", (word or "").lower())


def _fluency_tokens(result):
    """(normalized_token, start_sec | None) stream. Prefer per-word timestamps;
    fall back to splitting the transcript text (no timecodes) when segments
    carry no word detail. Empty tokens (pure punctuation) are dropped."""
    words = _flatten_words(result.get("segments"))
    if words:
        out = [(_norm(w["word"]), w["start"]) for w in words]
    else:
        out = [(_norm(t), None) for t in (result.get("text") or "").split()]
    return [(n, s) for (n, s) in out if n]


def _fluency_available():
    return True


def _fluency_run(result, audio_path):
    """Count filler words / hesitations / immediate word repeats. Pure
    text+timestamp rules — no audio, no model."""
    toks = _fluency_tokens(result)
    wc = len(toks)
    if not wc:
        return {}
    fillers, repeats = [], []
    i = 0
    while i < wc:
        norm, at = toks[i]
        at_sec = round(at, 2) if at is not None else None
        if i + 1 < wc and f"{norm} {toks[i + 1][0]}" in _FLUENCY_MULTIWORD:
            fillers.append({"filler": f"{norm} {toks[i + 1][0]}",
                            "at_sec": at_sec})
            i += 2
            continue
        if norm in _FLUENCY_FILLERS:
            fillers.append({"filler": norm, "at_sec": at_sec})
        elif i > 0 and norm == toks[i - 1][0]:
            repeats.append({"word": norm, "at_sec": at_sec})
        i += 1
    return {
        "word_count": wc,
        "filler_count": len(fillers),
        "filler_rate_per_100w": round(len(fillers) / wc * 100, 1),
        "fillers": fillers,
        "repeat_count": len(repeats),
        "repeats": repeats,
        "note": ("rule-based on the transcript, no model; conservative EN+RU "
                 "hesitation lexicon (under-counts rather than over-flags). "
                 "Repeats may be emphatic, not disfluent."),
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


# ── analyzer: audio_events (Phase 3, worker-side — PANNs CNN14 ONNX) ─────
# Non-speech sound events + ambience (cough, laughter, sigh, music, room tone).
# Compute lives in `speech_worker._audio_events` (onnxruntime + numpy, no torch;
# both already ship with .venv-stt). Language-agnostic — sound events aren't
# tied to a language — so unlike emotion there's no per-lang model, just a fixed
# set of files fetched on enable into `.venv-stt/models/audio_events/`:
# the ONNX graph, its external weights (`.onnx.data`, referenced by the graph by
# relative name, so both must land in the same dir), and the AudioSet labels.
_PANNS_BASE = ("https://huggingface.co/pranjal-pravesh/"
               "PANNs_CNN14_ONNX/resolve/main/")
_AUDIO_EVENTS_FILES = [
    {"name": "Cnn14_16k.onnx", "url": _PANNS_BASE + "Cnn14_16k.onnx"},
    {"name": "Cnn14_16k.onnx.data", "url": _PANNS_BASE + "Cnn14_16k.onnx.data"},
    {"name": "class_labels_indices.csv",
     "url": ("https://raw.githubusercontent.com/qiuqiangkong/"
             "audioset_tagging_cnn/master/metadata/class_labels_indices.csv")},
]
_AUDIO_EVENTS_SIZE_MB = 327


def _audio_events_available():
    """True once every audio_events model file is present in the side-venv.
    onnxruntime + numpy already ship with .venv-stt, so only the files gate it."""
    d = _model_dir("audio_events", "")
    return all(os.path.isfile(os.path.join(d, f["name"]))
               for f in _AUDIO_EVENTS_FILES)


# ── registry ───────────────────────────────────────────────────────────
# `where`: "inprocess" runs in the bot on the transcript result; "worker" runs
# in `.venv-stt` during transcription and arrives pre-computed in `result`.
# `deps`: pip packages installed into `.venv-stt` for a worker analyzer.
_REGISTRY = [
    {"id": "timing", "title": "Tempo & pauses", "needs_install": False,
     "where": "inprocess", "deps": [],
     "available": _timing_available, "run": _timing_run},
    {"id": "fluency", "title": "Fillers & hesitation", "needs_install": False,
     "where": "inprocess", "deps": [],
     "available": _fluency_available, "run": _fluency_run},
    {"id": "prosody", "title": "Voice tone & pitch", "needs_install": True,
     "where": "worker", "deps": ["praat-parselmouth"],
     "available": _prosody_available, "run": None},
    {"id": "emotion", "title": "Emotion (tone of feeling)", "needs_install": True,
     "where": "worker", "deps": [], "models": _EMOTION_MODELS,
     "available": _emotion_available, "run": None},
    {"id": "audio_events", "title": "Sounds & ambience", "needs_install": True,
     "where": "worker", "deps": [], "files": _AUDIO_EVENTS_FILES,
     "size_mb": _AUDIO_EVENTS_SIZE_MB,
     "available": _audio_events_available, "run": None},
]


def registry():
    """Descriptors for the settings menu: id, title, needs_install, available,
    where, and (for model-backed analyzers) the active-language download size."""
    out = []
    for a in _REGISTRY:
        m = (a.get("models") or {}).get(speech_lang())
        # Per-language model size (emotion) or a flat descriptor size
        # (audio_events: one language-agnostic download).
        size_mb = m["size_mb"] if m else a.get("size_mb")
        out.append({"id": a["id"], "title": a["title"],
                    "needs_install": a["needs_install"],
                    "available": a["available"](),
                    "where": a.get("where", "inprocess"),
                    "size_mb": size_mb})
    return out


def _by_id(aid):
    return next((a for a in _REGISTRY if a["id"] == aid), None)


def active_analyzers():
    """Enabled analyzer ids from SPEECH_ANALYZERS (.env, csv). Read live so a
    /settings toggle takes effect without a restart."""
    raw = os.environ.get("SPEECH_ANALYZERS", "")
    return [x for x in (s.strip() for s in raw.split(",")) if x]


def fluency_active():
    """True if the in-process ``fluency`` analyzer is enabled. The STT worker
    reads this to bias Whisper toward keeping fillers (um/uh/э-э) in the
    transcript — Whisper normalizes them out by default, so without the bias the
    fluency analyzer has almost nothing to count (#126, measured on real clips)."""
    return "fluency" in active_analyzers()


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
    # Language-agnostic file set (audio_events): a fixed list of files fetched
    # into one model dir — the ONNX graph, its external weights, and labels.
    files = a.get("files") or []
    if files:
        d = _model_dir(aid, "")
        for f in files:
            if not stt_install.download_model(f["url"],
                                              os.path.join(d, f["name"])):
                return False
    # Per-language model (emotion): fetch only the active language, not the
    # whole catalog — RU is 1.27GB and there's no point pulling a language the
    # user isn't using.
    models = a.get("models") or {}
    if models:
        lang = speech_lang()
        m = models.get(lang)
        if not m:
            return False
        dest = os.path.join(_model_dir(aid, lang), m["file"])
        if not stt_install.download_model(m["url"], dest):
            return False
    return bool(a.get("deps") or models or files)


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
