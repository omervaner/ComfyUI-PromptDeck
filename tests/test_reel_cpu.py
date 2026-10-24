"""CPU smoke for Reel: grid arithmetic, anchors and splicing on fake AV latents.

Run with the sandbox ComfyUI's python (it has torch):
    /Users/omer/Documents/Projects/ComfyUI-dev-sandbox/.venv/bin/python tests/test_reel_cpu.py
or under pytest. Set COMFY_SANDBOX to point at another ComfyUI checkout.
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SANDBOX = os.environ.get("COMFY_SANDBOX", "/Users/omer/Documents/Projects/ComfyUI-dev-sandbox")
for p in (SANDBOX, REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

import torch  # noqa: E402
from comfy.nested_tensor import NestedTensor  # noqa: E402

import reel  # noqa: E402

reel.NestedTensor = NestedTensor  # the module imports it itself inside ComfyUI

H, W = 768 // 16, 1344 // 16  # latent spatial for the default canvas


def film(frames, seed=0, h=H, w=W):
    """A finished clip: video 5k+2 tokens, audio exactly on the global boundary."""
    g = torch.Generator().manual_seed(seed)
    video = torch.randn((1, 24, reel.video_t(frames), h, w), generator=g)
    audio = torch.randn((1, 32, 2, reel.audio_t(frames)), generator=g)
    return {"samples": NestedTensor((video, audio))}


def streams(latent):
    v, a = latent["samples"].tensors
    return v, a


def positive(keyframes=None):
    meta = {"minimax_refs": [{"kind": "image"}], "pooled_output": None}
    if keyframes is not None:
        meta["minimax_keyframes"] = keyframes
    return [[torch.zeros(1, 4, 8), meta]]


def sampled_like(window, seed=1):
    v, a = streams(window)
    g = torch.Generator().manual_seed(seed)
    return {"samples": NestedTensor((torch.randn(v.shape, generator=g), torch.randn(a.shape, generator=g)))}


def valid_window_audio(frames):
    lower, rem = divmod(frames * 40, 24)
    return {lower} if rem == 0 else {lower, lower + 1}


# ------------------------------------------------------------------ tests

def test_grid_helpers():
    for k in range(0, 22):
        frames = 17 * k + 5
        assert reel.video_t(frames) == 5 * k + 2
        assert reel.frames_of(5 * k + 2) == frames
    assert reel.audio_t(124) == 207 and reel.audio_t(102) == 170 and reel.audio_t(0) == 0
    assert reel.snap_extension(5.0) == 119 and reel.snap_extension(0.1) == 17
    assert reel.snap_gap(5.0) == 114 and reel.snap_gap(0.1) == 12
    assert reel.snap_first(5.0) == 124 and reel.snap_first(0.5) == 22
    for bad in (4, 6, 17, 123):
        try:
            reel.video_t(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def test_audio_boundaries_follow_global_timeline():
    for P in (22, 56, 107, 124, 141, 243, 362):
        for a in (5, 22, 39, 56, 73):
            if a > P:
                continue
            p = reel.plan("extend", 5.0, a, 22, P, 0)
            assert p["tail_audio_t"] in (math.floor(a * 5 / 3), math.ceil(a * 5 / 3))
            assert p["window_audio_t"] in valid_window_audio(p["window_frames"])
            # the film after splicing is exact by construction
            assert reel.audio_t(P) + p["new_audio_t"] == reel.audio_t(P + p["new_frames"])


def test_extend_plan_and_anchor():
    prev = film(124)
    pv, pa = streams(prev)
    pos = positive([{"resolved_frame_index": 0, "latent": torch.zeros(1, 24, 1, H, W)}])
    g = reel.ReelGuide()
    out_pos, window, ctx = g.guide(pos, "extend", "22", "22", 5.0, 1344, 768, False, "t", previous=prev)
    assert (ctx["new_frames"], ctx["window_frames"]) == (119, 141)
    assert (ctx["tail_video_t"], ctx["new_video_t"], ctx["window_video_t"]) == (7, 35, 42)
    assert (ctx["tail_audio_t"], ctx["new_audio_t"], ctx["window_audio_t"]) == (37, 198, 235)
    wv, wa = streams(window)
    assert tuple(wv.shape) == (1, 24, 42, H, W) and tuple(wa.shape) == (1, 32, 2, 235)
    assert float(wv.abs().sum()) == 0.0 and float(wa.abs().sum()) == 0.0
    kfs = out_pos[0][1]["minimax_keyframes"]
    assert len(kfs) == 1, "the colliding first-frame guide must be dropped"
    kf = kfs[0]
    assert set(kf) == {"resolved_frame_index", "latent", "audio_latent"}
    assert kf["resolved_frame_index"] == 0
    assert torch.equal(kf["latent"], pv[:, :, -7:]) and tuple(kf["latent"].shape) == (1, 24, 7, H, W)
    assert torch.equal(kf["audio_latent"], pa[..., -37:])
    assert out_pos[0][1]["minimax_refs"] == [{"kind": "image"}], "refs untouched"
    assert pos[0][1]["minimax_keyframes"][0]["resolved_frame_index"] == 0, "input conditioning not mutated"

    s = reel.ReelSplice()
    sampled = sampled_like(window)
    out, seg, frames = s.splice(sampled, ctx, previous=prev)
    ov, oa = streams(out)
    sv, sa = streams(sampled)
    assert frames == 243 and tuple(ov.shape) == (1, 24, 72, H, W) and oa.shape[-1] == reel.audio_t(243) == 405
    assert torch.equal(ov[:, :, :37], pv) and torch.equal(ov[:, :, 37:], sv[:, :, 7:])
    assert torch.equal(oa[..., :207], pa) and torch.equal(oa[..., 207:], sa[..., 37:])
    gv, ga = streams(seg)
    assert tuple(gv.shape) == (1, 24, 35, H, W) and ga.shape[-1] == 198
    # the result is itself a valid film for the next extend
    reel.ReelGuide().guide(positive(), "extend", "73", "22", 3.0, 1344, 768, False, "t", previous=out)


def test_first_segment_keeps_fitting_keyframes():
    pos = positive([{"resolved_frame_index": 0, "latent": torch.zeros(1, 24, 1, H, W)},
                    {"resolved_frame_index": 123, "latent": torch.zeros(1, 24, 1, H, W)},
                    {"resolved_frame_index": 500, "latent": torch.zeros(1, 24, 1, H, W)}])
    out_pos, window, ctx = reel.ReelGuide().guide(pos, "extend", "22", "22", 5.0, 1344, 768, False, "t")
    assert ctx["window_frames"] == 124 and ctx["tail_video_t"] == 0 and ctx["previous_frames"] == 0
    wv, wa = streams(window)
    assert tuple(wv.shape) == (1, 24, 37, H, W) and wa.shape[-1] == 207
    kept = [k["resolved_frame_index"] for k in out_pos[0][1]["minimax_keyframes"]]
    assert kept == [0, 123], kept
    out, seg, frames = reel.ReelSplice().splice(sampled_like(window), ctx)
    assert frames == 124 and streams(out)[0].shape[2] == 37


def test_prequel():
    prev = film(124, seed=3)
    pv, pa = streams(prev)
    out_pos, window, ctx = reel.ReelGuide().guide(positive(), "prequel", "22", "22", 5.0, 1344, 768, False, "t", previous=prev)
    assert (ctx["new_frames"], ctx["window_frames"], ctx["head_frame"]) == (119, 141, 119)
    assert (ctx["new_video_t"], ctx["head_video_t"]) == (35, 7)
    assert (ctx["new_audio_t"], ctx["head_audio_t"], ctx["window_audio_t"]) == (198, 37, 235)
    kf = out_pos[0][1]["minimax_keyframes"][0]
    assert kf["resolved_frame_index"] == 119
    assert torch.equal(kf["latent"], pv[:, :, :7])
    # head audio sits at 119 * 5/3 = 198.33; AddGuide's clamp floor(235 - 198.33) = 36 trims the 37th tick
    assert torch.equal(kf["audio_latent"], pa[..., :36]) and kf["audio_latent"].shape[-1] == math.floor(235 - 5 / 3 * 119)
    sampled = sampled_like(window)
    out, seg, frames = reel.ReelSplice().splice(sampled, ctx, previous=prev)
    ov, oa = streams(out)
    sv, sa = streams(sampled)
    assert frames == 243 and ov.shape[2] == 72 and oa.shape[-1] == 405
    assert torch.equal(ov[:, :, :35], sv[:, :, :35]) and torch.equal(ov[:, :, 35:], pv)
    assert torch.equal(oa[..., :198], sa[..., :198]) and torch.equal(oa[..., 198:], pa)


def test_bridge():
    left, right = film(124, seed=4), film(124, seed=5)
    lv, la = streams(left)
    rv, ra = streams(right)
    out_pos, window, ctx = reel.ReelGuide().guide(positive(), "bridge", "22", "22", 5.0, 1344, 768, False, "t",
                                                  previous=left, next=right)
    assert (ctx["new_frames"], ctx["window_frames"]) == (114, 158)
    assert (ctx["tail_video_t"], ctx["new_video_t"], ctx["head_video_t"], ctx["window_video_t"]) == (7, 33, 7, 47)
    assert (ctx["tail_audio_t"], ctx["new_audio_t"], ctx["head_audio_t"]) == (37, 190, 36)
    assert ctx["window_audio_t"] == 263 and 263 in valid_window_audio(158)
    kfs = out_pos[0][1]["minimax_keyframes"]
    assert [k["resolved_frame_index"] for k in kfs] == [0, 136]
    assert torch.equal(kfs[0]["latent"], lv[:, :, -7:]) and torch.equal(kfs[0]["audio_latent"], la[..., -37:])
    assert torch.equal(kfs[1]["latent"], rv[:, :, :7]) and torch.equal(kfs[1]["audio_latent"], ra[..., :36])
    # AddGuide's audio clamp: floor(window_audio - 5/3 * head_frame)
    assert kfs[1]["audio_latent"].shape[-1] <= math.floor(263 - 5 / 3 * 136)
    sampled = sampled_like(window)
    out, seg, frames = reel.ReelSplice().splice(sampled, ctx, previous=left, next=right)
    ov, oa = streams(out)
    sv, sa = streams(sampled)
    assert frames == 362 and ov.shape[2] == 107 and oa.shape[-1] == reel.audio_t(362) == 603
    assert torch.equal(ov[:, :, :37], lv) and torch.equal(ov[:, :, 37:70], sv[:, :, 7:40]) and torch.equal(ov[:, :, 70:], rv)
    # 207 + 190 + 207 = 604 -> one tick trimmed at the very end, nothing else moved
    assert torch.equal(oa[..., :207], la) and torch.equal(oa[..., 207:397], sa[..., 37:227])
    assert torch.equal(oa[..., 397:], ra[..., :206])
    gv, ga = streams(seg)
    assert gv.shape[2] == 33 and ga.shape[-1] == 190


def test_loop():
    prev = film(124, seed=6)
    pv, pa = streams(prev)
    out_pos, window, ctx = reel.ReelGuide().guide(positive(), "loop", "22", "22", 5.0, 1344, 768, False, "t", previous=prev)
    # previous + gap = 238 frames is off the grid; the regenerated head stays, loop point at 238
    assert (ctx["new_frames"], ctx["window_frames"], ctx["total_frames"], ctx["loop_point_frame"]) == (114, 158, 260, 238)
    kfs = out_pos[0][1]["minimax_keyframes"]
    assert [k["resolved_frame_index"] for k in kfs] == [0, 136]
    assert torch.equal(kfs[0]["latent"], pv[:, :, -7:]) and torch.equal(kfs[1]["latent"], pv[:, :, :7])
    sampled = sampled_like(window)
    out, seg, frames = reel.ReelSplice().splice(sampled, ctx, previous=prev)
    ov, oa = streams(out)
    sv, sa = streams(sampled)
    assert frames == 260 and ov.shape[2] == 77 and oa.shape[-1] == reel.audio_t(260) == 433
    assert torch.equal(ov[:, :, :37], pv) and torch.equal(ov[:, :, 37:70], sv[:, :, 7:40]) and torch.equal(ov[:, :, 70:], sv[:, :, 40:])
    assert torch.equal(oa[..., 207:397], sa[..., 37:227]) and torch.equal(oa[..., 397:], sa[..., 227:])


def test_errors():
    g = reel.ReelGuide()
    def raises(exc, fn):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"expected {exc.__name__}")
    raises(ValueError, lambda: g.guide(positive(), "extend", "73", "22", 15.0, 1344, 768, False, "t", previous=film(362)))  # 430 > 362
    raises(ValueError, lambda: g.guide(positive(), "bridge", "73", "73", 10.0, 1344, 768, False, "t", previous=film(124), next=film(124)))
    raises(ValueError, lambda: g.guide(positive(), "extend", "22", "22", 5.0, 1344, 768, False, "t", previous=film(5)))  # too short
    raises(ValueError, lambda: g.guide(positive(), "bridge", "22", "22", 5.0, 1344, 768, False, "t",
                                       previous=film(124), next=film(124, h=24, w=42)))  # canvas mismatch
    raises(ValueError, lambda: g.guide(positive(), "prequel", "22", "22", 5.0, 1344, 768, False, "t"))  # no previous
    raises(ValueError, lambda: g.guide(positive(), "extend", "22", "22", 5.0, 1344, 768, False, "t",
                                       previous={"samples": torch.zeros(1, 4, 8, 8)}))  # not an AV latent
    bad_audio = film(124)
    v, a = streams(bad_audio)
    raises(ValueError, lambda: g.guide(positive(), "extend", "22", "22", 5.0, 1344, 768, False, "t",
                                       previous={"samples": NestedTensor((v, a[..., :-1]))}))  # off the boundary
    raises(NotImplementedError, lambda: g.guide(positive(), "cut", "22", "22", 5.0, 1344, 768, False, "t", previous=film(124)))
    # splice refuses a window that is not the one the ctx planned
    _, window, ctx = g.guide(positive(), "extend", "22", "22", 5.0, 1344, 768, False, "t", previous=film(124))
    raises(ValueError, lambda: reel.ReelSplice().splice(film(124), ctx, previous=film(124)))


def test_matches_native_addguide_format():
    """Our anchor entries must look exactly like what MiniMaxH3AddGuide appends."""
    try:
        import comfy_extras.nodes_minimax_h3 as native
    except Exception as exc:  # the sandbox lacks a dependency; format was read from source instead
        print(f"  (skipped native AddGuide comparison: {type(exc).__name__}: {str(exc)[:80]})")
        return

    class FakeVAE:
        def encode(self, frames):  # frames [N, H, W, 3] -> [1, 24, t, H/16, W/16]
            n = frames.shape[0]
            t = 1 if n == 1 else reel.video_t(n)
            return torch.zeros(1, 24, t, frames.shape[1] // 16, frames.shape[2] // 16)

    class FakeAudioVAE:
        audio_sample_rate = 32000
        def encode(self, waveform):  # -> [1, 32, 2, T]
            return torch.zeros(1, 32, 2, 37)

    prev = film(124)
    _, window, ctx = reel.ReelGuide().guide(positive(), "extend", "22", "22", 5.0, 1344, 768, False, "t", previous=prev)
    ours = reel.ReelGuide().guide(positive(), "extend", "22", "22", 5.0, 1344, 768, False, "t", previous=prev)[0][0][1]["minimax_keyframes"][0]
    image = torch.zeros(22, 768, 1344, 3)
    audio = {"waveform": torch.zeros(1, 2, 32000), "sample_rate": 32000}
    res = native.MiniMaxH3AddGuide.execute(positive(), window, 0, vae=FakeVAE(), audio_vae=FakeAudioVAE(), image=image, audio=audio)
    out_pos = getattr(res, "result", None)
    out_pos = out_pos[0] if isinstance(out_pos, (tuple, list)) else (res.args[0] if hasattr(res, "args") else out_pos)
    theirs = out_pos[0][1]["minimax_keyframes"][-1]
    assert set(theirs) == set(ours) == {"resolved_frame_index", "latent", "audio_latent"}
    assert theirs["resolved_frame_index"] == ours["resolved_frame_index"] == 0
    assert tuple(theirs["latent"].shape) == tuple(ours["latent"].shape) == (1, 24, 7, H, W)
    assert theirs["audio_latent"].ndim == ours["audio_latent"].ndim == 4
    assert theirs["audio_latent"].shape[:3] == ours["audio_latent"].shape[:3] == (1, 32, 2)
    print("  (native AddGuide comparison ran)")


if __name__ == "__main__":
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok", n)
    print(f"{len(names)} tests passed")
