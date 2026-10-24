"""Reel: latent-level time editing for MiniMax H3 (extend, prequel, bridge, loop).

Two nodes around any stock sampler:

    Reel Guide  -> positive + zero window + reel_ctx      (before the sampler)
    Reel Splice -> cumulative latent + new tokens + frames (after the sampler)

The mechanism is the one every H3 continuation uses: slice anchor tokens straight
out of stored latents (no VAE round trip), pin them into a fresh zero window as
`minimax_keyframes` exactly as native MiniMaxH3AddGuide would, sample the whole
window, then drop the anchored overlap and concatenate on the model's 17k+5 grid.

Grid facts (verified against comfy_extras/nodes_minimax_h3.py and the MIT
continuation reference by ttulttul):
- pixel frames = 17k + 5, video tokens = 5k + 2 (token 0 of each group of 5
  spans 1 frame, the other four span 4 frames each)
- audio latent runs at 40 Hz; its index at pixel frame f is round(f * 40 / 24),
  always computed on the GLOBAL timeline so A/V never drifts a tick per append
- anchors (tail / head) are 17k+5 frames long: 5, 22, 39, 56, 73
- a bridge window is tail + gap + head and must itself be 17k+5, so the gap is
  17m - 5 frames (12, 29, 46, ...); extend / prequel gaps are multiples of 17
- 362 frames (~15 s) is the trained range; one window never exceeds it
- a loop cannot be perfectly periodic on this grid (the film's 5-frame stub never
  divides by 17), so Reel keeps the window's regenerated head: the film ends with a
  copy of its first `head_frames`, and `loop_point_frame` says where the exporter cuts
"""

import json
import math
import os
import time

import torch

try:  # inside ComfyUI
    from comfy.nested_tensor import NestedTensor
except Exception:  # pragma: no cover - tests import it from the sandbox themselves
    NestedTensor = None

try:
    import folder_paths
except Exception:  # pragma: no cover
    folder_paths = None

try:
    from comfy.utils import save_torch_file as _save_torch_file
except Exception:  # pragma: no cover
    _save_torch_file = None

try:
    import comfy.model_management as _mm
except Exception:  # pragma: no cover
    _mm = None


FPS = 24
AUDIO_FPS = 40
FRAME_RESCALE = AUDIO_FPS / FPS  # 5/3, the same constant the DiT packs with
VIDEO_CH = 24
AUDIO_CH = 32
AUDIO_STEREO = 2
MAX_WINDOW_FRAMES = 362
ANCHOR_FRAMES = ("5", "22", "39", "56", "73")
OPS = ["extend", "prequel", "bridge", "loop", "cut", "polish"]
CTX_TYPE = "PROMPTDECK_REEL_CTX"


# ---------------------------------------------------------------- grid helpers

def audio_t(frame: int) -> int:
    """Cumulative 40 Hz audio-latent index at a 24 fps frame boundary."""
    if frame < 0:
        raise ValueError("frame index must not be negative")
    return int(round(frame * AUDIO_FPS / FPS))


