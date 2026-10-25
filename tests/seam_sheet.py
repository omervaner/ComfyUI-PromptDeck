"""Contact sheet of frames around a seam, for judging a join by eye.

    python tests/seam_sheet.py <clip.mp4> [--seam 5.0] [--out sheet.png]

Writes a 2x5 grid: 4.50, 4.75, 4.90, 4.96 (last frame before the seam at 24 fps), 5.00,
5.04, 5.10, 5.25, 5.50, 6.00 s, each labelled. Needs ffmpeg on PATH.
"""
import argparse
import os
import subprocess
import sys
import tempfile

OFFSETS = (-0.5, -0.25, -0.1, -0.042, 0.0, 0.042, 0.1, 0.25, 0.5, 1.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip")
    ap.add_argument("--seam", type=float, default=5.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--width", type=int, default=384, help="tile width")
    args = ap.parse_args(argv)
    out = args.out or os.path.splitext(args.clip)[0] + f"_seam{args.seam:.1f}.png"
    with tempfile.TemporaryDirectory() as tmp:
        tiles = []
        for i, off in enumerate(OFFSETS):
            t = max(0.0, args.seam + off)
            tile = os.path.join(tmp, f"t{i:02d}.png")
            label = f"{t:.3f}s ({off:+.3f})"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", args.clip, "-frames:v", "1",
                            "-vf", f"scale={args.width}:-2,drawtext=text='{label}':x=8:y=8:fontsize=20:"
                                   "fontcolor=white:box=1:boxcolor=black@0.6",
                            tile], check=True)
            tiles.append(tile)
        inputs = sum((["-i", t] for t in tiles), [])
        subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs,
                        "-filter_complex", f"{''.join(f'[{i}:v]' for i in range(len(tiles)))}xstack=inputs={len(tiles)}:"
                        "layout=0_0|w0_0|w0+w1_0|w0+w1+w2_0|w0+w1+w2+w3_0|0_h0|w0_h0|w0+w1_h0|w0+w1+w2_h0|w0+w1+w2+w3_h0",
                        out], check=True)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
