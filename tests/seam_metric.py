"""Objective seam trace: per-frame mean absolute difference (motion/jump) and audio RMS per 10 ms,
reported around a seam time versus the rest of the clip. A visual cut shows as a MAD spike at the
seam that exceeds the clip's own 99th percentile; an audio hole shows as an RMS dip.

    python tests/seam_metric.py <clip> [--seam 5.0]
"""
import argparse, sys
import numpy as np
import av


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("clip"); ap.add_argument("--seam", type=float, default=5.0)
    a = ap.parse_args(argv)
    c = av.open(a.clip)
    vs = c.streams.video[0]
    prev, mads, times = None, [], []
    for fr in c.decode(vs):
        img = fr.to_ndarray(format="gray", width=192, height=336).astype(np.float32)
        t = float(fr.pts * vs.time_base)
        if prev is not None:
            mads.append(float(np.abs(img - prev).mean())); times.append(t)
        prev = img
    c.close()
    mads, times = np.array(mads), np.array(times)
    fps = 1 / np.median(np.diff(times))
    i = int(np.argmin(np.abs(times - a.seam)))
    win = mads[max(0, i - 3): i + 4]
    rest = np.delete(mads, np.arange(max(0, i - 3), min(len(mads), i + 4)))
    print(f"video: {len(mads)+1} frames @ {fps:.1f} fps; frame->frame MAD: median {np.median(rest):.2f}, p99 {np.percentile(rest, 99):.2f}, max elsewhere {rest.max():.2f}")
    for k in range(max(0, i - 3), min(len(mads), i + 4)):
        flag = "  <-- seam" if k == i else ""
        print(f"   {times[k]:.3f}s MAD {mads[k]:.2f}{flag}")
    ratio = mads[i] / max(1e-6, np.percentile(rest, 99))
    print(f"seam MAD / p99 elsewhere = {ratio:.2f}  ({'JUMP' if ratio > 1.5 else 'no visual jump'})")
    c = av.open(a.clip)
    if c.streams.audio:
        ast = c.streams.audio[0]
        chunks = [f.to_ndarray().astype(np.float32) for f in c.decode(ast)]
        c.close()
        x = np.concatenate(chunks, axis=-1)
        if x.ndim == 2: x = x.mean(axis=0)
        sr = ast.rate; hop = sr // 100
        rms = np.sqrt(np.convolve(x**2, np.ones(hop) / hop, mode="valid")[::hop])
        t = np.arange(len(rms)) * hop / sr
        j = int(np.argmin(np.abs(t - a.seam)))
        seg = rms[max(0, j - 8): j + 9]
        floor = np.percentile(rms[rms > 0], 5) if (rms > 0).any() else 0
        print(f"audio: {sr} Hz; RMS 10 ms windows around seam (min {seg.min():.4f}, median clip {np.median(rms):.4f}, 5th pct {floor:.4f}):")
        print("   " + " ".join(f"{v:.3f}" for v in seg))
        dip = seg.min() < 0.25 * np.median(rms)
        print(f"audio hole at seam: {'YES' if dip else 'no'}")
    else:
        print("audio: none")


if __name__ == "__main__":
    sys.exit(main())