def video_t(frames: int) -> int:
    """Video tokens for an aligned pixel frame count."""
    if frames < 5 or frames % 17 != 5:
        raise ValueError(f"{frames} frames is not on the MiniMax H3 17k+5 grid")
    return 2 if frames == 5 else ((frames - 5) // 17) * 5 + 2


def frames_of(tokens: int) -> int:
    """Pixel frames represented by a video token count."""
    if tokens < 2 or (tokens - 2) % 5 != 0:
        raise ValueError(f"{tokens} video tokens is not on the MiniMax H3 5k+2 grid")
    return ((tokens - 2) // 5) * 17 + 5


def snap_extension(seconds: float) -> int:
    """New frames for extend / prequel: a multiple of 17, at least 17."""
    return max(17, int(math.floor(seconds * FPS / 17 + 0.5)) * 17)


def snap_gap(seconds: float) -> int:
    """New frames for bridge / loop: 17m - 5 so tail + gap + head lands on 17k+5."""
    m = max(1, int(math.floor((seconds * FPS + 5) / 17 + 0.5)))
    return 17 * m - 5


def snap_first(seconds: float) -> int:
    """Frames for a first segment with no anchors: 17k + 5, at least 22."""
    k = max(1, int(math.floor((seconds * FPS - 5) / 17 + 0.5)))
    return 17 * k + 5


# ---------------------------------------------------------------- latent access

def _streams(latent, name: str):
    """(video, audio, frames) of an H3 AV latent, without checking the audio length."""
    samples = latent.get("samples") if isinstance(latent, dict) else None
    if samples is None or not getattr(samples, "is_nested", False) or len(samples.tensors) != 2:
        raise ValueError(f"{name} is not a MiniMax H3 AV latent (expected a video+audio NestedTensor)")
    video, audio = samples.tensors
    if video.ndim != 5 or video.shape[1] != VIDEO_CH:
        raise ValueError(f"{name} video latent must be [B, {VIDEO_CH}, T, H/16, W/16]")
    if audio.ndim != 4 or audio.shape[1] != AUDIO_CH or audio.shape[2] != AUDIO_STEREO:
        raise ValueError(f"{name} audio latent must be [B, {AUDIO_CH}, {AUDIO_STEREO}, T]")
    try:
        frames = frames_of(video.shape[2])
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from exc
    return video, audio, frames


def _film(latent, name: str):
    """A finished clip: audio length must sit exactly on the global boundary."""
    video, audio, frames = _streams(latent, name)
    expected = audio_t(frames)
    if audio.shape[-1] != expected:
        raise ValueError(
            f"{name} audio has {audio.shape[-1]} latent frames but {frames} video frames need "
            f"{expected}. Run it through Reel Splice (or re-encode) before using it as a film.")
    return video, audio, frames


def _nested(video, audio):
    if NestedTensor is None:
        raise RuntimeError("comfy.nested_tensor is not importable; Reel needs ComfyUI with MiniMax H3 support")
    return {"samples": NestedTensor((video, audio))}


def _set_cond(conditioning, values: dict):
    """Same semantics as node_helpers.conditioning_set_values, without the import."""
    out = []
    for entry in conditioning:
        meta = dict(entry[1])
        meta.update(values)
        out.append([entry[0], meta])
    return out


def _guide_frames(keyframe: dict) -> int:
    latent = keyframe.get("latent")
    if isinstance(latent, torch.Tensor) and latent.ndim == 5 and latent.shape[2] > 1:
        try:
            return frames_of(latent.shape[2])
        except ValueError:
            return 1
    return 1


# ---------------------------------------------------------------- the plan

def plan(op: str, new_seconds: float, tail_frames: int, head_frames: int,
         previous_frames: int, next_frames: int) -> dict:
    """Resolve every token count for one window. Pure arithmetic, no tensors."""
    P, Q = int(previous_frames), int(next_frames)
    a, b = int(tail_frames), int(head_frames)
    for value, label in ((a, "tail_frames"), (b, "head_frames")):
        if value < 5 or value % 17 != 5:
            raise ValueError(f"{label} must be one of 5, 22, 39, 56, 73")

    if op == "extend" and P == 0:
        n = snap_first(new_seconds)
        a = b = 0
        W = n
        tail_vt = head_vt = tail_at = head_at = 0
        new_vt = video_t(W)
        new_at = audio_t(W)
        head_frame = 0
        total = W
    elif op == "extend":
        if a > P:
            raise ValueError(f"tail_frames {a} is longer than the previous clip ({P} frames)")
        b = 0
        n = snap_extension(new_seconds)
        W = a + n
        tail_vt, head_vt = video_t(a), 0
        new_vt = n // 17 * 5
        tail_at = audio_t(P) - audio_t(P - a)
        new_at = audio_t(P + n) - audio_t(P)
        head_at = 0
        head_frame = 0
        total = P + n
    elif op == "prequel":
        if P == 0:
            raise ValueError("prequel needs the film in `previous`")
        if b > P:
            raise ValueError(f"head_frames {b} is longer than the previous clip ({P} frames)")
        a = 0
        n = snap_extension(new_seconds)
        W = n + b
        tail_vt, head_vt = 0, video_t(b)
        new_vt = n // 17 * 5
        tail_at = 0
        new_at = audio_t(n)
        head_at = audio_t(n + b) - audio_t(n)
        head_frame = n
        total = n + P
    elif op in ("bridge", "loop"):
        if P == 0:
            raise ValueError(f"{op} needs the film in `previous`")
        if op == "bridge" and Q == 0:
            raise ValueError("bridge needs the clip to the right in `next`")
        if op == "loop":
            Q = P
        if a > P:
            raise ValueError(f"tail_frames {a} is longer than the previous clip ({P} frames)")
        if b > Q:
            raise ValueError(f"head_frames {b} is longer than the next clip ({Q} frames)")
        n = snap_gap(new_seconds)
        W = a + n + b
        tail_vt, head_vt = video_t(a), video_t(b)
        new_vt = video_t(W) - tail_vt - head_vt
        tail_at = audio_t(P) - audio_t(P - a)
        new_at = audio_t(P + n) - audio_t(P)
        head_at = audio_t(P + n + b) - audio_t(P + n)
        head_frame = a + n
        # bridge keeps the next clip; loop keeps the window's regenerated head too, because
        # previous + gap alone (17k+5 + 17m-5) is never on the 17k+5 grid. The loop point is
        # P + n: the last `b` frames duplicate the film's first `b` frames for the exporter to cut.
        total = P + n + (Q if op == "bridge" else b)
    elif op in ("cut", "polish"):
        raise NotImplementedError(f"Reel `{op}` arrives with the strip UI; v1 does extend, prequel, bridge and loop")
    else:
        raise ValueError(f"unknown Reel op {op!r}")

    if W % 17 != 5:
        raise AssertionError(f"window of {W} frames left the 17k+5 grid (bug)")
    if W > MAX_WINDOW_FRAMES:
        raise ValueError(
            f"window would be {W} frames (tail {a} + new {n} + head {b}); the limit is {MAX_WINDOW_FRAMES} "
            f"(~15 s). Shorten new_seconds or use smaller anchors.")
    window_vt = video_t(W)
    if tail_vt + new_vt + head_vt != window_vt:
        raise AssertionError("token split does not add up (bug)")
    return {
        "op": op,
        "tail_frames": a, "head_frames": b, "new_frames": n, "window_frames": W,
        "tail_video_t": tail_vt, "tail_audio_t": tail_at,
        "new_video_t": new_vt, "new_audio_t": new_at,
        "head_video_t": head_vt, "head_audio_t": head_at,
        "window_video_t": window_vt, "window_audio_t": tail_at + new_at + head_at,
        "head_frame": head_frame,
        "loop_point_frame": (P + n) if op == "loop" else None,
        "previous_frames": P, "next_frames": Q if op in ("bridge", "loop") else int(next_frames),
        "total_frames": total,
    }


# ---------------------------------------------------------------- nodes

class ReelGuide:
    CATEGORY = "PromptDeck"
    FUNCTION = "guide"
    RETURN_TYPES = ("CONDITIONING", "LATENT", CTX_TYPE)
    RETURN_NAMES = ("positive", "latent", "reel_ctx")
    DESCRIPTION = (
        "Prepares one MiniMax H3 window: pins the previous clip's tail and/or the next clip's "
        "head as native guides inside a fresh zero latent. Sample it with any sampler, then "
        "feed the result to Reel Splice. With no previous clip it makes a plain first segment."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "positive": ("CONDITIONING",),
                "op": (OPS, {"default": "extend", "tooltip": "extend: after previous. prequel: before previous. "
                             "bridge: between previous and next. loop: previous back to its own start."}),
                "tail_frames": (list(ANCHOR_FRAMES), {"default": "22",
                                "tooltip": "Frames of the previous clip's tail pinned at the window start (extend, bridge, loop)."}),
                "head_frames": (list(ANCHOR_FRAMES), {"default": "22",
                                "tooltip": "Frames of the next clip's head pinned at the window end (prequel, bridge, loop)."}),
                "new_seconds": ("FLOAT", {"default": 5.0, "min": 0.5, "max": 15.0, "step": 0.1,
                                "tooltip": "New visible seconds. Snaps to the model's grid: 17-frame steps "
                                           "for extend/prequel, 17m-5 for bridge/loop."}),
                "width": ("INT", {"default": 1344, "min": 32, "max": 8192, "step": 32,
                          "tooltip": "Only used for a first segment (no previous clip)."}),
                "height": ("INT", {"default": 768, "min": 32, "max": 8192, "step": 32,
                           "tooltip": "Only used for a first segment (no previous clip)."}),
                "save": ("BOOLEAN", {"default": False,
                         "tooltip": "Reel Splice writes latent.safetensors + meta.json under output/promptdeck_reel/<session>/."}),
                "session": ("STRING", {"default": "reel"}),
            },
            "optional": {
                "previous": ("LATENT", {"tooltip": "The film so far (or the clip to the left for bridge)."}),
                "next": ("LATENT", {"tooltip": "The clip to the right (bridge only)."}),
            },
        }

    def guide(self, positive, op, tail_frames, head_frames, new_seconds, width, height, save, session,
              previous=None, next=None):
        if op in ("cut", "polish"):
            raise NotImplementedError(f"Reel `{op}` arrives with the strip UI; v1 does extend, prequel, bridge and loop")
        pv = pa = nv = na = None
        P = Q = 0
        if previous is not None:
            pv, pa, P = _film(previous, "previous")
        if next is not None:
            nv, na, Q = _film(next, "next")
        if op in ("loop", "prequel"):
            # the head anchor comes from the film's own start
            if pv is None:
                raise ValueError(f"{op} needs the film in `previous`")
            nv, na, Q = pv, pa, P
        if pv is not None and nv is not None and (pv.shape[3:] != nv.shape[3:]):
            raise ValueError(
                f"previous is {pv.shape[4] * 16}x{pv.shape[3] * 16} but next is {nv.shape[4] * 16}x{nv.shape[3] * 16}; "
                "Reel only joins clips on the same canvas")

        p = plan(op, float(new_seconds), int(tail_frames), int(head_frames), P, Q)

        if pv is not None:
            lh, lw = pv.shape[3], pv.shape[4]
            ref = pv
        elif nv is not None:
            lh, lw = nv.shape[3], nv.shape[4]
            ref = nv
        else:
            if width % 32 or height % 32:
                raise ValueError("width and height must be multiples of 32")
            lh, lw = height // 16, width // 16
            ref = None

        if ref is not None:
            device, dtype = ref.device, ref.dtype
        else:
            device = _mm.intermediate_device() if _mm is not None else torch.device("cpu")
            dtype = torch.float32
        video = torch.zeros((1, VIDEO_CH, p["window_video_t"], lh, lw), device=device, dtype=dtype)
        audio = torch.zeros((1, AUDIO_CH, AUDIO_STEREO, p["window_audio_t"]), device=device, dtype=dtype)

        W = p["window_frames"]
        tail_end = p["tail_frames"]
        head_start = W - p["head_frames"] if p["head_frames"] else W
        kept = []
        for kf in list(positive[0][1].get("minimax_keyframes", [])):
            idx = kf.get("resolved_frame_index")
            if not isinstance(idx, (int, float)):
                continue
            idx = int(idx)
            span = _guide_frames(kf)
            fits = 0 <= idx and idx + span <= W
            collides = (idx < tail_end) or (idx + span > head_start)
            if fits and not collides:
                kept.append(dict(kf))
        keyframes = kept

        if p["tail_video_t"]:
            keyframes.append({
                "resolved_frame_index": 0,
                "latent": pv[:, :, -p["tail_video_t"]:].clone(),
                "audio_latent": pa[..., -p["tail_audio_t"]:].clone(),
            })
        if p["head_video_t"]:
            head_audio = na[..., :p["head_audio_t"]].clone()
            max_rt = math.floor(p["window_audio_t"] - FRAME_RESCALE * p["head_frame"])
            if max_rt < 1:
                raise ValueError("head anchor falls past the end of the window's audio track (bug)")
            if head_audio.shape[-1] > max_rt:
                head_audio = head_audio[..., :max_rt].clone()
            keyframes.append({
                "resolved_frame_index": p["head_frame"],
                "latent": nv[:, :, :p["head_video_t"]].clone(),
                "audio_latent": head_audio,
            })
        keyframes.sort(key=lambda kf: float(kf["resolved_frame_index"]))
        positive = _set_cond(positive, {"minimax_keyframes": keyframes})

        ctx = dict(p)
        ctx.update({
            "session": str(session).strip() or "reel",
            "segment": f"{op}@{P:04d}",
            "save": bool(save),
            "width": lw * 16, "height": lh * 16,
            "version": 1,
        })
        return (positive, _nested(video, audio), ctx)


class ReelSplice:
    CATEGORY = "PromptDeck"
    FUNCTION = "splice"
    RETURN_TYPES = ("LATENT", "LATENT", "INT")
    RETURN_NAMES = ("latent", "segment", "frames")
    DESCRIPTION = (
        "Drops the anchored overlap from a sampled Reel window and concatenates the new tokens "
        "onto the film on the 17k+5 grid. `segment` is the new tokens alone; `frames` the film's length."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "sampled": ("LATENT", {"tooltip": "The Reel Guide window after sampling."}),
                "reel_ctx": (CTX_TYPE,),
            },
            "optional": {
                "previous": ("LATENT", {"tooltip": "The same film you gave Reel Guide."}),
                "next": ("LATENT", {"tooltip": "The same next clip you gave Reel Guide (bridge)."}),
            },
        }

    def splice(self, sampled, reel_ctx, previous=None, next=None):
        ctx = reel_ctx
        if not isinstance(ctx, dict) or ctx.get("version") != 1:
            raise ValueError("reel_ctx must come from Reel Guide")
        op = ctx["op"]
        sv, sa, _ = _streams(sampled, "sampled")
        if sv.shape[2] != ctx["window_video_t"] or sa.shape[-1] != ctx["window_audio_t"]:
            raise ValueError(
                f"sampled window has {sv.shape[2]} video / {sa.shape[-1]} audio tokens but Reel Guide made "
                f"{ctx['window_video_t']} / {ctx['window_audio_t']}; wire the window that this reel_ctx produced")
        t0, nt = ctx["tail_video_t"], ctx["new_video_t"]
        a0, na_ = ctx["tail_audio_t"], ctx["new_audio_t"]
        new_video = sv[:, :, t0:t0 + nt]
        new_audio = sa[..., a0:a0 + na_]

        pv = pa = nv = na = None
        if previous is not None:
            pv, pa, P = _film(previous, "previous")
            if P != ctx["previous_frames"]:
                raise ValueError(f"previous has {P} frames but Reel Guide planned for {ctx['previous_frames']}")
        if next is not None:
            nv, na, Q = _film(next, "next")
            if op == "bridge" and Q != ctx["next_frames"]:
                raise ValueError(f"next has {Q} frames but Reel Guide planned for {ctx['next_frames']}")

        if op == "extend" and ctx["previous_frames"] == 0:
            parts_v, parts_a = [new_video], [new_audio]
        elif op == "extend":
            if pv is None:
                raise ValueError("extend splice needs `previous`")
            parts_v, parts_a = [pv, new_video], [pa, new_audio]
        elif op == "loop":
            if pv is None:
                raise ValueError("loop splice needs `previous`")
            # keep the regenerated head: it is the on-grid duplicate of the film's first frames
            head_video = sv[:, :, t0 + nt:]
            head_audio = sa[..., a0 + na_:]
            parts_v, parts_a = [pv, new_video, head_video], [pa, new_audio, head_audio]
        elif op == "prequel":
            if pv is None:
                raise ValueError("prequel splice needs `previous`")
            parts_v, parts_a = [new_video, pv], [new_audio, pa]
        elif op == "bridge":
            if pv is None or nv is None:
                raise ValueError("bridge splice needs `previous` and `next`")
            parts_v, parts_a = [pv, new_video, nv], [pa, new_audio, na]
        else:
            raise ValueError(f"unknown Reel op {op!r}")

        video = torch.cat(parts_v, dim=2)
        audio = torch.cat(parts_a, dim=3)
        frames = frames_of(video.shape[2])
        if frames != ctx["total_frames"]:
            raise AssertionError(f"spliced film is {frames} frames, planned {ctx['total_frames']} (bug)")
        want = audio_t(frames)
        have = audio.shape[-1]
        if abs(have - want) > 1:
            raise ValueError(f"audio came out {have} latent frames, expected {want}; inputs are not on one timeline")
        if have > want:
            audio = audio[..., :want]  # joining a foreign clip costs at most one 25 ms tick at the end
        elif have < want:
            audio = torch.cat([audio, audio[..., -1:]], dim=3)
        video = video.contiguous()
        audio = audio.contiguous()

        if ctx.get("save"):
            _save_take(ctx, video, audio, new_video, new_audio, frames)
        return (_nested(video, audio), _nested(new_video.contiguous(), new_audio.contiguous()), int(frames))


