#!/usr/bin/env python3
"""Transcribe an audio file with faster-whisper.

Runs under the dedicated `.venv-stt` interpreter (NOT the bot's main venv),
invoked as a subprocess by `stt.py`. Keeping faster-whisper out of the main
process honours the project rule that the bot's venv stays light
(requests + python-dotenv only).

Usage:
    python transcribe.py <audio_path> [model_name]

Prints one JSON line to stdout:
    {"text": "...", "segments": [{"start":s,"end":e,"text":t}], "language":"ru"}
or on failure:
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
    model_name = (sys.argv[2] if len(sys.argv) > 2
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
        print(json.dumps({"text": text, "segments": segs,
                          "language": info.language}, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — report any failure as JSON
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
