#!/usr/bin/env python3
"""Extract scene-change frames from a video with PyAV — no system ffmpeg.

PyAV (the `av` package, installed in .venv-stt alongside faster-whisper)
bundles its own ffmpeg codecs, so we decode + sample frames in pure Python.
Runs under the `.venv-stt` interpreter, invoked as a subprocess by frames.py.

Scene detection: normalized mean-absolute RGB diff (on a 64x64 downscale)
between the current frame and the last KEPT frame. Above `thr` → a new
scene. The first frame is always kept. Frames are evaluated at most every
`min_interval` seconds (so a long static stretch isn't scanned frame by
frame) and capped at `max_frames`.

Usage:
    python extract_frames.py <video> <out_dir> [max] [thr] [min_interval]

Prints one JSON line:
    {"frames": [{"path": "...", "t": 1.5}], "duration": 12.3}
or {"error": "..."} (exit non-zero).
"""
import json
import os
import sys


def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: extract_frames.py <video> <out_dir>"}))
        sys.exit(1)
    video, out_dir = sys.argv[1], sys.argv[2]
    max_frames = int(sys.argv[3]) if len(sys.argv) > 3 \
        else int(os.environ.get("FRAME_MAX", "8"))
    thr = float(sys.argv[4]) if len(sys.argv) > 4 \
        else float(os.environ.get("FRAME_SCENE_THRESHOLD", "0.15"))
    min_interval = float(sys.argv[5]) if len(sys.argv) > 5 \
        else float(os.environ.get("FRAME_MIN_INTERVAL", "0.4"))
    try:
        import av
        import numpy as np
        os.makedirs(out_dir, exist_ok=True)
        cont = av.open(video)
        kept = []
        prev = None        # last KEPT frame (downscaled), for diffing
        last_eval = -1e9    # gate evaluation to every min_interval seconds
        duration = 0.0
        for frame in cont.decode(video=0):
            t = (float(frame.pts * frame.time_base)
                 if frame.pts is not None else last_eval)
            duration = max(duration, t)
            if t - last_eval < min_interval:
                continue
            last_eval = t
            small = frame.reformat(width=64, height=64,
                                   format="rgb24").to_ndarray().astype("float32")
            if prev is None:
                keep = True
            else:
                keep = (np.abs(small - prev).mean() / 255.0) > thr
            if keep:
                path = os.path.join(out_dir, f"frame_{len(kept):03d}.jpg")
                frame.to_image().save(path, "JPEG", quality=80)
                kept.append({"path": path, "t": round(t, 2)})
                prev = small
                if len(kept) >= max_frames:
                    break
        cont.close()
        print(json.dumps({"frames": kept, "duration": round(duration, 2)}))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
