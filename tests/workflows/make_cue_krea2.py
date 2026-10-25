"""Emit cue_krea2.json: Cue (target Krea 2) -> CLIPTextEncode -> KSampler -> Keeper (keep_all).

API format (runbook §1.7). Titles the batch file addresses: Cue, KSampler. Chain follows
ComfyUI's bundled image_krea2_turbo_t2i_int8 template; models are the ones this box lists.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

UNET = "Krea2_Turbo_convrot_int8mixed.safetensors"
CLIP = "qwen3vl_4b_fp8_scaled.safetensors"
VAE = "qwen_image_vae.safetensors"
WRITER = "huihui_ai/qwen3-vl-abliterated:4b-instruct"
W, H, STEPS = 1024, 1024, 8


def node(graph, nid, class_type, title=None, **inputs):
    graph[nid] = {"class_type": class_type, "inputs": inputs, "_meta": {"title": title or class_type}}


def krea2():
    g = {}
    node(g, "1", "UNETLoader", "UNET", unet_name=UNET, weight_dtype="default")
    node(g, "2", "CLIPLoader", "CLIP", clip_name=CLIP, type="krea2", device="default")
    node(g, "3", "VAELoader", "VAE", vae_name=VAE)
    node(g, "7", "PromptDeckCue", "Cue", intent="", notes="", target="Krea 2", mode="t2v",
         duration_seconds=5.0, dialogue_language="English", detail="medium", writer=WRITER,
         endpoint="http://127.0.0.1:11434", seed=1, lock=False)
    node(g, "8", "CLIPTextEncode", "Encode", clip=["2", 0], text=["7", 0])
    node(g, "9", "ConditioningZeroOut", "Negative", conditioning=["8", 0])
    node(g, "10", "EmptyLatentImage", "Latent", width=W, height=H, batch_size=1)
    node(g, "11", "KSampler", "KSampler", model=["1", 0], seed=1, steps=STEPS, cfg=1.0,
         sampler_name="euler", scheduler="simple", positive=["8", 0], negative=["9", 0],
         latent_image=["10", 0], denoise=1.0)
    node(g, "12", "VAEDecode", "Decode", samples=["11", 0], vae=["3", 0])
    node(g, "13", "PromptDeckKeeper", "Keeper", images=["12", 0],
         filename_prefix="promptdeck_tests/cue_krea2", keep_all=True)
    return g


if __name__ == "__main__":
    path = os.path.join(HERE, "cue_krea2.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(krea2(), f, indent=2)
    print("wrote", path)
