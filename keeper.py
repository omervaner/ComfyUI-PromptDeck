import json
import os
import random

import folder_paths
import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

try:
    from comfy.cli_args import args
    _DISABLE_METADATA = bool(args.disable_metadata)
except Exception:
    _DISABLE_METADATA = False


def _inside(base: str, path: str) -> bool:
    base = os.path.abspath(base)
    return os.path.commonpath((base, os.path.abspath(path))) == base


class Keeper:
    """Preview Image that remembers: every gen lands in temp (like Preview Image),
    and only the ones you click 💾 on get written to the output folder."""

    CATEGORY = "PromptDeck"
    FUNCTION = "preview"
    OUTPUT_NODE = True
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    DESCRIPTION = (
        "Previews images without saving them. Browse the last 10 with ◀ ▶ and "
        "click 💾 to save one to the output folder using filename_prefix "
        "(same %date:...% formatting as Save Image), or Save as… to name it."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "The images to preview."}),
                "filename_prefix": ("STRING", {
                    "default": "ComfyUI",
                    "tooltip": "Used when you click 💾. Same formatting as Save Image, "
                               "e.g. MyModel/%date:yyyy-MM-dd%/%date:hhmmss%",
                }),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    def preview(self, images, filename_prefix="ComfyUI", prompt=None, extra_pnginfo=None):
        temp_dir = folder_paths.get_temp_directory()
        tag = "".join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(5))
        full_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            f"keeper_{tag}", temp_dir, images[0].shape[1], images[0].shape[0])

        metadata = None
        if not _DISABLE_METADATA:
            metadata = PngInfo()
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for k in extra_pnginfo:
                    metadata.add_text(k, json.dumps(extra_pnginfo[k]))

        results = []
        for batch_number, image in enumerate(images):
            arr = np.clip(255.0 * image.cpu().numpy(), 0, 255).astype(np.uint8)
            file = f"{filename}_{counter:05}_.png"
            # compress_level 1 like Preview Image: fast, and re-encoded on save
            Image.fromarray(arr).save(os.path.join(full_folder, file), pnginfo=metadata, compress_level=1)
            results.append({
                "filename": file,
                "subfolder": subfolder,
                "type": "temp",
                # resolved at queue time by the JS; %batch_num% is ours to fill
                "prefix": filename_prefix.replace("%batch_num%", str(batch_number)),
            })
            counter += 1

        # custom ui key, so the frontend's stock image preview stays out of our way
        return {"ui": {"keeper_images": results}, "result": (images,)}


NODE_CLASS_MAPPINGS = {"PromptDeckKeeper": Keeper}
NODE_DISPLAY_NAME_MAPPINGS = {"PromptDeckKeeper": "Keeper"}


def _save(src: str, prefix: str, name: str) -> str:
    """Copy a temp preview into the output folder, keeping its metadata.
    Returns the path relative to the output folder."""
    output_dir = folder_paths.get_output_directory()
    img = Image.open(src)
    img.load()

    metadata = None
    text = {k: v for k, v in img.info.items() if isinstance(v, str)}
    if text:
        metadata = PngInfo()
        for k, v in text.items():
            metadata.add_text(k, v)

    if name:
        # Save as…: exact name, never overwrite — append _2, _3, ... instead
        rel = os.path.normpath(name.strip().replace("\\", "/"))
        if rel.lower().endswith(".png"):
            rel = rel[:-4]
        base = os.path.join(output_dir, rel)
        if not _inside(output_dir, base) or not os.path.basename(rel):
            raise ValueError("name must stay inside the output folder")
        os.makedirs(os.path.dirname(base), exist_ok=True)
        dest = base + ".png"
        n = 2
        while os.path.exists(dest):
            dest = f"{base}_{n}.png"
            n += 1
    else:
        # 💾: exactly what Save Image would have written
        folder, filename, counter, _, _ = folder_paths.get_save_image_path(
            prefix or "ComfyUI", output_dir, img.width, img.height)
        dest = os.path.join(folder, f"{filename}_{counter:05}_.png")

    img.save(dest, pnginfo=metadata, compress_level=4)
    return os.path.relpath(dest, output_dir).replace("\\", "/")


def _register_routes():
    try:
        from server import PromptServer
        from aiohttp import web
    except Exception:
        return  # not running inside ComfyUI (tests, linting)

    @PromptServer.instance.routes.post("/promptdeck/keeper/save")
    async def keeper_save(request):
        try:
            data = await request.json()
            filename = os.path.basename(data.get("filename", ""))
            subfolder = data.get("subfolder", "")
            prefix = data.get("prefix", "")
            name = data.get("name", "")
        except Exception:
            return web.json_response({"ok": False, "error": "bad request"})
        temp_dir = folder_paths.get_temp_directory()
        src = os.path.join(temp_dir, subfolder, filename)
        if not filename or not _inside(temp_dir, src) or not os.path.isfile(src):
            return web.json_response({"ok": False, "error": "preview is gone (temp cleared?)"})
        try:
            saved = _save(src, prefix, name)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
        return web.json_response({"ok": True, "saved": saved})


_register_routes()
