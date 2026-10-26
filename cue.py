"""Cue: intent + pictures -> a prompt in the format the target model actually wants.

A small local vision model (Ollama) writes the creative text; this node writes the fixed
scaffolding (MiniMax H3 header lines, subject definitions), validates the result, retries once,
caches, and streams the text into the node while it is being written.

Design: docs/CUE_DESIGN.md. Templates: templates/*.md (a new target model is a new file).
"""
import base64
import hashlib
import io
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_HERE, "templates")
_CACHE_PATH = os.path.join(_HERE, "cue_cache.json")
_CACHE_MAX = {"descriptions": 600, "prompts": 300, "stats": 50}

CUE_VERSION = "1.8.4"  # salts the prompt cache: a code or template change must not serve old prompts
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_WRITER = "huihui_ai/qwen3-vl-abliterated:4b-instruct"
RECOMMENDED_WRITERS = [
    ("huihui_ai/qwen3-vl-abliterated:4b-instruct", "default, 3.3 GB"),
    ("huihui_ai/qwen3-vl-abliterated:2b-instruct", "smallest, 1.9 GB"),
    ("huihui_ai/qwen3-vl-abliterated:8b-instruct", "bigger, ~5 GB"),
]
RECOMMENDED_TEXT_WRITERS = [
    ("huihui_ai/qwen3-abliterated:8b", "text, fast"),
    ("huihui_ai/qwen3-abliterated:30b-a3b", "text, MoE: 30B quality near 8B speed"),
    ("huihui_ai/mistral-small-abliterated:24b", "text, strong prose, daily pick (box bake-off 2026-10-26)"),
    ("hf.co/bartowski/mlabonne_gemma-3-12b-it-abliterated-GGUF:Q8_0", "text, Gemma 3 12B, 12.5 GB (huihui's gemma3 files do not load)"),
    ("hf.co/bartowski/TheDrummer_Cydonia-24B-v4.3-GGUF:Q6_K", "text, Cydonia 24B v4.3, 19 GB"),
]
DETAIL_LEVELS = ["auto", "short", "medium", "long"]
FRAMINGS = ["auto", "extreme close-up", "close-up", "medium shot", "full body", "wide shot"]
FRAMING_TERMS = {
    "extreme close-up": ("extreme close-up", "extreme closeup", "extreme close up", "macro"),
    "close-up": ("close-up", "closeup", "close up"),
    "medium shot": ("medium shot", "medium close-up", "medium close up", "mid shot", "mid-shot", "waist up", "waist-up"),
    "full body": ("full body", "full-body", "full-length", "full length", "head to toe", "head-to-toe", "full figure"),
    "wide shot": ("wide shot", "wide angle", "wide-angle", "establishing shot", "long shot", "far away", "in the distance"),
}
ALL_MODES = ["t2v", "i2v", "first+last", "last", "ref2v", "continuation (DaSiWa)", "continuation (Reel)"]
MODE_TO_DIRECTOR = {"t2v": "T2VA", "i2v": "I2VA", "first+last": "FL2VA", "last": "L2VA", "ref2v": "REF2VA"}
H3_FIELDS_BASE = ["integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"]
H3_FIELDS_REF = ["summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"]
MAX_REFS = 9
IMAGE_LONG_EDGE = 1024
STREAM_EVERY = 0.15  # seconds between UI updates while streaming

SEAM_POLICY = (
    "Continue the same uninterrupted shot. Preserve the subjects' identity, clothing, lighting "
    "and environment. Match their positions and instantaneous motion, the camera, and sound at "
    "the seam. Do not restart the action, repeat completed dialogue, introduce a cut, fade, "
    "title, freeze, loop, or swap identity."
)

_PERSON_WORDS = {
    "man", "woman", "girl", "boy", "guy", "lady", "person", "character", "hero", "heroine",
    "villain", "cop", "detective", "soldier", "nurse", "doctor", "waiter", "waitress", "kid",
    "child", "teen", "teenager", "mother", "father", "mom", "dad", "wife", "husband", "friend",
    "stranger", "agent", "officer", "boss", "killer", "victim", "witness", "suspect", "thief",
    "bride", "groom", "queen", "king", "prince", "princess", "knight", "monk", "nun", "priest",
    "superman", "batman", "model", "actress", "actor", "singer", "dancer", "player",
}

_FRAME_LABEL_RE = re.compile(r"(?i)^\s*(first|start|starting|opening|last|end|ending|final)(\s+frame)?\s*$")
_QUOTE_RE = re.compile(r'"([^"\n]{2,}?)"|“([^”\n]{2,}?)”|«([^»\n]{2,}?)»')
_BRACE_TOKEN_RE = re.compile(r"\{([^{}\n]{1,40})\}")
_ANGLE_LABEL_RE = re.compile(r"<(Subject|Picture|Video|Audio)\s+(\d+)>")
_SHOT_RE = re.compile(r"\[Shot\s+(\d+)\]")
_TIMESTAMP_RE = re.compile(r"\bAt\s+(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?")
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)
_FADE_RE = re.compile(r"\b(?:fade[sd]?\s+(?:in|out|to\s+black|to\s+white)|(?:cuts?|goes|smash\s+cuts?)\s+to\s+black|freeze[\s-]frame|title\s+card)\b", re.I)
_D_TAG_RE = re.compile(r"<d>\s*\[([^\]]*)\]\s*(.*?)</d>", re.S)


# ---------------------------------------------------------------- templates

class Template:
    def __init__(self, path):
        self.path = path
        self.file = os.path.basename(path)
        self.header = {}
        self.sections = {}
        self.mtime = 0
        self._parse()

    def _parse(self):
        with open(self.path, "r", encoding="utf-8") as f:
            text = f.read()
        self.mtime = os.stat(self.path).st_mtime_ns
        header, sections, current, buf = {}, {}, None, []

        def flush():
            if current is not None:
                sections[current] = "\n".join(buf).strip()

        for line in text.splitlines():
            if line.startswith("## "):
                flush()
                current, buf = line[3:].strip().lower(), []
            elif current is None:
                if ":" in line and not line.startswith("#"):
                    k, v = line.split(":", 1)
                    header[k.strip().lower()] = v.strip()
            else:
                buf.append(line)
        flush()
        self.header, self.sections = header, sections

    @property
    def name(self):
        return self.header.get("name") or os.path.splitext(self.file)[0]

    @property
    def kind(self):
        return self.header.get("kind", "image")

    @property
    def modes(self):
        raw = self.header.get("modes", "")
        return [m.strip() for m in raw.split(",") if m.strip()]

    @property
    def timeline(self):
        return self.header.get("timeline", "no").lower() in ("yes", "true", "1")

    @property
    def default_detail(self):
        d = self.header.get("detail", "medium")
        return d if d in ("short", "medium", "long") else "medium"

    @property
    def words(self):
        out = {"short": 60, "medium": 120, "long": 200}
        for part in self.header.get("words", "").split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                try:
                    out[k.strip()] = int(v.strip())
                except ValueError:
                    pass
        return out

    def task(self, mode):
        if mode and ("task " + mode.lower()) in self.sections:
            return self.sections["task " + mode.lower()]
        return self.sections.get("task", "Write the prompt.")

    def info(self):
        return {"name": self.name, "file": self.file, "kind": self.kind, "modes": self.modes,
                "timeline": self.timeline, "detail": self.default_detail}


_templates_cache = {"stamp": None, "items": []}


def load_templates():
    """All templates/*.md, re-read when any file changes. Sorted: H3 first, then by name."""
    try:
        files = sorted(f for f in os.listdir(_TEMPLATES_DIR) if f.lower().endswith(".md"))
    except OSError:
        files = []
    stamp = tuple((f, os.stat(os.path.join(_TEMPLATES_DIR, f)).st_mtime_ns) for f in files)
    if stamp != _templates_cache["stamp"]:
        items = []
        for f in files:
            try:
                items.append(Template(os.path.join(_TEMPLATES_DIR, f)))
            except OSError:
                pass
        items.sort(key=lambda t: (0 if "minimax" in t.file.lower() else 1, t.name.lower()))
        _templates_cache.update(stamp=stamp, items=items)
    return _templates_cache["items"]


def template_names():
    names = [t.name for t in load_templates()]
    return names or ["Generic"]


def find_template(name):
    for t in load_templates():
        if t.name == name:
            return t
    items = load_templates()
    return items[0] if items else None


# -------------------------------------------------------------------- cache

_cache_lock = threading.Lock()


def _cache_load():
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"descriptions": {}, "prompts": {}}


def cache_get(section, key):
    with _cache_lock:
        entry = _cache_load().get(section, {}).get(key)
    return entry.get("v") if isinstance(entry, dict) else None


def cache_put(section, key, value):
    with _cache_lock:
        data = _cache_load()
        bucket = data.setdefault(section, {})
        bucket[key] = {"v": value, "t": time.time()}
        limit = _CACHE_MAX.get(section, 300)
        if len(bucket) > limit:
            for k in sorted(bucket, key=lambda k: bucket[k].get("t", 0))[: len(bucket) - limit]:
                del bucket[k]
        try:
            tmp = _CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, _CACHE_PATH)
        except OSError:
            pass


def _h(*parts):
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- images

