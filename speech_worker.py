#!/usr/bin/env python3
"""Worker-side speech analyzers — run under the `.venv-stt` interpreter, NOT
the bot's main venv (#126).

Imported by ``transcribe.py`` (which already runs in `.venv-stt`). This is
where the *heavy* analyzers live — the ones that need to decode the audio or
load an audio model — so the bot's light venv never sees these dependencies.
Every heavy import is lazy (inside the analyzer), so importing this module
costs nothing until an analyzer actually runs.

Contract: ``run(audio_path, ids) -> {id: section_dict}``. A failing analyzer
is dropped (logged to stderr) so one failure never blocks the turn, and
NOTHING here writes to stdout — ``transcribe.py``'s stdout must stay a single
JSON line for ``stt.py`` to parse.

The first analyzer (``prosody``) reads the raw waveform, so this module owns
``AudioBundle``: decode the file ONCE (PyAV → float32 mono @16k) and hand the
same samples to every analyzer. 16k mono is also exactly what the later
wav2vec2/PANNs ONNX analyzers consume, so the decode is shared, not repeated.
"""
import contextlib
import gc
import os
import sys

_TARGET_SR = 16000
# Below this we have too little voiced signal for stable pitch/jitter stats.
_MIN_SECONDS = 0.3

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
# Mirror of `speech._model_dir` resolved under the .venv-stt interpreter — keep
# the layout (.venv-stt/models/<id>/<lang>/) in sync with the bot side.
_STT_VENV = os.environ.get("STT_VENV", os.path.join(_BOT_DIR, ".venv-stt"))


@contextlib.contextmanager
def _heavy_lock():
    """Serialize heavy model inference across concurrent transcribe.py spawns.

    Each voice/video message spawns its own worker process, so two arriving at
    once would load two models at the same time — an OOM risk on a small box.
    A cross-process file lock (flock) lets only one heavy analysis run at a
    time. POSIX-only (fcntl); this worker only ever runs on Linux/WSL/mac."""
    import fcntl
    os.makedirs(os.path.join(_STT_VENV, "models"), exist_ok=True)
    lock_path = os.path.join(_STT_VENV, "models", ".heavy.lock")
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


class AudioBundle:
    """One-time decode of an audio/video file to float32 mono @16k.

    Lazy + cached: the costly PyAV decode happens on first ``.samples`` access
    and is reused by every analyzer in a single ``run()``.
    """

    def __init__(self, path):
        self.path = path
        self.sr = _TARGET_SR
        self._samples = None

    @property
    def samples(self):
        if self._samples is None:
            self._samples = self._decode()
        return self._samples

    def _decode(self):
        import av
        import numpy as np
        container = av.open(self.path)
        try:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(
                format="flt", layout="mono", rate=_TARGET_SR)
            chunks = []
            for frame in container.decode(stream):
                for rf in resampler.resample(frame):
                    chunks.append(rf.to_ndarray().reshape(-1))
            for rf in resampler.resample(None):  # flush
                chunks.append(rf.to_ndarray().reshape(-1))
        finally:
            container.close()
        if not chunks:
            return np.zeros(0, dtype="float32")
        return np.concatenate(chunks).astype("float32")


def _round(x, n=1):
    """Round to n places; map NaN / non-numeric (Praat 'undefined') to None so
    the JSON carries explicit nulls instead of NaN (invalid JSON)."""
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if xf != xf:  # NaN
        return None
    return round(xf, n)


# ── analyzer: prosody (parselmouth / Praat, Tier A) ───────────────────
def _prosody(bundle, ctx):
    """Voice tone & quality from the waveform: pitch (F0) level + range,
    loudness, and roughness/strain (jitter, shimmer, HNR). Cheap, no model."""
    import parselmouth
    from parselmouth.praat import call

    samples = bundle.samples
    if samples.size < bundle.sr * _MIN_SECONDS:
        return {}
    snd = parselmouth.Sound(samples.astype("float64"),
                            sampling_frequency=bundle.sr)

    f0 = snd.to_pitch().selected_array["frequency"]
    voiced = f0[f0 > 0]
    inten = snd.to_intensity().values[0]
    pp = call(snd, "To PointProcess (periodic, cc)", 75, 500)
    jitter = call(pp, "Get jitter (local)", 0, 0, 1e-4, 0.02, 1.3)
    shimmer = call([snd, pp], "Get shimmer (local)",
                   0, 0, 1e-4, 0.02, 1.3, 1.6)
    hnr = snd.to_harmonicity().values
    hnr = hnr[hnr != -200]  # -200 = Praat's "undefined" frame marker

    return {
        "duration_sec": _round(snd.get_total_duration(), 2),
        "voiced_ratio": _round(voiced.size / f0.size, 2) if f0.size else None,
        "pitch_mean_hz": _round(voiced.mean()) if voiced.size else None,
        "pitch_min_hz": _round(voiced.min()) if voiced.size else None,
        "pitch_max_hz": _round(voiced.max()) if voiced.size else None,
        "pitch_range_hz": (_round(voiced.max() - voiced.min())
                           if voiced.size else None),
        "loudness_db": _round(inten.mean()) if inten.size else None,
        "jitter_pct": _round(jitter * 100, 2),
        "shimmer_pct": _round(shimmer * 100, 2),
        "hnr_db": _round(hnr.mean()) if hnr.size else None,
    }


