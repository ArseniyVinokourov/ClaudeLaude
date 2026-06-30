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


# ── analyzer: emotion (Phase 3, worker-side) ────────────────────────────
# Emotion is the one analyzer with a USER-CHOSEN model (``SPEECH_EMOTION_MODEL``);
# there is no default — the owner picks one in /settings, or leaves it off.
# Measured on a hand-labelled EN+RU set (#126): naming the emotion with a
# CATEGORICAL classifier beats the old dimensional model by far (EN 85% vs 33%);
# the dimensional "valence" axis carries almost no signal from audio, so we drop
# it and let Claude read positivity from the transcript words instead (decision-
# level text+audio fusion — see the note in ``analyze``).
#
#   light    — wav2vec2-base SER (ONNX, 6 classes, English). Runs in the existing
#              .venv-stt on onnxruntime — NO torch, ~0.4 GB. Great EN, weak RU.
#   accurate — MERaLiON-SER (7 classes + VAD, multilingual incl. RU). Needs torch,
#              so it lives in a SEPARATE venv (.venv-ser) and is ~4 GB total.
# Keep ids/paths in sync with ``speech_worker`` (which resolves them under its
# own interpreter).
_EMOTION_MODELS = {
    "light": {
        "title": "Light · English · 0.4 GB",
        "size_mb": 379, "venv": "stt", "deps": [],
        "files": [{"name": "model.onnx",
                   "url": ("https://huggingface.co/onnx-community/"
                           "wav2vec2-base-Speech_Emotion_Recognition-ONNX/"
                           "resolve/main/onnx/model.onnx")}],
    },
    "accurate": {
        "title": "Accurate · multilingual (RU) · 4 GB",
        "size_mb": 4000, "venv": "ser",
        "deps": ["torch", "transformers==4.48.3", "torchaudio", "omegaconf",
                 "peft", "accelerate"],  # MERaLiON's custom modeling imports peft
        "repo": "MERaLiON/MERaLiON-SER-v1",
    },
}


def emotion_model():
    """The user-chosen emotion model id (``SPEECH_EMOTION_MODEL``: light/accurate),
    or "" when emotion analysis is off. Read live so a /settings change applies
    without a restart."""
    m = os.environ.get("SPEECH_EMOTION_MODEL", "")
    return m if m in _EMOTION_MODELS else ""


def _model_dir(aid, lang):
    """Where an analyzer's per-language model file lives inside the side-venv.
    Mirrored by ``speech_worker`` (which resolves the same path under the
    .venv-stt interpreter), so any change here must change there too."""
    import stt
    return os.path.join(stt._STT_VENV, "models", aid, lang)


def _ser_venv():
    """Separate venv for the torch-based ``accurate`` model, so the light path
    (and the rest of the bot) never sees torch. Sits next to .venv-stt."""
    import stt
    return os.path.join(os.path.dirname(stt._STT_VENV), ".venv-ser")


def _emotion_dir(model):
    """Where a chosen emotion model is stored: ``light`` → a single ONNX file in
    .venv-stt/models/emotion/light/; ``accurate`` → HF cache under .venv-ser."""
    import stt
    return os.path.join(stt._STT_VENV, "models", "emotion", model)


def _emotion_available():
    """True once the CHOSEN model is installed. ``light`` = its ONNX file present;
    ``accurate`` = the .venv-ser python + a download marker present."""
    m = emotion_model()
    if m == "light":
        return os.path.isfile(os.path.join(_emotion_dir("light"), "model.onnx"))
    if m == "accurate":
        return (os.path.isfile(os.path.join(_ser_venv(), "bin", "python"))
                and os.path.isfile(os.path.join(_emotion_dir("accurate"), ".ready")))
    return False


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


# ── analyzer: speaker (Phase 3, worker-side — wav2vec2 age-gender ONNX) ──
# Rough speaker traits (age estimate + gender probability) from the waveform.
# Compute lives in `speech_worker._speaker` (onnxruntime + numpy, no torch).
# audeering's official ONNX export (wav2vec2-large-robust fine-tuned on aGender/
# CommonVoice/TIMIT/VoxCeleb2); the SMALL 6-layer variant — 363 MB, ~0.5 GB RAM,
# language-agnostic — so, like audio_events, it's a fixed `archive` (the only
# source is a Zenodo .zip) fetched on enable into `.venv-stt/models/speaker/`.
# The output is an ESTIMATE, never a fact (age is rough); framed as such to Claude.
_SPEAKER_ARCHIVE = {
    "url": ("https://zenodo.org/records/7761387/files/"
            "w2v2-L-robust-6-age-gender.25c844af-1.1.1.zip?download=1"),
    "member": "model.onnx", "name": "model.onnx",
}
_SPEAKER_SIZE_MB = 363


