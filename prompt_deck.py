import json
import os
import re

# leading list numbering like "12. ", "12) ", "12: ", "12 - " — stripped so
# numbered prompt lists paste straight in without the numbers leaking into prompts
_NUMBERING_RE = re.compile(r"^\s*\d{1,4}(?:\s*[.):]|\s+-)\s*")
_SLUG_RE = re.compile(r"[^0-9A-Za-z]+")

RATING_FILTERS = ["all", "good only", "hide bad"]


def _sidecar_path(file_path: str) -> str:
    return file_path + ".deck.json"


def _load_ratings(file_path: str) -> dict:
    """Ratings live in a sidecar JSON next to the txt, keyed by the raw line text,
    so they survive server restarts, reordering, and workflow switches."""
    try:
        with open(_sidecar_path(file_path), "r", encoding="utf-8") as f:
            data = json.load(f)
        ratings = data.get("ratings", {})
        return ratings if isinstance(ratings, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_rating(file_path: str, key: str, rating: str):
    path = _sidecar_path(file_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    ratings = data.setdefault("ratings", {})
    if rating in ("good", "bad"):
        ratings[key] = rating
    else:
        ratings.pop(key, None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _parse_replacements(spec: str):
    """Each line: `search -> replace`. Applied top to bottom, in order."""
    pairs = []
    for raw in spec.splitlines():
        if "->" not in raw:
            continue
        old, new = raw.split("->", 1)
        old = old.strip()
        if old:
            pairs.append((old, new.strip()))
    return pairs


def _parse_disabled(spec: str):
    out = set()
    for part in str(spec).replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _parse_markers(spec: str):
    return [m.strip() for m in spec.split(",") if m.strip()]


def _apply_cuts(text: str, cut_to: str, cut_from: str) -> str:
    # cut_to: drop everything up to and including the earliest marker
    positions = [(text.find(m), m) for m in _parse_markers(cut_to)]
    positions = [(p, m) for p, m in positions if p >= 0]
    if positions:
        p, m = min(positions)
        text = text[p + len(m):]
    # cut_from: drop everything from the earliest marker onward
    positions = [text.find(m) for m in _parse_markers(cut_from)]
    positions = [p for p in positions if p >= 0]
    if positions:
        text = text[:min(positions)]
    return text.strip().strip(",").strip()


def _process_line(raw: str, cut_to: str, cut_from: str, replacements: str) -> str:
    text = _NUMBERING_RE.sub("", raw.strip())
    text = _apply_cuts(text, cut_to, cut_from)
    for old, new in _parse_replacements(replacements):
        text = text.replace(old, new)
    return text


def _rating_ok(rating: str, rating_filter: str) -> bool:
    if rating_filter == "good only":
        return rating == "good"
    if rating_filter == "hide bad":
        return rating != "bad"
    return True


def _load_entries(file_path: str):
    """One entry per file line: {n, raw, blank}. blank = empty or # comment."""
    entries = []
    if not file_path or not os.path.isfile(file_path):
        return entries
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for n, raw in enumerate(f.read().splitlines(), start=1):
            stripped = raw.strip()
            blank = not stripped or stripped.startswith("#")
            entries.append({"n": n, "raw": raw, "blank": blank})
    return entries


def _runnable(entries, disabled, ratings, rating_filter):
    out = []
    for e in entries:
        if e["blank"] or e["n"] in disabled:
            continue
        if not _rating_ok(ratings.get(e["raw"].strip(), ""), rating_filter):
            continue
        out.append(e)
    return out


class PromptDeck:
    CATEGORY = "PromptDeck"
    FUNCTION = "read"
    OUTPUT_NODE = True  # can run standalone, no downstream nodes needed
    RETURN_TYPES = ("STRING", "INT", "INT", "INT", "STRING")
    RETURN_NAMES = ("text", "line_number", "total_lines", "remaining_lines", "filename_slug")
    DESCRIPTION = (
        "Feeds prompts line-by-line from a text file, with a visible list, "
        "click-to-jump, hold, ratings, and per-line disable. Blank lines and "
        "# comments are skipped; leading numbering like `12.` is stripped. "
        "line_index counts runnable lines and wraps around."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "file_path": ("STRING", {
                    "default": "",
                    "tooltip": "Path to a .txt file, one prompt per line.",
                }),
                "line_index": ("INT", {
                    "default": 0, "min": 0, "max": 2**31 - 1,
                    "control_after_generate": True,
                    "tooltip": "Position among runnable lines (wraps around). "
                               "Control: increment = forward, decrement = reverse, "
                               "randomize = random, fixed = hold.",
                }),
                "pre_text": ("STRING", {"default": "", "multiline": True}),
                "after_text": ("STRING", {"default": "", "multiline": True}),
                "replacements": ("STRING", {
                    "default": "", "multiline": True,
                    "placeholder": "search -> replace   (one per line, applied in order)",
                }),
                "cut_to": ("STRING", {
                    "default": "",
                    "tooltip": "Comma-separated markers. Cuts everything up to and "
                               "including the earliest match (trims the line's head).",
                }),
                "cut_from": ("STRING", {
                    "default": "",
                    "tooltip": "Comma-separated markers. Cuts everything from the "
                               "earliest match onward (trims the line's tail).",
                }),
                "rating_filter": (RATING_FILTERS, {"default": "all"}),
            },
            "optional": {
                # managed by the JS widget (checkboxes); comma-separated 1-based line numbers
                "disabled_lines": ("STRING", {"default": ""}),
                # write-back of the final composed prompt, so the *actual* text used
                # ends up in the workflow JSON / PNG metadata
                "resolved": ("STRING", {"default": "", "multiline": True}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, file_path="", **kwargs):
        # re-run when the file or its ratings sidecar change on disk;
        # widget changes already invalidate the cache on their own
        parts = []
        for p in (file_path, _sidecar_path(file_path) if file_path else ""):
            try:
                st = os.stat(p)
                parts.append(f"{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append("x")
        return "|".join(parts)

    def read(self, file_path, line_index, pre_text, after_text, replacements,
             cut_to, cut_from, rating_filter, disabled_lines="", resolved=""):
        entries = _load_entries(file_path)
        ratings = _load_ratings(file_path)
        runnable = _runnable(entries, _parse_disabled(disabled_lines), ratings, rating_filter)

        if not runnable:
            ui = {"deck_state": [{"line": 0, "ordinal": 0, "runnable_total": 0, "resolved": ""}]}
            return {"ui": ui, "result": ("", 0, 0, 0, "empty")}

        ordinal = line_index % len(runnable)
        entry = runnable[ordinal]

        line = _process_line(entry["raw"], cut_to, cut_from, replacements)
        text = f"{pre_text}{line}{after_text}"
        slug = _SLUG_RE.sub("_", line).strip("_")[:60] or "prompt"
        remaining = len(runnable) - ordinal - 1

        ui = {"deck_state": [{
            "line": entry["n"],
            "ordinal": ordinal,
            "runnable_total": len(runnable),
            "resolved": text,
        }]}
        return {"ui": ui, "result": (text, entry["n"], len(runnable), remaining, slug)}


NODE_CLASS_MAPPINGS = {"PromptDeck": PromptDeck}
NODE_DISPLAY_NAME_MAPPINGS = {"PromptDeck": "Prompt Deck 🎴"}


def _register_routes():
    try:
        from server import PromptServer
        from aiohttp import web
    except Exception:
        return  # not running inside ComfyUI (tests, linting)

    routes = PromptServer.instance.routes

    @routes.post("/promptdeck/read_file")
    async def promptdeck_read_file(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        path = (data or {}).get("path", "")
        if not path or not os.path.isfile(path):
            return web.json_response({"ok": False, "lines": [], "ratings": {}})
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            return web.json_response({"ok": False, "lines": [], "ratings": {}})
        return web.json_response({"ok": True, "lines": lines, "ratings": _load_ratings(path)})

    @routes.post("/promptdeck/rate")
    async def promptdeck_rate(request):
        try:
            data = await request.json()
            path = data.get("path", "")
            key = data.get("key", "")
            rating = data.get("rating", "")
        except Exception:
            return web.json_response({"ok": False})
        if not path or not os.path.isfile(path) or not key:
            return web.json_response({"ok": False})
        try:
            _save_rating(path, key, rating)
        except OSError:
            return web.json_response({"ok": False})
        return web.json_response({"ok": True})


_register_routes()
