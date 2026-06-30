#!/usr/bin/env python3
"""MERaLiON-SER inference worker — runs under the SEPARATE .venv-ser (torch),
NOT the bot's venv or .venv-stt (#126). Called by ``speech_worker._emotion_accurate``
as: ``<.venv-ser>/bin/python speech_ser.py <samples.npy>`` where the .npy is a
float32 mono 16 kHz waveform. Prints ONE JSON line to stdout:

    {"label": "anger", "confidence": 0.82, "top": [...], "energy": 0.77,
     "model": "MERaLiON-SER (multilingual)"}

The categorical head names the emotion (7 classes); we also surface arousal
(energy) from the dimensional head. Valence is deliberately NOT reported —
positivity comes from the transcript words (text+audio fusion lives in Claude).
Everything heavy is imported lazily; nothing but the JSON goes to stdout."""
import json
import os
import sys

REPO = "MERaLiON/MERaLiON-SER-v1"
# MERaLiON id2label -> our canonical labels
CANON = {"neutral": "neutral", "happy": "happy", "sad": "sad", "angry": "anger",
         "fearful": "fear", "disgusted": "disgust", "surprised": "surprise"}


def main():
    if len(sys.argv) < 2 or not os.path.isfile(sys.argv[1]):
        print("{}")
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")     # model already downloaded
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import numpy as np
    import torch
    from transformers import AutoModelForAudioClassification, AutoProcessor

    samples = np.load(sys.argv[1]).astype("float32")
    proc = AutoProcessor.from_pretrained(REPO, trust_remote_code=True)
    model = AutoModelForAudioClassification.from_pretrained(
        REPO, trust_remote_code=True, low_cpu_mem_usage=False)
    model.eval()
    id2label = {int(k): v for k, v in getattr(model.config, "id2label", {}).items()}

    inputs = proc(samples, sampling_rate=16000, return_tensors="pt",
                  return_attention_mask=True)
    feed = {k: v for k, v in inputs.items()
            if k in ("input_features", "attention_mask", "input_values")}
    with torch.inference_mode():
        out = model(**feed)
    logits = (out["logits"] if isinstance(out, dict) else out.logits)[0]
    dims = (out["dims"][0] if isinstance(out, dict) and "dims" in out
            else getattr(out, "dims", [None])[0])

    probs = torch.softmax(logits, -1).tolist()
    order = sorted(range(len(probs)), key=lambda i: probs[i], reverse=True)
    top = [{"emotion": CANON.get(id2label.get(i, str(i)), id2label.get(i, str(i))),
            "p": round(float(probs[i]), 2)} for i in order[:3]]
    # dims order per model card: [valence, arousal, dominance] -> energy = arousal
    energy = round(float(dims[1]), 2) if dims is not None else None
    print(json.dumps({"label": top[0]["emotion"], "confidence": top[0]["p"],
                      "top": top, "energy": energy,
                      "model": "MERaLiON-SER (multilingual)"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
