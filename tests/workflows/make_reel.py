"""Emit the Reel test graphs in API format (runbook §1.7): reel_extend, reel_bridge,
reel_prequel, reel_refine.

Source clip -> GetVideoComponents -> VAEEncode + VAEEncodeAudio -> LTXVConcatAVLatent gives
the AV latent Reel Guide wants in `previous` (and `next` for bridge). Cue in
"continuation (Reel)" mode writes the prompt; MiniMaxH3ImageToVideo turns it into
conditioning (its own latent is unused); Reel Guide pins the anchors; the stock
SamplerCustomAdvanced samples the window; Reel Splice concatenates; decode and save.

reel_refine = reel_extend plus a second BasicScheduler/SamplerCustomAdvanced pass on the
window, titled `Refine` (the batch sets its `steps` and `denoise`; denoise 0.0 yields empty
sigmas and the sampler returns the window untouched).

Titles the batch files address: Source, Next Source, Reel Guide, Cue, Noise, Refine.
Source clips must sit on H3's 17k+5 frame grid at 24 fps (124 frames = 5 s) and on one
canvas (1344x768) — prepare them with ffmpeg before queueing.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VAE_VIDEO = "minimax_h3_video_vae_int8_convrot.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"
LORA = "MiniMaxH3\\minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors"
WRITER = "huihui_ai/qwen3-vl-abliterated:4b-instruct"
W, H, LENGTH, FPS, STEPS = 768, 1344, 124, 24, 6  # portrait: sources are conformed to 768x1344
SOURCE = "promptdeck_tests/sources/char1_5s.mp4"


def node(graph, nid, class_type, title=None, **inputs):
    graph[nid] = {"class_type": class_type, "inputs": inputs, "_meta": {"title": title or class_type}}


def clip_to_latent(g, base_id, title):
    """LoadVideo -> AV latent. Returns the id of the LTXVConcatAVLatent node."""
    a, b, c, d, e = (str(base_id + i) for i in range(5))
    node(g, a, "LoadVideo", title, file=SOURCE)
    node(g, b, "GetVideoComponents", f"{title} parts", video=[a, 0])
    node(g, c, "VAEEncode", f"{title} video latent", pixels=[b, 0], vae=["3", 0])
    node(g, d, "VAEEncodeAudio", f"{title} audio latent", audio=[b, 1], vae=["4", 0])
    node(g, e, "LTXVConcatAVLatent", f"{title} AV latent", video_latent=[c, 0], audio_latent=[d, 0])
    return e


def reel(op, refine=False):
    g = {}
    node(g, "1", "UNETLoader", "UNET", unet_name=UNET, weight_dtype="default")
    node(g, "2", "CLIPLoader", "CLIP", clip_name=CLIP, type="minimax", device="default")
    node(g, "3", "VAELoader", "Video VAE", vae_name=VAE_VIDEO)
    node(g, "4", "VAELoader", "Audio VAE", vae_name=VAE_AUDIO)
    node(g, "5", "LoraLoaderModelOnly", "Turbo LoRA", model=["1", 0], lora_name=LORA, strength_model=1.0)

    prev = clip_to_latent(g, 30, "Source")
    nxt = clip_to_latent(g, 40, "Next Source") if op == "bridge" else None

    # Cue's continuation mode reads the footage's edge frame from `image` ("the last frame so
    # far is the only context the writer can get", cue.py). Run 1 wired nothing in and the
    # writer invented a scene. extend/bridge: last frame of the source; prequel: its first.
    edge_index = 0 if op == "prequel" else LENGTH - 1
    node(g, "6", "ImageFromBatch", "Edge Frame", image=["31", 0], batch_index=edge_index, length=1)
    node(g, "7", "PromptDeckCue", "Cue", intent="", notes="", target="MiniMax H3",
         mode="continuation (Reel)", duration_seconds=5.0, dialogue_language="English",
         detail="long", writer=WRITER, endpoint="http://127.0.0.1:11434", seed=1, lock=False,
         image=["6", 0])
    node(g, "8", "MiniMaxH3ImageToVideo", "H3 conditioning", clip=["2", 0], vae=["3", 0],
         prompt=["7", 0], width=W, height=H, length=LENGTH)

    guide = dict(positive=["8", 0], op=op, tail_frames="22", head_frames="22", new_seconds=5.0,
                 width=W, height=H, save=False, session="promptdeck_tests", previous=[prev, 0])
    if nxt:
        guide["next"] = [nxt, 0]
    node(g, "9", "PromptDeckReelGuide", "Reel Guide", **guide)

    node(g, "10", "KSamplerSelect", "Sampler", sampler_name="euler")
    node(g, "11", "BasicScheduler", "Scheduler", model=["5", 0], scheduler="simple", steps=STEPS, denoise=1.0)
    node(g, "12", "RandomNoise", "Noise", noise_seed=1)
    node(g, "13", "BasicGuider", "Guider", model=["5", 0], conditioning=["9", 0])
    node(g, "14", "SamplerCustomAdvanced", "Sample", noise=["12", 0], guider=["13", 0],
         sampler=["10", 0], sigmas=["11", 0], latent_image=["9", 1])
    sampled = ["14", 0]

    if refine:
        node(g, "50", "BasicScheduler", "Refine", model=["5", 0], scheduler="simple", steps=10, denoise=0.15)
        node(g, "51", "RandomNoise", "Refine Noise", noise_seed=2)
        node(g, "52", "SamplerCustomAdvanced", "Refine Sample", noise=["51", 0], guider=["13", 0],
             sampler=["10", 0], sigmas=["50", 0], latent_image=["14", 0])
        sampled = ["52", 0]

    splice = dict(sampled=sampled, reel_ctx=["9", 2], previous=[prev, 0])
    if nxt:
        splice["next"] = [nxt, 0]
    node(g, "15", "PromptDeckReelSplice", "Reel Splice", **splice)

    node(g, "16", "VAEDecode", "Decode Video", samples=["15", 0], vae=["3", 0])
    node(g, "17", "VAEDecodeAudio", "Decode Audio", samples=["15", 0], vae=["4", 0])
    node(g, "18", "CreateVideo", "Create Video", images=["16", 0], fps=FPS, audio=["17", 0])
    name = "reel_refine" if refine else f"reel_{op}"
    node(g, "19", "SaveVideo", "Save Video", video=["18", 0],
         filename_prefix=f"promptdeck_tests/{name}", format="auto", codec="auto")
    return g


if __name__ == "__main__":
    for name, kw in (("reel_extend", dict(op="extend")), ("reel_bridge", dict(op="bridge")),
                     ("reel_prequel", dict(op="prequel")), ("reel_refine", dict(op="extend", refine=True))):
        path = os.path.join(HERE, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reel(**kw), f, indent=2)
        print("wrote", path)
