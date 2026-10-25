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
_CACHE_MAX = {"descriptions": 600, "prompts": 300}

CUE_VERSION = "1.6.1"  # salts the prompt cache: a code or template change must not serve old prompts
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_WRITER = "huihui_ai/qwen3-vl-abliterated:4b-instruct"
RECOMMENDED_WRITERS = [
    ("huihui_ai/qwen3-vl-abliterated:4b-instruct", "default, 3.3 GB"),
    ("huihui_ai/qwen3-vl-abliterated:2b-instruct", "smallest, 1.9 GB"),
    ("huihui_ai/qwen3-vl-abliterated:8b-instruct", "bigger, ~5 GB"),
]
DETAIL_LEVELS = ["auto", "short", "medium", "long"]
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
                num_predict=1200, on_chunk=None, timeout=180):
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

    out, last = [], 0.0
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
_TRAILING_ARTICLE_RE = re.compile(r"(^|\W)(a|an|the)\s+(\w+\s+){0,2}$", re.IGNORECASE)


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
            return token_handle(tok)
        seen.add(tok)
        desc = subjects[tok]
        # the writer restated the description right next to the token ("{woman} in her twenties,
        # slender, ..."): substituting the full description would double it and trip the
        # repetition trimmer, so use the short handle instead
        probe = _ARTICLE_RE.sub("", desc, count=1).lower()[:28]
        around = re.sub(r"[{}]", "", m.string[max(0, m.start() - 160):m.end() + 160]).lower()
        if probe and probe in around:
            return token_handle(tok)
        if _TRAILING_ARTICLE_RE.search(m.string[:m.start()]):
            desc = _ARTICLE_RE.sub("", desc, count=1)
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


def draw_palette(seed):
    """One seeded line per axis of templates/palette.md, as a suggestion block for the writer.
    Small models fall into one house style whenever the intent leaves a choice open; concrete,
    seed-chosen material gives them something to vary on. Empty string if there is no palette."""
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
    rng = random.Random(int(seed) * 7919 + 17)
    picks = [(axis, rng.choice(axes[axis])) for axis in _PALETTE_AXES if axes.get(axis)]
    if not picks:
        return ""
    body = "; ".join(f"{axis.replace('_', ' ')} — {text}" for axis, text in picks)
    return ("Seeded suggestions, use them only where the intent leaves the choice open (the intent "
            "always wins; drop any that clash with it): " + body + ".")


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


