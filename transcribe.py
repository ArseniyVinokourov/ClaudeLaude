#!/usr/bin/env python3
"""Transcribe an audio file with faster-whisper.

Runs under the dedicated `.venv-stt` interpreter (NOT the bot's main venv),
invoked as a subprocess by `stt.py`. Keeping faster-whisper out of the main
process honours the project rule that the bot's venv stays light
(requests + python-dotenv only).

Usage:
    python transcribe.py <audio_path> [model_name] [--analyzers=id,id]

Prints one JSON line to stdout:
    {"text": "...", "segments": [{"start":s,"end":e,"text":t}], "language":"ru"}
With --analyzers, each worker analyzer's section is merged in under its id
(e.g. "prosody": {...}) — computed in this venv by speech_worker (#126).
On failure:
    {"error": "..."}   (and exits non-zero)
"""
import json
import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "no audio path given"}))
        sys.exit(1)
    audio = sys.argv[1]
    # Positional model name + optional --analyzers= flag, order-independent.
    analyzer_ids: list[str] = []
    positional: list[str] = []
    for arg in sys.argv[2:]:
        if arg.startswith("--analyzers="):
            analyzer_ids = [x for x in arg.split("=", 1)[1].split(",") if x]
        else:
            positional.append(arg)
    model_name = (positional[0] if positional
                  else os.environ.get("WHISPER_MODEL", "small"))
    if not os.path.isfile(audio):
        print(json.dumps({"error": f"file not found: {audio}"}))
        sys.exit(1)
    try:
        from faster_whisper import WhisperModel
        # int8 on CPU: small footprint, no GPU needed. Model is cached in
        # ~/.cache/huggingface after first download (setup.sh pre-fetches it).
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        # vad_filter drops long silences so a quiet recording isn't padded.
        # word_timestamps gives per-word start/end — needed for pause/tempo
        # analysis (#126) and measured to add no cost (often faster).
        segments, info = model.transcribe(audio, vad_filter=True,
                                          word_timestamps=True)
        segs = []
        for s in segments:
            words = [{"word": w.word, "start": round(w.start, 2),
                      "end": round(w.end, 2)} for w in (s.words or [])]
            segs.append({"start": round(s.start, 2), "end": round(s.end, 2),
                         "text": s.text.strip(), "words": words})
        text = " ".join(s["text"] for s in segs).strip()
        out = {"text": text, "segments": segs, "language": info.language}
        # Worker-side speech analyzers (#126) — heavy ones live here in the
        # side-venv. A failure inside never aborts transcription: the section
        # is simply absent. stdout must stay the single JSON line below, so
        # speech_worker logs only to stderr.
        if analyzer_ids:
            try:
                import gc
                import speech_worker
                # Free the whisper model before loading any heavy analyzer
                # model (e.g. emotion ~200MB) — they shouldn't be co-resident
                # on a small box. The transcript is already in `out`.
                del model
                gc.collect()
                out.update(speech_worker.run(audio, analyzer_ids, text=text))
            except Exception as e:  # noqa: BLE001
                print(f"[transcribe] analyzers failed: {e}",
                      file=sys.stderr, flush=True)
        print(json.dumps(out, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — report any failure as JSON
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
