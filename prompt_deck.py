import json
import os
import re

# leading list numbering like "12. ", "12) ", "12: ", "12 - " — stripped so
# numbered prompt lists paste straight in without the numbers leaking into prompts
_NUMBERING_RE = re.compile(r"^\s*\d{1,4}(?:\s*[.):]|\s+-)\s*")
_SLUG_RE = re.compile(r"[^0-9A-Za-z]+")
# {key} or {key|default}: filled from the vars box, else the default, else left literal
_VAR_RE = re.compile(r"\{([A-Za-z0-9_][A-Za-z0-9_ -]{0,39})(?:\|([^{}]*))?\}")
_BLOCK_HEADER = "## "

RATING_FILTERS = ["all", "good only", "hide bad"]

_RECENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recent_files.json")
_RECENTS_MAX = 10


def _load_recents():
    try:
        with open(_RECENTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        recents = data.get("recents", [])
        return recents if isinstance(recents, list) else []
    except (OSError, ValueError):
        return []


def _remember_recent(path: str):
    recents = _load_recents()
    if recents and recents[0] == path:
        return  # already freshest, skip the disk write
    if path in recents:
        recents.remove(path)
    recents.insert(0, path)
    del recents[_RECENTS_MAX:]
    try:
        with open(_RECENTS_PATH, "w", encoding="utf-8") as f:
            json.dump({"recents": recents}, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


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


def _parse_vars(text: str) -> dict:
    """One `key = value` per line, `#` comments allowed (same format as Cue's subjects_override)."""
    out = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().strip("{}").strip()
        if k:
            out[k] = v.strip()
    return out


def _apply_vars(text: str, vars_map: dict):
    """Fill {key} / {key|default}. Returns (text, unfilled keys)."""
    unfilled = []

    def rep(m):
        key, default = m.group(1).strip(), m.group(2)
        if key in vars_map and vars_map[key] != "":
            return vars_map[key]
        if default is not None:
            return default
        unfilled.append(key)
        return m.group(0)
    return _VAR_RE.sub(rep, text), unfilled


def _process_line(raw: str, cut_to: str, cut_from: str, search: str, replace: str,
                  block: bool = False, vars_map=None):
    """Line mode: numbering stripped, then cuts, then search/replace.
    Block mode: the text verbatim (no numbering strip), vars first, then the same surgery."""
    unfilled = []
    if block:
        text, unfilled = _apply_vars(raw.strip("\n"), vars_map or {})
    else:
        text = _NUMBERING_RE.sub("", raw.strip())
        if vars_map:
            text, unfilled = _apply_vars(text, vars_map)
    text = _apply_cuts(text, cut_to, cut_from)
    if search:
        text = text.replace(search, replace)
    return text, unfilled


def _rating_ok(rating: str, rating_filter: str) -> bool:
    if rating_filter == "good only":
        return rating == "good"
    if rating_filter == "hide bad":
        return rating != "bad"
    return True


def _is_block_text(lines) -> bool:
    return any(line.startswith(_BLOCK_HEADER) for line in lines)


def _block_entries(lines):
    """Block mode: every `## name` starts an entry; its text is everything up to the next header,
    verbatim (inner blank lines kept, outer ones trimmed, `#` comment lines dropped). Text before
    the first header is deck notes and ignored."""
    entries, name, buf = [], None, []

    def flush():
        if name is None:
            return
        text = "\n".join(buf).strip("\n")
        entries.append({"n": len(entries) + 1, "raw": text, "blank": False, "name": name})

    for line in lines:
        if line.startswith(_BLOCK_HEADER):
            flush()
            name, buf = line[len(_BLOCK_HEADER):].strip(), []
        elif name is not None:
            if line.lstrip().startswith("#"):
                continue
            buf.append(line.rstrip("\r"))
    flush()
    return entries


def _load_entries(file_path: str):
    """Line mode: one entry per file line {n, raw, blank}; blank = empty or # comment.
    Block mode (a `## name` header somewhere in the file): one entry per block, with `name`."""
    entries = []
    if not file_path or not os.path.isfile(file_path):
        return entries
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    if _is_block_text(lines):
        return _block_entries(lines)
    for n, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        blank = not stripped or stripped.startswith("#")
        entries.append({"n": n, "raw": raw, "blank": blank})
    return entries


def _entry_key(e) -> str:
    """Ratings key: the block name in block mode (so editing the text keeps the star), else the line."""
    return e["name"] if e.get("name") else e["raw"].strip()


def _runnable(entries, disabled, ratings, rating_filter):
    out = []
    for e in entries:
        if e["blank"] or e["n"] in disabled:
            continue
        if not _rating_ok(ratings.get(_entry_key(e), ""), rating_filter):
            continue
        out.append(e)
    return out


class PromptDeck:
    CATEGORY = "PromptDeck"
    FUNCTION = "read"
    OUTPUT_NODE = True  # can run standalone, no downstream nodes needed
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "filename_slug")
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
                "search": ("STRING", {
                    "default": "",
                    "tooltip": "Text to find in each line (leave empty for no replacement).",
                }),
                "replace": ("STRING", {
                    "default": "",
                    "tooltip": "What `search` gets replaced with.",
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
                # last on purpose: saved workflows keep their widget slots
                "vars": ("STRING", {"default": "", "multiline": True,
                                    "tooltip": "One `key = value` per line. Fills {key} and {key|default} in the prompts, "
                                               "so one scene deck serves every character (look = tall woman in a red coat)."}),
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

    def read(self, file_path, line_index, pre_text, after_text, search, replace,
             cut_to, cut_from, rating_filter, disabled_lines="", resolved="", vars=""):
        entries = _load_entries(file_path)
        block = bool(entries) and bool(entries[0].get("name"))
        vars_map = _parse_vars(vars)
        ratings = _load_ratings(file_path)
        runnable = _runnable(entries, _parse_disabled(disabled_lines), ratings, rating_filter)

        if not runnable:
            ui = {"deck_state": [{"line": 0, "ordinal": 0, "runnable_total": 0, "resolved": ""}]}
            return {"ui": ui, "result": ("", "empty")}

        ordinal = line_index % len(runnable)
        entry = runnable[ordinal]

        line, unfilled = _process_line(entry["raw"], cut_to, cut_from, search, replace, block, vars_map)
        text = f"{pre_text}{line}{after_text}"
        slug_source = entry["name"] if block else line
        slug = _SLUG_RE.sub("_", slug_source).strip("_")[:60] or "prompt"

        ui = {"deck_state": [{
            "line": entry["n"],
            "ordinal": ordinal,
            "runnable_total": len(runnable),
            "resolved": text,
            "mode": "block" if block else "line",
            "unfilled": unfilled,
        }]}
        return {"ui": ui, "result": (text, slug)}


NODE_CLASS_MAPPINGS = {"PromptDeck": PromptDeck}
NODE_DISPLAY_NAME_MAPPINGS = {"PromptDeck": "Prompt Deck"}


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
        _remember_recent(path)
        if _is_block_text(lines):
            entries = _block_entries(lines)
            # `lines` carries the names so an older frontend still shows one row per block
            return web.json_response({"ok": True, "mode": "block", "lines": [e["name"] for e in entries],
                                      "entries": [{"name": e["name"], "text": e["raw"]} for e in entries],
                                      "ratings": _load_ratings(path)})
        return web.json_response({"ok": True, "mode": "line", "lines": lines, "ratings": _load_ratings(path)})

    @routes.get("/promptdeck/recents")
    async def promptdeck_recents(request):
        return web.json_response({"recents": _load_recents()})

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