def image_to_jpeg(image):
    """ComfyUI IMAGE tensor [B,H,W,C] (first frame) -> (jpeg bytes, sha1). None if no image."""
    if image is None:
        return None, None
    try:
        import numpy as np
        from PIL import Image
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Cue needs Pillow and numpy (ComfyUI ships both)") from e
    frame = image[0] if getattr(image, "ndim", 0) == 4 else image
    arr = frame.detach().cpu().numpy() if hasattr(frame, "detach") else np.asarray(frame)
    arr = (np.clip(arr, 0.0, 1.0) * 255.0).astype("uint8")
    img = Image.fromarray(arr)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((IMAGE_LONG_EDGE, IMAGE_LONG_EDGE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    data = buf.getvalue()
    return data, hashlib.sha1(data).hexdigest()


# -------------------------------------------------------------------- ollama

class OllamaError(RuntimeError):
    pass


def _endpoint(url):
    url = (url or DEFAULT_ENDPOINT).strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def _unreachable(endpoint, exc):
    return OllamaError(
        f"Ollama not reachable at {endpoint} ({exc}). Install it from ollama.com, start it, "
        f"then click ↻ on the node. The Windows box already has it; check the endpoint field.")


def ollama_tags(endpoint):
    endpoint = _endpoint(endpoint)
    try:
        with urllib.request.urlopen(endpoint + "/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise _unreachable(endpoint, e)
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def _interrupted():
    try:
        import comfy.model_management as mm
        return mm.processing_interrupted()
    except Exception:
        return False


def _raise_interrupt():
    try:
        import comfy.model_management as mm
        raise mm.InterruptProcessingException()
    except ImportError:
        raise KeyboardInterrupt()


def ollama_chat(endpoint, model, system, user, images=None, seed=0, temperature=0.7,
                num_predict=1200, on_chunk=None, timeout=180, schema=None, on_token=None):
    """Streamed /api/chat. Returns the full text. keep_alive 0 frees the VRAM right after."""
    endpoint = _endpoint(endpoint)
    message = {"role": "user", "content": user}
    if images:
        message["images"] = [base64.b64encode(b).decode("ascii") for b in images]
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, message],
        "stream": True,
        "keep_alive": 0,
        "options": {"temperature": temperature, "seed": int(seed) & 0x7FFFFFFF,
                    "num_predict": int(num_predict), "num_ctx": 16384, "repeat_penalty": 1.15},
    }
    if schema:
        body["format"] = schema  # Ollama constrains the output to this JSON schema
    req = urllib.request.Request(endpoint + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        if e.code == 404 or "not found" in detail.lower():
            raise OllamaError(f"Model '{model}' is not installed in Ollama at {endpoint}. "
                              f"Pick it from models ▾ to pull it, or run: ollama pull {model}")
        raise OllamaError(f"Ollama error {e.code}: {detail or e.reason}")
    except (urllib.error.URLError, OSError) as e:
        raise _unreachable(endpoint, e)

    out, last, n_tokens = [], 0.0, 0
    with resp:
        for raw in resp:
            if _interrupted():
                _raise_interrupt()
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if evt.get("error"):
                raise OllamaError(f"Ollama: {evt['error']}")
            piece = (evt.get("message") or {}).get("content", "")
            if piece:
                out.append(piece)
                n_tokens += 1
                if on_token:
                    on_token(n_tokens)
                now = time.time()
                if on_chunk and now - last >= STREAM_EVERY:
                    last = now
                    on_chunk("".join(out))
            if evt.get("done"):
                break
    text = _THINK_RE.sub("", "".join(out)).strip()
    if on_chunk:
        on_chunk(text)
    return text


def ollama_pull(endpoint, model, on_progress):
    endpoint = _endpoint(endpoint)
    req = urllib.request.Request(endpoint + "/api/pull",
                                 data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except (urllib.error.URLError, OSError) as e:
        raise _unreachable(endpoint, e)
    with resp:
        for raw in resp:
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if evt.get("error"):
                raise OllamaError(evt["error"])
            on_progress(evt)


# ---------------------------------------------------------------- subjects

def slug_token(label):
    s = re.sub(r"[^0-9a-z]+", "_", str(label).strip().lower()).strip("_")
    return s[:40] or "subject"


def is_short_label(label):
    return len(str(label).split()) <= 3


def looks_like_person(label):
    words = set(re.findall(r"[a-z]+", str(label).lower()))
    return bool(words & _PERSON_WORDS)


def parse_overrides(text):
    """'token = description' per line. Token with or without {} / <> / 'Subject N'."""
    out = {}
    for line in str(text or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().strip("{}<>").strip()
        v = v.strip()
        if not k or not v:
            continue
        m = re.fullmatch(r"(?i)subject\s*(\d+)", k)
        out[("subject", int(m.group(1))) if m else slug_token(k)] = v
    return out


def extract_quotes(text):
    out = []
    for m in _QUOTE_RE.finditer(text or ""):
        q = next(g for g in m.groups() if g)
        out.append(q.strip())
    return out


def split_subjects_block(text):
    """Parse the optional 'subjects:' ... 'prompt:' protocol. Returns (subjects dict, body)."""
    subjects = {}
    lines = text.splitlines()
    s_idx = next((i for i, l in enumerate(lines) if l.strip().lower().rstrip(":") == "subjects"), None)
    if s_idx is None:
        # no header, but the answer may still open with "{token} = description" lines
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            m = re.match(r"^\{?\s*([A-Za-z][A-Za-z0-9_ ]{0,39}?)\s*\}?\s*=\s*(.+)$", line)
            if not line:
                i += 1
                continue
            if m and ":" not in m.group(1):
                subjects[slug_token(m.group(1))] = m.group(2).strip().rstrip(".")
                i += 1
                continue
            break
        rest = "\n".join(lines[i:]).strip()
        rest = re.sub(r"(?im)^\s*prompt\s*:\s*", "", rest, count=1)
        return subjects, (rest if subjects else text.strip())
    p_idx = next((i for i, l in enumerate(lines) if i > s_idx and re.match(r"(?i)^\s*\**prompt\**\s*:", l)), None)
    end = p_idx if p_idx is not None else len(lines)
    body_start = p_idx + 1 if p_idx is not None else None
    if p_idx is not None:
        tail = re.sub(r"(?i)^\s*\**prompt\**\s*:\s*", "", lines[p_idx]).strip()
        if tail:
            lines[p_idx] = tail
            body_start = p_idx
    for i in range(s_idx + 1, end):
        line = lines[i].strip()
        if not line:
            if p_idx is None:
                body_start = i + 1
                break
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = slug_token(k.strip().strip("{}"))
            if k and v.strip():
                subjects[k] = v.strip().rstrip(".")
    body = "\n".join(lines[body_start:]).strip() if body_start is not None else ""
    if not body:
        body = "\n".join(lines[:s_idx]).strip()
    return subjects, body


_PROPER_NAMES = {"superman", "batman"}
_PLACEMENT_RE = re.compile(r"[,;]?\s+\b(?:standing|sitting|lying|kneeling|walking|running|leaping|holding|looking|leaning|facing|gazing|staring|saving|fighting|caught|seen|shown|positioned|placed|set)\b", re.I)


def clean_description(desc, person=True):
    """Appearance only: cut a person's description before placement/action phrases and cap
    its length, so substituting it into the body never drags the scene along."""
    desc = " ".join(str(desc).split()).strip().rstrip(".,;")
    if person:
        m = _PLACEMENT_RE.search(desc)
        if m and len(desc[: m.start()].split()) >= 4:
            desc = desc[: m.start()].rstrip(",; ")
    words = desc.split()
    if len(words) > 32:
        cut = desc.rfind(",", 0, len(" ".join(words[:32])))
        desc = (desc[:cut] if cut > 20 else " ".join(words[:32])).rstrip(",; ")
    return desc


def clean_frame_description(desc, max_words=45):
    """For '<Picture N> is the first frame of [Shot 1], showing ...': drop 'the image is' openers,
    cap the length at a sentence boundary."""
    desc = " ".join(str(desc).split()).strip()
    desc = re.sub(r"(?i)^(this|the)\s+(image|picture|frame|photo|still)\s+(is|shows|depicts|features)\s+(a|an)?\s*", "", desc)
    desc = re.sub(r"(?i)^(a|an)\s+(film still|photograph|picture|image)\s+(with|in|of)\s+", "", desc)
    words = desc.split()
    if len(words) > max_words:
        head = " ".join(words[:max_words])
        cut = max(head.rfind(". "), head.rfind("; "))
        desc = head[:cut + 1] if cut > 40 else head.rstrip(",;") + "."
    return desc.rstrip(",;")


def token_handle(tok):
    word = tok.replace("_", " ")
    if word in _PROPER_NAMES:
        return word.capitalize()
    return "the " + word


_ARTICLE_RE = re.compile(r"^(a|an|the)\s+", re.IGNORECASE)
# a determiner within the last three words before the token: "The {w}", "A lone {w}", "by a {w}"
_TRAILING_ARTICLE_RE = re.compile(r"(^|[^\w-])(a|an|the)\s+([\w-]+\s+){0,2}$", re.IGNORECASE)


def _drop_overlap(before, desc):
    """'a young {woman}' + 'a young woman in her twenties' -> 'a young ' + 'woman in her twenties':
    words the writer already put in front of the token are not repeated by the description."""
    bw = before.rstrip().split()
    dw = desc.split()
    while bw and dw and bw[-1].lower().strip(",.;") == dw[0].lower().strip(",.;"):
        bw.pop()
        dw.pop(0)
    return " ".join(dw)


def substitute_tokens(body, subjects):
    """First mention of a token gets the full description, later mentions a short handle.
    'The {woman}' + 'a young woman ...' must not become 'The a young woman': when the text before
    the token already ends in an article, the description's own article is dropped."""
    seen = set()

    def rep(m):
        tok = slug_token(m.group(1))
        if tok not in subjects:
            return m.group(0)
        if tok in seen:
            if _TRAILING_ARTICLE_RE.search(m.string[:m.start()]):
                return tok.replace("_", " ")  # "A tall {woman}" -> "A tall woman", never "A tall the woman"
            return token_handle(tok)
        seen.add(tok)
        desc = subjects[tok]
        # the writer restated the description right next to the token ("{woman} in her twenties,
        # slender, ..."): substituting the full description would double it and trip the
        # repetition trimmer, so use the short handle instead
        probe = _ARTICLE_RE.sub("", desc, count=1).lower()[:28]
        around = re.sub(r"[{}]", "", m.string[max(0, m.start() - 160):m.end() + 160]).lower()
        if probe and probe in around:
            return tok.replace("_", " ") if _TRAILING_ARTICLE_RE.search(m.string[:m.start()]) else token_handle(tok)
        before = m.string[:m.start()]
        if _TRAILING_ARTICLE_RE.search(before):
            desc = _ARTICLE_RE.sub("", desc, count=1)
        desc = _drop_overlap(before, desc) or desc
        return desc
    out = _BRACE_TOKEN_RE.sub(rep, body)
    # a writer that wraps a whole description in braces ({an older woman in a cardigan}) is not
    # using a token; the model must never see literal braces
    return re.sub(r"\{([^{}]+)\}", r"\1", out)


_META_LINE_RE = re.compile(r"(?im)^\s*[-—–(\[]*\s*(word count:?\s*)?\d+\s*words\s*[-—–)\]]*\s*$")


_META_TAIL_RE = re.compile(r"\s*[-—–(\[]*\s*(word count:?\s*)?\d+\s*words\s*[-—–)\]]*\s*$", re.IGNORECASE)


_META_PARA_RE = re.compile(r"(?is)\n\s*(?:[-—–]{2,}\s*)?\n\s*(?:note|notes|n\.b\.|nb|comment|explanation)\s*:.*$")


def strip_meta_lines(text):
    """Small writers append '— 120 words —', '(118 words)' or a trailing 'Note: ...' paragraph. Not prompt text."""
    text = _META_PARA_RE.sub("", text)
    text = _META_LINE_RE.sub("", text)
    text = _META_TAIL_RE.sub("", text.rstrip())
    return text.strip()


_PALETTE_PATH = os.path.join(_HERE, "templates", "palette.md")
_PALETTE_AXES = ("setting", "time_weather", "light", "camera", "finish", "mood", "subject_person",
                 "subject_other", "object", "colour")


_PERSON_HINT_RE = re.compile(r"(?i)\b(he|she|they|him|her|his|hers|man|woman|men|women|girl|boy|guy|lady|person|people|couple|crowd|child|kid|someone|anyone|face|portrait|body|hand|hands|self|selfie|\w+ing)\b")


def draw_palette(seed, intent=""):
    """One seeded line per axis of templates/palette.md, as a suggestion block for the writer.
    Small models fall into one house style whenever the intent leaves a choice open; concrete,
    seed-chosen material gives them something to vary on. The draw mixes in the intent, so
    two intents never share a palette on the same seed, and the person axis is dropped when the
    intent names no person. Empty string if there is no palette."""
    try:
        with open(_PALETTE_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    axes, current = {}, None
    for line in lines:
        if line.startswith("## "):
            current = line[3:].strip().lower()
            axes[current] = []
        elif current and line.startswith("- ") and line[2:].strip():
            axes[current].append(line[2:].strip())
    rng = random.Random(int(seed) * 7919 + 17 + int(hashlib.sha1(" ".join(str(intent).lower().split()).encode("utf-8")).hexdigest()[:8], 16))
    person = bool(_PERSON_HINT_RE.search(str(intent))) or looks_like_person(intent)
    skip = () if person else ("subject_person",)
    recent = cache_get("stats", "palette_recent") or {}
    # two axes per run, from the ones that make a scene; ten fragments at once make a collage
    scene_axes = [a for a in ("setting", "time_weather", "light", "mood", "finish") if axes.get(a)]
    chosen = rng.sample(scene_axes, min(2, len(scene_axes))) if scene_axes else []
    picks = []
    for axis in _PALETTE_AXES:
        pool = axes.get(axis) or []
        if not pool or axis in skip or axis not in chosen:
            continue
        used = recent.get(axis, [])
        fresh = [x for x in pool if x not in used] if len(pool) > 2 * len(used) else pool
        pick = rng.choice(fresh or pool)
        picks.append((axis, pick))
        recent[axis] = (used + [pick])[-20:]
    cache_put("stats", "palette_recent", recent)
    if not picks:
        return ""
    body = "; ".join(f"{axis.replace('_', ' ')} — {text}" for axis, text in picks)
    return ("Two seeded suggestions for this picture, to use only where the intent leaves the choice open "
            "(the intent always wins; drop either if it clashes): " + body + ".")


def trim_repetition(text):
    """Small models loop. Cut where a clause pair repeats an earlier consecutive pair, or a
    clause shows up for the third time. Keeps the original text (and its line breaks) up to
    the cut. Returns (text, trimmed?)."""
    clauses = [(m.start(), " ".join(m.group(0).lower().split())) for m in re.finditer(r"[^.;,\n]+[.;,]?", text)]
    seen_pairs, counts = set(), {}
    prev = None
    for i, (pos, norm) in enumerate(clauses):
        if len(norm) < 20:
            prev = None
            continue
        counts[norm] = counts.get(norm, 0) + 1
        pair = (prev, norm) if prev else None
        if counts[norm] >= 3 or (pair and pair in seen_pairs):
            cut = clauses[i - 1][0] if pair and pair in seen_pairs and i > 0 else pos
            return text[:cut].rstrip(), True
        if pair:
            seen_pairs.add(pair)
        prev = norm
    return text, False


def auto_tokenize(body, subjects):
    """A defined token the model never used, but whose bare word is in the body
    ("superman is standing"), is tokenized on word boundaries (so "man" never hits "woman")."""
    for tok in subjects:
        if re.search(r"\{\s*" + re.escape(tok) + r"\s*\}", body, re.I):
            continue
        word = tok.replace("_", " ")
        if len(word) < 3:
            continue
        body = re.sub(r"(?i)\b(?:the |a |an )?" + re.escape(word) + r"\b", "{" + tok + "}", body, count=1)
    return body


_D_ANY_RE = re.compile(r"<d>(.*?)</d>", re.S)


def normalize_d_tags(body, language):
    """<d>words</d> without a language tag gets one; quotes directly inside a tag go."""
    def rep(m):
        inner = m.group(1).strip()
        lang = language
        lm = re.match(r"^\[([^\]]*)\]\s*(.*)$", inner, re.S)
        if lm:
            lang, inner = lm.group(1).strip() or language, lm.group(2).strip()
        inner = inner.strip().strip('"\u201c\u201d\u00ab\u00bb').strip()
        return f"<d>[{lang}] {inner}</d>"
    return _D_ANY_RE.sub(rep, body)


def _inside_d_tag(body, idx):
    return any(m.start() <= idx < m.end() for m in _D_ANY_RE.finditer(body))


def fix_dialogue(body, quotes, language):
    """A line the user quoted that sits in plain quotes gets wrapped as H3 speech."""
    verbs = ("says", "whispers", "shouts", "asks", "replies", "mutters", "adds", "answers", "yells", "murmurs")
    body = normalize_d_tags(body, language)
    for q in quotes:
        if any(q in words for _, words in _D_TAG_RE.findall(body)):
            continue
        for opener, closer in (('"', '"'), ("\u201c", "\u201d"), ("\u00ab", "\u00bb"), ("'", "'"), ("\u2018", "\u2019")):
            needle = opener + q + closer
            idx = body.find(needle)
            while idx >= 0 and _inside_d_tag(body, idx):
                idx = body.find(needle, idx + 1)
            if idx < 0:
                continue
            before, after = body[:idx], body[idx + len(needle):]
            tag = f"<d>[{language}] {q}</d>"
            stripped = before.rstrip()
            if stripped.endswith(verbs):
                before = stripped + ": "
            elif stripped.endswith(":"):
                before = stripped + " "
            elif before and not before.endswith(" "):
                before = before + " "
            body = before + tag + after
            break
    return body


# ------------------------------------------------------------ JSON protocol

def field_names(mode, has_modes, is_cont):
    if has_modes and mode == "ref2v":
        return list(H3_FIELDS_REF)
    if has_modes and mode == "continuation (DaSiWa)":
        return ["next_action"]
    if has_modes:
        return list(H3_FIELDS_BASE)
    return ["prompt"]


def beat_budget(seconds):
    """How many actions fit: one per ~2.5 s. 5 s -> 2, 10 s -> 4, 15 s -> 6."""
    return max(1, int(round(float(seconds) / 2.5)))


def shot_budget(seconds):
    return 1 if seconds <= 5.5 else (2 if seconds <= 10.5 else 3)


def schema_for(fields, use_brace_tokens, with_beats):
    """JSON schema handed to Ollama's `format`. Key order is generation order: subjects, then
    the beat plan, then the prose fields, so the model plans before it writes."""
    props = {}
    if use_brace_tokens:
        props["subjects"] = {"type": "array", "items": {"type": "object", "properties": {
            "token": {"type": "string"}, "description": {"type": "string"}},
            "required": ["token", "description"]}}
    if with_beats:
        props["beats"] = {"type": "array", "items": {"type": "object", "properties": {
            "start_seconds": {"type": "number"}, "action": {"type": "string"}},
            "required": ["start_seconds", "action"]}}
    for f in fields:
        props[f] = {"type": "string", "minLength": 1}  # an empty sound field is a missing field
    return {"type": "object", "properties": props, "required": list(props)}


def _unescape(text):
    try:
        return json.loads('"' + text + '"')
    except ValueError:
        return text.replace("\\n", "\n").replace('\\"', '"')


_JSON_STR = r'"((?:[^"\\]|\\.)*)'  # a JSON string body, closing quote optional (truncation)


def parse_json_answer(raw, fields):
    """{"subjects": {token: desc}, "beats": [{"start", "action"}], "fields": {name: text}} or
    None when the answer is not JSON. A truncated answer is salvaged key by key."""
    text = raw.strip()
    start = text.find("{")
    if start < 0:
        return None
    text = text[start:]
    data = None
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
    except ValueError:
        data = None
    out = {"subjects": {}, "beats": None, "fields": {}}
    if data is not None:
        for item in data.get("subjects") or []:
            if isinstance(item, dict) and item.get("token") and item.get("description"):
                out["subjects"][slug_token(item["token"])] = str(item["description"]).strip().rstrip(".")
        if isinstance(data.get("beats"), list):
            out["beats"] = []
            for b in data["beats"]:
                if isinstance(b, dict):
                    try:
                        out["beats"].append({"start": float(b.get("start_seconds", 0)), "action": str(b.get("action", ""))})
                    except (TypeError, ValueError):
                        pass
        for f in fields:
            v = data.get(f)
            if isinstance(v, str):
                out["fields"][f] = v
    else:
        # salvage from a cut-off answer: every string field we can find, then subjects and beats
        for f in fields:
            m = re.search(r'"' + re.escape(f) + r'"\s*:\s*' + _JSON_STR, text)
            if m:
                out["fields"][f] = _unescape(m.group(1))
        for m in re.finditer(r'"token"\s*:\s*"([^"]*)"\s*,\s*"description"\s*:\s*' + _JSON_STR, text):
            out["subjects"][slug_token(m.group(1))] = _unescape(m.group(2)).strip().rstrip(".")
        beats = [{"start": float(a), "action": _unescape(b)} for a, b in
                 re.findall(r'"start_seconds"\s*:\s*([0-9.]+)\s*,\s*"action"\s*:\s*' + _JSON_STR, text)]
        if beats:
            out["beats"] = beats
        if not out["fields"]:
            return None
    return out


_PREVIEW_KEYS = ("integrated_multimodal_description", "detailed_description", "summary", "retention_analysis",
                 "overall_soundscape", "non_diegetic_music", "prompt", "next_action")


def json_stream_preview(text):
    """What the node shows while JSON streams in: the prose fields so far, readable."""
    if not text.lstrip().startswith("{"):
        return text
    parts = []
    for m in re.finditer(r'"(' + "|".join(_PREVIEW_KEYS) + r')"\s*:\s*"', text):
        val = re.match(r'(?:[^"\\]|\\.)*', text[m.end():])
        parts.append(f"{m.group(1)}: {_unescape(val.group(0)) if val else ''}")
    if parts:
        return "\n\n".join(parts)
    if '"beats"' in text:
        return "planning beats…"
    if '"subjects"' in text:
        return "defining subjects…"
    return "…"


_SPEAKER_RE = re.compile(r"(?i)\b([a-z]+)\s+(?:says|whispers|shouts|asks|replies|mutters|yells|murmurs|answers)\b")
_NOT_SPEAKERS = {"and", "then", "who", "that", "which", "but", "or", "as", "while", "before", "after"}


def guess_speaker(intent, quote):
    before = intent[: intent.find(quote)] if quote in intent else intent
    word = None
    for m in _SPEAKER_RE.finditer(before):
        word = m.group(1).lower()
    if not word or word in _NOT_SPEAKERS:
        m = re.match(r"\s*(?:(?:the|a|an)\s+)?([A-Za-z]+)", intent)
        word = m.group(1).lower() if m else "voice"
    if word in ("she", "he", "they"):
        return word.capitalize()
    if word in _PROPER_NAMES:
        return word.capitalize()
    return "The " + word


def append_dialogue(body, intent, quotes, language):
    """A quoted line the writer dropped is added at the end of the description as speech, in the
    user's exact words. Speaker from the intent ("she says", "the detective says") or 'a voice'.
    Returns (body, appended_count)."""
    n = 0
    for q in quotes:
        if any(q in words for _, words in _D_TAG_RE.findall(body)):
            continue
        who = guess_speaker(intent, q)
        body = body.rstrip()
        if body and not body.endswith((".", "!", "?")):
            body += "."
        body += f" {who} (S1) says: <d>[{language}] {q}</d>"
        n += 1
    return body, n


_END_CUT_RE = re.compile(r"\[Shot\s+\d+\]\s*At\s+(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\s*,?\s*(?:the\s+camera\s+cuts\s+to\s*)?", re.I)


_LOOSE_TS_RE = re.compile(r"\bAt\s+(\d{1,2})(?:[:.](\d{1,3}))?(?:[:.](\d{1,3}))?(\s*(?:s|sec|seconds?)\b)?")


def normalize_timestamps(text):
    """Cut markers in the shapes small writers produce become 'At 00:SS.mmm':
    'At 0.500' / 'At 2.5 s' (seconds), 'At 04:591' (seconds:millis), 'At 02:75' (seconds.hundredths),
    'At 00:04.500' (already right)."""
    def rep(m):
        a, b, c, unit = m.group(1), m.group(2), m.group(3), m.group(4)
        sep = m.group(0)[len("At ") + len(a): len("At ") + len(a) + 1] if b is not None else ""
        try:
            if b is None:
                secs = float(a)
            elif c is not None:
                mm, ss, ms = int(a), int(b), int(c.ljust(3, "0"))
                secs = mm * 60 + ss + ms / 1000.0 if ss < 60 else None
            elif sep == "." or unit:
                secs = float(f"{a}.{b}")
            elif len(b) == 3:
                secs = int(a) + int(b) / 1000.0           # 04:591 -> 4.591
            elif int(b) >= 60:
                secs = int(a) + int(b) / 100.0            # 02:75 -> 2.75
            else:
                secs = int(a) * 60 + int(b)               # mm:ss
            if secs is None:
                return m.group(0)
        except ValueError:
            return m.group(0)
        return f"At {int(secs // 60):02d}:{secs % 60:06.3f}"
    return _LOOSE_TS_RE.sub(rep, text)


def repair_end_cuts(text, seconds):
    """A cut at or past the clip end is dropped: its shot marker and timestamp go, the sentence
    continues the previous shot. Returns (text, removed_count)."""
    removed = 0

    def rep(m):
        nonlocal removed
        t = int(m.group(1)) * 60 + int(m.group(2)) + (int(m.group(3).ljust(3, "0")) / 1000.0 if m.group(3) else 0)
        if t >= seconds - 0.05:
            removed += 1
            return ""
        return m.group(0)
    out = _END_CUT_RE.sub(rep, text)
    if removed:
        out = re.sub(r"\s{2,}", " ", out).strip()
        out = out[0].upper() + out[1:] if out else out
    return out, removed


_SHOT_MARKER_RE = re.compile(r"\[Shot\s+(\d+)\]\s*(?:At\s+\d{1,2}:\d{2}(?:\.\d{1,3})?\s*,?\s*(the\s+camera\s+cuts\s+to\s*)?)?", re.I)


def repair_extra_shots(text, max_shots):
    """Shots beyond the budget lose their marker and cut timestamp; the text runs on as the
    previous shot. Returns (text, removed_count)."""
    removed = 0

    def rep(m):
        nonlocal removed
        if int(m.group(1)) > max_shots:
            removed += 1
            return "The camera moves to " if m.group(2) else ""
        return m.group(0)
    out = _SHOT_MARKER_RE.sub(rep, text)
    if removed:
        out = re.sub(r"\s{2,}", " ", out).strip()
    return out, removed


def validate_beats(beats, seconds, budget):
    errors, warnings = [], []
    if len(beats) > budget:
        errors.append(f"too many beats: {len(beats)} actions for {seconds:.1f}s, at most {budget}; "
                      "keep the essential ones and give each its onset, development and end")
    last = -1.0
    for b in beats:
        t = b.get("start", 0.0)
        if t >= seconds - 0.05:
            errors.append(f"beat at {t:.2f}s starts at or past the clip end ({seconds:.2f}s)")
        if t < last:
            warnings.append("beats are not in time order")
        last = t
    if not beats:
        warnings.append("no beats planned")
    return errors, warnings


# ------------------------------------------------------------------- H3 math

def h3_frames(seconds):
    """Nearest 17k+5 frame count for a full clip (24 fps)."""
    k = max(0, int(round((float(seconds) * 24.0 - 5) / 17.0)))
    return 17 * k + 5


def h3_new_frames(seconds):
    """Continuation: newly visible frames, a multiple of 17."""
    return 17 * max(1, int(round(float(seconds) * 24.0 / 17.0)))


def h3_header(mode, seconds, shots):
    s = f"{seconds:.2f}"
    if mode == "i2v":
        return "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
    if mode == "first+last":
        return ("How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns "
                f"with the 0.00-second mark of the target video; Picture 2 (from Shot {shots}) aligns with "
                f"the {s}-second mark of the target video.")
    if mode == "last":
        return ("How the reference pictures align with the target video — <Picture 1> (from "
                f"[Shot {shots}]) aligns with the {s}-second mark of the target video.")
    return ""


def shots_in(text):
    nums = [int(n) for n in _SHOT_RE.findall(text or "")]
    return max(nums) if nums else 1


def extract_fields(text, names):
    """Pull 'name: ...' fields out of model output, tolerant of chatter before the first one and
    of decorated names ([summary]:, **summary**:, ## summary:). Fields are assigned in order:
    each one is the first occurrence after the previous field, so a stray early mention
    ("detailed_description: [summary]: ...") does not hijack the real field.
    Returns (dict field -> text, errors)."""
    errors, positions = [], []
    deco_l, deco_r = r"[\[\(\*#_`]*", r"[\]\)\*_`]*"
    last_end = -1
    for name in names:
        # plain "summary" needs its colon (it is an English word); a decorated "[summary]" / "**summary**" does not
        pat = re.compile(r"(?i)(?<![A-Za-z_])(?:" + deco_l[:-1] + "+" + re.escape(name) + deco_r + r"\s*:?\s*|"
                         + re.escape(name) + r"(?![A-Za-z_])\s*:\s*)")
        hit = None
        for m in pat.finditer(text):
            if m.start() > last_end:
                hit = m
                break
        if hit is None:
            errors.append(f"missing field '{name}'" if not pat.search(text) else f"field '{name}' is out of order")
            continue
        positions.append((hit.start(), hit.end(), name))
        last_end = hit.end()
    found = {}
    for i, (start, end, name) in enumerate(positions):
        stop = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        found[name] = text[end:stop].strip()
        if not found[name]:
            errors.append(f"field '{name}' is empty")
    return found, errors


def validate_h3(fields, mode, seconds, defined_labels, quotes, language):
    errors, warnings = [], []
    body_name = "detailed_description" if mode == "ref2v" else "integrated_multimodal_description"
    body = fields.get(body_name, "")
    if body and "[Shot 1]" not in body:
        errors.append(f"{body_name} must start with [Shot 1]")
    for m in _TIMESTAMP_RE.finditer(body):
        t = int(m.group(1)) * 60 + int(m.group(2)) + (int(m.group(3).ljust(3, "0")) / 1000.0 if m.group(3) else 0)
        if t >= seconds - 0.05:
            is_cut = bool(_SHOT_RE.search(body[max(0, m.start() - 14): m.start()])) or "cut" in body[m.end(): m.end() + 60].lower()
            (errors if is_cut else warnings).append(
                f"{'cut' if is_cut else 'beat'} at {t:.3f}s is at or past the clip end ({seconds:.2f}s)")
    whole = "\n".join(fields.values())
    if _FADE_RE.search(whole):
        errors.append("no fades, cuts to black, titles or freeze frames: end on the action still in motion")
    for kind, num in _ANGLE_LABEL_RE.findall(whole):
        if f"<{kind} {num}>" not in defined_labels:
            errors.append(f"undefined label <{kind} {num}>")
    for lab in defined_labels:
        if lab.startswith("<Subject") and lab not in whole:
            warnings.append(f"{lab} is defined but never used")
    if quotes:
        spoken = [(lang.strip(), words.strip()) for lang, words in _D_TAG_RE.findall(whole)]
        for q in quotes:
            hit = [s for s in spoken if q in s[1]]
            if not hit:
                errors.append(f'dialogue "{q}" must appear verbatim inside <d>[{language}] ... </d>')
            elif language and all(s[0].lower() != language.lower() for s in hit):
                warnings.append(f'dialogue "{q}" is tagged {hit[0][0]}, expected [{language}]')
    return errors, warnings


# ---------------------------------------------------------------- the node

def _send(unique_id, payload):
    try:
        from server import PromptServer
        PromptServer.instance.send_sync("promptdeck.cue", {"node": unique_id, **payload})
    except Exception:
        pass


class _Job:
    """One run: collects everything needed to build, call, validate, and report."""

    def __init__(self, node_inputs):
        self.i = node_inputs
        self.warnings = []
        self.stages = []
        self._hb_stop = threading.Event()
        self._hb_thread = None
        self._hb_text = ""
        self._hb_t0 = time.time()

    # ---- heartbeat: a status message every 2 s for as long as Ollama keeps us waiting, so the
    # websocket never sits idle while a big model loads (an idle socket through a proxy dies
    # without a close frame, and the tab then looks connected but is not) ----
    def heartbeat_start(self, text):
        self._hb_text, self._hb_t0 = text, time.time()
        if self._hb_thread and self._hb_thread.is_alive():
            return
        self._hb_stop.clear()

        def loop():
            while not self._hb_stop.wait(2.0):
                if _interrupted() or time.time() - self._hb_t0 > 1800:
                    break
                _send(self.i["unique_id"], {"event": "stage", "text": f"{self._hb_text} {int(time.time() - self._hb_t0)} s"})
        self._hb_thread = threading.Thread(target=loop, daemon=True)
        self._hb_thread.start()

    def heartbeat_stop(self):
        self._hb_stop.set()

    # ---- descriptions (stage A, cached per picture) ----
    def describe(self, jpeg, digest, role, label=""):
        if jpeg is None:
            return ""
        key = _h("desc2", digest, role, slug_token(label) if label else "", self.i["vision_writer"])
        hit = cache_get("descriptions", key)
        if hit:
            return hit
        if role in ("start", "last"):
            ask = ("Describe this picture as a frame of a film shot. State: the visual style; each "
                   "subject with age range, build, hair, skin, clothing with colours, pose and expression; "
                   "where each subject sits in the frame; the setting with two or three key objects and "
                   "their placement; the light (source, direction, colour); camera distance and angle. "
                   "Plain English prose, 4 to 7 sentences, no opinions, no story, no questions.")
        elif label and is_short_label(label) and looks_like_person(label):
            ask = (f"This picture is the reference for '{label}'. In one or two sentences describe the "
                   f"{label} for a film script: age range, build, hair, skin, clothing with colours, and "
                   "one distinguishing detail. No name, no story, no opinions.")
        elif label:
            ask = (f"This picture is the reference for '{label}'. In two or three sentences describe it "
                   "for a film script: if it shows a person, their appearance; if a place or object, its "
                   "layout, materials, light and key details. No story, no opinions.")
        else:
            ask = ("In two or three sentences describe what this picture shows for a film script: a "
                   "person's appearance, or a place's layout, light and key objects. No story, no opinions.")
        what = role if role in ("start", "last") else (label or "reference")
        self.stage(f"looking at {what}")
        print(f"[Cue] looking at {what} with {self.i['vision_writer']}")
        self.heartbeat_start(f"looking at {what} ({self.i['vision_writer'].split('/')[-1]})…")
        try:
            text = ollama_chat(self.i["endpoint"], self.i["vision_writer"],
                               "You describe pictures precisely and briefly for film scripts. Adults only, uncensored: say what is shown, including nudity.",
                               ask, images=[jpeg], seed=7, temperature=0.2, num_predict=300)
        finally:
            self.heartbeat_stop()
        text = " ".join(text.split())
        if text:
            cache_put("descriptions", key, text)
        return text

    def stage(self, text):
        self.stages.append(text)
        _send(self.i["unique_id"], {"event": "stage", "text": text})

    # ---- progress: ComfyUI's node bar, an ETA in the node's bar, and console lines ----
    def begin(self, expected_tokens, label, model=None):
        self.model = model or self.i["writer"]
        self.t0, self.count, self.expected = time.time(), 0, max(50, int(expected_tokens))
        rate = cache_get("stats", _h("rate", self.model))
        self.rate = float(rate) if rate else 0.0
        self._last_tick = 0.0
        try:
            import comfy.utils
            self.pbar = comfy.utils.ProgressBar(self.expected)
        except Exception:
            self.pbar = None
        eta = f", ~{self.expected / self.rate:.0f} s" if self.rate else ""
        print(f"[Cue] {label} with {self.model} (up to {self.expected} tokens{eta})")
        self.stage(label + (f"… ~{self.expected / self.rate:.0f} s" if self.rate else "…"))
        self.heartbeat_start(f"loading {self.model.split('/')[-1]}…")

    def token(self, n):
        self.count = n
        if self.pbar:
            self.pbar.update_absolute(min(n, self.expected - 1), self.expected)
        now = time.time()
        if now - self._last_tick >= 1.0:
            self._last_tick = now
            elapsed = now - self.t0
            rate = n / elapsed if (elapsed > 1.0 and n >= 20) else self.rate  # early tokens include the load time
            pct = int(100 * min(n, self.expected) / self.expected)
            left = f" · ~{max(0.0, self.expected - n) / rate:.0f} s left" if rate else ""
            self._hb_text, self._hb_t0 = "writing…", self.t0  # the heartbeat reports elapsed time, tokens report progress
            _send(self.i["unique_id"], {"event": "stage", "text": f"writing {pct}%{left}"})

    def end(self, words, valid):
        self.heartbeat_stop()
        elapsed = time.time() - self.t0
        if self.count and elapsed > 0:
            cache_put("stats", _h("rate", self.model), self.count / elapsed)
        if self.pbar:
            self.pbar.update_absolute(self.expected, self.expected)
        print(f"[Cue] done in {elapsed:.1f} s: {self.count} tokens, {words} words, {'valid' if valid else 'with warnings'}")

    def chunk(self, text):
        _send(self.i["unique_id"], {"event": "chunk", "text": json_stream_preview(text)})


def _ref_slots(kwargs):
    """[(index, image tensor, label)] for connected ref inputs, in slot order."""
    out = []
    for n in range(1, MAX_REFS + 1):
        img = kwargs.get(f"ref_{n}")
        if img is None:
            continue
        out.append((n, img, str(kwargs.get(f"label_{n}") or "").strip()))
    return out


class Cue:
    _no_examples = False
    CATEGORY = "PromptDeck"
    FUNCTION = "run"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "STRING", "STRING", "FLOAT", "STRING")
    RETURN_NAMES = ("prompt", "prompt_tokens", "subjects", "duration_seconds", "mode")
    DESCRIPTION = (
        "Turns an intent ('undressing', 'fighting', 'superman saving people') plus optional "
        "pictures into a prompt in the target model's own format, written by a small local "
        "vision model through Ollama. MiniMax H3 gets the exact rewriter format (t2v, i2v, "
        "first+last, last, ref2v with labelled references); image models get prose. "
        "Subjects become swappable tokens; the result streams into the node and can be locked."
    )

    @classmethod
    def INPUT_TYPES(cls):
        labels = {f"label_{n}": ("STRING", {"default": "", "tooltip": f"What reference {n} is: man, woman, the bar, ... Same label on several slots = one subject."})
                  for n in range(1, MAX_REFS + 1)}
        refs = {f"ref_{n}": ("IMAGE", {"tooltip": f"Reference picture {n}. Feeding the Director: slot {n} must be its {n}th image tile."})
                for n in range(1, MAX_REFS + 1)}
        return {
            "required": {
                "intent": ("STRING", {"default": "", "multiline": True,
                                      "tooltip": "What should happen. A word, a line, or a brief. Quoted text is dialogue, kept verbatim. Link Prompt Deck's text here to feed it line by line."}),
                "notes": ("STRING", {"default": "", "multiline": True,
                                     "tooltip": "Standing notes sent with every request: world, house rules, LoRA trigger words. Saved in the workflow."}),
                "target": (template_names(), {"tooltip": "Target model. One file per model in templates/."}),
                "mode": (ALL_MODES, {"default": "i2v", "tooltip": "MiniMax H3 only. ref2v uses the ref slots; continuation modes write only the next action."}),
                "duration_seconds": ("FLOAT", {"default": 5.0, "min": 0.5, "max": 60.0, "step": 0.1,
                                                "tooltip": "Clip length the prompt is planned for (snapped to H3's 17k+5 frame grid). Link the output to the Director's duration."}),
                "dialogue_language": ("STRING", {"default": "English", "tooltip": "Language tag for spoken lines: <d>[English] ...</d>. The body stays English."}),
                "detail": (DETAIL_LEVELS, {"default": "auto", "tooltip": "auto = the template's default (H3 long, Krea 2 medium)."}),
                "framing": (FRAMINGS, {"default": "auto", "tooltip": "Forces the shot size. The writer must state it and keep it; the validator checks."}),
                "writer": ("STRING", {"default": DEFAULT_WRITER, "tooltip": "Ollama model that writes the prompt when no picture is involved. A text-only model is faster and better here (models ▾ lists some)."}),
                "vision_writer": ("STRING", {"default": DEFAULT_WRITER, "tooltip": "Vision model used to look at pictures, and to write when the request includes a picture (i2v, first+last, last, image targets with a picture)."}),
                "endpoint": ("STRING", {"default": DEFAULT_ENDPOINT, "tooltip": "Ollama server."}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 2**31 - 1, "control_after_generate": "fixed",
                                 "tooltip": "Writer seed. ↻ rerolls; fixed keeps the same prompt on re-queue."}),
                "lock": ("BOOLEAN", {"default": False, "tooltip": "Locked: the result box is editable and used verbatim; no model call."}),
            },
            "optional": {
                "image": ("IMAGE", {"tooltip": "Start frame (i2v, first+last) or the picture to work from."}),
                "image_last": ("IMAGE", {"tooltip": "Last frame (first+last, last)."}),
                **refs,
                **labels,
                "subjects_override": ("STRING", {"default": "", "multiline": True,
                                                 "tooltip": "Lines 'token = description' that replace a subject's description without a model call. Link a second Prompt Deck here for actions x characters."}),
                "result": ("STRING", {"default": "", "multiline": True}),
                "history": ("STRING", {"default": ""}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        stamps = [t.mtime for t in load_templates()]
        if kwargs.get("lock"):
            stamps.append(kwargs.get("result", ""))
        return _h(*stamps)

    # ------------------------------------------------------------------ run
    def run(self, intent, notes, target, mode, duration_seconds, dialogue_language, detail, writer,
            endpoint, seed, lock, vision_writer=DEFAULT_WRITER, framing="auto", image=None, image_last=None,
            subjects_override="", result="", history="", unique_id=None, **kwargs):
        vision_writer = (vision_writer or writer).strip()
        writer = (writer or vision_writer).strip()
        tpl = find_template(target)
        if tpl is None:
            raise ValueError("Cue: no templates found in templates/")
        has_modes = bool(tpl.modes)
        if has_modes and mode not in tpl.modes:
            mode = tpl.modes[0]
        if not has_modes:
            mode = ""
        is_cont = mode.startswith("continuation")
        if tpl.timeline:
            frames = h3_new_frames(duration_seconds) if is_cont else h3_frames(duration_seconds)
            seconds = frames / 24.0
        else:
            frames, seconds = 0, float(duration_seconds)
        detail_level = tpl.default_detail if detail == "auto" else detail
        director_mode = MODE_TO_DIRECTOR.get(mode, "")
        language = (dialogue_language or "English").strip()

        if lock:
            text = (result or "").strip()
            self._ui(unique_id, text, text, "", [], seed, True, mode, tpl, seconds, frames, locked=True)
            return {"ui": {"cue": [self._payload]}, "result": (text, text, "", seconds, director_mode)}

        refs = _ref_slots(kwargs)
        start_jpeg, start_hash = image_to_jpeg(image)
        last_jpeg, last_hash = image_to_jpeg(image_last)
        ref_imgs = [(n, lab) + image_to_jpeg(img) for n, img, lab in refs]  # (n, label, jpeg, hash)
        overrides = parse_overrides(subjects_override)

        key = _h("prompt", CUE_VERSION, tpl.file, tpl.mtime, mode, intent, notes, language, frames if tpl.timeline else seconds,
                 detail_level, writer, vision_writer, framing, seed, start_hash, last_hash,
                 json.dumps([(n, lab, h) for n, lab, _, h in ref_imgs]), json.dumps(sorted(str(k) for k in overrides)),
                 json.dumps({str(k): v for k, v in overrides.items()}))
        cached = cache_get("prompts", key)
        if cached:
            p, pt, subj, warns = cached["prompt"], cached["tokens"], cached["subjects"], cached.get("warnings", [])
            self._ui(unique_id, p, pt, subj, warns, seed, True, mode, tpl, seconds, frames,
                     errors=cached.get("errors", []), retries=cached.get("retries", 0))
            return {"ui": {"cue": [self._payload]}, "result": (p, pt, subj, seconds, director_mode)}

        if not intent.strip() and image is None and image_last is None and not refs:
            raise ValueError("Cue: give an intent (a word is enough) or connect a picture")
        job = _Job({"writer": writer, "vision_writer": vision_writer, "endpoint": endpoint, "unique_id": unique_id})
        quotes = extract_quotes(intent)
        words = tpl.words
        fill = {
            "duration": f"{seconds:.2f}", "frames": frames, "language": language,
            "words": words.get(detail_level, 120), "words_short": words.get("short", 60),
            "words_medium": words.get("medium", 120), "words_long": words.get("long", 200),
            "tail_seconds": f"{22 / 24:.2f}", "detail": detail_level, "framing": framing,
            "image_description": "", "image_last_description": "", "subject_definitions": "",
            "palette": draw_palette(seed, intent) if tpl.kind == "image" else "",
        }

        # ---- pictures -> words (stage A) ----
        defined_labels, subject_lines, predefined, pictures_for_b = [], [], {}, []
        if has_modes and mode in ("i2v", "first+last"):
            if start_jpeg is None:
                raise ValueError(f"Cue: mode {mode} needs the start picture on 'image'")
            fill["image_description"] = job.describe(start_jpeg, start_hash, "start")
            defined_labels.append("<Picture 1>")
            pictures_for_b.append(start_jpeg)
        if has_modes and mode in ("first+last", "last"):
            if last_jpeg is None:
                raise ValueError(f"Cue: mode {mode} needs the last picture on 'image_last'")
            fill["image_last_description"] = job.describe(last_jpeg, last_hash, "last")
            defined_labels.append("<Picture 2>" if mode == "first+last" else "<Picture 1>")
            pictures_for_b.append(last_jpeg)
        if has_modes and mode == "ref2v":
            if not ref_imgs and start_jpeg is None:
                raise ValueError("Cue: ref2v needs at least one reference on ref_1..ref_9 (or a start frame on image)")
            # a slot labelled "first frame" / "last frame" is a frame anchor, not a subject; the
            # image socket is the first frame too and becomes the next picture number
            frame_lines, groups = [], []  # groups: [key, label, [(slot, hash, jpeg)]]
            all_pics = list(ref_imgs)
            if start_jpeg is not None:
                all_pics.append((len(ref_imgs) + 1, "first frame", start_jpeg, start_hash))
                job.warnings.append(f"start frame is <Picture {len(ref_imgs) + 1}>: put it as the Director's picture {len(ref_imgs) + 1} (or the reference node's slot {len(ref_imgs) + 1})")
            for n, lab, jpeg, digest in all_pics:
                fm = _FRAME_LABEL_RE.match(lab or "")
                if fm:
                    role = "first" if fm.group(1).lower() in ("first", "start", "starting", "opening") else "last"
                    desc = job.describe(jpeg, digest, "start" if role == "first" else "last")
                    frame_lines.append((n, role, desc))
                    continue
                keyl = slug_token(lab) if lab else f"slot{n}"
                for g in groups:
                    if g[0] == keyl:
                        g[2].append((n, digest, jpeg))
                        break
                else:
                    groups.append([keyl, lab, [(n, digest, jpeg)]])
            for si, (keyl, lab, members) in enumerate(groups, start=1):
                pics = ", ".join(f"<Picture {n}>" for n, _, _ in members)
                ov = overrides.get(("subject", si)) or overrides.get(keyl)
                if ov:
                    desc = ov
                elif lab and not is_short_label(lab):
                    desc = lab
                else:
                    desc = clean_description(job.describe(members[0][2], members[0][1], "ref", lab), looks_like_person(lab))
                bare = re.sub(r"(?i)^(the|a|an)\s+", "", lab).strip()
                head = f"the {bare}" if lab and is_short_label(lab) else ("the subject" if not lab else "")
                if head:
                    line = f"<Subject {si}> is {head} in {pics}, {desc[0].lower() + desc[1:] if desc else ''}".rstrip(", ")
                else:
                    line = f"<Subject {si}> is {desc}, from {pics}"
                subject_lines.append(line.rstrip(".") + ".")
                defined_labels.append(f"<Subject {si}>")
                defined_labels.extend(f"<Picture {n}>" for n, _, _ in members)
            frame_notes = []
            for n, role, desc in frame_lines:
                shot = "[Shot 1]" if role == "first" else "the final shot"
                fd = clean_frame_description(desc)
                subject_lines.append(f"<Picture {n}> is the {role} frame of {shot}, showing {fd[0].lower() + fd[1:] if fd else 'the scene'}".rstrip(".") + ".")
                defined_labels.append(f"<Picture {n}>")
                frame_notes.append(f"the shot begins from <Picture {n}>" if role == "first" else f"the video ends on <Picture {n}>")
            fill["subject_definitions"] = "subject_definitions:\n" + "\n".join(subject_lines)
            if frame_notes:
                fill["subject_definitions"] += ("\n\nFrame anchors: " + "; ".join(frame_notes) +
                                               ". Say so in detailed_description at the matching point and keep that picture's composition there.")
        elif ref_imgs:
            # brace-token targets (and H3 base modes) with labelled references: predefined tokens
            for n, lab, jpeg, digest in ref_imgs:
                tok = slug_token(lab) if lab else f"ref_{n}"
                if tok in predefined:
                    continue
                desc = overrides.get(tok) or (lab if lab and not is_short_label(lab) else job.describe(jpeg, digest, "ref", lab))
                predefined[tok] = desc
        if start_jpeg is not None and not has_modes:
            # image targets: the picture is the subject to re-prompt
            fill["image_description"] = job.describe(start_jpeg, start_hash, "start")
        if is_cont and start_jpeg is not None:
            # continuation: the last frame so far is the only context the writer can get
            fill["image_description"] = job.describe(start_jpeg, start_hash, "start")
            pictures_for_b.append(start_jpeg)
        for tok, desc in overrides.items():
            if isinstance(tok, str) and not (has_modes and mode == "ref2v"):
                predefined[tok] = desc

        # ---- the request (stage B) ----
        use_brace_tokens = not (has_modes and mode == "ref2v") and not is_cont
        self._no_examples = is_cont
        system = self._system(tpl, fill, detail_level, notes, use_brace_tokens, predefined)
        user = self._user(tpl, mode, fill, intent, seconds, language, quotes, has_modes, is_cont, predefined)
        wanted = field_names(mode, has_modes, is_cont)
        schema = schema_for(wanted, use_brace_tokens, tpl.timeline)
        max_tokens = int(words.get(detail_level, 120) * 2.0) + (380 if has_modes else 100) + 140 + 160  # prose + sound fields + subjects + beats/JSON

        writer_b = vision_writer if pictures_for_b else writer
        attempt, errors, best = 0, [], None
        while attempt < 2:
            attempt += 1
            job.begin(max_tokens * 0.7, "writing" if attempt == 1 else "rewriting", writer_b)
            ask = user if attempt == 1 else (user + "\n\nYour previous answer had these problems:\n- "
                                             + "\n- ".join(errors) + "\nRewrite the complete answer.")
            try:
                raw = ollama_chat(endpoint, writer_b, system, ask, images=pictures_for_b or None,
                                  seed=int(seed) + attempt - 1,
                                  temperature=0.9 if tpl.kind == "image" else 0.7,  # images: more variety, validator catches breakage
                                  num_predict=max_tokens,
                                  on_chunk=job.chunk, schema=schema, on_token=job.token)
            except BaseException:
                job.heartbeat_stop()
                raise
            built = self._build(tpl, mode, raw, fill, seconds, frames, defined_labels, subject_lines,
                                predefined, overrides, quotes, language, use_brace_tokens, has_modes, is_cont)
            errors = built["errors"]
            if best is None or len(errors) < len(best["errors"]):
                best = built
            if not errors:
                break
        p, pt, subj = best["prompt"], best["tokens"], best["subjects"]
        job.end(len(p.split()), not best["errors"])
        if best["errors"] and tpl.timeline:
            # last resort for a writer that keeps cutting at the end: drop those cuts, re-validate
            fixed_p, n1 = repair_end_cuts(p, seconds)
            fixed_pt, n2 = repair_end_cuts(pt, seconds)
            if n1 or n2:
                p, pt = fixed_p, fixed_pt
                best["warnings"].append(f"{max(n1, n2)} cut(s) at the clip end removed")
                best["errors"] = [e for e in best["errors"] if "past the clip end" not in e]
            if quotes and any("must appear verbatim" in e for e in best["errors"]) and has_modes:
                fixed_p, n1 = append_dialogue(p, intent, quotes, language)
                fixed_pt, n2 = append_dialogue(pt, intent, quotes, language)
                if n1 or n2:
                    p, pt = fixed_p, fixed_pt
                    best["warnings"].append(f"{max(n1, n2)} dropped line(s) appended as speech")
                    best["errors"] = [e for e in best["errors"] if "must appear verbatim" not in e]
            if any("too many shots" in e for e in best["errors"]) and mode != "continuation (DaSiWa)":
                fixed_p, n1 = repair_extra_shots(p, shot_budget(seconds))
                fixed_pt, n2 = repair_extra_shots(pt, shot_budget(seconds))
                if n1 or n2:
                    p, pt = fixed_p, fixed_pt
                    best["warnings"].append(f"{max(n1, n2)} extra shot(s) merged into one")
                    best["errors"] = [e for e in best["errors"] if "too many shots" not in e]
        warns = job.warnings + best["warnings"] + ([f"validation failed after retry: {'; '.join(best['errors'])}"] if best["errors"] else [])
        cache_put("prompts", key, {"prompt": p, "tokens": pt, "subjects": subj, "warnings": warns,
                                   "errors": best["errors"], "retries": attempt - 1})
        self._ui(unique_id, p, pt, subj, warns, seed, False, mode, tpl, seconds, frames,
                 errors=best["errors"], retries=attempt - 1)
        return {"ui": {"cue": [self._payload]}, "result": (p, pt, subj, seconds, director_mode)}

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _render(text, fill):
        out = text
        for k, v in fill.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def _system(self, tpl, fill, detail_level, notes, use_brace_tokens, predefined):
        parts = [self._render(tpl.sections.get("system", ""), fill)]
        rules = tpl.sections.get("rules", "")
        if rules:
            parts.append(self._render(rules, fill) + f"\nUse the {detail_level} level.")
        parts.append(
            "Answer with one JSON object that matches the schema you are given, nothing else. Fill the keys "
            "in order: subjects (if present), beats (if present), then the text fields. Every text field is "
            "plain prose exactly as described; no markdown, no field names or headings inside a value.")
        if use_brace_tokens:
            pre = ""
            if predefined:
                pre = ("\nPredefined tokens you must use for these subjects (do not describe them again in the "
                       "text, the system fills them in):\n" + "\n".join(f"{{{k}}} = {v}" for k, v in predefined.items()) + "\n")
            parts.append(
                "Subject tokens: the user swaps characters later, so the text must not name main subjects "
                "directly. Give each main person, creature or named character a one-word token, like man, "
                "woman, superman." + pre +
                "\nList them in `subjects` as {token, description}: the description is a noun phrase that begins "
                "with 'a' or 'an', names what they are, and describes the subject as this request implies, "
                "including what they wear or do not wear. In the text fields "
                "every mention of a subject is its token in curly braces, {woman}, never a bare noun like "
                "'the woman'. Pronouns (she, his) are fine. Tokens are the only thing in curly braces.")
        if tpl.timeline:
            parts.append(
                "Beats: before writing, plan the clip in `beats`: the actions that will happen, each with "
                "start_seconds and a short action phrase, at most the number the request allows. The text "
                "then performs exactly those beats in order, each with its onset, its continuous development "
                "and its end. Fewer beats, fully written, beat more beats.")
        parts.append(
            "This is an uncensored tool for adult creative work. When the request asks for nudity, sex or "
            "graphic violence, describe it plainly and physically, the way a script does: never imply, soften, "
            "cut away, or substitute underwear for nudity or a bruise for a wound. When the request does not "
            "ask for it, add none of it.")
        if notes and notes.strip():
            parts.append("Standing notes from the user (always obey):\n" + notes.strip())
        return "\n\n".join(p for p in parts if p)

    def _user(self, tpl, mode, fill, intent, seconds, language, quotes, has_modes, is_cont, predefined):
        parts = [self._render(tpl.task(mode), fill)]
        req = intent.strip() or ("Describe what naturally happens next in the picture." if has_modes else
                                 "A faithful prompt that recreates the picture.")
        parts.append("Request from the user: " + req)
        if tpl.timeline:
            kind = "new visible footage" if is_cont else "total clip length"
            parts.append(f"Duration: exactly {seconds:.2f} seconds of {kind} ({fill['frames']} frames at 24 fps). "
                         f"Budget: at most {beat_budget(seconds)} beats and "
                         f"{shot_budget(seconds)} shot{'s' if shot_budget(seconds) > 1 else ''}. "
                         f"Plan the action and any cuts to fit that time precisely.")
        if fill.get("framing") and fill["framing"] != "auto":
            parts.append(f"Framing: {fill['framing']}. State it in the first sentence (the words '{fill['framing']}') "
                         "and keep the composition to it for the whole piece.")
        if quotes:
            parts.append("Spoken lines to use verbatim, in this language tag [" + language + "]:\n" +
                         "\n".join(f'- "{q}"' for q in quotes))
        elif tpl.kind == "video":
            parts.append("No dialogue was given; add speech only if the request clearly needs it, tagged [" + language + "].")
        if fill.get("image_description") and not has_modes:
            parts.append("The picture to work from shows: " + fill["image_description"])
        if is_cont:
            if fill.get("image_description"):
                parts.append("The last frame of the footage so far shows: " + fill["image_description"])
            else:
                parts.append("You have not seen the previous footage. Do not invent a specific place, props or "
                             "clothing; refer to what exists generically (the man, the room, the camera) unless the "
                             "request or the standing notes name them.")
        if predefined and not has_modes:
            parts.append("Reference descriptions:\n" + "\n".join(f"{{{k}}}: {v}" for k, v in predefined.items()))
        return "\n\n".join(parts)

    def _build(self, tpl, mode, raw, fill, seconds, frames, defined_labels, subject_lines, predefined,
               overrides, quotes, language, use_brace_tokens, has_modes, is_cont):
        raw = _THINK_RE.sub("", raw).strip()
        errors, warnings = [], []
        wanted = field_names(mode, has_modes, is_cont)
        data = parse_json_answer(raw, wanted)
        subjects = dict(predefined)
        beats = None
        if data is not None:
            # ---- the normal path: a JSON answer constrained by the schema ----
            if use_brace_tokens:
                subjects.update(data["subjects"])
            beats = data["beats"]
            fields = {}
            for name in wanted:
                value = " ".join(str(data["fields"].get(name, "")).split())
                if value:
                    value, trimmed = trim_repetition(value)
                    if trimmed:
                        warnings.append(f"repetition trimmed in {name}")
                if value.strip():
                    fields[name] = value.strip()
                else:
                    errors.append(f"missing field '{name}'")
            if quotes and has_modes:
                fields = {k: fix_dialogue(v, quotes, language) for k, v in fields.items()}
            body = fields.get("prompt") or fields.get("next_action") or ""
        else:
            # ---- fallback: a writer that ignored the schema and wrote text ----
            raw, trimmed = trim_repetition(raw)
            if trimmed:
                warnings.append("repetition trimmed")
            body = raw
            if use_brace_tokens:
                found, body = split_subjects_block(raw)
                subjects.update(found)
            if quotes and has_modes:
                body = fix_dialogue(body, quotes, language)
            if has_modes and mode != "continuation (DaSiWa)":
                fields, ferr = extract_fields(body, wanted)
                errors.extend(ferr)
                if not fields and body:
                    fields = {wanted[2] if mode == "ref2v" else wanted[0]: body}
            elif has_modes:
                fields = {"next_action": body}
            else:
                fields = {"prompt": body}
        if use_brace_tokens:
            for tok, desc in overrides.items():
                if isinstance(tok, str):
                    subjects[tok] = desc
            subjects = {t: clean_description(d, looks_like_person(t)) for t, d in subjects.items()}

        if has_modes and mode != "continuation (DaSiWa)":
            body_name = "detailed_description" if mode == "ref2v" else "integrated_multimodal_description"
            if fields.get(body_name) and "[Shot 1]" not in fields[body_name]:
                fields[body_name] = "[Shot 1] " + fields[body_name].lstrip()
                warnings.append("[Shot 1] opener added")
            if fields.get(body_name):
                fixed = normalize_timestamps(fields[body_name])
                if fixed != fields[body_name]:
                    fields[body_name] = fixed
                    warnings.append("timestamps normalised")
        main = "detailed_description" if mode == "ref2v" else ("integrated_multimodal_description" if has_modes else ("next_action" if mode == "continuation (DaSiWa)" else "prompt"))
        framing = fill.get("framing", "auto")
        if framing and framing != "auto" and fields.get(main):
            low = fields[main].lower()
            if not any(t in low for t in FRAMING_TERMS.get(framing, (framing,))):
                errors.append(f"framing '{framing}' is not stated; name it in the first sentence and keep it")
        if fill.get("detail") == "long":
            target = int(fill.get("words", 0) or 0)
            n_words = len(str(fields.get(main, "")).split())
            if target and n_words < 0.6 * target:
                errors.append(f"too short for long: {n_words} words in {main}, about {target} expected; describe every motion continuously")

        # ---- timeline budget: beats and shots against the duration ----
        if tpl.timeline:
            if beats is not None:
                berr, bwarn = validate_beats(beats, seconds, beat_budget(seconds))
                errors.extend(berr)
                warnings.extend(bwarn)
            if mode != "continuation (DaSiWa)":
                body_name = "detailed_description" if mode == "ref2v" else "integrated_multimodal_description"
                shots = shots_in(fields.get(body_name, ""))
                if shots > shot_budget(seconds):
                    errors.append(f"too many shots: {shots} for {seconds:.1f}s, at most {shot_budget(seconds)}")

        # ---- assemble per mode ----
        if has_modes and not is_cont:
            if fields:
                verr, vwarn = validate_h3(fields, mode, seconds, defined_labels, quotes, language)
                errors.extend(verr)
                warnings.extend(vwarn)
            body_tokens = "\n\n".join(f"{n}: {fields[n]}" for n in wanted if n in fields) or body
            header = h3_header(mode, seconds, shots_in(fields.get("integrated_multimodal_description", "")))
            if mode == "ref2v":
                body_tokens = "subject_definitions:\n" + "\n".join(subject_lines) + "\n\n" + body_tokens
            elif header:
                body_tokens = header + "\n\n" + body_tokens
        elif has_modes and mode == "continuation (Reel)":
            if fields:
                verr, vwarn = validate_h3(fields, mode, seconds, defined_labels, quotes, language)
                errors.extend(verr)
                warnings.extend(vwarn)
                imd = fields.get("integrated_multimodal_description", "")
                # System-side window framing, as DaSiWa's _window_context does: the writer is
                # told about the hidden overlap but rarely repeats it, and the model needs it.
                window = (f"This is a continuation window. Its opening {float(fill['tail_seconds']):.3f} seconds "
                          "are hidden overlapping context from the end of the preceding shot. Weld to that tail "
                          "at the seam: same subjects, space, and instantaneous motion, camera, and sound. Do not "
                          f"freeze its tempo or camera for the rest of the clip. The following {seconds:.3f} "
                          "seconds are the new visible continuation.")
                rest = imd[len("[Shot 1]"):].strip() if imd.startswith("[Shot 1]") else imd
                fields["integrated_multimodal_description"] = "[Shot 1] " + window + " " + SEAM_POLICY + " " + rest
                body_tokens = "\n\n".join(f"{n}: {fields[n]}" for n in H3_FIELDS_BASE if n in fields)
            else:
                body_tokens = body
                errors.append("empty answer")
        elif has_modes:  # continuation (DaSiWa): the next action only, the Director wraps it
            body_tokens = re.sub(r"(?im)^\s*(next action|prompt)\s*:\s*", "", fields.get("next_action", "")).strip()
            body_tokens = re.sub(r"^\s*\[Shot\s*1\]\s*", "", body_tokens)
            if not body_tokens:
                errors.append("empty answer")
            for q in quotes:
                if q not in body_tokens:
                    errors.append(f'dialogue "{q}" missing verbatim')
        else:
            body_tokens = strip_meta_lines(fields.get("prompt", ""))
            if not body_tokens:
                errors.append("empty answer")
            for q in quotes:
                if q not in body_tokens:
                    warnings.append(f'dialogue "{q}" not found verbatim')

        if use_brace_tokens and subjects:
            body_tokens = auto_tokenize(body_tokens, subjects)
        if use_brace_tokens:
            used = {slug_token(t) for t in _BRACE_TOKEN_RE.findall(body_tokens)}
            missing = sorted(t for t in used if t not in subjects)
            if missing:
                errors.append("tokens used but not defined in subjects: " + ", ".join("{" + t + "}" for t in missing))
            unused = sorted(t for t in subjects if t not in used)
            if unused:
                warnings.append("defined but unused tokens: " + ", ".join("{" + t + "}" for t in unused))
            prompt = substitute_tokens(body_tokens, subjects)
            subjects_text = "\n".join(f"{{{k}}} = {v}" for k, v in subjects.items())
        else:
            prompt = body_tokens
            subjects_text = "\n".join(subject_lines)
        return {"prompt": prompt, "tokens": body_tokens, "subjects": subjects_text,
                "errors": errors, "warnings": warnings, "beats": beats}

    def _ui(self, unique_id, prompt, tokens, subjects, warnings, seed, cached, mode, tpl, seconds, frames, locked=False,
            errors=None, retries=0):
        shots = shots_in(prompt) if tpl.timeline else 0
        info = []
        if tpl.timeline:
            info.append(f"{seconds:.2f} s · {frames} f" + (f" · {shots} shot{'s' if shots != 1 else ''}" if shots else ""))
        info.append(f"{len(prompt.split())} words")
        if cached:
            info.append("locked" if locked else "cached")
        self._payload = {
            "prompt": prompt, "tokens": tokens, "subjects": subjects, "warnings": warnings,
            "seed": int(seed), "cached": bool(cached), "locked": bool(locked), "mode": mode,
            "target": tpl.name, "info": " · ".join(info),
            # read by tests/overnight.py
            "valid": not errors, "errors": list(errors or []), "retries": int(retries),
        }
        _send(unique_id, {"event": "done", **self._payload})


NODE_CLASS_MAPPINGS = {"PromptDeckCue": Cue}
NODE_DISPLAY_NAME_MAPPINGS = {"PromptDeckCue": "Cue"}


# -------------------------------------------------------------------- routes

def _register_routes():
    try:
        from server import PromptServer
        from aiohttp import web
    except Exception:
        return  # not inside ComfyUI (tests, linting)

    routes = PromptServer.instance.routes

    @routes.get("/promptdeck/cue/templates")
    async def cue_templates(request):
        return web.json_response({"templates": [t.info() for t in load_templates()]})

    @routes.get("/promptdeck/cue/models")
    async def cue_models(request):
        endpoint = request.query.get("endpoint", DEFAULT_ENDPOINT)
        try:
            installed = ollama_tags(endpoint)
            return web.json_response({"ok": True, "installed": installed,
                                      "recommended": [{"name": n, "note": d} for n, d in RECOMMENDED_WRITERS],
                                      "recommended_text": [{"name": n, "note": d} for n, d in RECOMMENDED_TEXT_WRITERS + RECOMMENDED_WRITERS]})
        except OllamaError as e:
            return web.json_response({"ok": False, "error": str(e), "installed": [],
                                      "recommended": [{"name": n, "note": d} for n, d in RECOMMENDED_WRITERS]})

    @routes.post("/promptdeck/cue/pull")
    async def cue_pull(request):
        try:
            data = await request.json()
            model = str(data.get("model", "")).strip()
            endpoint = str(data.get("endpoint", DEFAULT_ENDPOINT))
            node = data.get("node")
            field = str(data.get("field", "writer"))
        except Exception:
            return web.json_response({"ok": False, "error": "bad request"})
        if not model:
            return web.json_response({"ok": False, "error": "no model"})

        def worker():
            def progress(evt):
                PromptServer.instance.send_sync("promptdeck.cue.pull", {
                    "node": node, "model": model, "status": evt.get("status", ""),
                    "completed": evt.get("completed", 0), "total": evt.get("total", 0)})
            try:
                ollama_pull(endpoint, model, progress)
                PromptServer.instance.send_sync("promptdeck.cue.pull", {"node": node, "model": model, "done": True, "field": field})
            except Exception as e:
                PromptServer.instance.send_sync("promptdeck.cue.pull", {"node": node, "model": model, "done": True, "error": str(e), "field": field})

        threading.Thread(target=worker, daemon=True).start()
        return web.json_response({"ok": True})


_register_routes()
