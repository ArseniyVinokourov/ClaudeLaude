"""Speech analysis layer (#126) — optional, modular "how it was said" signals.

The transcript text still goes to Claude unchanged. When any analyzer is
enabled (``SPEECH_ANALYZERS`` in .env, toggled from /settings), the full
analysis is written to a JSON file in ``rt.upload_dir`` and referenced to Claude
as an ``[Attached file: ...]`` — so the turn text stays clean and Claude reads
the detail on demand.

Design: a small registry of analyzers. Each declares an id, a human title for
the settings menu, whether extra packages must be installed, an availability
check, and a ``run(result, audio_path) -> dict`` returning ITS signals only.
The runner merges whatever is enabled AND available; an analyzer that raises is
dropped (its section is simply absent) so one failure never blocks the turn.

Phase 1 ships one analyzer: ``timing`` — speech tempo, pauses and rhythm,
computed purely from the Whisper word timestamps. No extra dependency, no model,
runs in-process. Heavier analyzers (voice acoustics, emotion, audio events) plug
into the same registry in later phases and run via the ``.venv-stt`` worker.
"""
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


# ── registry ───────────────────────────────────────────────────────────
_REGISTRY = [
    {"id": "timing", "title": "Tempo & pauses", "needs_install": False,
     "available": _timing_available, "run": _timing_run},
]


def registry():
    """Descriptors for the settings menu: id, title, needs_install, available."""
    return [{"id": a["id"], "title": a["title"],
             "needs_install": a["needs_install"], "available": a["available"]()}
            for a in _REGISTRY]


def _by_id(aid):
    return next((a for a in _REGISTRY if a["id"] == aid), None)


def active_analyzers():
    """Enabled analyzer ids from SPEECH_ANALYZERS (.env, csv). Read live so a
    /settings toggle takes effect without a restart."""
    raw = os.environ.get("SPEECH_ANALYZERS", "")
    return [x for x in (s.strip() for s in raw.split(",")) if x]


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