def _speaker_available():
    """True once the age-gender ONNX is extracted into the side-venv. onnxruntime
    + numpy already ship with .venv-stt, so only the model file gates it."""
    return os.path.isfile(os.path.join(_model_dir("speaker", ""), "model.onnx"))


# ── analyzer: diarization (Phase 3, worker-side — sherpa-onnx, no torch) ─
# "Who spoke when / how many voices" from the waveform. Compute lives in
# `speech_worker._diarization` via sherpa-onnx (a pip dep), which runs two small
# ONNX models — a pyannote segmentation export + a 3dspeaker embedding extractor
# — with NO torch and NO gated HF token (the usual pyannote path needs both).
# Language-agnostic. The segmentation ships as a tar.bz2 (extract one member);
# the embedding is a single .onnx. Output is an ESTIMATE; speaker ids are
# arbitrary labels, not identities.
_DIAR_SEG_ARCHIVE = {
    "url": ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-segmentation-models/"
            "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"),
    "member": "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
    "name": "segmentation.onnx",
}
_DIAR_FILES = [
    {"name": "embedding.onnx",
     "url": ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
             "speaker-recongition-models/"
             "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")},
]
_DIAR_SIZE_MB = 46


def _diarization_available():
    """True once sherpa-onnx is installed in the side-venv AND both ONNX models
    are present. sherpa-onnx is an extra pip dep (unlike onnxruntime, which ships
    with .venv-stt), so check for its package dir from the bot side like prosody
    does for parselmouth."""
    import stt
    has_pkg = bool(glob.glob(os.path.join(
        stt._STT_VENV, "lib", "python*", "site-packages", "sherpa_onnx*")))
    d = _model_dir("diarization", "")
    return (has_pkg
            and os.path.isfile(os.path.join(d, "segmentation.onnx"))
            and os.path.isfile(os.path.join(d, "embedding.onnx")))


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
     "where": "worker", "deps": [], "model_choice": True,
     "available": _emotion_available, "run": None},
    {"id": "audio_events", "title": "Sounds & ambience", "needs_install": True,
     "where": "worker", "deps": [], "files": _AUDIO_EVENTS_FILES,
     "size_mb": _AUDIO_EVENTS_SIZE_MB,
     "available": _audio_events_available, "run": None},
    {"id": "speaker", "title": "Age & gender (estimate)", "needs_install": True,
     "where": "worker", "deps": [], "archive": _SPEAKER_ARCHIVE,
     "size_mb": _SPEAKER_SIZE_MB,
     "available": _speaker_available, "run": None},
    {"id": "diarization", "title": "Speakers (who & how many)",
     "needs_install": True, "where": "worker", "deps": ["sherpa-onnx"],
     "files": _DIAR_FILES, "archive": _DIAR_SEG_ARCHIVE, "size_mb": _DIAR_SIZE_MB,
     "available": _diarization_available, "run": None},
]


def registry():
    """Descriptors for the settings menu: id, title, needs_install, available,
    where, and (for model-backed analyzers) the active-language download size."""
    out = []
    for a in _REGISTRY:
        if a.get("model_choice"):           # emotion: size of the CHOSEN model
            spec = _EMOTION_MODELS.get(emotion_model())
            size_mb = spec["size_mb"] if spec else None
        else:
            size_mb = a.get("size_mb")
        out.append({"id": a["id"], "title": a["title"],
                    "needs_install": a["needs_install"],
                    "available": a["available"](),
                    "where": a.get("where", "inprocess"),
                    "size_mb": size_mb,
                    "model_choice": a.get("model_choice", False)})
    return out


def _by_id(aid):
    return next((a for a in _REGISTRY if a["id"] == aid), None)