# ── analyzer: emotion (CATEGORICAL, user-chosen model) ────────────────
# Measured (#126): naming the emotion with a categorical classifier beats the old
# dimensional model by far, and audio "valence" (positive vs negative) is near-
# useless — that signal lives in the words, so we leave positivity to Claude and
# only emit the named emotion + energy here. Dispatch on SPEECH_EMOTION_MODEL:
#   light    → wav2vec2-base SER ONNX, run right here on onnxruntime (no torch).
#   accurate → MERaLiON-SER in the separate .venv-ser (torch); we can't import
#              torch here, so hand it the decoded samples and read back JSON.
# Both gate on the transcript: empty text (non-speech) → skip; None → don't gate.
_EMOTION_LIGHT_LABELS = {0: "sad", 1: "anger", 2: "disgust", 3: "fear",
                         4: "happy", 5: "neutral"}
_EMOTION_NOTE = ("the emotion the VOICE sounds like, from acoustics — a real cue "
                 "to HOW it was said, but an estimate, not a fact. Reliable on "
                 "energy/intensity; weak at positive-vs-negative and at telling "
                 "apart similar-energy feelings (anger/fear/excitement). Read "
                 "positivity from the words, not this label.")


def _emotion(bundle, ctx):
    model = os.environ.get("SPEECH_EMOTION_MODEL", "")
    if model == "light":
        return _emotion_light(bundle, ctx)
    if model == "accurate":
        return _emotion_accurate(bundle, ctx)
    return {}


def _emotion_light(bundle, ctx):
    """wav2vec2-base SER (ONNX, 6-class, English). onnxruntime + numpy only."""
    text = ctx.get("text")
    if text is not None and not text.strip():
        return {}
    model_path = os.path.join(_STT_VENV, "models", "emotion", "light", "model.onnx")
    if not os.path.isfile(model_path):
        return {}
    samples = bundle.samples
    if samples.size < bundle.sr * _MIN_SECONDS:
        return {}

    import numpy as np
    import onnxruntime as ort
    x = samples.astype("float32")
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)   # do_normalize=True
    with _heavy_lock():
        sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        name = sess.get_inputs()[0].name
        logits = sess.run(None, {name: x[None, :]})[0][0]
    e = np.exp(logits - logits.max())
    probs = e / e.sum()
    order = [int(i) for i in np.argsort(probs)[::-1]]
    top = [{"emotion": _EMOTION_LIGHT_LABELS.get(i, str(i)),
            "p": _round(float(probs[i]), 2)} for i in order[:3]]
    return {"label": top[0]["emotion"], "confidence": top[0]["p"], "top": top,
            "model": "wav2vec2-base SER (English, 6-class)", "note": _EMOTION_NOTE}


def _emotion_accurate(bundle, ctx):
    """MERaLiON-SER in the separate .venv-ser (torch). We can't import torch in
    .venv-stt, so dump the decoded samples to a temp .npy and run speech_ser.py
    under that venv, reading back one JSON line."""
    text = ctx.get("text")
    if text is not None and not text.strip():
        return {}
    ser_py = os.path.join(os.path.dirname(_STT_VENV), ".venv-ser", "bin", "python")
    script = os.path.join(_BOT_DIR, "speech_ser.py")
    if not (os.path.isfile(ser_py) and os.path.isfile(script)):
        return {}
    samples = bundle.samples
    if samples.size < bundle.sr * _MIN_SECONDS:
        return {}

    import json as _json
    import subprocess
    import tempfile
    import numpy as np
    with _heavy_lock():
        with tempfile.NamedTemporaryFile(suffix=".npy", delete=False) as tf:
            np.save(tf, samples.astype("float32"))
            npy = tf.name
        try:
            r = subprocess.run([ser_py, script, npy],
                               capture_output=True, text=True, timeout=300)
        finally:
            with contextlib.suppress(OSError):
                os.remove(npy)
    if r.returncode != 0:
        print(f"[speech_worker] accurate emotion failed: {r.stderr[-300:]}",
              file=sys.stderr, flush=True)
        return {}
    try:
        sec = _json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {}
    sec.setdefault("note", _EMOTION_NOTE)
    return sec


