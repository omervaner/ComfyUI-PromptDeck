"""Emit the Cue + native MiniMax H3 test graphs in API format (runbook §1.7).

cue_h3_t2v.json, cue_h3_i2v.json, cue_h3_ref2v.json. Node titles are what the batch
files' `set` entries address: Cue, Start Frame, Ref 1..3, Noise. Model names are the
ones this box's loaders list (/object_info). Length 124 = 5 s on H3's 17n+5 grid.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

UNET_FL2VA = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
UNET_REF2VA = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VAE_VIDEO = "minimax_h3_video_vae_int8_convrot.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"
LORA = "MiniMaxH3\\minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors"
WRITER = "huihui_ai/qwen3-vl-abliterated:4b-instruct"
W, H, LENGTH, FPS, STEPS = 768, 1344, 124, 24, 6  # portrait: the test refs/starts are 9:16


def node(graph, nid, class_type, title=None, **inputs):
    graph[nid] = {"class_type": class_type, "inputs": inputs, "_meta": {"title": title or class_type}}
    return nid


def base(mode, unet):
    g = {}
    node(g, "1", "UNETLoader", "UNET", unet_name=unet, weight_dtype="default")
    node(g, "2", "CLIPLoader", "CLIP", clip_name=CLIP, type="minimax", device="default")
    node(g, "3", "VAELoader", "Video VAE", vae_name=VAE_VIDEO)
    node(g, "4", "VAELoader", "Audio VAE", vae_name=VAE_AUDIO)
    node(g, "5", "LoraLoaderModelOnly", "Turbo LoRA", model=["1", 0], lora_name=LORA, strength_model=1.0)
    node(g, "7", "PromptDeckCue", "Cue", intent="", notes="", target="MiniMax H3", mode=mode,
         duration_seconds=5.0, dialogue_language="English", detail="long", writer=WRITER,
         endpoint="http://127.0.0.1:11434", seed=1, lock=False)
    node(g, "10", "KSamplerSelect", "Sampler", sampler_name="euler")
    node(g, "11", "BasicScheduler", "Scheduler", model=["5", 0], scheduler="simple", steps=STEPS, denoise=1.0)
    node(g, "12", "RandomNoise", "Noise", noise_seed=1)
    node(g, "13", "BasicGuider", "Guider", model=["5", 0], conditioning=["8", 0])
    node(g, "14", "SamplerCustomAdvanced", "Sample", noise=["12", 0], guider=["13", 0],
         sampler=["10", 0], sigmas=["11", 0], latent_image=["8", 1])
    node(g, "15", "VAEDecode", "Decode Video", samples=["14", 0], vae=["3", 0])
    node(g, "16", "VAEDecodeAudio", "Decode Audio", samples=["14", 0], vae=["4", 0])
    node(g, "17", "CreateVideo", "Create Video", images=["15", 0], fps=FPS, audio=["16", 0])
    node(g, "18", "SaveVideo", "Save Video", video=["17", 0],
         filename_prefix=f"promptdeck_tests/cue_h3_{mode}", format="auto", codec="auto")
    return g


def t2v():
    g = base("t2v", UNET_FL2VA)
    node(g, "8", "MiniMaxH3ImageToVideo", "H3 Text to Video", clip=["2", 0], vae=["3", 0],
         prompt=["7", 0], width=W, height=H, length=LENGTH)
    return g


def i2v():
    g = base("i2v", UNET_FL2VA)
    node(g, "6", "LoadImage", "Start Frame", image="promptdeck_tests/starts/char1.png")
    g["7"]["inputs"]["image"] = ["6", 0]
    node(g, "8", "MiniMaxH3ImageToVideo", "H3 Image to Video", clip=["2", 0], vae=["3", 0],
         prompt=["7", 0], width=W, height=H, length=LENGTH, first_frame=["6", 0])
    return g


def ref2v():
    g = base("ref2v", UNET_REF2VA)
    # Autogrow inputs travel as flat dotted ids in API JSON ("ref_images.ref_image_0");
    # the server nests them before the node runs (comfy_api/latest/_io.py, execution.py).
    refs = {}
    for n in (1, 2, 3):
        nid = f"2{n}"
        node(g, nid, "LoadImage", f"Ref {n}", image=f"promptdeck_tests/refs/char1/{n}.png")
        g["7"]["inputs"][f"ref_{n}"] = [nid, 0]
        # Same label on every slot = one subject (Cue tooltip). Empty labels made the
        # writers describe three different women in run 1.
        g["7"]["inputs"][f"label_{n}"] = "char1"
        refs[f"ref_images.ref_image_{n - 1}"] = [nid, 0]
    node(g, "8", "MiniMaxH3ReferenceToVideo", "H3 Reference to Video", clip=["2", 0],
         prompt=["7", 0], width=W, height=H, length=LENGTH, ref_image_size="match",
         vae=["3", 0], audio_vae=["4", 0], **refs)
    return g


if __name__ == "__main__":
    for name, fn in (("cue_h3_t2v", t2v), ("cue_h3_i2v", i2v), ("cue_h3_ref2v", ref2v)):
        path = os.path.join(HERE, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fn(), f, indent=2)
        print("wrote", path)
