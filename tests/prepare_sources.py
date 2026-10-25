"""Conform source clips for the Reel batches (runbook §1.5).

MiniMax H3 wants 17k+5 frames at 24 fps on one canvas, and Reel Guide wants the audio latent to
sit exactly on the global boundary: for 124 frames that is 207 audio-latent frames at 40 Hz, and
the H3 audio VAE floors, so the audio must be a hair longer than the video — [5.175, 5.2) s.
Learned the hard way on 2026-10-25 (206 != 207).

    python tests/prepare_sources.py <clip> [<clip> ...] [--name char1] [--frames 124]
                                    [--size 768x1344] [--out ../../input/promptdeck_tests/sources]

Writes <out>/<name>_5s.mp4 (video: libx264 crf 14, exactly --frames frames at 24 fps, scaled and
center-cropped to --size; audio: aac 192k, padded/trimmed to frames/24 + 0.0133 s). Needs ffmpeg
and ffprobe on PATH.
"""
import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FPS = 24
AUDIO_FPS = 40


SAMPLE_RATE = 32000
SAMPLES_PER_TOKEN = 800  # H3 audio VAE: 32 kHz / 40 Hz


def audio_samples(frames):
    """Exactly tokens x 800 samples at 32 kHz, as DaSiWa's importer does (video_source.encode_source):
    pad with silence if the source is shorter, trim if longer, never a loose fraction of a token."""
    return round(frames * AUDIO_FPS / FPS) * SAMPLES_PER_TOKEN   # 124 -> 207 x 800 = 165600


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate,nb_frames", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=duration", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    return out, a


def conform(src, dst, frames, size):
    if frames < 5 or frames % 17 != 5:
        raise SystemExit(f"{frames} frames is not on the 17k+5 grid (5, 22, 39, ..., 124, ...)")
    w, h = size.split("x")
    samples = audio_samples(frames)
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS}"
    # Three passes: ffmpeg stops at -frames:v and drops the padded audio tail when both streams
    # share one run, so cut video and audio separately and mux. pcm, not aac: an aac encoder adds
    # priming samples and the exact sample count is the point.
    dst = dst.replace(".mp4", ".mov")
    base = dst[:-4]
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-an", "-vf", vf, "-frames:v", str(frames),
                    "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", base + ".video.mp4"], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn",
                    "-af", f"aresample={SAMPLE_RATE},apad,atrim=end_sample={samples}",
                    "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), base + ".audio.wav"], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", base + ".video.mp4", "-i", base + ".audio.wav",
                    "-map", "0:v", "-map", "1:a", "-c", "copy", dst], check=True)
    for tmp in (base + ".video.mp4", base + ".audio.wav"):
        os.remove(tmp)
    v, a = probe(dst)
    print(f"{os.path.basename(dst)}: video {v}  audio {a} s  (target {samples} samples = {samples / SAMPLE_RATE:.4f} s)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--name", help="output stem; default: char1, char2, ... in order")
    ap.add_argument("--frames", type=int, default=124)
    ap.add_argument("--size", default="768x1344")
    ap.add_argument("--out", default=os.path.join(REPO, "..", "..", "input", "promptdeck_tests", "sources"))
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    for i, clip in enumerate(args.clips, 1):
        name = args.name if args.name and len(args.clips) == 1 else f"char{i}"
        conform(clip, os.path.join(args.out, f"{name}_5s.mp4"), args.frames, args.size)


if __name__ == "__main__":
    sys.exit(main())