# ── analyzer: audio_events (PANNs CNN14 ONNX, Tier B — onnxruntime, no torch) ─
# Non-speech sound events + ambience over the whole clip (cough, laughter, sigh,
# music, room tone…). The 16kHz CNN14 ONNX computes its mel spectrogram INSIDE
# the graph, so we feed the raw 16k waveform — pure onnxruntime + numpy, no
# torch/librosa. 527 AudioSet classes; the label order lives in the downloaded
# class_labels_indices.csv next to the model (verified to match the model's
# output indices). Output `clip_scores` are already sigmoid (multi-label, NOT
# softmax — they do not sum to 1).
#
# NOT gated on the transcript: a clip that is only coughing has no speech but
# still has events worth surfacing. The generic speech umbrella labels are
# dropped (the transcript already conveys speech); everything else is kept.
_AUDIO_EVENTS_SPEECH_UMBRELLA = {
    "Speech", "Male speech, man speaking", "Female speech, woman speaking",
    "Child speech, kid speaking", "Conversation", "Narration, monologue",
}
_AUDIO_EVENTS_MIN_SCORE = 0.10
_AUDIO_EVENTS_MAX = 6


def _audio_events_dir():
    return os.path.join(_STT_VENV, "models", "audio_events")


def _audio_events_labels():
    """index -> display_name from the AudioSet label CSV beside the model."""
    import csv
    out = {}
    path = os.path.join(_audio_events_dir(), "class_labels_indices.csv")
    with open(path) as f:
        for row in csv.DictReader(f):
            out[int(row["index"])] = row["display_name"]
    return out


def _audio_events(bundle, ctx):
    """Tag non-speech sounds & ambience via PANNs CNN14 (AudioSet, 16kHz ONNX).

    onnxruntime + numpy only (no torch); the mel is computed in-graph, so the
    raw 16k waveform goes straight in. Reports the highest-scoring events above
    a threshold, minus the speech umbrella. A hint, not reliable — top-1 can be
    wrong for short or overlapping sounds."""
    model = os.path.join(_audio_events_dir(), "Cnn14_16k.onnx")
    if not os.path.isfile(model):
        return {}
    samples = bundle.samples
    if samples.size < bundle.sr * _MIN_SECONDS:
        return {}

    import numpy as np
    import onnxruntime as ort
    x = samples.astype("float32")[None, :]
    with _heavy_lock():  # one heavy model resident at a time (anti-OOM)
        sess = ort.InferenceSession(model,
                                    providers=["CPUExecutionProvider"])
        name = sess.get_inputs()[0].name
        scores = sess.run(["clip_scores"], {name: x})[0][0]
    labels = _audio_events_labels()
    events = []
    for i in (int(j) for j in np.argsort(scores)[::-1]):  # high → low
        s = float(scores[i])
        if s < _AUDIO_EVENTS_MIN_SCORE or len(events) >= _AUDIO_EVENTS_MAX:
            break  # sorted descending: nothing below the threshold remains
        label = labels.get(i, str(i))
        if label in _AUDIO_EVENTS_SPEECH_UMBRELLA:
            continue  # transcript already conveys speech
        events.append({"label": label, "score": _round(s, 3)})
    if not events:
        return {}
    return {
        "events": events,
        "model": "PANNs CNN14 (AudioSet 527, 16kHz)",
        "note": ("acoustic event tags over the whole clip (non-speech sounds "
                 "and ambience); a hint, not reliable — the top guess can be "
                 "wrong for short or overlapping sounds"),
    }


_ANALYZERS = {
    "prosody": _prosody,
    "emotion": _emotion,
    "audio_events": _audio_events,
}


def run(audio_path, ids, text=None):
    """Run each requested worker analyzer once. Returns ``{id: section}``,
    omitting any analyzer that is unknown, errors, or yields nothing.

    ``text`` is the whisper transcript, passed so speech-gated analyzers (e.g.
    emotion) can skip non-speech audio. None means "not provided — don't gate"."""
    ids = [i for i in (ids or []) if i in _ANALYZERS]
    if not ids:
        return {}
    bundle = AudioBundle(audio_path)
    ctx = {"text": text}
    out = {}
    for i in ids:
        try:
            section = _ANALYZERS[i](bundle, ctx)
        except Exception as e:  # noqa: BLE001 — never block the turn
            print(f"[speech_worker] {i} failed: {e}",
                  file=sys.stderr, flush=True)
            continue
        finally:
            # Heavy analyzers load a model each; reclaim it before the next one
            # so two model footprints are never co-resident (anti-OOM, §5).
            gc.collect()
        if section:
            out[i] = section
    return out


if __name__ == "__main__":  # manual: python speech_worker.py <clip> prosody emotion
    import json
    _ids = sys.argv[2:] or ["prosody"]
    print(json.dumps(run(sys.argv[1], _ids), ensure_ascii=False, indent=2))