def active_analyzers():
    """Enabled analyzer ids. The on/off analyzers come from SPEECH_ANALYZERS
    (.env, csv); ``emotion`` is governed by its own knob (``SPEECH_EMOTION_MODEL``)
    so it's appended only when the owner picked a model. Read live so a /settings
    change takes effect without a restart."""
    raw = os.environ.get("SPEECH_ANALYZERS", "")
    ids = [x for x in (s.strip() for s in raw.split(",")) if x and x != "emotion"]
    if emotion_model():
        ids.append("emotion")
    return ids


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
    """Install a worker analyzer's requirements. Blocks (pip + download), so
    callers run it on a daemon thread. Returns True only if everything needed is
    present.

    Most analyzers install pip ``deps`` and/or ``files`` into ``.venv-stt``
    (e.g. prosody → praat-parselmouth; audio_events → the PANNs ONNX + labels).
    ``emotion`` is special — it installs the user-CHOSEN model (light into
    .venv-stt, accurate into the separate .venv-ser); see ``_install_emotion``."""
    a = _by_id(aid)
    if not a:
        return False
    if a.get("model_choice"):
        return _install_emotion()
    import stt_install
    if a.get("deps") and not stt_install.install_analyzer(a["deps"]):
        return False
    # Fixed file set (audio_events): the ONNX graph, its external weights, labels.
    files = a.get("files") or []
    if files:
        d = _model_dir(aid, "")
        for f in files:
            if not stt_install.download_model(f["url"],
                                              os.path.join(d, f["name"])):
                return False
    # Zip archive (speaker): the only source is a .zip, so fetch + extract one
    # member into the model dir.
    arch = a.get("archive")
    if arch:
        dest = os.path.join(_model_dir(aid, ""), arch["name"])
        if not stt_install.download_archive(arch["url"], arch["member"], dest):
            return False
    return bool(a.get("deps") or files or arch)


def _install_emotion():
    """Install the chosen emotion model. ``light`` → a single ONNX file into
    .venv-stt (reuses onnxruntime, NO torch). ``accurate`` → torch + the MERaLiON
    model into a SEPARATE .venv-ser, so the light path stays torch-free."""
    import stt_install
    spec = _EMOTION_MODELS.get(emotion_model())
    if not spec:
        return False
    if spec["venv"] == "stt":
        f = spec["files"][0]
        return stt_install.download_model(
            f["url"], os.path.join(_emotion_dir("light"), f["name"]))
    # accurate: build the side venv, then download the model into it (marker
    # file .ready written on success — see _emotion_available).
    return stt_install.install_ser(_ser_venv(), spec["deps"], spec["repo"],
                                   _emotion_dir("accurate"))


def set_active(ids):
    import config
    config.set_env("SPEECH_ANALYZERS", ",".join(x for x in ids if x != "emotion"))


def toggle(aid):
    """Flip one on/off analyzer; returns the new active list. ``emotion`` is NOT
    toggled here — it's a model choice (see ``set_emotion_model``)."""
    if aid == "emotion":
        return active_analyzers()
    ids = [x for x in active_analyzers() if x != "emotion"]
    if aid in ids:
        ids = [x for x in ids if x != aid]
    elif _by_id(aid):
        ids.append(aid)
    set_active(ids)
    return active_analyzers()


def set_emotion_model(model):
    """Pick the emotion model ('' = off). Persists to .env + live env so the
    change applies without a restart."""
    import config
    config.set_env("SPEECH_EMOTION_MODEL",
                   model if model in _EMOTION_MODELS else "")


def emotion_models():
    """{id: {title, size_mb}} for the settings model picker, in declared order."""
    return {mid: {"title": s["title"], "size_mb": s["size_mb"]}
            for mid, s in _EMOTION_MODELS.items()}


def emotion_ready():
    """True if the currently-chosen emotion model is installed (public wrapper)."""
    return _emotion_available()


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
    # Text+audio fusion guidance for Claude (#126). The acoustic model is good at
    # the SOUND of an emotion and its energy, but poor at positive-vs-negative
    # (valence) — that lives in the words. So: take energy/intensity and the
    # named emotion as a real cue, but read positivity from the transcript, and
    # when tone and words disagree on whether it's positive or negative, trust the
    # words. Only when a model-based analyzer ran (timing/fluency are exact counts).
    if {"emotion", "audio_events"} & set(ran):
        out["note"] = ("These are acoustic cues about HOW it was said — factor "
                       "them in, but they are estimates, not facts. The emotion "
                       "model hears energy and intensity well; it is weak at "
                       "positive-vs-negative, and can confuse similar-energy "
                       "feelings (anger/fear/excitement). Read positivity from "
                       "the words: when the tone and the transcript disagree on "
                       "whether it's positive or negative, trust the words. Treat "
                       "a confident or extreme value as informative but uncertain.")
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