# ---------------------------------------------------------------- store

def _safe_name(value: str) -> str:
    keep = "".join(ch if (ch.isalnum() or ch in "-_@.") else "_" for ch in str(value))
    return keep.strip("._") or "reel"


def _write_tensors(path: str, tensors: dict):
    tensors = {k: v.detach().cpu().contiguous().clone() for k, v in tensors.items()}
    if _save_torch_file is not None:
        _save_torch_file(tensors, path)
    else:
        from safetensors.torch import save_file
        save_file(tensors, path)


def _save_take(ctx: dict, video, audio, seg_video, seg_audio, frames: int) -> str:
    if folder_paths is None:
        raise RuntimeError("saving needs ComfyUI's folder_paths")
    root = os.path.join(folder_paths.get_output_directory(), "promptdeck_reel",
                        _safe_name(ctx["session"]), _safe_name(ctx["segment"]))
    os.makedirs(root, exist_ok=True)
    take = 1
    while os.path.exists(os.path.join(root, f"take_{take:03d}")):
        take += 1
    folder = os.path.join(root, f"take_{take:03d}")
    os.makedirs(folder)
    final = os.path.join(folder, "latent.safetensors")
    tmp = final + ".staged"
    _write_tensors(tmp, {"video": video, "audio": audio, "segment_video": seg_video, "segment_audio": seg_audio})
    os.replace(tmp, final)  # staged -> ready
    meta = {
        "frames": int(frames), "width": ctx["width"], "height": ctx["height"], "op": ctx["op"],
        "parent": ctx["previous_frames"], "new_frames": ctx["new_frames"],
        "tail_frames": ctx["tail_frames"], "head_frames": ctx["head_frames"],
        "loop_point_frame": ctx.get("loop_point_frame"),
        "session": ctx["session"], "segment": ctx["segment"], "take": take,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "ready": True,
    }
    meta_tmp = os.path.join(folder, "meta.json.staged")
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    os.replace(meta_tmp, os.path.join(folder, "meta.json"))
    return folder


NODE_CLASS_MAPPINGS = {"PromptDeckReelGuide": ReelGuide, "PromptDeckReelSplice": ReelSplice}
NODE_DISPLAY_NAME_MAPPINGS = {"PromptDeckReelGuide": "Reel Guide", "PromptDeckReelSplice": "Reel Splice"}
