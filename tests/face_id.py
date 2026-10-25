"""Deterministic identity check for judged items: ArcFace cosine between a reference face and
the faces in each item's extracted frames (frames/*.jpg written by overnight.py's judge).

    python tests/face_id.py <batch_dir> --ref <image> [--provider cpu|cuda] [--model buffalo_l]

Writes <item>/face_id.json ({"ref": ..., "frames": {label: cosine|null}, "mean": x, "min": x,
"seam_min": x}) and <batch_dir>/face_id.csv. A VLM judge is unreliable at "same person?";
this is the tie-breaker Omer asked for (2026-10-25). Cosine > ~0.5 on buffalo_l is the same
person in practice; < 0.3 is a different one.
"""
import argparse
import csv
import json
import os
import sys

import cv2
import numpy as np


def load_app(model, provider):
    from insightface.app import FaceAnalysis
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if provider == "cuda" else ["CPUExecutionProvider"]
    app = FaceAnalysis(name=model, providers=providers)
    app.prepare(ctx_id=0 if provider == "cuda" else -1, det_size=(640, 640))
    return app


def embed(app, path):
    img = cv2.imread(path)
    if img is None:
        return None
    faces = app.get(img)
    if not faces:
        return None
    face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    v = face.normed_embedding
    return v / (np.linalg.norm(v) + 1e-9)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch_dir")
    ap.add_argument("--ref", required=True, help="reference picture of the person")
    ap.add_argument("--provider", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--model", default="buffalo_l")
    args = ap.parse_args(argv)

    app = load_app(args.model, args.provider)
    ref = embed(app, args.ref)
    if ref is None:
        sys.exit(f"no face found in reference {args.ref}")

    rows = []
    for item in sorted(os.listdir(args.batch_dir)):
        fdir = os.path.join(args.batch_dir, item, "frames")
        if not os.path.isdir(fdir):
            continue
        sims = {}
        for fn in sorted(os.listdir(fdir)):
            if not fn.lower().endswith((".jpg", ".png")):
                continue
            e = embed(app, os.path.join(fdir, fn))
            sims[os.path.splitext(fn)[0]] = None if e is None else round(float(np.dot(ref, e)), 3)
        vals = [v for v in sims.values() if v is not None]
        seam_vals = [v for k, v in sims.items() if k.startswith("seam") and v is not None]
        rec = {"ref": os.path.abspath(args.ref), "model": args.model, "frames": sims,
               "faces_found": len(vals), "frames_total": len(sims),
               "mean": round(float(np.mean(vals)), 3) if vals else None,
               "min": round(float(min(vals)), 3) if vals else None,
               "seam_min": round(float(min(seam_vals)), 3) if seam_vals else None,
               "seam_mean": round(float(np.mean(seam_vals)), 3) if seam_vals else None}
        with open(os.path.join(args.batch_dir, item, "face_id.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
        rows.append({"id": item, **{k: rec[k] for k in ("faces_found", "frames_total", "mean", "min", "seam_mean", "seam_min")}})
        print(f"{item}: faces {rec['faces_found']}/{rec['frames_total']} mean {rec['mean']} min {rec['min']} seam_min {rec['seam_min']}")

    if rows:
        with open(os.path.join(args.batch_dir, "face_id.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {os.path.join(args.batch_dir, 'face_id.csv')} ({len(rows)} items)")


if __name__ == "__main__":
    main()