def fix_dialogue(body, quotes, language):
    """A line the user quoted that sits in plain quotes gets wrapped as H3 speech."""
    verbs = ("says", "whispers", "shouts", "asks", "replies", "mutters", "adds", "answers", "yells", "murmurs")
    for q in quotes:
        if any(q in words for _, words in _D_TAG_RE.findall(body)):
            continue
        for opener, closer in (('"', '"'), ("\u201c", "\u201d"), ("\u00ab", "\u00bb")):
            needle = opener + q + closer
            idx = body.find(needle)
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

    # ---- descriptions (stage A, cached per picture) ----
    def describe(self, jpeg, digest, role, label=""):
        if jpeg is None:
            return ""
        key = _h("desc2", digest, role, slug_token(label) if label else "", self.i["writer"])
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
        self.stage(f"looking at {role if role in ('start', 'last') else (label or 'reference')}")
        text = ollama_chat(self.i["endpoint"], self.i["writer"],
                           "You describe pictures precisely and briefly for film scripts. Adults only.",
                           ask, images=[jpeg], seed=7, temperature=0.2, num_predict=300)
        text = " ".join(text.split())
        if text:
            cache_put("descriptions", key, text)
        return text

    def stage(self, text):
        self.stages.append(text)
        _send(self.i["unique_id"], {"event": "stage", "text": text})

    def chunk(self, text):
        _send(self.i["unique_id"], {"event": "chunk", "text": text})


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
                "writer": ("STRING", {"default": DEFAULT_WRITER, "tooltip": "Ollama model that writes. Use models ▾ to pick or pull one."}),
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
            endpoint, seed, lock, image=None, image_last=None, subjects_override="", result="",
            history="", unique_id=None, **kwargs):
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
                 detail_level, writer, seed, start_hash, last_hash,
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
        job = _Job({"writer": writer, "endpoint": endpoint, "unique_id": unique_id})
        quotes = extract_quotes(intent)
        words = tpl.words
        fill = {
            "duration": f"{seconds:.2f}", "frames": frames, "language": language,
            "words": words.get(detail_level, 120), "words_short": words.get("short", 60),
            "words_medium": words.get("medium", 120), "words_long": words.get("long", 200),
            "tail_seconds": f"{22 / 24:.2f}", "detail": detail_level,
            "image_description": "", "image_last_description": "", "subject_definitions": "",
            "palette": draw_palette(seed) if tpl.kind == "image" else "",
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
        max_tokens = int(words.get(detail_level, 120) * 2.0) + (380 if has_modes else 100) + 140  # body + sound fields + subjects block

        attempt, errors, best = 0, [], None
        while attempt < 2:
            attempt += 1
            job.stage("writing" if attempt == 1 else "rewriting")
            ask = user if attempt == 1 else (user + "\n\nYour previous answer had these problems:\n- "
                                             + "\n- ".join(errors) + "\nRewrite the complete answer.")
            raw = ollama_chat(endpoint, writer, system, ask, images=pictures_for_b or None,
                              seed=int(seed) + attempt - 1,
                              temperature=0.9 if tpl.kind == "image" else 0.7,  # images: more variety, validator catches breakage
                              num_predict=max_tokens,
                              on_chunk=job.chunk)
            built = self._build(tpl, mode, raw, fill, seconds, frames, defined_labels, subject_lines,
                                predefined, overrides, quotes, language, use_brace_tokens, has_modes, is_cont)
            errors = built["errors"]
            if best is None or len(errors) < len(best["errors"]):
                best = built
            if not errors:
                break
        warns = job.warnings + best["warnings"] + ([f"validation failed after retry: {'; '.join(best['errors'])}"] if best["errors"] else [])
        p, pt, subj = best["prompt"], best["tokens"], best["subjects"]
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
        if use_brace_tokens:
            pre = ""
            if predefined:
                pre = ("\nPredefined tokens you must use for these subjects (do not describe them again in the "
                       "body, the system fills them in):\n" + "\n".join(f"{{{k}}} = {v}" for k, v in predefined.items()) + "\n")
            parts.append(
                "Subject tokens: the user swaps characters later, so the body must not name main subjects "
                "directly. Give each main person, creature or named character a one-word token in curly "
                "braces, like {man}, {woman}, {superman}." + pre +
                "\nStart your answer with the block\n\nsubjects:\n{token} = a noun phrase that begins with 'a' or "
                "'an' and names what they are, then age range, build, hair, skin, clothing with colours, one "
                "distinguishing detail (for example {woman} = a woman in her forties, broad-shouldered, grey "
                "cropped hair, olive skin, a navy work shirt, a scar on her left hand)\n\nthen a line "
                "containing only\n\nprompt:\n\nand then the prompt itself, in which every mention of a subject is "
                "its token, never a bare noun like 'the woman'. Pronouns (she, his) are fine. Tokens are the only "
                "thing in curly braces.")
        if notes and notes.strip():
            parts.append("Standing notes from the user (always obey):\n" + notes.strip())
        ex = tpl.sections.get("examples", "")
        if ex and not self._no_examples:
            parts.append(self._render(ex, fill))
        return "\n\n".join(p for p in parts if p)

    def _user(self, tpl, mode, fill, intent, seconds, language, quotes, has_modes, is_cont, predefined):
        parts = [self._render(tpl.task(mode), fill)]
        req = intent.strip() or ("Describe what naturally happens next in the picture." if has_modes else
                                 "A faithful prompt that recreates the picture.")
        parts.append("Request from the user: " + req)
        if tpl.timeline:
            kind = "new visible footage" if is_cont else "total clip length"
            parts.append(f"Duration: exactly {seconds:.2f} seconds of {kind} ({fill['frames']} frames at 24 fps). "
                         f"Plan the action and any cuts to fit that time precisely.")
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
        raw, trimmed = trim_repetition(raw)
        if trimmed:
            warnings.append("repetition trimmed")
        subjects = dict(predefined)
        body = raw
        if use_brace_tokens:
            found, body = split_subjects_block(raw)
            subjects.update(found)
            for tok, desc in overrides.items():
                if isinstance(tok, str):
                    subjects[tok] = desc
            subjects = {t: clean_description(d, looks_like_person(t)) for t, d in subjects.items()}
        if quotes and has_modes:
            body = fix_dialogue(body, quotes, language)

        if has_modes and not is_cont:
            names = H3_FIELDS_REF if mode == "ref2v" else H3_FIELDS_BASE
            fields, ferr = extract_fields(body, names)
            errors.extend(ferr)
            if not fields and body:
                # nothing labelled at all: keep the text as the description so the box is not empty
                fields = {names[2] if mode == "ref2v" else names[0]: body}
            if fields:
                verr, vwarn = validate_h3(fields, mode, seconds, defined_labels, quotes, language)
                errors.extend(verr)
                warnings.extend(vwarn)
            body_tokens = "\n\n".join(f"{n}: {fields[n]}" for n in names if n in fields) or body
            header = h3_header(mode, seconds, shots_in(fields.get("integrated_multimodal_description", "")))
            if mode == "ref2v":
                body_tokens = "subject_definitions:\n" + "\n".join(subject_lines) + "\n\n" + body_tokens
            elif header:
                body_tokens = header + "\n\n" + body_tokens
        elif has_modes and is_cont:
            if not body:
                errors.append("empty answer")
            if mode == "continuation (Reel)":
                fields, ferr = extract_fields(body, H3_FIELDS_BASE)
                errors.extend(ferr)
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
                    if imd.startswith("[Shot 1]"):
                        imd = "[Shot 1] " + window + " " + SEAM_POLICY + " " + imd[len("[Shot 1]"):].strip()
                    else:
                        imd = "[Shot 1] " + window + " " + SEAM_POLICY + " " + imd
                    fields["integrated_multimodal_description"] = imd
                    body_tokens = "\n\n".join(f"{n}: {fields[n]}" for n in H3_FIELDS_BASE if n in fields)
                else:
                    body_tokens = body
            else:
                body_tokens = re.sub(r"(?im)^\s*(next action|prompt)\s*:\s*", "", body).strip()
                body_tokens = re.sub(r"^\s*\[Shot\s*1\]\s*", "", body_tokens)
                if quotes:
                    for q in quotes:
                        if q not in body_tokens:
                            errors.append(f'dialogue "{q}" missing verbatim')
        else:
            body_tokens = strip_meta_lines(body)
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
                errors.append("tokens used but not defined in the subjects block: " + ", ".join("{" + t + "}" for t in missing))
            unused = sorted(t for t in subjects if t not in used)
            if unused:
                warnings.append("defined but unused tokens: " + ", ".join("{" + t + "}" for t in unused))
            prompt = substitute_tokens(body_tokens, subjects)
            subjects_text = "\n".join(f"{{{k}}} = {v}" for k, v in subjects.items())
        else:
            prompt = body_tokens
            subjects_text = "\n".join(subject_lines)
        return {"prompt": prompt, "tokens": body_tokens, "subjects": subjects_text,
                "errors": errors, "warnings": warnings}

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
                                      "recommended": [{"name": n, "note": d} for n, d in RECOMMENDED_WRITERS]})
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
                PromptServer.instance.send_sync("promptdeck.cue.pull", {"node": node, "model": model, "done": True})
            except Exception as e:
                PromptServer.instance.send_sync("promptdeck.cue.pull", {"node": node, "model": model, "done": True, "error": str(e)})

        threading.Thread(target=worker, daemon=True).start()
        return web.json_response({"ok": True})


_register_routes()
