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
import sys

_TARGET_SR = 16000
# Below this we have too little voiced signal for stable pitch/jitter stats.
_MIN_SECONDS = 0.3


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
def _prosody(bundle):
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


_ANALYZERS = {
    "prosody": _prosody,
}


def run(audio_path, ids):
    """Run each requested worker analyzer once. Returns ``{id: section}``,
    omitting any analyzer that is unknown, errors, or yields nothing."""
    ids = [i for i in (ids or []) if i in _ANALYZERS]
    if not ids:
        return {}
    bundle = AudioBundle(audio_path)
    out = {}
    for i in ids:
        try:
            section = _ANALYZERS[i](bundle)
        except Exception as e:  # noqa: BLE001 — never block the turn
            print(f"[speech_worker] {i} failed: {e}",
                  file=sys.stderr, flush=True)
            continue
        if section:
            out[i] = section
    return out


if __name__ == "__main__":  # manual: python speech_worker.py <clip> prosody
    import json
    _ids = sys.argv[2:] or ["prosody"]
    print(json.dumps(run(sys.argv[1], _ids), ensure_ascii=False, indent=2))
